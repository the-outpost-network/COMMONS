"""COMMONS Matrix bot: a thin relay between Element and the COMMONS app.

    python -m bot.main

- Logs in with an access token from a Docker secret (issued by MAS for a
  fixed device id, see scripts/bot_token.sh). Never logs in with a password.
- End-to-end encryption via mautrix + libolm. Element X creates encrypted DMs
  only, so without this the bot could not read anything. The crypto store and
  the cross-signing recovery key live in the bot's own volume (/data), never
  in COMMONS' database.
- Joins rooms only when invited by an account on this homeserver.
- No business logic, no database access and no model access: every message
  goes to the app's internal API (/internal/assistant) with the sender's Matrix
  id, and the app's reply is posted back. Message text is never logged.
- Vote messages (opened, reminder, result) come from the app's outbox and go to
  each member's direct chat (bot/notify.py).
- Follows the app's event stream (/internal/events) and posts the feed to the
  community room as quiet notices, keeping the room topic on the levels
  (bot/feed.py). The room is chosen by an admin in chat, stored by the app.
  Private messages in the stream (a reminder to give a thing back) go to that
  member alone (bot/dm.py).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from collections import OrderedDict

import httpx
from mautrix.client import Client, InternalEventType, MembershipEventDispatcher
from mautrix.client.state_store.asyncpg import PgStateStore
from mautrix.crypto import OlmMachine, PgCryptoStore
from mautrix.errors import MatrixConnectionError, MatrixError, MForbidden
from mautrix.types import (
    EncryptedEvent,
    EventID,
    EventType,
    Format,
    InReplyTo,
    Membership,
    MessageEvent,
    MessageType,
    PaginationDirection,
    ReactionEvent,
    RedactionEvent,
    RelatesTo,
    RelationType,
    RoomID,
    RoomTopicStateEventContent,
    StrippedStateEvent,
    TextMessageEventContent,
    UserID,
    WhoamiResponse,
)
from mautrix.util.async_db import Database

from app.i18n import en
from bot.app_client import AppClient, AppError, follow_events
from bot.config import BotSettings, load_settings, read_secret
from bot.dm import DirectMessages
from bot.feed import LastId, RoomFeed
from bot.history import LIMIT, recent
from bot.notify import Notifier
from bot.rooms import should_answer

log = logging.getLogger("commons.bot")
TYPING_MS = 30_000
BALLOT_MEMORY = 5000  # ballot reactions remembered for removals (since start)
# Read back at most this many pages of 50 timeline events for the chat history.
HISTORY_PAGES = 6


class CommonsBot:
    def __init__(self, settings: BotSettings, client: Client, app: AppClient) -> None:
        self.s = settings
        self.client = client
        self.app = app
        # Events from before startup (e.g. replayed after a restart) are ignored.
        self.started_ms = int(time.time() * 1000)
        client.add_dispatcher(MembershipEventDispatcher)
        client.add_event_handler(InternalEventType.INVITE, self.on_invite)
        client.add_event_handler(EventType.ROOM_MESSAGE, self.on_message)
        client.add_event_handler(EventType.REACTION, self.on_reaction)
        client.add_event_handler(EventType.ROOM_REDACTION, self.on_redaction)
        # Reactions that were ballots: event id -> room (a removal gets an answer).
        self._ballot_reactions: OrderedDict[str, str] = OrderedDict()

    def _is_local(self, user_id: str) -> bool:
        return user_id.endswith(":" + self.s.server_name)

    async def on_invite(self, evt: StrippedStateEvent) -> None:
        if evt.state_key != self.s.user_id:
            return
        if not self._is_local(evt.sender):
            log.info("ignoring invite from another server")
            return
        try:
            await self.client.join_room(evt.room_id)
            log.info("joined %s", evt.room_id)
        except MatrixError as e:
            log.warning("could not join %s: %s", evt.room_id, e.errcode)

    async def _joined_count(self, room_id: str) -> int:
        members = await self.client.state_store.get_members(room_id, memberships=(Membership.JOIN,))
        if not members:
            members = list(await self.client.get_joined_members(room_id))
        return len(members)

    async def on_reaction(self, evt: ReactionEvent) -> None:
        """A tap on a vote message. Ballots only count in a member's own direct chat."""
        if evt.sender == self.s.user_id or evt.timestamp < self.started_ms:
            return
        if not self._is_local(evt.sender):
            return
        rel = evt.content.relates_to
        if rel is None or not rel.event_id or not rel.key:
            return
        if await self._joined_count(evt.room_id) > 2:
            return  # a reaction in a shared room is public: never a ballot
        try:
            reply = await self.app.reaction(evt.sender, evt.room_id, rel.event_id, rel.key)
        except AppError as e:
            log.warning("COMMONS did not take a tap: %s", e)
            return
        if reply:
            self._ballot_reactions[str(evt.event_id)] = str(evt.room_id)
            while len(self._ballot_reactions) > BALLOT_MEMORY:
                self._ballot_reactions.popitem(last=False)
            out = TextMessageEventContent(msgtype=MessageType.TEXT, body=reply)
            await self.client.send_message(evt.room_id, out)

    async def on_redaction(self, evt: RedactionEvent) -> None:
        """A removed tap keeps the vote; say so, and how to change it."""
        if evt.sender == self.s.user_id or evt.timestamp < self.started_ms:
            return
        room = self._ballot_reactions.pop(str(evt.redacts), None)
        if room is None or room != str(evt.room_id):
            return
        out = TextMessageEventContent(msgtype=MessageType.TEXT, body=en.VOTE_TAP_REMOVED)
        await self.client.send_message(evt.room_id, out)

    async def on_message(self, evt: MessageEvent) -> None:
        if evt.sender == self.s.user_id or evt.timestamp < self.started_ms:
            return
        if not self._is_local(evt.sender):
            return
        content = evt.content
        if content.msgtype != MessageType.TEXT or not isinstance(content.body, str):
            return  # notices (other bots), images, files, ...
        rel = content.relates_to
        if rel and rel.rel_type == RelationType.REPLACE:
            return  # edits
        mentions = (content.get("m.mentions") or {}).get("user_ids") or []
        if not isinstance(mentions, list):
            mentions = []
        joined = await self._joined_count(evt.room_id)
        if not should_answer(
            joined_members=joined, mentions=mentions, body=content.body, bot_user_id=self.s.user_id
        ):
            return

        await self.client.send_receipt(evt.room_id, evt.event_id)
        await self.client.set_typing(evt.room_id, timeout=TYPING_MS)
        typing = asyncio.create_task(self._keep_typing(evt.room_id))
        try:
            try:
                history = await self._history(evt)
                reply_to = content.get_reply_to()
                reply = await self.app.ask(
                    evt.sender,
                    evt.room_id,
                    content.body,
                    history,
                    direct=joined <= 2,
                    reply_to=str(reply_to) if reply_to else None,
                )
            except AppError as e:
                log.warning("COMMONS did not answer: %s", e)
                reply = en.BOT_UNAVAILABLE
        finally:
            typing.cancel()
            await self.client.set_typing(evt.room_id, timeout=0)

        # m.text, not m.notice: Element's default push rules mute notices.
        out = TextMessageEventContent(msgtype=MessageType.TEXT, body=reply)
        if joined > 2:
            out.set_reply(evt)
        await self.client.send_message(evt.room_id, out)

    async def _history(self, evt: MessageEvent) -> list[dict[str, str]]:
        """The last messages of this chat (the member's and COMMONS'), oldest first.

        Reads back page by page until it has LIMIT real messages: deleted
        messages and the deletion markers take up room in the timeline too.
        """
        lines: list[tuple[str, str, str]] = []
        token = None
        for _ in range(HISTORY_PAGES):
            try:
                page = await self.client.get_messages(
                    evt.room_id, PaginationDirection.BACKWARD, from_token=token, limit=50
                )
            except MatrixError as e:
                log.warning("could not read the chat history: %s", e.errcode)
                break
            for e in page.events:
                line = await self._line(e)
                if line is not None:
                    lines.append(line)
            got = recent(
                lines,
                current_event_id=str(evt.event_id),
                member=str(evt.sender),
                bot=self.s.user_id,
            )
            if len(got) >= LIMIT or not page.end or not page.events:
                return got
            token = page.end
        return recent(
            lines, current_event_id=str(evt.event_id), member=str(evt.sender), bot=self.s.user_id
        )

    async def _line(self, e) -> tuple[str, str, str] | None:
        """(event id, sender, text) for a text message, else None (deleted, edits, images …)."""
        # A deleted message has no content left: decrypting it fails, so it's skipped.
        if isinstance(e, EncryptedEvent) and self.client.crypto is not None:
            try:
                e = await self.client.crypto.decrypt_megolm_event(e)
            except Exception as err:  # an old message the bot has no key for
                log.debug("skipping a message it can't decrypt: %s", type(err).__name__)
                return None
        if e.type != EventType.ROOM_MESSAGE:
            return None
        content = e.content
        body = getattr(content, "body", None)
        if content.msgtype not in (MessageType.TEXT, MessageType.NOTICE) or not isinstance(
            body, str
        ):
            return None
        rel = content.relates_to
        if rel and rel.rel_type == RelationType.REPLACE:
            return None  # edits
        return str(e.event_id), str(e.sender), body

    async def _keep_typing(self, room_id: str) -> None:
        """CPU inference can take longer than one typing timeout."""
        while True:
            await asyncio.sleep(TYPING_MS / 1000 - 5)
            await self.client.set_typing(room_id, timeout=TYPING_MS)


