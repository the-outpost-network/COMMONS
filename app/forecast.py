"""Shortage forecasts: small, classical statistics in numpy. No LLM.

Method: exponential smoothing with a damped additive trend (Gardner and
McKenzie, 1985). It is Holt's linear method with a damping factor phi: phi = 1
is plain Holt, a trend of zero is simple exponential smoothing. Damped trend
smoothing is a long-standing, well-tested default for short operational series:
it was among the most accurate methods in the M3 forecasting competition
(Makridakis and Hibon, 2000) and is the usual benchmark in later work
(Hyndman and Athanasopoulos, "Forecasting: Principles and Practice"). It needs
a few dozen arithmetic steps per fit, so it runs in milliseconds on any box.

What is forecast is daily use, not the level itself: refills make the level
jump, while use moves smoothly. For a `stock` resource:

1. The ledger gives the level after every entry (ledger.stock_steps, the same
   numbers the board shows). Use is a logged `use`, or the drop between a
   reading and the level before it, spread evenly over the time in between.
   A reading above the level before it is an unlogged refill, not negative use.
2. Use per full local day between the first and the last log is the series
   (the two edge days are partial). Fewer than `MIN_POINTS` days is refused
   (`insufficient_data`), and the reply says why.
3. alpha, beta and phi are chosen by a small grid search that minimises the
   one-step-ahead squared error. Short series (under `TREND_MIN_POINTS` days)
   get simple exponential smoothing only: a trend from a few days is noise.
4. The level is projected forward from the last logged level; the shortage
   date is the first day it falls below `shortage_threshold`. An 80 % range
   comes from the residual spread (cumulative error grows with sqrt(days)).

`flow` resources have no level, so they get a use forecast but no shortage.
Every run is stored in `forecast_run` with its inputs, parameters and result.
A shortage goes to the COMMONS room once per crossing (`flag_due`).
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from app import categories, ledger
from app.events import Event, publish
from app.i18n import en
from app.models import ForecastRun, ResourceCategory, ResourceEntry, utcnow
from app.textfmt import number

log = logging.getLogger("commons.forecast")

MIN_POINTS = 4  # days of logs; fewer is refused
TREND_MIN_POINTS = 8  # below this, no trend (simple exponential smoothing)
ALPHAS = (0.1, 0.2, 0.3, 0.5, 0.7, 0.9)
BETAS = (0.05, 0.1, 0.2, 0.3)
PHIS = (0.8, 0.9, 0.98, 1.0)
Z80 = 1.2816  # two-sided 80 % range
FLAG_MOVE_DAYS = 2  # a flagged date has to move earlier by more than this to flag again
RERUN_AFTER = timedelta(hours=24)  # recompute at least daily, even without new logs
METHOD_DAMPED = "damped_trend"
METHOD_SES = "simple"


# --- the model ------------------------------------------------------------------


@dataclass(frozen=True)
class Fit:
    method: str
    alpha: float
    beta: float
    phi: float
    level: float  # smoothed value at the end of the series
    trend: float
    sigma: float  # standard deviation of the one-step errors

    def path(self, days: int) -> np.ndarray:
        """Forecast for each of the next `days` days, never below zero."""
        h = np.arange(1, days + 1)
        if self.phi == 1.0:
            damp = h.astype(float)
        else:
            damp = self.phi * (1 - self.phi**h) / (1 - self.phi)
        return np.maximum(self.level + damp * self.trend, 0.0)

    def params(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "alpha": self.alpha,
            "beta": self.beta,
            "phi": self.phi,
            "sigma": round(self.sigma, 6),
        }


def _smooth(y: np.ndarray, alpha: float, beta: float, phi: float) -> tuple[float, float, float]:
    """One pass: (sum of squared one-step errors, final level, final trend)."""
    level = float(y[0])
    trend = float(y[1] - y[0]) if beta > 0 and len(y) > 1 else 0.0
    sse = 0.0
    for value in y[1:]:
        predicted = level + phi * trend
        error = float(value) - predicted
        sse += error * error
        new_level = alpha * float(value) + (1 - alpha) * predicted
        trend = beta * (new_level - level) + (1 - beta) * phi * trend
        level = new_level
    return sse, level, trend


def fit(y: np.ndarray) -> Fit:
    """Pick the smoothing with the smallest one-step-ahead squared error."""
    grid: list[tuple[str, float, float, float]] = [(METHOD_SES, a, 0.0, 1.0) for a in ALPHAS]
    if len(y) >= TREND_MIN_POINTS:
        grid += [(METHOD_DAMPED, a, b, p) for a in ALPHAS for b in BETAS for p in PHIS]
    best: tuple[float, Fit] | None = None
    for method, a, b, p in grid:
        sse, level, trend = _smooth(y, a, b, p)
        if best is None or sse < best[0] - 1e-12:
            best = (sse, Fit(method, a, b, p, level, trend, 0.0))
    if best is None:  # unreachable: the grid is never empty
        raise ValueError("no fit")
    sse, f = best
    sigma = math.sqrt(sse / max(1, len(y) - 1))
    return Fit(f.method, f.alpha, f.beta, f.phi, f.level, f.trend, sigma)


# --- from ledger entries to a daily series ------------------------------------------


@dataclass(frozen=True)
class Series:
    first_day: date
    use: np.ndarray  # use per local day, first_day .. last day with a log
    entries: int
    level_now: float | None  # stock: the level after the last log
    last_log: datetime
    log_day: date  # local day of the last log; projections count from here

    @property
    def points(self) -> int:
        return len(self.use)

    @property
    def last_day(self) -> date:
        return self.first_day + timedelta(days=self.points - 1)


def _spread(use: dict[date, float], start: datetime, end: datetime, amount: float, tz) -> None:
    """Add `amount` to the local days between start and end, in proportion to the time."""
    total = (end - start).total_seconds()
    if total <= 0:
        use[end.astimezone(tz).date()] = use.get(end.astimezone(tz).date(), 0.0) + amount
        return
    t = start
    while t < end:
        local = t.astimezone(tz)
        next_midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), tz)
        stop = min(end, next_midnight)
        day = local.date()
        use[day] = use.get(day, 0.0) + amount * (stop - t).total_seconds() / total
        t = stop


def series(counted: list[ledger.Counted], kind: str, tz: ZoneInfo) -> Series | None:
    """Daily use from the ledger, the way the board counts it. None if nothing counts."""
    live = [c for c in counted if not c.void]
    if not live:
        return None
    use: dict[date, float] = {}
    level_now: float | None = None
    if kind == "stock":
        before = 0.0
        previous: datetime | None = None
        for c, after in ledger.stock_steps(live):
            when = c.entry.occurred_at
            if c.entry.entry_type == "use":
                use[when.astimezone(tz).date()] = (
                    use.get(when.astimezone(tz).date(), 0.0) + c.amount
                )
            elif c.entry.entry_type == "reading" and previous is not None and after < before:
                _spread(use, previous, when, before - after, tz)
            before, previous = after, when
        level_now = before
    else:
        for c in live:
            if c.entry.entry_type == "use":
                day = c.entry.occurred_at.astimezone(tz).date()
                use[day] = use.get(day, 0.0) + c.amount
    times = [c.entry.occurred_at for c in live]
    first, last = min(times).astimezone(tz).date(), max(times).astimezone(tz).date()
    # Only full days between the first and the last log: the day of the first log
    # misses the use before it, the day of the last log the use after it.
    days = max(0, (last - first).days - 1)
    values = np.array([use.get(first + timedelta(days=1 + i), 0.0) for i in range(days)])
    return Series(first + timedelta(days=1), values, len(live), level_now, max(times), last)


# --- one forecast --------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    status: str  # "ok" or "insufficient_data"
    series: Series | None
    fit: Fit | None = None
    shortage_on: date | None = None
    earliest: date | None = None  # 80 % range of the shortage date (None: beyond horizon)
    latest: date | None = None
    already_below: bool = False
    use_per_day: float | None = None  # mean forecast use over the horizon
    horizon_days: int = 0


def _first_below(level: float, threshold: float, cumulative: np.ndarray) -> int | None:
    below = np.nonzero(level - cumulative < threshold)[0]
    return int(below[0]) + 1 if below.size else None


def forecast(category: ResourceCategory, s: Series | None, today: date) -> Outcome:
    """The forecast for one resource from its daily series (pure: no database)."""
    if s is None or s.points < MIN_POINTS:
        return Outcome("insufficient_data", s)
    f = fit(s.use)
    # From the last log to the end of the horizon, counted from today.
    days = max(0, (today - s.log_day).days) + category.forecast_horizon_days
    path = f.path(days)
    tail = path[-category.forecast_horizon_days :]
    outcome = Outcome(
        "ok", s, f, use_per_day=float(tail.mean()), horizon_days=category.forecast_horizon_days
    )
    threshold = category.shortage_threshold
    if category.kind != "stock" or threshold is None or s.level_now is None:
        return outcome
    if s.level_now < threshold:
        return Outcome(**{**outcome.__dict__, "already_below": True, "shortage_on": s.log_day})
    cumulative = np.cumsum(path)
    spread = Z80 * f.sigma * np.sqrt(np.arange(1, days + 1))
    mid = _first_below(s.level_now, threshold, cumulative)
    if mid is None:
        return outcome
    early = _first_below(s.level_now, threshold, cumulative + spread) or mid
    late = _first_below(s.level_now, threshold, np.maximum(cumulative - spread, 0.0))

    def on(day: int) -> date:
        return max(s.log_day + timedelta(days=day), today)

    return Outcome(
        **{
            **outcome.__dict__,
            "shortage_on": on(mid),
            "earliest": on(early),
            "latest": on(late) if late is not None else None,
        }
    )


# --- runs, flags and the background loop ----------------------------------------------


def latest_run(db: Session, category_id: int) -> ForecastRun | None:
    return db.scalar(
        select(ForecastRun)
        .where(ForecastRun.category_id == category_id)
        .order_by(ForecastRun.id.desc())
        .limit(1)
    )


def flag_due(db: Session, category_id: int, shortage_on: date) -> bool:
    """Post a shortage once per crossing: not again until the forecast cleared,
    unless the date moved earlier by more than FLAG_MOVE_DAYS."""
    flagged = db.scalar(
        select(ForecastRun)
        .where(ForecastRun.category_id == category_id, ForecastRun.flagged.is_(True))
        .order_by(ForecastRun.id.desc())
        .limit(1)
    )
    if flagged is None:
        return True
    cleared = db.scalar(
        select(ForecastRun.id)
        .where(
            ForecastRun.category_id == category_id,
            ForecastRun.id > flagged.id,
            ForecastRun.status == "ok",
            ForecastRun.shortage_on.is_(None),
        )
        .limit(1)
    )
    if cleared is not None:
        return True
    return (flagged.shortage_on - shortage_on).days > FLAG_MOVE_DAYS


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d is not None else None


def run(db: Session, category: ResourceCategory, now: datetime, tz: ZoneInfo) -> ForecastRun:
    """Forecast one resource, store the run and flag a new shortage. The caller commits."""
    s = series(ledger.counted_entries(db, category), category.kind, tz)
    out = forecast(category, s, now.astimezone(tz).date())
    flag = (
        out.shortage_on is not None
        and not out.already_below
        and flag_due(db, category.id, out.shortage_on)
    )
    row = ForecastRun(
        category_id=category.id,
        created_at=now,
        status=out.status,
        points=s.points if s else 0,
        entries=s.entries if s else 0,
        period_start=s.first_day if s else None,
        period_end=s.last_day if s else None,
        method=out.fit.method if out.fit else None,
        params=out.fit.params() if out.fit else {},
        inputs={
            "kind": category.kind,
            "unit": category.unit,
            "threshold": category.shortage_threshold,
            "horizon_days": category.forecast_horizon_days,
            "level_now": s.level_now if s else None,
            "min_points": MIN_POINTS,
        },
        result={
            "use_per_day": None if out.use_per_day is None else round(out.use_per_day, 6),
            "already_below": out.already_below,
            "earliest": _iso(out.earliest),
            "latest": _iso(out.latest),
        },
        shortage_on=out.shortage_on,
        flagged=flag,
    )
    db.add(row)
    db.flush()
    if flag:
        publish(db, Event("forecast.shortage", {"category_id": category.id, "run_id": row.id}))
    return row


def due(db: Session, category: ResourceCategory, now: datetime) -> bool:
    """New logs since the last run, or the last run is a day old."""
    last = latest_run(db, category.id)
    if last is None:
        return True
    if now - last.created_at >= RERUN_AFTER:
        return True
    newest = db.scalar(
        select(ResourceEntry.created_at)
        .where(ResourceEntry.category_id == category.id)
        .order_by(ResourceEntry.created_at.desc())
        .limit(1)
    )
    return newest is not None and newest > last.created_at


def run_due(sm: sessionmaker[Session], tz: ZoneInfo, now: datetime | None = None) -> int:
    """Forecast every active resource that needs it, one transaction each."""
    now = now or utcnow()
    with sm() as db:
        ids = [c.id for c in categories.list_active(db)]
    done = 0
    for category_id in ids:
        try:
            with sm() as db, db.begin():
                category = db.get(ResourceCategory, category_id)
                if category is None or not category.is_active or not due(db, category, now):
                    continue
                run(db, category, now, tz)
                done += 1
        except Exception:
            log.exception("forecast for resource %s failed", category_id)
    return done


async def run_forever(sm: sessionmaker[Session], every_seconds: float, tz: ZoneInfo) -> None:
    """Background loop started by the app's lifespan; one app worker, so one loop."""
    while True:
        try:
            await run_in_threadpool(run_due, sm, tz)
        except Exception:
            log.exception("forecasts failed")
        await asyncio.sleep(every_seconds)


