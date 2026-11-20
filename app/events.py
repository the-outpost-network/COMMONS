"""In-process event bus: services announce what changed, others react.

A service calls `publish(db, Event(...))` next to its write. The event is held
on the database session and only sent to subscribers once that session commits;
a rollback drops it. So nobody ever hears about a change that didn't happen.

Subscribers (the community room feed in D6, the alerts in D7, the web board
later) get events in the app process. The app runs with one uvicorn worker, so
this one bus sees every write. A failing subscriber is logged and never breaks
the write or the other subscribers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session

log = logging.getLogger("commons.events")

PENDING = "commons_pending_events"


@dataclass(frozen=True)
class Event:
    kind: str  # e.g. "ledger.entry_added"
    data: dict[str, Any] = field(default_factory=dict)


Subscriber = Callable[[Event], None]
_subscribers: list[Subscriber] = []


def subscribe(fn: Subscriber) -> Callable[[], None]:
    """Register `fn`; returns a function that unsubscribes it again."""
    _subscribers.append(fn)
    return lambda: _subscribers.remove(fn) if fn in _subscribers else None


def publish(db: Session, event: Event) -> None:
    """Send `event` to the subscribers after `db` commits (never if it rolls back)."""
    db.info.setdefault(PENDING, []).append(event)


def _deliver(event: Event) -> None:
    for fn in list(_subscribers):
        try:
            fn(event)
        except Exception:
            log.exception("event subscriber failed for %s", event.kind)


@sa_event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    session.info.pop(MARKS, None)
    pending = session.info.pop(PENDING, [])
    for e in pending:
        _deliver(e)


# A savepoint (`begin_nested`) that rolls back drops only the events published
# inside it; the rest of the transaction, and its events, carry on.
MARKS = "commons_pending_marks"


@sa_event.listens_for(Session, "after_transaction_create")
def _mark_savepoint(session: Session, transaction) -> None:
    if transaction.nested:
        marks = session.info.setdefault(MARKS, {})
        marks[id(transaction)] = len(session.info.get(PENDING, []))


@sa_event.listens_for(Session, "after_soft_rollback")
def _after_soft_rollback(session: Session, previous_transaction) -> None:
    if not previous_transaction.nested:
        return
    mark = session.info.get(MARKS, {}).pop(id(previous_transaction), None)
    pending = session.info.get(PENDING)
    if mark is not None and pending is not None:
        del pending[mark:]


@sa_event.listens_for(Session, "after_rollback")
def _after_rollback(session: Session) -> None:
    # Also fires for a savepoint; that case is handled in _after_soft_rollback.
    if session.in_nested_transaction():
        return
    session.info.pop(PENDING, None)
    session.info.pop(MARKS, None)
