"""The calendar: /calendar* for members, plus the phone-calendar (ICS) link.

Everything goes through `app.calendar` as the signed-in member, with the same
checks as chat: the overlap refusal names who has the thing, members change,
give back and cancel only their own bookings, and an admin's change or cancel
of someone else's booking is signed into the decision log. Every POST needs the
CSRF token and redirects afterwards, so reloading never books twice.

The one public route is the ICS file, `/calendar/ics/<token>.ics`: calendar
apps can't sign in, so the secret token is the permission (app.phone_calendar).
"""

from __future__ import annotations

import time
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from app import bookables, calendar_view, phone_calendar
from app import calendar as cal
from app.auth import DB, CsrfChecked, CurrentSession, SignedIn
from app.i18n import en
from app.members import admin_names
from app.models import Booking, utcnow
from app.ratelimit import RateLimiter, client_ip
from app.textfmt import web_time, when_range

router = APIRouter(prefix="/calendar")
FIELDS = ("thing", "start", "end", "note", "destination", "reason")
FRESH_LINK_SECONDS = 600
ICS_PER_MINUTE = 30


# --- helpers ---------------------------------------------------------------------


def _render(request: Request, current: CurrentSession, template: str, status=200, **context):
    tz = context.get("tz") or ZoneInfo(request.app.state.settings.timezone)

    def loc(t: datetime) -> str:
        """A time in the community's time zone, like the rest of the site: Today 14:00."""
        return web_time(t, tz)

    return request.app.state.templates.TemplateResponse(
        request,
        template,
        {"current": current, "nav": "calendar", "loc": loc, "who": cal.member_name, **context},
        status_code=status,
    )


def _local(t: datetime | None, tz: ZoneInfo) -> str:
    """For a datetime-local input."""
    return "" if t is None else t.astimezone(tz).strftime("%Y-%m-%dT%H:%M")


