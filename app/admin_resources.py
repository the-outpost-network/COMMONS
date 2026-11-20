"""Admin pages for resource categories: /admin/resources*. Admin role only.

Create, edit, archive/restore, reorder and delete (only without history). The
rules live in `app.categories`; this module turns forms into `CategorySpec`
and shows the errors next to the right field. Every change except the display
order is signed into the decision log as `config.changed` in the same
transaction. Every POST needs the CSRF token and redirects afterwards, so
reloading never submits twice.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import ValidationError

from app import categories, config_log
from app.auth import DB, Admin, AdminCsrfChecked, CurrentSession
from app.i18n import en
from app.models import KINDS, ResourceCategory

router = APIRouter(prefix="/admin/resources")

Text = Annotated[str, Form(max_length=200)]
Number = Annotated[str, Form(max_length=30)]


def _render(
    request: Request,
    current: CurrentSession,
    template: str,
    status: int = 200,
    **context,
) -> Response:
    return request.app.state.templates.TemplateResponse(
        request,
        template,
        {"current": current, "nav": "settings", "section": "resources", "kinds": KINDS, **context},
        status_code=status,
    )


def _category(db: DB, category_id: int) -> ResourceCategory:
    c = categories.get(db, category_id)
    if c is None:
        raise HTTPException(status_code=404)
    return c


def _list(request: Request, db: DB, current: CurrentSession, *, error=None, status=200, done=None):
    return _render(
        request,
        current,
        "admin_resources.html",
        status,
        title=en.RESOURCES_TITLE,
        resources=categories.list_all(db),
        done=en.RESOURCES_DONE.get(done or ""),
        error=error,
    )


def _log(request: Request, db: DB, current: CurrentSession, slug: str, old, new) -> None:
    config_log.changed(
        db,
        request.app.state.log_signer,
        f"resource_categories.{slug}",
        old,
        new,
        actor=current.member.id,
        source="web",
    )


def _done(what: str) -> Response:
    return RedirectResponse(f"/admin/resources?done={what}", status_code=303)


def _number(raw: str, field: str, errors: dict[str, str]) -> float | None:
    raw = raw.strip().replace(",", ".")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        errors[field] = en.RESOURCES_NOT_A_NUMBER
        return None


def _form_spec(form: dict[str, str]) -> tuple[categories.CategorySpec | None, dict[str, str]]:
    """The submitted form as a spec, or the errors per field."""
    errors: dict[str, str] = {}
    threshold = _number(form["shortage_threshold"], "shortage_threshold", errors)
    capacity = _number(form["capacity"], "capacity", errors)
    horizon = _number(form["forecast_horizon_days"], "forecast_horizon_days", errors)
    if horizon is not None and not horizon.is_integer():
        errors["forecast_horizon_days"] = en.RESOURCES_WHOLE_DAYS
        horizon = None
    data = {
        "slug": form["slug"].strip().lower(),
        "name": form["name"],
        "unit": form["unit"],
        "kind": form["kind"],
        "shortage_threshold": threshold,
        "capacity": capacity,
        "aliases": [a for a in form["aliases"].split(",")],
    }
    if horizon is not None:
        data["forecast_horizon_days"] = int(horizon)
    # Check everything at once, so the admin sees every problem in one go.
    try:
        spec = categories.CategorySpec.model_validate(data)
    except ValidationError as e:
        return None, {**categories.spec_errors(e), **errors}
    return (None, errors) if errors else (spec, {})


def _form(
    request: Request,
    current: CurrentSession,
    form: dict[str, str],
    errors: dict[str, str],
    category: ResourceCategory | None,
    status: int = 200,
) -> Response:
    general = errors.get("")
    return _render(
        request,
        current,
        "admin_resource_form.html",
        status,
        title=en.RESOURCES_EDIT_TITLE if category else en.RESOURCES_NEW_TITLE,
        form=form,
        errors=errors,
        error=general,
        category=category,
    )


def _form_values(c: ResourceCategory | None) -> dict[str, str]:
    if c is None:
        return {
            "slug": "",
            "name": "",
            "unit": "",
            "kind": "stock",
            "shortage_threshold": "",
            "capacity": "",
            "forecast_horizon_days": "30",
            "aliases": "",
        }

    def n(v: float | None) -> str:
        return "" if v is None else request_number(v)

    return {
        "slug": c.slug,
        "name": c.name,
        "unit": c.unit,
        "kind": c.kind,
        "shortage_threshold": n(c.shortage_threshold),
        "capacity": n(c.capacity),
        "forecast_horizon_days": str(c.forecast_horizon_days),
        "aliases": ", ".join(c.alias_names),
    }


def request_number(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else str(v)


# --- routes ------------------------------------------------------------------


@router.get("")
def list_resources(request: Request, db: DB, current: Admin, done: str | None = None) -> Response:
    """Admin: list resource categories, active first."""
    return _list(request, db, current, done=done)


@router.get("/new")
def new_resource(request: Request, current: Admin) -> Response:
    """Admin: empty form for a new resource category."""
    return _form(request, current, _form_values(None), {}, None)


async def _submitted(request: Request) -> dict[str, str]:
    raw = await request.form()
    keys = (
        "slug",
        "name",
        "unit",
        "kind",
        "shortage_threshold",
        "capacity",
        "forecast_horizon_days",
        "aliases",
    )
    out = {}
    for k in keys:
        v = raw.get(k, "")
        out[k] = v[:400] if isinstance(v, str) else ""
    return out


@router.post("")
async def create_resource(request: Request, db: DB, current: AdminCsrfChecked) -> Response:
    """Admin: create a resource category."""
    form = await _submitted(request)
    spec, errors = _form_spec(form)
    if spec is None:
        return _form(request, current, form, errors, None, 400)
    try:
        c = categories.create(db, spec)
    except categories.CategoryError as e:
        db.rollback()
        return _form(request, current, form, {e.field or "": str(e)}, None, 409)
    _log(request, db, current, c.slug, None, categories.values(c))
    db.commit()
    return _done("created")


@router.get("/{category_id}/edit")
def edit_resource(request: Request, db: DB, current: Admin, category_id: int) -> Response:
    """Admin: form to change a resource category."""
    c = _category(db, category_id)
    return _form(request, current, _form_values(c), {}, c)


@router.post("/{category_id}")
async def update_resource(
    request: Request, db: DB, current: AdminCsrfChecked, category_id: int
) -> Response:
    """Admin: save changes to a resource category (its id stays)."""
    c = _category(db, category_id)
    form = await _submitted(request)
    form["slug"] = c.slug  # fixed once created; whatever was sent is ignored
    spec, errors = _form_spec(form)
    if spec is None:
        return _form(request, current, form, errors, c, 400)
    before = categories.values(c)
    try:
        categories.update(db, c, spec)
    except categories.CategoryError as e:
        db.rollback()
        return _form(request, current, form, {e.field or "": str(e)}, c, 409)
    _log(request, db, current, c.slug, before, categories.values(c))
    db.commit()
    return _done("updated")


@router.post("/{category_id}/archive")
def archive_resource(request: Request, db: DB, current: AdminCsrfChecked, category_id: int):
    """Admin: hide a resource from chat and the board; its history stays."""
    c = _category(db, category_id)
    before = categories.values(c)
    categories.archive(db, c)
    _log(request, db, current, c.slug, before, categories.values(c))
    db.commit()
    return _done("archived")


@router.post("/{category_id}/restore")
def restore_resource(request: Request, db: DB, current: AdminCsrfChecked, category_id: int):
    """Admin: bring an archived resource back."""
    c = _category(db, category_id)
    before = categories.values(c)
    try:
        categories.restore(db, c)
    except categories.CategoryError as e:
        db.rollback()
        return _list(request, db, current, error=str(e), status=409)
    _log(request, db, current, c.slug, before, categories.values(c))
    db.commit()
    return _done("restored")


@router.post("/{category_id}/move")
def move_resource(
    request: Request,
    db: DB,
    current: AdminCsrfChecked,
    category_id: int,
    direction: Annotated[str, Form(pattern="^(up|down)$")],
):
    """Admin: move a resource up or down in the display order."""
    categories.move(db, _category(db, category_id), -1 if direction == "up" else 1)
    db.commit()
    return RedirectResponse("/admin/resources", status_code=303)


@router.get("/{category_id}/delete")
def delete_confirm(request: Request, db: DB, current: Admin, category_id: int) -> Response:
    """Admin: ask before deleting a resource for good."""
    return _render(
        request,
        current,
        "admin_resource_delete.html",
        title=en.RESOURCES_DELETE_TITLE,
        category=_category(db, category_id),
    )


@router.post("/{category_id}/delete")
def delete_resource(request: Request, db: DB, current: AdminCsrfChecked, category_id: int):
    """Admin: delete a resource that has no history (otherwise archive it)."""
    c = _category(db, category_id)
    slug, before = c.slug, categories.values(c)
    try:
        categories.delete(db, c)
    except categories.HasHistory as e:
        db.rollback()
        return _list(request, db, current, error=str(e), status=409)
    _log(request, db, current, slug, before, None)
    db.commit()
    return _done("deleted")