# --- words -----------------------------------------------------------------------------


def day_text(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b}"


def basis_text(r: ForecastRun) -> str:
    return en.FORECAST_BASIS.format(days=r.points, entries=r.entries)


def range_text(r: ForecastRun) -> str:
    earliest, latest = r.result.get("earliest"), r.result.get("latest")
    if earliest is None:
        return ""
    first = day_text(date.fromisoformat(earliest))
    if latest is None:
        return en.FORECAST_RANGE_OPEN.format(first=first)
    if earliest == latest:
        return ""
    return en.FORECAST_RANGE.format(first=first, last=day_text(date.fromisoformat(latest)))


def describe(r: ForecastRun | None, category: ResourceCategory) -> str | None:
    """One line for chat; every number comes from the stored run."""
    if r is None:
        return None
    if r.status == "insufficient_data":
        return en.FORECAST_NOT_ENOUGH.format(days=r.points, min=MIN_POINTS)
    if r.result.get("already_below"):
        return None  # the level line already says it is running low
    basis = basis_text(r)
    if r.shortage_on is not None:
        span = range_text(r)
        return en.FORECAST_SHORTAGE.format(
            threshold=number(category.shortage_threshold),
            unit=category.unit,
            day=day_text(r.shortage_on),
            basis=f"{span}; {basis}" if span else basis,
        )
    if category.kind == "stock" and category.shortage_threshold is not None:
        return en.FORECAST_NO_SHORTAGE.format(days=r.inputs.get("horizon_days"), basis=basis)
    per_day = r.result.get("use_per_day")
    if per_day is None:
        return None
    return en.FORECAST_USE.format(
        amount=number(round(per_day, 1)),
        unit=category.unit,
        days=r.inputs.get("horizon_days"),
        basis=basis,
    )