def _parse(raw: str, tz: ZoneInfo, what: str) -> datetime | None:
    """A datetime-local value in the community's time zone; empty is None."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        local = datetime.fromisoformat(raw)
    except ValueError:
        raise cal.BookingError(en.CAL_BAD_TIME.format(what=what)) from None
    if local.tzinfo is not None:
        raise cal.BookingError(en.CAL_BAD_TIME.format(what=what))
    return local.replace(tzinfo=tz)


async def _submitted(request: Request) -> dict[str, str]:
    raw = await request.form()
    out = {}
    for k in FIELDS:
        v = raw.get(k, "")
        out[k] = v[:300] if isinstance(v, str) else ""
    return out


def _booking(db: DB, booking_id: int) -> Booking:
    booking = cal.get(db, booking_id)
    if booking is None:
        raise HTTPException(status_code=404)
    return booking


def _is_admin(current: CurrentSession) -> bool:
    return "admin" in current.member.role_names


# --- the calendar ----------------------------------------------------------------


@router.get("")
def calendar_page(
    request: Request,
    db: DB,
    current: SignedIn,
    view: str = "month",
    day: str | None = None,
    type: str | None = None,
    thing: str | None = None,
) -> Response:
    """Members: month or week view of all bookings, filterable by type or thing."""
    tz = cal.timezone(db)
    now = utcnow()
    kind = view if view in calendar_view.VIEWS else "month"
    anchor = calendar_view.parse_day(day, now.astimezone(tz).date())
    flt = calendar_view.make_filter(db, type, thing)
    things = bookables.list_active(db)
    return _render(
        request,
        current,
        "calendar.html",
        title=en.CAL_TITLE,
        v=calendar_view.build(db, kind, anchor, flt, tz, now=now),
        things=things,
        admins=[] if things else admin_names(db),
        types=en.CAL_TYPES,
        today=now.astimezone(tz).date(),
        tz=tz,
        now=now,
    )


# --- booking ---------------------------------------------------------------------


def _clash(db, current: CurrentSession, e: cal.Conflict) -> tuple[str, dict | None]:
    """The refusal for a clash, naming the booking in the way (every member can see
    it anyway), and a link to its day in the calendar, filtered to the thing."""
    if not e.blocking:
        return _sentence(str(e)), None
    b = e.blocking[0]
    tz = cal.timezone(db)
    who = en.CAL_YOU if b.member_id == current.member.id else cal.member_name(b.member)
    if b.ends_at is None:  # someone has it with no end: "Saw is with ana since 14:00"
        text = _sentence(str(e))
    else:
        when = when_range(b.starts_at, b.ends_at, tz)
        text = en.CAL_CONFLICT.format(name=b.bookable.name, who=who, when=when)
    day = b.starts_at.astimezone(tz).date()
    link = {
        "href": f"/calendar?view=week&day={day.isoformat()}&thing={quote(b.bookable.slug)}",
        "text": en.CAL_CONFLICT_SEE.format(day=f"{day.day} {day.strftime('%b')}"),
    }
    return text, link


def _new_form(request, db, current, form, error=None, status=200, clash=None) -> Response:
    things = bookables.list_active(db)
    return _render(
        request,
        current,
        "calendar_new.html",
        status,
        title=en.CAL_NEW_TITLE,
        form=form,
        error=error,
        clash=clash,
        bad=_error_field(error) if error else None,
        things=things,
        admins=[] if things else admin_names(db),
        open_hours=bookables.settings(db).open_booking_hours,
    )


@router.get("/new")
def new_booking(
    request: Request, db: DB, current: SignedIn, thing: str | None = None, day: str | None = None
) -> Response:
    """Members: the booking form, optionally with a thing and a day filled in."""
    tz = cal.timezone(db)
    start = ""
    if day:
        d = calendar_view.parse_day(day, utcnow().astimezone(tz).date())
        if d > utcnow().astimezone(tz).date():
            start = f"{d.isoformat()}T09:00"
    form = {"thing": thing or "", "start": start, "end": "", "note": "", "destination": ""}
    return _new_form(request, db, current, form)


@router.post("/bookings")
async def create_booking(request: Request, db: DB, current: CsrfChecked) -> Response:
    """Members: book a thing as themselves (same rules as chat)."""
    form = await _submitted(request)
    tz = cal.timezone(db)
    thing = bookables.by_slug(db, form["thing"]) if form["thing"] else None
    if thing is None or not thing.is_active:
        return _new_form(request, db, current, form, en.CAL_PICK_THING, 400)
    now = utcnow()
    try:
        starts_at = _parse(form["start"], tz, "start") or now
        ends_at = _parse(form["end"], tz, "end")
        booking = cal.book(
            db,
            current.member,
            thing,
            starts_at,
            ends_at,
            channel="web",
            note=form["note"] or None,
            destination=form["destination"] or None,
            now=now,
        )
    except cal.Conflict as e:
        text, link = _clash(db, current, e)
        db.rollback()
        return _new_form(request, db, current, form, text, 409, clash=link)
    except cal.BookingError as e:
        db.rollback()
        return _new_form(request, db, current, form, _sentence(str(e)), 400)
    db.commit()
    return RedirectResponse(f"/calendar/bookings/{booking.id}?done=booked", status_code=303)


def _error_field(error: str) -> str | None:
    """Which form field a shown error is about, so the page marks that field; None
    for errors about the booking as a whole (e.g. a clash with another booking)."""
    for name in ("start", "end", "note", "destination", "reason"):
        # CAL_BAD_TIME, BOOKING_TEXT_TOO_LONG, BOOKING_ONE_LINE: "The note can be ..."
        if error.startswith(f"The {name} "):
            return name
    fields = {
        en.CAL_PICK_THING: "thing",
        en.BOOKING_IN_THE_PAST: "start",
        en.BOOKING_TOO_FAR: "start",
        en.BOOKING_START_FIXED: "start",
        en.BOOKING_END_BEFORE_START: "end",
        en.BOOKING_END_IN_THE_PAST: "end",
        en.BOOKING_DESTINATION_VEHICLES: "destination",
        en.BOOKING_NEEDS_REASON: "reason",
    }
    return next((f for msg, f in fields.items() if _sentence(msg) == error), None)


def _sentence(message: str) -> str:
    return message[:1].upper() + message[1:] + ("" if message.endswith(".") else ".")


def _booking_page(
    request, db, current, booking, *, form=None, error=None, done=None, status=200, clash=None
) -> Response:
    tz = cal.timezone(db)
    now = utcnow()
    own = booking.member_id == current.member.id
    is_admin = _is_admin(current)
    open_ = booking.cancelled_at is None and booking.returned_at is None
    started = booking.starts_at <= now
    if form is None:
        form = {
            "start": _local(booking.starts_at, tz),
            "end": _local(booking.ends_at, tz),
            "note": booking.note or "",
            "destination": booking.destination or "",
            "reason": "",
        }
    return _render(
        request,
        current,
        "calendar_booking.html",
        status,
        title=en.CAL_BOOKING_TITLE.format(name=booking.bookable.name),
        b=booking,
        form=form,
        error=error,
        clash=clash,
        bad=_error_field(error) if error else None,
        done=en.CAL_DONE.get(done or ""),
        when=when_range(booking.starts_at, booking.ends_at, tz),
        can_change=open_ and (own or is_admin),
        can_return=open_ and own and started,
        can_cancel=open_ and own and not started,
        can_admin_cancel=open_ and is_admin and not own,
        started=started,
        holding=calendar_view.is_holding(booking, now),
        late=calendar_view.is_late(booking, now),
        tz=tz,
        open_hours=bookables.settings(db).open_booking_hours,
    )


@router.get("/bookings/{booking_id}")
def booking_page(
    request: Request, db: DB, current: SignedIn, booking_id: int, done: str | None = None
) -> Response:
    """Members: one booking; its member (and admins) can change it here."""
    return _booking_page(request, db, current, _booking(db, booking_id), done=done)


def _done(booking: Booking, what: str) -> Response:
    return RedirectResponse(f"/calendar/bookings/{booking.id}?done={what}", status_code=303)


@router.post("/bookings/{booking_id}")
async def change_booking(
    request: Request, db: DB, current: CsrfChecked, booking_id: int
) -> Response:
    """Members: change their own booking; admins anyone's (logged)."""
    booking = _booking(db, booking_id)
    form = await _submitted(request)
    tz = cal.timezone(db)
    now = utcnow()
    changes: dict = {"note": form["note"] or None}
    if booking.bookable.type == "vehicle":
        changes["destination"] = form["destination"] or None
    try:
        if booking.starts_at > now:
            changes["starts_at"] = _parse(form["start"], tz, "start") or booking.starts_at
        changes["ends_at"] = _parse(form["end"], tz, "end")
        cal.change(
            db, current.member, booking, signer=request.app.state.log_signer, now=now, **changes
        )
    except cal.NotAllowed as e:
        db.rollback()
        raise HTTPException(status_code=403, detail=_sentence(str(e))) from None
    except cal.BookingError as e:
        text, link = _clash(db, current, e) if isinstance(e, cal.Conflict) else (None, None)
        db.rollback()
        status = 409 if isinstance(e, cal.Conflict) else 400
        return _booking_page(
            request, db, current, _booking(db, booking_id), form=form,
            error=text or _sentence(str(e)), status=status, clash=link,
        )  # fmt: skip
    db.commit()
    return _done(booking, "changed")


