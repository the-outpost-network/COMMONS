"""Admin page for bookable things: /admin/bookable*. Admin role only.

Add, edit, archive/restore and delete (only without bookings). A thing has a
name, a type (space, vehicle, tool), where it is and a description; its id is
made from the name. The rules live in `app.bookables`. Every change is signed
into the decision log as `config.changed` (key `bookable.<id>`) in the
same transaction. Every POST needs the CSRF token and redirects afterwards, so
reloading never submits twice.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import ValidationError

from app import bookables, categories, config_log
from app.auth import DB, Admin, AdminCsrfChecked, CurrentSession
from app.i18n import en
from app.models import THING_TYPES, Bookable

router = APIRouter(prefix="/admin/bookable")
FIELDS = ("name", "type", "location", "description")


def _render(request: Request, current: CurrentSession, template: str, status=200, **context):
    return request.app.state.templates.TemplateResponse(
        request,
        template,
        {"current": current, "nav": "settings", "section": "bookable", "types": THING_TYPES,
         **context},
        status_code=status,
    )  # fmt: skip


def _thing(db: DB, thing_id: int) -> Bookable:
    thing = bookables.get(db, thing_id)
    if thing is None:
        raise HTTPException(status_code=404)
    return thing


def _list(request, db, current, *, error=None, status=200, done=None) -> Response:
    return _render(
        request,
        current,
        "admin_bookable.html",
        status,
        title=en.BOOKABLE_TITLE,
        things=bookables.list_all(db),
        done=en.BOOKABLE_DONE.get(done or ""),
        error=error,
    )


def _form(request, current, form, errors, thing, status=200) -> Response:
    return _render(
        request,
        current,
        "admin_bookable_form.html",
        status,
        title=en.BOOKABLE_EDIT_TITLE if thing else en.BOOKABLE_NEW_TITLE,
        form=form,
        errors=errors,
        error=errors.get(""),
        thing=thing,
    )


def _form_values(thing: Bookable | None) -> dict[str, str]:
    if thing is None:
        return {"name": "", "type": "space", "location": "", "description": ""}
    return {
        "name": thing.name,
        "type": thing.type,
        "location": thing.location or "",
        "description": thing.description,
    }


async def _submitted(request: Request) -> dict[str, str]:
    raw = await request.form()
    out = {}
    for k in FIELDS:
        v = raw.get(k, "")
        out[k] = v[:600] if isinstance(v, str) else ""
    return out


def _spec(form: dict[str, str]):
    """The submitted form as a spec, or the errors per field."""
    data = {**form, "location": form["location"] or None}
    try:
        return bookables.ThingSpec.model_validate(data), {}
    except ValidationError as e:
        return None, categories.spec_errors(e)


def _log(request: Request, db: DB, current: CurrentSession, slug: str, old, new) -> None:
    config_log.changed(
        db,
        request.app.state.log_signer,
        f"bookable.{slug}",
        old,
        new,
        actor=current.member.id,
        source="web",
    )


def _done(what: str) -> Response:
    return RedirectResponse(f"/admin/bookable?done={what}", status_code=303)


# --- routes ------------------------------------------------------------------


@router.get("")
def list_things(request: Request, db: DB, current: Admin, done: str | None = None) -> Response:
    """Admin: list the bookable things, active first."""
    return _list(request, db, current, done=done)


@router.get("/new")
def new_thing(request: Request, current: Admin) -> Response:
    """Admin: empty form for a new bookable thing."""
    return _form(request, current, _form_values(None), {}, None)


@router.post("")
async def create_thing(request: Request, db: DB, current: AdminCsrfChecked) -> Response:
    """Admin: add a bookable thing; logged as config.changed."""
    form = await _submitted(request)
    spec, errors = _spec(form)
    if spec is None:
        return _form(request, current, form, errors, None, 400)
    try:
        thing = bookables.create(db, spec)
    except bookables.BookablesError as e:
        db.rollback()
        return _form(request, current, form, {e.field or "": str(e)}, None, 409)
    _log(request, db, current, thing.slug, None, bookables.values(thing))
    db.commit()
    return _done("created")


@router.get("/{thing_id}/edit")
def edit_thing(request: Request, db: DB, current: Admin, thing_id: int) -> Response:
    """Admin: form to change a bookable thing."""
    thing = _thing(db, thing_id)
    return _form(request, current, _form_values(thing), {}, thing)


@router.post("/{thing_id}")
async def update_thing(
    request: Request, db: DB, current: AdminCsrfChecked, thing_id: int
) -> Response:
    """Admin: save changes to a bookable thing (its id stays); logged as config.changed."""
    thing = _thing(db, thing_id)
    form = await _submitted(request)
    spec, errors = _spec(form)
    if spec is None:
        return _form(request, current, form, errors, thing, 400)
    before = bookables.values(thing)
    try:
        bookables.update(db, thing, spec)
    except bookables.BookablesError as e:
        db.rollback()
        return _form(request, current, form, {e.field or "": str(e)}, thing, 409)
    _log(request, db, current, thing.slug, before, bookables.values(thing))
    db.commit()
    return _done("updated")


@router.post("/{thing_id}/archive")
def archive_thing(request: Request, db: DB, current: AdminCsrfChecked, thing_id: int):
    """Admin: stop a thing being booked; its bookings stay. Logged."""
    thing = _thing(db, thing_id)
    before = bookables.values(thing)
    bookables.archive(db, thing)
    _log(request, db, current, thing.slug, before, bookables.values(thing))
    db.commit()
    return _done("archived")


@router.post("/{thing_id}/restore")
def restore_thing(request: Request, db: DB, current: AdminCsrfChecked, thing_id: int):
    """Admin: bring an archived thing back. Logged."""
    thing = _thing(db, thing_id)
    before = bookables.values(thing)
    try:
        bookables.restore(db, thing)
    except bookables.BookablesError as e:
        db.rollback()
        return _list(request, db, current, error=str(e), status=409)
    _log(request, db, current, thing.slug, before, bookables.values(thing))
    db.commit()
    return _done("restored")


@router.get("/{thing_id}/delete")
def delete_confirm(request: Request, db: DB, current: Admin, thing_id: int) -> Response:
    """Admin: ask before deleting a thing for good."""
    return _render(
        request, current, "admin_bookable_delete.html", title=en.BOOKABLE_DELETE_TITLE,
        thing=_thing(db, thing_id),
    )  # fmt: skip


@router.post("/{thing_id}/delete")
def delete_thing(request: Request, db: DB, current: AdminCsrfChecked, thing_id: int):
    """Admin: delete a thing nobody has booked (otherwise archive it). Logged."""
    thing = _thing(db, thing_id)
    slug, before = thing.slug, bookables.values(thing)
    try:
        bookables.delete(db, thing)
    except bookables.HasHistory as e:
        db.rollback()
        return _list(request, db, current, error=str(e), status=409)
    _log(request, db, current, slug, before, None)
    db.commit()
    return _done("deleted")
