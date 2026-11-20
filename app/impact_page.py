"""The contribution page (/me) and the member's data export (/me/export.json).

Both show only the signed-in member's own data; there is no member id in the
address to change. Admins see their own page like everyone else
(admins get aggregate participation only, not other members' pages).

Opening the page also awards the badges the member has reached
(app/impact.py `sync`); that write is idempotent and dates each badge by when
it was earned, so it is harmless on a GET.
"""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import Response

from app import calendar as cal
from app import impact
from app.auth import DB, SignedIn
from app.i18n import en

router = APIRouter(prefix="/me")


@router.get("")
def me_page(request: Request, db: DB, current: SignedIn) -> Response:
    """Member: my contributions, bookings and badges."""
    tz = cal.timezone(db)
    data = impact.summary(db, current.member)
    db.commit()

    def loc(t: datetime) -> str:
        return t.astimezone(tz).strftime("%d.%m.%y %H:%M")

    def day(t: datetime) -> str:
        return t.astimezone(tz).strftime("%d.%m.%y")

    return request.app.state.templates.TemplateResponse(
        request,
        "me.html",
        {
            "current": current,
            "nav": "me",
            "title": en.ME_TITLE,
            "s": data,
            "loc": loc,
            "day": day,
        },
    )


@router.get("/export.json")
def me_export(db: DB, current: SignedIn) -> Response:
    """Member: download all of my own data as JSON."""
    body = json.dumps(impact.export(db, current.member), ensure_ascii=False, indent=2)
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="commons-my-data.json"',
            "Cache-Control": "no-store",
        },
    )