class MatrixRoom:
    """bot.feed.Room on mautrix: notices (silent by default push rules), alerts, edits, topic."""

    def __init__(self, client: Client) -> None:
        self.client = client

    async def send_notice(self, room_id: str, text: str) -> str:
        content = TextMessageEventContent(msgtype=MessageType.NOTICE, body=text)
        return str(await self.client.send_message(RoomID(room_id), content))

    async def send_alert(self, room_id: str, text: str) -> str:
        # m.text: unlike notices, Element's default push rules notify for it.
        content = TextMessageEventContent(msgtype=MessageType.TEXT, body=text)
        return str(await self.client.send_message(RoomID(room_id), content))

    async def edit_notice(self, room_id: str, event_id: str, text: str) -> None:
        content = TextMessageEventContent(msgtype=MessageType.NOTICE, body=text)
        content.set_edit(EventID(event_id))
        await self.client.send_message(RoomID(room_id), content)

    @staticmethod
    def _content(text: str, html: str | None) -> TextMessageEventContent:
        # m.text: members get a notification (notices are muted by default).
        content = TextMessageEventContent(msgtype=MessageType.TEXT, body=text)
        if html:  # built and escaped by the app
            content.format = Format.HTML
            content.formatted_body = html
        return content

    async def send_announcement(self, room_id: str, text: str, html: str | None) -> str:
        content = self._content(text, html)
        # Notifies everyone if the bot may use @room in this room (power level 50).
        content["m.mentions"] = {"room": True}
        return str(await self.client.send_message(RoomID(room_id), content))

    async def send_reply(
        self, room_id: str, reply_to: str | None, text: str, html: str | None
    ) -> str:
        content = self._content(text, html)
        if reply_to:
            content.relates_to = RelatesTo(in_reply_to=InReplyTo(event_id=EventID(reply_to)))
        return str(await self.client.send_message(RoomID(room_id), content))

    async def set_topic(self, room_id: str, text: str) -> bool:
        try:
            await self.client.send_state_event(
                RoomID(room_id), EventType.ROOM_TOPIC, RoomTopicStateEventContent(topic=text)
            )
        except MForbidden:
            return False
        return True


