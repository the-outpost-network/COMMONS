"""Default deny: every route needs an explicit guard unless it is listed as public."""

from __future__ import annotations

from pathlib import Path

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.staticfiles import StaticFiles
from starlette.routing import Mount

import app as app_module
from app.auth import require_member
from app.internal import require_service_token

# (method, path) pairs anyone may call. Adding one needs a reason in its docstring.
PUBLIC = {
    ("GET", "/"),
    ("GET", "/health"),
    ("GET", "/auth/login"),
    ("GET", "/auth/callback"),
    ("GET", "/join/{code}"),
    # Phone calendars can't sign in; the secret token in the address is the permission.
    ("GET", "/calendar/ics/{token}.ics"),
}


def _calls(dep: Dependant) -> set:
    found = {dep.call} if dep.call else set()
    for sub in dep.dependencies:
        found |= _calls(sub)
    return found


def routes(app) -> list:
    """Every API route with its effective path and dependencies (routers flattened).

    Mounts are left out; `test_the_only_mount_is_the_stylesheet_folder` checks them.
    """
    return [r for r in iter_route_contexts(app.routes) if not isinstance(r.original_route, Mount)]


def test_the_only_mount_is_the_static_folder(app):
    """/static is public like any site's CSS and JS; it must hold nothing else."""
    mounts = [r.original_route for r in iter_route_contexts(app.routes)]
    mounts = [m for m in mounts if isinstance(m, Mount)]
    assert [m.path for m in mounts] == ["/static"]
    assert isinstance(mounts[0].app, StaticFiles)
    static = Path(app_module.__file__).parent / "static"
    assert sorted(p.name for p in static.iterdir()) == [
        "app.css",
        "app.js",
        "snackbar.js",
        "vendor",
    ]
    assert sorted(p.name for p in (static / "vendor").iterdir()) == [
        "LICENSE.htmx",
        "LICENSE.htmx-ext-sse",
        "README.md",
        "SHA256SUMS",
        "fonts",
        "htmx-ext-sse.min.js",
        "htmx.min.js",
    ]
    assert all(p.suffix in {".woff2", ".txt"} for p in (static / "vendor" / "fonts").iterdir())


def _is_role_guard(call) -> bool:
    return getattr(call, "__qualname__", "") == "require_role.<locals>.guard"


def _guard(route) -> str | None:
    calls = _calls(route.dependant)
    if any(_is_role_guard(c) for c in calls):
        return "role"
    if require_member in calls:
        return "member"
    if require_service_token in calls:
        return "service"
    return None


def test_routes_are_found(app):
    paths = {r.path for r in routes(app)}
    assert {"/auth/logout", "/internal/assistant", "/admin/members"} <= paths


def test_an_unguarded_route_is_caught(app):
    @app.post("/oops")
    def oops():
        return {}

    assert "POST /oops" in _unguarded(app)


def _unguarded(app) -> list[str]:
    found = []
    for route in routes(app):
        assert isinstance(route.original_route, APIRoute), f"unexpected route: {route.path}"
        for method in route.methods:
            if (method, route.path) not in PUBLIC and _guard(route) is None:
                found.append(f"{method} {route.path}")
    return found


def test_every_non_public_route_is_guarded(app):
    assert _unguarded(app) == []


def test_public_routes_still_exist(app):
    """A stale allowlist entry would silently allow a future route at that path."""
    present = {(m, r.path) for r in routes(app) for m in r.methods}
    assert present >= PUBLIC


def test_admin_routes_need_the_admin_role(app):
    from app.auth import admin_guard

    admin_routes = [r for r in routes(app) if r.path.startswith("/admin")]
    assert admin_routes
    for route in admin_routes:
        assert admin_guard in _calls(route.dependant), route.path


def test_cancelling_a_vote_needs_the_board_or_admin_role(app):
    """Any member proposes a vote (Votes PRD); only the board or an admin cancels."""
    from app.auth import board_guard

    (cancel,) = [r for r in routes(app) if r.path == "/proposals/{proposal_id}/cancel"]
    assert board_guard in _calls(cancel.dependant)
    proposing = [
        r
        for r in routes(app)
        if (r.path == "/proposals" and "POST" in r.methods) or r.path == "/proposals/new"
    ]
    assert len(proposing) == 2
    for route in proposing:
        assert board_guard not in _calls(route.dependant), route.path


def test_internal_routes_need_the_service_token(app):
    internal = [r for r in routes(app) if r.path.startswith("/internal")]
    assert internal
    for route in internal:
        assert require_service_token in _calls(route.dependant), route.path


def test_public_routes_say_so(app):
    for route in routes(app):
        if any((m, route.path) in PUBLIC for m in route.methods):
            assert (route.endpoint.__doc__ or "").startswith("Public:"), route.path
