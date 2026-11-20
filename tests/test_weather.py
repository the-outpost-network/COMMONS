"""Weather: SMHI parsing, the cached service, the reply text and the chat intent."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from tests.conftest import ADMIN_SECRET, INTERNAL_TOKEN, SERVER

from app.assistant import parse_rules
from app.i18n import en
from app.main import create_app
from app.weather import Forecast, WeatherError, WeatherService, describe, parse_smhi

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Europe/Stockholm")
NOW = datetime(2026, 11, 9, 12, 20, tzinfo=UTC)  # Monday, 13:20 local


def entry(time: datetime, temp=2.0, precip=0.0, prob=10, symbol=6, **extra) -> dict:
    data = {
        "air_temperature": temp,
        "wind_speed": 1.7,
        "wind_speed_of_gust": 4.7,
        "precipitation_amount_mean": precip,
        "probability_of_precipitation": prob,
        "symbol_code": symbol,
        **extra,
    }
    return {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ"), "data": data}


def smhi(hours: int = 96, **kw) -> dict:
    start = NOW.replace(minute=0) + timedelta(hours=1)
    return {"timeSeries": [entry(start + timedelta(hours=i), **kw) for i in range(hours)]}


def forecast(**kw) -> Forecast:
    return parse_smhi(smhi(**kw), NOW)


# --- parsing ----------------------------------------------------------------


def test_parse_skips_bad_entries() -> None:
    data = smhi(hours=3)
    data["timeSeries"].append({"time": "nonsense", "data": {}})
    data["timeSeries"].append(entry(NOW + timedelta(hours=9), temp=999))  # implausible
    data["timeSeries"].append(entry(NOW + timedelta(hours=10), symbol=99))
    data["timeSeries"].append(entry(NOW + timedelta(hours=11), temp=True))
    assert len(parse_smhi(data, NOW).hours) == 3


@pytest.mark.parametrize("data", [None, [], {}, {"timeSeries": "x"}, {"timeSeries": []}])
def test_parse_refuses_unusable_responses(data) -> None:
    with pytest.raises(WeatherError):
        parse_smhi(data, NOW)


# --- reply text -------------------------------------------------------------


def test_describe_dry() -> None:
    text = describe(forecast(), "the Outpost", NOW, TZ)
    assert text.splitlines() == [
        "Weather at the Outpost now: 2 °C, overcast, wind 2 m/s (gusts 5).",
        "Next 12 hours: 2 °C, staying dry.",
        "Tomorrow (Tuesday): 2 °C, mostly overcast, chance of precipitation 10 %.",
        "Later: Wed 2 °C, overcast; Thu 2 °C, overcast; Fri 2 °C, overcast.",
        "Forecast by SMHI, updated 13:20.",
    ]


def test_describe_rain_and_temperature_range() -> None:
    data = smhi()
    for i, e in enumerate(data["timeSeries"]):
        e["data"]["air_temperature"] = -1.0 + i % 5
    data["timeSeries"][4]["data"].update(precipitation_amount_mean=0.8, symbol_code=18)
    text = describe(parse_smhi(data, NOW), "the Outpost", NOW, TZ)
    # Entries start at 13:00 UTC (14:00 local), so entry 4 is 18:00 local.
    assert "Next 12 hours: -1 to 3 °C, light rain from about 18:00." in text


def test_describe_notes_old_data() -> None:
    old = dataclasses.replace(forecast(), fetched_at=NOW - timedelta(hours=5))
    text = describe(old, "the Outpost", NOW, TZ)
    assert text.endswith(en.WEATHER_STALE.format(time="Mon 08:20"))


def test_describe_without_data() -> None:
    assert describe(None, "the Outpost", NOW, TZ) == en.WEATHER_UNAVAILABLE
    past = parse_smhi(smhi(hours=2), NOW)
    assert describe(past, "x", NOW + timedelta(days=1), TZ) == en.WEATHER_UNAVAILABLE


# --- service ----------------------------------------------------------------


def service(settings, handler) -> WeatherService:
    s = dataclasses.replace(settings, weather_latitude=59.8374443, weather_longitude=15.5033958)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return WeatherService(s, http, now=lambda: NOW)


def test_refresh_sends_only_rounded_coordinates(settings) -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=smhi())

    svc = service(settings, handler)
    assert anyio.run(svc.refresh) is True
    [request] = seen
    assert request.url.host == "opendata-download-metfcst.smhi.se"
    assert request.url.path.endswith("/geotype/point/lon/15.503/lat/59.837/data.json")
    assert not request.url.query
    assert "cookie" not in request.headers
    assert "authorization" not in request.headers
    assert svc.forecast is not None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"timeSeries": []}),
        httpx.Response(200, content=b"x" * 2_000_001),
    ],
)
def test_failed_refresh_keeps_the_last_forecast(settings, response) -> None:
    calls = [httpx.Response(200, json=smhi()), response]
    svc = service(settings, lambda _: calls.pop(0))
    assert anyio.run(svc.refresh) is True
    first = svc.forecast
    assert anyio.run(svc.refresh) is False
    assert svc.forecast is first


def test_connection_error_is_handled(settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    svc = service(settings, handler)
    assert anyio.run(svc.refresh) is False
    assert svc.forecast is None


def test_no_coordinates_means_no_requests(settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call SMHI")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    svc = WeatherService(settings, http)
    assert not svc.enabled
    assert anyio.run(svc.refresh) is False


# --- chat intent ------------------------------------------------------------


@dataclasses.dataclass
class FakeWeather:
    forecast: Forecast | None
    enabled: bool = True


@pytest.fixture
def weather_client(settings, fake_matrix, fake_llm, nora):
    s = dataclasses.replace(settings, weather_latitude=59.837, weather_longitude=15.503)
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake_matrix.handler))
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(hours=1)
    data = {"timeSeries": [entry(start + timedelta(hours=i), temp=-3.0) for i in range(96)]}
    app = create_app(
        s,
        http=http,
        require_secrets=False,
        llm=fake_llm,
        internal_token=INTERNAL_TOKEN,
        admin_client_secret=ADMIN_SECRET,
        weather=FakeWeather(parse_smhi(data, now)),
    )
    with TestClient(app, base_url="https://commons.example.test") as c:
        yield c


def ask(client, text: str) -> str:
    r = client.post(
        "/internal/assistant",
        json={"sender": f"@nora:{SERVER}", "room_id": f"!dm:{SERVER}", "text": text},
        headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
    )
    assert r.status_code == 200, r.text
    return r.json()["reply"]


def test_weather_rule_answers_when_the_model_is_down(weather_client, fake_llm) -> None:
    text = ask(weather_client, "How's the weather?")  # nothing queued: the model is down
    assert text.startswith("Weather at the Outpost now: -3 °C")
    assert "SMHI" in text


def test_weather_intent_from_the_model_uses_codes_numbers(weather_client, fake_llm) -> None:
    # Even if the model tried to answer, only code-written numbers are sent.
    fake_llm.queue("weather", reply="It's 25 °C and sunny!")
    text = ask(weather_client, "do I need a jacket tomorrow?")
    assert "25 °C" not in text
    assert "-3 °C" in text


def test_weather_not_configured(client, nora, fake_llm) -> None:
    r = client.post(
        "/internal/assistant",
        json={"sender": f"@nora:{SERVER}", "room_id": f"!dm:{SERVER}", "text": "weather?"},
        headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
    )
    assert r.json()["reply"] == en.WEATHER_NOT_CONFIGURED


@pytest.mark.parametrize(
    ("text", "matches"),
    [
        ("weather?", True),
        ("How's the weather?", True),
        ("what is the weather like tomorrow?", True),
        ("forecast", True),
        ("will it rain later?", False),  # the model decides
        ("our rainwater tank is full", False),
        ("the weather station needs a battery", False),
    ],
)
def test_weather_rule(text: str, matches: bool) -> None:
    got = parse_rules(text)
    assert (got is not None and got.name == "weather") is matches


# --- network ----------------------------------------------------------------


def test_egress_allows_only_push_and_smhi() -> None:
    rules = (ROOT / "deploy/egress/allowlist.txt").read_text().split()
    assert rules == [r"^matrix\.org$", r"^opendata-download-metfcst\.smhi\.se$"]