class MatrixDirect:
    """bot.dm.Matrix on mautrix: find or open a member's 1:1 room and send them a message."""

    def __init__(self, client: Client) -> None:
        self.client = client
        self.bot_user_id = str(client.mxid)

    async def joined_rooms(self) -> list[str]:
        return [str(r) for r in await self.client.get_joined_rooms()]

    async def members(self, room_id: str) -> set[str]:
        found = await self.client.state_store.get_members(
            RoomID(room_id), memberships=(Membership.JOIN, Membership.INVITE)
        )
        if not found:
            found = list(await self.client.get_joined_members(RoomID(room_id)))
        return {str(m) for m in found}

    async def create_direct(self, user_id: str) -> str:
        room = await self.client.create_room(
            is_direct=True,
            invitees=[UserID(user_id)],
            initial_state=[
                {
                    "type": "m.room.encryption",
                    "state_key": "",
                    "content": {"algorithm": "m.megolm.v1.aes-sha2"},
                }
            ],
        )
        log.info("opened a direct room with a member")
        return str(room)

    async def send_text(self, room_id: str, text: str) -> None:
        # m.text, not m.notice: the member should get a notification.
        content = TextMessageEventContent(msgtype=MessageType.TEXT, body=text)
        await self.client.send_message(RoomID(room_id), content)


class MatrixRooms:
    """What bot/notify.py needs from Matrix."""

    def __init__(self, client: Client) -> None:
        self.client = client

    async def joined_rooms(self) -> list[str]:
        return [str(r) for r in await self.client.get_joined_rooms()]

    async def members(self, room_id: str) -> dict[str, str]:
        events = await self.client.get_members(RoomID(room_id))
        return {str(e.state_key): e.content.membership.value for e in events}

    async def create_direct(self, user_id: str) -> str:
        return str(await MatrixDirect(self.client).create_direct(user_id))

    async def can_encrypt_for(self, room_id: str, user_id: str) -> bool:
        """The room keys go to the members the state store knows, so it must know them."""
        store = self.client.state_store
        if not await store.has_full_member_list(RoomID(room_id)):
            return True  # mautrix then fetches the list from the server
        joined = await store.get_members(RoomID(room_id), memberships=(Membership.JOIN,))
        return user_id in joined

    async def send(self, room_id: str, text: str, html: str | None = None) -> str:
        # m.text, not m.notice: Element's default push rules mute notices.
        out = TextMessageEventContent(msgtype=MessageType.TEXT, body=text)
        if html:  # built and escaped by the app
            out.format = Format.HTML
            out.formatted_body = html
        return str(await self.client.send_message(RoomID(room_id), out))

    async def react(self, room_id: str, event_id: str, key: str) -> None:
        await self.client.react(RoomID(room_id), EventID(event_id), key)

    async def invite(self, room_id: str, user_id: str) -> None:
        await self.client.invite_user(RoomID(room_id), UserID(user_id))


