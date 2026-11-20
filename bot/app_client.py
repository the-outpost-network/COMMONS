"""The bot's only way into COMMONS: the app's internal API.

The bot never decides anything itself. It passes the message and the sender's
Matrix id to /internal/assistant; the app looks up the member, applies their
permissions, and returns the reply text.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import httpx

from bot.feed import SSEParser, parse_data


class AppError(Exception):
    """The app is down or refused the call; the message never contains the token."""


class AppClient:
    def __init__(
        self, http: httpx.AsyncClient, base_url: str, token: str, timeout_s: float = 200.0
    ) -> None:
        self._http = http
        self._base = base_url.rstrip("/") + "/internal"
        self._url = self._base + "/assistant"
        self._headers = {"Authorization": f"Bearer {token}"}
        # Covers a slow CPU model answer.
        self._timeout = timeout_s

    async def ask(
        self,
        sender: str,
        room_id: str,
        text: str,
        history: list[dict[str, str]] | None = None,
        *,
        direct: bool = False,
        reply_to: str | None = None,
    ) -> str:
        body: dict[str, Any] = {
            "sender": sender,
            "room_id": room_id,
            "text": text,
            "history": history or [],
        }
        if direct:
            body["direct"] = True
            if reply_to:
                body["reply_to"] = reply_to
        try:
            resp = await self._http.post(
                self._url,
                json=body,
                headers=self._headers,
                timeout=self._timeout,
            )
        except httpx.HTTPError as e:
            raise AppError(f"request failed: {type(e).__name__}") from e
        if resp.status_code != 200:
            raise AppError(f"status {resp.status_code}")
        try:
            reply = resp.json()["reply"]
        except (ValueError, KeyError, TypeError) as e:
            raise AppError("malformed response") from e
        if not isinstance(reply, str) or not reply.strip():
            raise AppError("empty reply")
        return reply

    async def notifications(self) -> list[dict[str, Any]]:
        """Vote messages waiting to be sent: id, text and recipients."""
        try:
            resp = await self._http.get(
                # The app may ask the local model for intro lines first.
                self._base + "/notifications",
                headers=self._headers,
                timeout=180.0,
            )
        except httpx.HTTPError as e:
            raise AppError(f"request failed: {type(e).__name__}") from e
        if resp.status_code != 200:
            raise AppError(f"status {resp.status_code}")
        try:
            items = resp.json()["items"]
        except (ValueError, KeyError, TypeError) as e:
            raise AppError("malformed response") from e
        if not isinstance(items, list):
            raise AppError("malformed response")
        return items

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self._http.post(
                self._base + path, json=body, headers=self._headers, timeout=30.0
            )
        except httpx.HTTPError as e:
            raise AppError(f"request failed: {type(e).__name__}") from e
        if resp.status_code != 200:
            raise AppError(f"status {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as e:
            raise AppError("malformed response") from e
        if not isinstance(data, dict):
            raise AppError("malformed response")
        return data

    async def vote_message(
        self, proposal_id: int, kind: str, room_id: str, event_id: str, user_id: str | None
    ) -> None:
        """Tell the app which Matrix message shows which vote."""
        await self._post(
            "/vote-messages",
            {
                "proposal_id": proposal_id,
                "kind": kind,
                "room_id": room_id,
                "event_id": event_id,
                "user_id": user_id,
            },
        )

    async def reaction(self, sender: str, room_id: str, event_id: str, key: str) -> str | None:
        """A tap on a message; the app's answer if it was a ballot, else None."""
        data = await self._post(
            "/votes", {"sender": sender, "room_id": room_id, "event_id": event_id, "key": key}
        )
        reply = data.get("reply")
        return reply if isinstance(reply, str) and reply.strip() else None

    async def delivered(self, notification_id: int) -> None:
        try:
            resp = await self._http.post(
                f"{self._base}/notifications/{notification_id}/delivered",
                headers=self._headers,
                timeout=30.0,
            )
        except httpx.HTTPError as e:
            raise AppError(f"request failed: {type(e).__name__}") from e
        if resp.status_code not in (200, 404):  # 404: nothing left to confirm
            raise AppError(f"status {resp.status_code}")


# --- the COMMONS room feed: GET /internal/events (Server-Sent Events) -----------


async def follow_events(
    http: httpx.AsyncClient,
    base_url: str,
    token: str,
    handle,
    last_id,
    stop,
    *,
    max_backoff: float = 30.0,
) -> None:
    """Keep the event stream open until `stop` is set; reconnect and resume on errors.

    `handle(event, data)` gets each message; `last_id` (bot.feed.LastId) is sent
    as Last-Event-ID, so the app replays what was missed. The bot never reads
    the database: the stream is its only source.
    """
    log = logging.getLogger("commons.bot.events")
    url = base_url.rstrip("/") + "/internal/events"
    backoff = 1.0
    timeout = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)  # pings every 15 s
    while not stop.is_set():
        headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
        if last_id.value is not None:
            headers["Last-Event-ID"] = str(last_id.value)
        if last_id.scope:
            headers["X-Commons-Feed-Scope"] = last_id.scope
        try:
            async with http.stream("GET", url, headers=headers, timeout=timeout) as resp:
                if resp.status_code != 200:
                    raise AppError(f"status {resp.status_code}")
                backoff = 1.0
                parser = SSEParser()
                async for line in resp.aiter_lines():
                    msg = parser.feed(line)
                    if msg is None:
                        continue
                    data = parse_data(msg)
                    if data is None:
                        continue
                    if msg.event == "hello":
                        # First start or another database: begin with what comes
                        # next, not with that feed's history.
                        last_id.begin(data.get("feed_scope"), data.get("latest_id"))
                    try:
                        await handle(msg.event, data)
                    except Exception:
                        log.exception("could not handle a %s event", msg.event)
                    if msg.id and msg.id.isdigit():
                        last_id.set(int(msg.id))
                    if stop.is_set():
                        return
        except (httpx.HTTPError, AppError) as e:
            log.warning("event stream: %s; reconnecting in %.0f s", type(e).__name__, backoff)
        else:
            backoff = 1.0  # the app ended the stream normally
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), backoff)
        backoff = min(backoff * 2, max_backoff)
