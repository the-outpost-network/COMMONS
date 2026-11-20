"""Sign a community configuration change into the decision log (`config.changed`).

Admin pages call this in the same transaction as the change, so the log and
the configuration never disagree. `key` names one configured thing, e.g.
`bookables.saw` or `resource_categories.water`; `old` is None for something new
and `new` is None for something deleted.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app import decision_log


def changed(
    db: Session,
    signer: decision_log.Signer,
    key: str,
    old: Any,
    new: Any,
    *,
    actor: int,
    source: str,
) -> None:
    """Append `config.changed` unless nothing actually changed."""
    if old == new:
        return
    decision_log.append(
        db,
        signer,
        "config.changed",
        {"key": key, "old": old, "new": new, "source": source},
        actor=actor,
    )
