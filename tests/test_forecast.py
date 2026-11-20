"""Shortage forecasts: the model on known series, the data basis, runs and flags."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from sqlalchemy import func, select

from app import categories, chat_levels, forecast, ledger
from app.models import EventFeed, ForecastRun, ResourceCategory, ResourceEntry

TZ = ZoneInfo("Europe/Stockholm")
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
TODAY = date(2026, 11, 20)
Spec = categories.CategorySpec


def entry(kind: str, amount: float, at: datetime, entry_id: int = 0) -> ledger.Counted:
    e = ResourceEntry(id=entry_id, entry_type=kind, amount=amount, occurred_at=at)
    return ledger.Counted(e, amount, None)


def daily_readings(start_level: float, per_day: float, days: int, end: datetime = NOW):
    """One reading a day at noon, the last one at `end`."""
    first = end - timedelta(days=days - 1)
    return [
        entry("reading", start_level - per_day * i, first + timedelta(days=i), i)
        for i in range(days)
    ]


def category(kind="stock", threshold=1000.0, horizon=30) -> ResourceCategory:
    return ResourceCategory(
        slug="water",
        name="Water",
        unit="l",
        kind=kind,
        shortage_threshold=threshold,
        forecast_horizon_days=horizon,
    )


# --- the model ------------------------------------------------------------------


def test_constant_use_is_forecast_as_constant():
    f = forecast.fit(np.full(20, 100.0))
    assert f.path(5) == pytest.approx([100.0] * 5)
    assert f.sigma == pytest.approx(0.0)


def test_growing_use_gets_a_damped_trend():
    f = forecast.fit(np.arange(30, dtype=float) * 5 + 50)
    assert f.method == forecast.METHOD_DAMPED
    assert f.path(3)[0] > 190  # keeps growing, not stuck at the mean
    assert 0 < f.phi <= 1


def test_short_series_gets_no_trend():
    f = forecast.fit(np.array([10.0, 20.0, 30.0, 40.0, 50.0]))
    assert f.method == forecast.METHOD_SES
    assert f.trend == 0


def test_forecast_never_goes_below_zero():
    f = forecast.fit(np.array([50, 40, 30, 20, 10, 5, 2, 0, 0, 0], dtype=float))
    assert (f.path(60) >= 0).all()


# --- known series -> expected date ---------------------------------------------------


def test_linear_decline_gives_the_true_shortage_date():
    # 3000 l, 100 l used a day for 20 days: 1100 l now, below 1000 l in 2 days.
    s = forecast.series(daily_readings(3000, 100, 20), "stock", TZ)
    out = forecast.forecast(category(), s, TODAY)
    assert out.status == "ok"
    assert s.level_now == pytest.approx(1100)
    assert out.use_per_day == pytest.approx(100, rel=0.05)
    assert abs((out.shortage_on - (TODAY + timedelta(days=2))).days) <= 1


def test_sparse_readings_spread_the_use_over_the_days_between():
    # A reading every 4 days, 400 l less each time: still 100 l a day.
    first = NOW - timedelta(days=24)
    logs = [entry("reading", 3000 - 400 * i, first + timedelta(days=4 * i), i) for i in range(7)]
    s = forecast.series(logs, "stock", TZ)
    assert s.points == 23
    assert s.use.mean() == pytest.approx(100, rel=0.05)
    out = forecast.forecast(category(threshold=200), s, TODAY)
    # 600 l now, 100 l a day: below 200 l after 5 days.
    assert abs((out.shortage_on - (TODAY + timedelta(days=5))).days) <= 1


def test_refills_are_not_negative_use():
    logs = [
        *daily_readings(2000, 100, 6),
        entry("add", 1500, NOW + timedelta(hours=1), 99),
        entry("reading", 3200, NOW + timedelta(hours=2), 100),
    ]
    s = forecast.series(logs, "stock", TZ)
    assert (s.use >= 0).all()
    assert s.level_now == pytest.approx(3200)


def test_flat_level_means_no_shortage():
    s = forecast.series(daily_readings(2500, 0, 10), "stock", TZ)
    out = forecast.forecast(category(), s, TODAY)
    assert out.status == "ok"
    assert out.shortage_on is None


def test_shortage_range_brackets_the_date():
    noisy = [
        entry("reading", 3000 - 100 * i + (40 if i % 2 else -40), NOW - timedelta(days=19 - i), i)
        for i in range(20)
    ]
    out = forecast.forecast(category(threshold=500), forecast.series(noisy, "stock", TZ), TODAY)
    assert out.shortage_on is not None
    assert out.earliest <= out.shortage_on
    assert out.latest is None or out.latest >= out.shortage_on


def test_already_below_is_left_to_the_level_alert():
    s = forecast.series(daily_readings(1500, 100, 8), "stock", TZ)
    out = forecast.forecast(category(threshold=1000), s, TODAY)
    assert out.already_below


def test_flow_resources_get_use_but_no_shortage():
    logs = [entry("use", 2.0, NOW - timedelta(days=i), i) for i in range(10)]
    out = forecast.forecast(category(kind="flow"), forecast.series(logs, "flow", TZ), TODAY)
    assert out.use_per_day == pytest.approx(2.0, rel=0.05)
    assert out.shortage_on is None


@pytest.mark.parametrize("days", [1, 2, 3, 4, 5])
def test_fewer_than_four_days_is_refused(days):
    s = forecast.series(daily_readings(3000, 100, days), "stock", TZ)
    assert forecast.forecast(category(), s, TODAY).status == "insufficient_data"


def test_nothing_logged_is_refused():
    assert forecast.forecast(category(), None, TODAY).status == "insufficient_data"


# --- runs in the database ---------------------------------------------------------------


@pytest.fixture
def water(db):
    c = categories.create(
        db,
        Spec(slug="water", name="Water", unit="l", kind="stock", shortage_threshold=1000),
    )
    db.commit()
    return c


def log_readings(db, member, c, levels, end=NOW):
    first = end - timedelta(days=len(levels) - 1)
    for i, value in enumerate(levels):
        db.add(
            ResourceEntry(
                category_id=c.id,
                member_id=member.id,
                channel="web",
                entry_type="reading",
                amount=value,
                occurred_at=first + timedelta(days=i),
            )
        )
    db.commit()


def feed_count(db) -> int:
    return db.scalar(
        select(func.count()).select_from(EventFeed).where(EventFeed.kind == "forecast.shortage")
    )


def test_run_stores_the_data_basis_and_parameters(db, nora, water):
    log_readings(db, nora, water, [3000 - 100 * i for i in range(20)])
    r = forecast.run(db, water, NOW, TZ)
    db.commit()
    assert (r.status, r.points, r.entries) == ("ok", 18, 20)
    assert r.period_start == TODAY - timedelta(days=18)
    assert r.period_end == TODAY - timedelta(days=1)
    assert r.params["method"] in (forecast.METHOD_SES, forecast.METHOD_DAMPED)
    assert r.inputs["threshold"] == 1000
    assert r.shortage_on is not None


def test_shortage_is_flagged_once_per_crossing(db, nora, water):
    log_readings(db, nora, water, [3000 - 100 * i for i in range(20)])
    assert forecast.run(db, water, NOW, TZ).flagged
    db.commit()
    assert not forecast.run(db, water, NOW + timedelta(hours=1), TZ).flagged
    db.commit()
    assert feed_count(db) == 1
    line = db.scalar(select(EventFeed.summary).where(EventFeed.kind == "forecast.shortage"))
    assert "Water will likely drop below 1,000 l around" in line
    assert "based on 18 full days of logs, 20 entries" in line


def test_flags_again_after_the_forecast_cleared(db, nora, water):
    log_readings(db, nora, water, [3000 - 100 * i for i in range(20)])
    forecast.run(db, water, NOW, TZ)
    db.commit()
    later = NOW + timedelta(days=1)
    db.add(
        ResourceEntry(
            category_id=water.id, member_id=nora.id, channel="web",
            entry_type="add", amount=50000, occurred_at=later,
        )
    )  # fmt: skip
    db.commit()
    assert forecast.run(db, water, later, TZ).shortage_on is None  # refilled: cleared
    db.commit()
    assert forecast.flag_due(db, water.id, TODAY + timedelta(days=30))


def test_flags_again_when_the_date_moves_much_earlier(db, nora, water):
    log_readings(db, nora, water, [3000 - 100 * i for i in range(20)])
    first = forecast.run(db, water, NOW, TZ)
    db.commit()
    assert not forecast.flag_due(db, water.id, first.shortage_on - timedelta(days=2))
    assert forecast.flag_due(db, water.id, first.shortage_on - timedelta(days=3))


def test_too_few_logs_are_stored_as_insufficient(db, nora, water):
    log_readings(db, nora, water, [3000, 2900, 2800])
    r = forecast.run(db, water, NOW, TZ)
    db.commit()
    assert (r.status, r.points, r.flagged) == ("insufficient_data", 1, False)
    assert feed_count(db) == 0


def test_archived_resource_is_skipped(app, db, nora, water):
    log_readings(db, nora, water, [3000 - 100 * i for i in range(20)])
    categories.archive(db, water)
    db.commit()
    assert forecast.run_due(app.state.sessionmaker, TZ, NOW) == 0
    assert db.scalar(select(func.count()).select_from(ForecastRun)) == 0


def test_runs_only_again_after_new_logs_or_a_day(app, db, nora, water):
    log_readings(db, nora, water, [3000 - 100 * i for i in range(20)])
    sm = app.state.sessionmaker
    assert forecast.run_due(sm, TZ, NOW) == 1
    assert forecast.run_due(sm, TZ, NOW + timedelta(hours=2)) == 0
    assert forecast.run_due(sm, TZ, NOW + timedelta(hours=25)) == 1


def test_members_see_the_forecast_when_they_ask_about_levels(db, nora, water):
    log_readings(db, nora, water, [3000 - 100 * i for i in range(20)])
    forecast.run(db, water, NOW, TZ)
    db.commit()
    reply = chat_levels.levels_reply(db, water, NOW)
    assert "Forecast: likely below 1,000 l around" in reply
    assert "based on 18 full days of logs" in reply


def test_members_are_told_why_there_is_no_forecast(db, nora, water):
    log_readings(db, nora, water, [3000, 2900])
    forecast.run(db, water, NOW, TZ)
    db.commit()
    reply = chat_levels.levels_reply(db, water, NOW)
    assert "4 full days between the first and the latest log, and there are 0" in reply