@router.post("/bookings/{booking_id}/return")
def return_booking(request: Request, db: DB, current: CsrfChecked, booking_id: int):
    """Members: give back their own started booking."""
    booking = _booking(db, booking_id)
    try:
        cal.return_item(db, current.member, booking)
    except cal.NotAllowed as e:
        db.rollback()
        raise HTTPException(status_code=403, detail=_sentence(str(e))) from None
    except cal.BookingError as e:
        db.rollback()
        return _booking_page(request, db, current, booking, error=_sentence(str(e)), status=400)
    db.commit()
    return _done(booking, "returned")


@router.post("/bookings/{booking_id}/cancel")
async def cancel_booking(request: Request, db: DB, current: CsrfChecked, booking_id: int):
    """Members: cancel their own booking before it starts. Admins: anyone's, with a
    reason (logged)."""
    booking = _booking(db, booking_id)
    form = await _submitted(request)
    try:
        if booking.member_id == current.member.id:
            cal.cancel(db, current.member, booking)
        else:
            cal.admin_cancel(
                db, current.member, booking, form["reason"], request.app.state.log_signer
            )
    except cal.NotAllowed as e:
        db.rollback()
        raise HTTPException(status_code=403, detail=_sentence(str(e))) from None
    except cal.BookingError as e:
        db.rollback()
        return _booking_page(
            request, db, current, booking, form=None, error=_sentence(str(e)), status=400
        )
    db.commit()
    return _done(booking, "cancelled")


