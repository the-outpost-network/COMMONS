"""Which messages the bot passes on to COMMONS."""

from __future__ import annotations


def should_answer(
    *,
    joined_members: int,
    mentions: list[str],
    body: str,
    bot_user_id: str,
) -> bool:
    """Answer every message in a 1:1 room; in group rooms only when addressed.

    Addressed = an @-mention (Element sets `m.mentions`) or a message that starts
    with the bot's name ("commons, ..."). Just using the word "commons" mid-sentence
    does not count.
    """
    if joined_members <= 2:
        return True
    if bot_user_id in mentions:
        return True
    localpart = bot_user_id.split(":", 1)[0].lstrip("@").lower()
    return body.lstrip().lower().startswith(localpart)
