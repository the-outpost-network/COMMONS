"""The village board (/): resource levels, recent entries and "Log" forms.

GET / is defined in main.py (it is public: signed-out visitors get the sign-in
page). Logging from the web goes through `ledger.add_entry` exactly like the
chat does, as the signed-in member, with the CSRF token. The POST redirects, so
reloading never logs twice.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from app import categories, ledger, proposals
from app.auth import DB, CsrfChecked, CurrentSession
from app.i18n import en
from app.members import admin_names as _admin_names
from app.models import utcnow

router = APIRouter()

RECENT = 12
# Presentation only: a level whose last reading is older than this is shown as
# possibly out of date. (A candidate for community.yaml if the Outpost wants another.)
STALE_AFTER = timedelta(days=7)
# Recent entries show when something happened only if it differs this much from
# when it was logged (backdated entries).
BACKDATED_AFTER = timedelta(hours=1)


def page(
    request: Request,
    db: DB,
    current: CurrentSession,
    *,
    logged: int | None = None,
    form_error: dict | None = None,
    status: int = 200,
) -> Response:
    """The signed-in board. `form_error` reopens one card's form with its message."""
    done = None
    if logged is not None:
        entry = ledger.get(db, logged)
        if entry is not None and entry.member_id == current.member.id:
            done = entry
    levels = ledger.levels(db)
    return request.app.state.templates.TemplateResponse(
        request,
        "home.html",
        {
            "current": current,
            "nav": "board",
            "levels": levels,
            "open_vote": proposals.soonest_open(db, current.member),
            "now": utcnow(),
            "stale_after": STALE_AFTER,
            "backdated_after": BACKDATED_AFTER,
            "recent": ledger.recent(db, RECENT),
            "logged": done,
            "form_error": form_error,
            "types_for_kind": ledger.TYPES_FOR_KIND,
            "admins": [] if levels else _admin_names(db),
            "default_when": utcnow()
            .astimezone(ZoneInfo(request.app.state.settings.timezone))
            .strftime("%Y-%m-%dT%H:%M"),
        },
        status_code=status,
    )


def _parse_when(raw: str, tz: str) -> datetime | None:
    """A datetime-local value ("2026-11-10T14:20") in the community's timezone, as UTC."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        local = datetime.fromisoformat(raw)
    except ValueError as e:
        raise ledger.LedgerError("the time isn't valid", field="when") from e
    if local.tzinfo is not None:
        raise ledger.LedgerError("the time isn't valid", field="when")
    return local.replace(tzinfo=ZoneInfo(tz))


@router.post("/ledger/entries")
def log_entry(
    request: Request,
    db: DB,
    current: CsrfChecked,
    category_id: Annotated[int, Form()],
    entry_type: Annotated[str, Form(max_length=12)],
    amount: Annotated[str, Form(max_length=30)],
    when: Annotated[str, Form(max_length=30)] = "",
    when_prefill: Annotated[str, Form(max_length=30)] = "",
    note: Annotated[str, Form(max_length=400)] = "",
) -> Response:
    """Signed-in members: log a reading, use or add from the board."""
    category = categories.get(db, category_id)
    values = {
        "entry_type": entry_type,
        "amount": amount,
        "when": when,
        "when_prefill": when_prefill,
        "note": note,
    }
    # The form prefills "When" with the time the page was loaded. Left as it was,
    # it means "now": the moment of saving, not a page left open for hours.
    if when.strip() and when.strip() == when_prefill.strip():
        when = ""
    if category is None:
        return page(request, db, current, status=404)
    try:
        number = ledger.parse_amount(amount)
    except ledger.LedgerError:
        return page(
            request,
            db,
            current,
            form_error={
                "category_id": category.id,
                "message": en.LEDGER_NOT_A_NUMBER,
                "field": "amount",
                **values,
            },
            status=400,
        )
    try:
        entry = ledger.add_entry(
            db,
            current.member,
            category,
            entry_type,
            number,
            channel="web",
            note=note or None,
            occurred_at=_parse_when(when, request.app.state.settings.timezone),
        )
    except ledger.LedgerError as e:
        db.rollback()
        status = 403 if isinstance(e, ledger.NotAllowed) else 400
        return page(
            request,
            db,
            current,
            form_error={"category_id": category.id, "message": str(e), "field": e.field, **values},
            status=status,
        )
    db.commit()
    return RedirectResponse(f"/?logged={entry.id}#res-{category.slug}", status_code=303)