async def open_store(s: BotSettings) -> Database:
    """One SQLite file for the Matrix room state and the encryption keys.

    It has to be one database: mautrix keeps the current transaction in a
    context variable shared by all databases, so a second file gets queries
    meant for the first.
    """
    db = Database.create(
        f"sqlite:{s.data_dir / 'bot.db'}", upgrade_table=PgCryptoStore.upgrade_table
    )
    await db.start()
    await PgStateStore.upgrade_table.upgrade(db)
    return db


async def setup_crypto(s: BotSettings, client: Client, db: Database) -> None:
    store = PgCryptoStore(account_id=s.user_id, pickle_key=read_secret("bot_pickle_key"), db=db)
    await store.open()
    crypto = OlmMachine(client, store, client.state_store)
    client.crypto = crypto
    await crypto.load()

    stored = await store.get_device_id()
    if stored and stored != client.device_id:
        raise RuntimeError(
            f"crypto store belongs to device {stored}, token is for {client.device_id}; "
            "issue the token for the same device or wipe the bot volume"
        )
    if not stored:
        await store.put_device_id(client.device_id)
    await crypto.share_keys()

    # Cross-sign the bot's own device, so clients that refuse unverified
    # devices still share room keys with it.
    rk_file = s.data_dir / "recovery_key"
    if rk_file.exists():
        await crypto.verify_with_recovery_key(rk_file.read_text(encoding="utf-8").strip())
        log.info("device cross-signed with the stored recovery key")
    elif await crypto.get_own_cross_signing_public_keys() is None:
        recovery_key = await crypto.generate_recovery_key()
        fd = os.open(rk_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(recovery_key + "\n")
        log.info("cross-signing set up; recovery key stored in the bot volume")
    else:
        log.error(
            "the bot account has cross-signing keys but no stored recovery key; "
            "the device stays unverified (reset the bot's identity to fix)"
        )


async def wait_for_homeserver(client: Client, attempts: int = 30) -> WhoamiResponse:
    """The homeserver may still be starting when the bot starts."""
    for attempt in range(1, attempts + 1):
        try:
            return await client.whoami()
        except MatrixConnectionError:
            if attempt == attempts:
                raise
            log.info("waiting for the homeserver (%d/%d)", attempt, attempts)
            await asyncio.sleep(5)
    raise AssertionError("unreachable")


async def run() -> None:
    s = load_settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)

    db = await open_store(s)
    state_store = PgStateStore(db)

    client = Client(
        mxid=s.user_id,
        device_id=s.device_id,
        base_url=s.homeserver_url,
        token=read_secret("bot_access_token"),
        state_store=state_store,
    )
    who = await wait_for_homeserver(client)
    if who.user_id != s.user_id or who.device_id != s.device_id:
        raise RuntimeError("access token belongs to a different user or device")

    await setup_crypto(s, client, db)
    client.sync_store = client.crypto.crypto_store

    async with httpx.AsyncClient() as http:
        token = read_secret("internal_api_token")
        app = AppClient(http, s.app_url, token)
        CommonsBot(s, client, app)

        async def announced(proposal_id: int, room_id: str, event_id: str) -> None:
            await app.vote_message(proposal_id, "room", room_id, event_id, None)

        room_feed = RoomFeed(MatrixRoom(client), on_announced=announced)
        direct = DirectMessages(
            MatrixDirect(client),
            server_name=s.server_name,
            community_room=lambda: room_feed.room_id,
        )

        async def handle(event: str, data: dict) -> None:
            if event == "feed" and await direct.on_feed(data):
                return
            await room_feed.handle(event, data)

        await client.set_displayname("COMMONS")
        log.info("bot %s (device %s) is running", s.user_id, s.device_id)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        sync = client.start(None)
        events = asyncio.create_task(
            follow_events(http, s.app_url, token, handle, LastId(s.data_dir / "feed_last_id"), stop)
        )
        notifier = asyncio.create_task(
            Notifier(
                MatrixRooms(client), app, s.user_id, community_room=lambda: room_feed.room_id
            ).run()
        )
        await stop.wait()
        notifier.cancel()
        events.cancel()
        await room_feed.close()
        client.stop()
        sync.cancel()

    await client.api.session.close()
    await db.stop()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("COMMONS_BOT_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # mautrix logs request bodies at debug level; keep them out of the logs.
    logging.getLogger("mau").setLevel(logging.INFO)
    asyncio.run(run())


if __name__ == "__main__":
    main()
