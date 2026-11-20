"""Usage history pages for admins: /admin/resources/{id}/history*.

Admin role only, enforced by the `Admin` dependency on each route (members and
board get 403). Read only: nothing here writes. The numbers come from
`app.history`, which derives them exactly like the live level.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from app import calendar, categories, history
from app.auth import DB, Admin
from app.i18n import en
from app.models import ResourceCategory, utcnow

router = APIRouter(prefix="/admin/resources")

FromField = Annotated[str | None, Query(alias="from", max_length=20)]
ToField = Annotated[str | None, Query(alias="to", max_length=20)]


def _category(db: DB, category_id: int) -> ResourceCategory:
    c = categories.get(db, category_id)
    if c is None:
        raise HTTPException(status_code=404)
    return c


def _period(db: DB, raw_from: str | None, raw_to: str | None) -> history.Period:
    tz = calendar.timezone(db)
    return history.period(raw_from, raw_to, tz, utcnow().astimezone(tz).date())


@router.get("/{category_id}/history")
def history_page(
    request: Request,
    db: DB,
    current: Admin,
    category_id: int,
    raw_from: FromField = None,
    raw_to: ToField = None,
) -> Response:
    """Admin: entries, corrections and the level over a period (default 90 days)."""
    c = _category(db, category_id)
    error, status = None, 200
    try:
        p = _period(db, raw_from, raw_to)
    except history.PeriodError as e:
        error, status = str(e), 400
        p = _period(db, None, None)
    h = history.history(db, c, p)
    return request.app.state.templates.TemplateResponse(
        request,
        "admin_history.html",
        {
            "current": current,
            "nav": "settings",
            "section": "resources",
            "title": en.HISTORY_TITLE.format(name=c.name),
            "h": h,
            "error": error,
            "form": {"from": raw_from or p.start.isoformat(), "to": raw_to or p.end.isoformat()},
        },
        status_code=status,
    )


@router.get("/{category_id}/history.csv")
def history_csv(
    db: DB,
    current: Admin,
    category_id: int,
    raw_from: FromField = None,
    raw_to: ToField = None,
) -> Response:
    """Admin: the same rows as the history page, as a CSV download."""
    c = _category(db, category_id)
    try:
        p = _period(db, raw_from, raw_to)
    except history.PeriodError as e:
        return Response(str(e), status_code=400, media_type="text/plain; charset=utf-8")
    h = history.history(db, c, p)
    # The BOM makes spreadsheet apps read it as UTF-8 (m³, names with accents).
    body = "﻿" + history.to_csv(h)
    return Response(
        body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{history.csv_filename(h)}"',
            "Cache-Control": "no-store",
        },
    )
