"""Weather at the community's location, from SMHI open data.

SMHI is Sweden's public weather service (data under CC BY 4.0, no API key).
This is the third documented runtime exception to "no cloud":

- Only the configured coordinates leave the box, never anything about members.
- The app fetches the forecast in the background every `weather_refresh_minutes`
  through the allowlist egress proxy, and keeps the last good one in memory.
  Chat answers are built from that copy by code, so they are instant, and
  without internet COMMONS says how old its data is (or that it has none).
- The model only recognises "this is a weather question"; it never sees or
  invents the numbers.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings
from app.i18n import en

log = logging.getLogger("commons.weather")

# SMHI "snow1g" point forecast: hourly for ~2 days, then 3- to 12-hourly up to 10 days.
SMHI_URL = (
    "https://opendata-download-metfcst.smhi.se/api/category/snow1g/version/1"
    "/geotype/point/lon/{lon}/lat/{lat}/data.json"
)
MAX_RESPONSE_BYTES = 2_000_000
STALE_AFTER = timedelta(hours=3)
RAIN_MM_PER_HOUR = 0.1


@dataclass(frozen=True)
class Hour:
    time: datetime  # UTC
    temperature: float  # °C
    wind: float  # m/s, 10 min mean
    gust: float  # m/s
    precipitation: float  # mm/h, mean
    precipitation_probability: int  # %
    symbol: int  # SMHI weather symbol 1..27


@dataclass(frozen=True)
class Forecast:
    fetched_at: datetime  # UTC
    hours: tuple[Hour, ...]


class WeatherError(Exception):
    """SMHI could not be reached or sent something unusable."""


def _num(value: object, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or math.isnan(value):
        raise ValueError("not a number")
    if not low <= value <= high:
        raise ValueError("out of range")
    return float(value)


def parse_smhi(data: object, fetched_at: datetime) -> Forecast:
    """Validate SMHI's JSON. Entries with missing or implausible values are skipped."""
    if not isinstance(data, dict) or not isinstance(data.get("timeSeries"), list):
        raise WeatherError("unexpected response")
    hours = []
    for entry in data["timeSeries"]:
        try:
            values = entry["data"]
            time = datetime.fromisoformat(entry["time"].replace("Z", "+00:00")).astimezone(UTC)
            hours.append(
                Hour(
                    time=time,
                    temperature=_num(values["air_temperature"], -60, 50),
                    wind=_num(values["wind_speed"], 0, 80),
                    gust=_num(values["wind_speed_of_gust"], 0, 100),
                    precipitation=max(_num(values["precipitation_amount_mean"], -10, 200), 0.0),
                    precipitation_probability=int(
                        _num(values["probability_of_precipitation"], -10, 100)
                    ),
                    symbol=int(_num(values["symbol_code"], 1, 27)),
                )
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    if not hours:
        raise WeatherError("no usable forecast entries")
    hours.sort(key=lambda h: h.time)
    return Forecast(fetched_at=fetched_at, hours=tuple(hours))


class WeatherService:
    """Fetches and caches the forecast. One per app process."""

    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._s = settings
        self._http = http
        self._now = now or (lambda: datetime.now(UTC))
        self.forecast: Forecast | None = None

    @property
    def enabled(self) -> bool:
        return self._s.weather_latitude is not None and self._s.weather_longitude is not None

    def _url(self) -> str:
        # SMHI wants at most 6 decimals; 3 (about 100 m) is plenty for a 2.5 km grid.
        return SMHI_URL.format(
            lon=f"{self._s.weather_longitude:.3f}", lat=f"{self._s.weather_latitude:.3f}"
        )

    async def refresh(self) -> bool:
        """Fetch a new forecast. On failure the previous one is kept."""
        if not self.enabled:
            return False
        try:
            resp = await self._http.get(self._url(), timeout=20)
            if resp.status_code != 200:
                raise WeatherError(f"status {resp.status_code}")
            if len(resp.content) > MAX_RESPONSE_BYTES:
                raise WeatherError("response too large")
            self.forecast = parse_smhi(resp.json(), self._now())
            return True
        except (httpx.HTTPError, ValueError, WeatherError) as e:
            log.warning(
                "weather update failed: %s", e if isinstance(e, WeatherError) else type(e).__name__
            )
            return False

    async def run_forever(self) -> None:
        """Background loop started by the app's lifespan."""
        interval = max(self._s.weather_refresh_minutes, 5) * 60
        while True:
            await self.refresh()
            await asyncio.sleep(interval)


# --- turning numbers into a reply (code, not the model) -----------------------


def _symbol(code: int) -> str:
    return en.WEATHER_SYMBOLS.get(code, en.WEATHER_SYMBOLS[0])


def _range(hours: list[Hour]) -> str:
    low = round(min(h.temperature for h in hours))
    high = round(max(h.temperature for h in hours))
    return f"{low} °C" if low == high else f"{low} to {high} °C"


def _typical(hours: list[Hour]) -> str:
    # The most common symbol over the period; ties go to the "worse" (higher) code.
    counts = Counter(h.symbol for h in hours)
    return _symbol(max(counts, key=lambda c: (counts[c], c)))


def describe(forecast: Forecast | None, place: str, now: datetime, tz: ZoneInfo) -> str:
    """The weather reply: now, the next 12 hours, and tomorrow."""
    if forecast is None:
        return en.WEATHER_UNAVAILABLE
    future = [h for h in forecast.hours if h.time > now - timedelta(minutes=30)]
    if not future:
        return en.WEATHER_UNAVAILABLE
    current = future[0]
    lines = [
        en.WEATHER_NOW.format(
            place=place,
            temperature=round(current.temperature),
            sky=_symbol(current.symbol).lower(),
            wind=round(current.wind),
            gust=round(current.gust),
        )
    ]

    next12 = [h for h in future if h.time <= current.time + timedelta(hours=12)]
    wet = [h for h in next12 if h.precipitation >= RAIN_MM_PER_HOUR]
    if wet:
        when = wet[0].time.astimezone(tz).strftime("%H:%M")
        lines.append(
            en.WEATHER_NEXT_WET.format(
                range=_range(next12), when=when, sky=_symbol(wet[0].symbol).lower()
            )
        )
    else:
        lines.append(en.WEATHER_NEXT_DRY.format(range=_range(next12)))

    tomorrow = (now.astimezone(tz) + timedelta(days=1)).date()
    day = [h for h in forecast.hours if h.time.astimezone(tz).date() == tomorrow]
    if day:
        daytime = [h for h in day if 7 <= h.time.astimezone(tz).hour <= 19] or day
        lines.append(
            en.WEATHER_TOMORROW.format(
                day=tomorrow.strftime("%A"),
                range=_range(day),
                sky=_typical(daytime).lower(),
                chance=max(h.precipitation_probability for h in day),
            )
        )

    # The three days after tomorrow, one short entry each.
    later = []
    for offset in (2, 3, 4):
        date = (now.astimezone(tz) + timedelta(days=offset)).date()
        hours = [h for h in forecast.hours if h.time.astimezone(tz).date() == date]
        if hours:
            later.append(
                en.WEATHER_LATER_DAY.format(
                    day=date.strftime("%a"), range=_range(hours), sky=_typical(hours).lower()
                )
            )
    if later:
        lines.append(en.WEATHER_LATER.format(days="; ".join(later)))

    updated = forecast.fetched_at.astimezone(tz)
    if now - forecast.fetched_at > STALE_AFTER:
        lines.append(en.WEATHER_STALE.format(time=updated.strftime("%a %H:%M")))
    else:
        lines.append(en.WEATHER_SOURCE.format(time=updated.strftime("%H:%M")))
    return "\n".join(lines)
