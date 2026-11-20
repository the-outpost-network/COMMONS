"""Matrix bot: which messages it passes on, its call to the app, container setup.

The Matrix wiring (bot/main.py) needs mautrix + libolm and a homeserver, so it
is checked live. The assistant itself is tested in tests/test_assistant.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import anyio
import httpx
import pytest
import yaml

from bot.app_client import AppClient, AppError
from bot.rooms import should_answer

ROOT = Path(__file__).resolve().parents[1]
BOT = "@commons:commons.test"


# --- when to answer ---------------------------------------------------------


@pytest.mark.parametrize(
    ("members", "mentions", "body", "expected"),
    [
        (2, [], "hi", True),  # direct chat
        (1, [], "hi", True),
        (5, [], "hi everyone", False),  # group chat, not addressed
        (5, [], "we share the commons", False),
        (5, [BOT], "COMMONS: hi", True),  # @-mention
        (5, [], "commons, what is 2+2?", True),
        (5, ["@adelia:commons.test"], "adelia?", False),
    ],
)
def test_should_answer(members: int, mentions: list[str], body: str, expected: bool) -> None:
    got = should_answer(joined_members=members, mentions=mentions, body=body, bot_user_id=BOT)
    assert got is expected


# --- call to the app --------------------------------------------------------


def ask(handler) -> str:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = AppClient(http, "http://app:8000/", "service-token")
    return anyio.run(app.ask, "@nora:commons.test", "!room:commons.test", "hi")


def test_app_client_sends_sender_and_token() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"reply": "hello"})

    assert ask(handler) == "hello"
    assert seen["url"] == "http://app:8000/internal/assistant"
    assert seen["auth"] == "Bearer service-token"
    assert seen["body"] == {
        "sender": "@nora:commons.test",
        "room_id": "!room:commons.test",
        "text": "hi",
        "history": [],
    }


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, text="unauthorized"),
        httpx.Response(500, text="boom"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"reply": ""}),
        httpx.Response(200, json={"other": "x"}),
    ],
)
def test_app_client_errors(response: httpx.Response) -> None:
    with pytest.raises(AppError):
        ask(lambda _: response)


def test_app_client_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(AppError):
        ask(handler)


# --- containers -------------------------------------------------------------


class ComposeLoader(yaml.SafeLoader):
    """Reads Compose's merge tags (!override, !reset) as plain values."""


def _compose_tag(loader: yaml.SafeLoader, _suffix: str, node: yaml.Node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


ComposeLoader.add_multi_constructor("!", _compose_tag)


def test_llm_and_bot_are_private_and_hardened() -> None:
    text = (ROOT / "docker-compose.yml").read_text()
    compose = yaml.load(text, Loader=ComposeLoader)  # noqa: S506 - SafeLoader subclass
    services = compose["services"]
    for name in ("llm", "bot"):
        svc = services[name]
        assert "ports" not in svc, f"{name} must not publish ports"
        assert svc["networks"] == ["internal"], f"{name} must stay on the internal network"
        assert svc["user"] == "10001:10001"
        assert svc["read_only"] is True
        assert svc["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in svc["security_opt"]
        assert svc["mem_limit"]

    llm = services["llm"]
    assert "@sha256:" in llm["image"]
    assert "--no-webui" in llm["command"]
    assert "--api-key-file" in llm["command"]
    assert "./models:/models:ro" in llm["volumes"]
    # The bot gets neither database credentials nor the model key: it only talks to the app.
    assert set(services["bot"]["secrets"]) == {
        "bot_access_token",
        "bot_pickle_key",
        "internal_api_token",
    }
    assert not any("DATABASE" in k or "LLM" in k for k in services["bot"]["environment"])
    # The app reaches the model and has its MAS admin client secret.
    assert services["app"]["environment"]["COMMONS_LLM_URL"] == "http://llm:8080"
    assert "matrix_admin_client_secret" in services["app"]["secrets"]
    assert "--config" in services["mas"]["command"]


def test_model_checksum_is_recorded() -> None:
    sums = (ROOT / "deploy/models.sha256").read_text().split()
    assert len(sums[0]) == 64
    assert sums[1] == "SmolLM3-Q4_K_M.gguf"


def test_history_keeps_the_members_and_commons_messages_oldest_first() -> None:
    from bot.history import recent

    bot, nora, ana = "@commons:x", "@nora:x", "@ana:x"
    newest_first = [
        ("$now", nora, "I returned it"),
        ("$4", bot, "You said you'd give back the Saw by 15:30."),
        ("$3", ana, "someone else in a group room"),
        ("$2", nora, "  "),
        ("$1", nora, "I'm taking the saw till 15:30"),
    ]
    assert recent(newest_first, current_event_id="$now", member=nora, bot=bot) == [
        {"role": "user", "text": "I'm taking the saw till 15:30"},
        {"role": "assistant", "text": "You said you'd give back the Saw by 15:30."},
    ]
    many = [(f"${n}", nora, f"m{n}") for n in range(30, 0, -1)]
    got = recent(many, current_event_id="$now", member=nora, bot=bot)
    assert [g["text"] for g in got] == [f"m{n}" for n in range(21, 31)]