# --- phone calendar --------------------------------------------------------------


def _remember_link(request: Request, current: CurrentSession, raw: str) -> None:
    fresh = request.app.state.fresh_calendar_links
    now = time.monotonic()
    for key in [k for k, (_, deadline) in fresh.items() if deadline < now]:
        del fresh[key]
    fresh[current.session.id_hash] = (raw, now + FRESH_LINK_SECONDS)


def _take_link(request: Request, current: CurrentSession) -> str | None:
    entry = request.app.state.fresh_calendar_links.pop(current.session.id_hash, None)
    if entry is None or entry[1] < time.monotonic():
        return None
    return f"{request.app.state.settings.public_url}/calendar/ics/{entry[0]}.ics"


@router.get("/phone")
def phone_page(request: Request, db: DB, current: SignedIn, done: str | None = None):
    """Members: their phone-calendar link (made, shown once, revoked here)."""
    token = phone_calendar.active(db, current.member)
    response = _render(
        request,
        current,
        "calendar_phone.html",
        title=en.PHONE_TITLE,
        token=token,
        link=_take_link(request, current) if token else None,
        done=en.PHONE_DONE.get(done or ""),
        tz=cal.timezone(db),
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/phone")
def make_phone_link(request: Request, db: DB, current: CsrfChecked):
    """Members: make a new phone-calendar link; an old one stops working."""
    raw = phone_calendar.create(db, current.member)
    db.commit()
    _remember_link(request, current, raw)
    return RedirectResponse("/calendar/phone?done=created", status_code=303)


@router.post("/phone/revoke")
def revoke_phone_link(request: Request, db: DB, current: CsrfChecked):
    """Members: stop their phone-calendar link working."""
    phone_calendar.revoke(db, current.member)
    db.commit()
    request.app.state.fresh_calendar_links.pop(current.session.id_hash, None)
    return RedirectResponse("/calendar/phone?done=revoked", status_code=303)


@router.get("/ics/{token}.ics")
def ics_feed(request: Request, db: DB, token: str) -> Response:
    """Public: a member's bookings for their phone calendar; the secret token is the
    permission (revocable, stored only as a hash, kept out of the access log)."""
    limiter: RateLimiter = request.app.state.ics_limiter
    if not limiter.allow(client_ip(request)):
        return Response("Too many requests.\n", status_code=429, media_type="text/plain")
    member = phone_calendar.member_for(db, token)
    if member is None:
        return Response("Not found.\n", status_code=404, media_type="text/plain")
    host = request.app.state.settings.public_url.split("://", 1)[-1].split("/", 1)[0]
    body = phone_calendar.ics(db, member, host)
    return Response(
        body,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'inline; filename="commons.ics"',
        },
    )
