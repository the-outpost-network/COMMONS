"""The chat so far, for the app: the last messages of the room, reminders included.

The bot reads the room's latest messages from Matrix (bot/main.py decrypts
them) and passes the last `LIMIT` that are from the member who wrote or from
COMMONS itself, oldest first. In a group room other people's messages are
left out. The app gives them to the model, so "I returned it" after a
reminder about the saw means the saw.
"""

from __future__ import annotations

LIMIT = 10
TEXT_MAX = 4_000


def recent(
    newest_first: list[tuple[str, str, str]],
    *,
    current_event_id: str,
    member: str,
    bot: str,
    limit: int = LIMIT,
) -> list[dict[str, str]]:
    """(event id, sender, text) newest first -> [{"role", "text"}] oldest first."""
    out: list[dict[str, str]] = []
    for event_id, sender, text in newest_first:
        if event_id == current_event_id or not text.strip():
            continue
        if sender == bot:
            role = "assistant"
        elif sender == member:
            role = "user"
        else:
            continue
        out.append({"role": role, "text": text.strip()[:TEXT_MAX]})
        if len(out) >= limit:
            break
    return list(reversed(out))
