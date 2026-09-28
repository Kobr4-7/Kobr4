from datetime import UTC, datetime, timedelta
from decimal import Decimal

from kobr4.core.models import Bar
from kobr4.core.types import Timeframe
from kobr4.web.chart import aggregate, merge

T0 = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)


def m1(minute: int, o: str, h: str, lo: str, c: str) -> Bar:
    return Bar(
        symbol="EUR/USD",
        timeframe=Timeframe.M1,
        open_time=T0 + timedelta(minutes=minute),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(lo),
        close=Decimal(c),
    )


def test_aggregate_m1_into_hours() -> None:
    bars = [
        m1(0, "1.1000", "1.1010", "1.0990", "1.1005"),
        m1(30, "1.1005", "1.1030", "1.1000", "1.1020"),
        m1(61, "1.1020", "1.1025", "1.1015", "1.1018"),
    ]
    h = aggregate(bars, Timeframe.H1)
    assert [b.open_time.hour for b in h] == [8, 9]
    assert (h[0].open, h[0].high, h[0].low, h[0].close) == (
        Decimal("1.1000"),
        Decimal("1.1030"),
        Decimal("1.0990"),
        Decimal("1.1020"),
    )
    assert h[0].timeframe is Timeframe.H1


def test_merge_extends_stored_bar_with_live_data() -> None:
    stored = aggregate(
        [m1(-60, "1.09", "1.095", "1.085", "1.09"), m1(0, "1.1", "1.101", "1.099", "1.1")],
        Timeframe.H1,
    )
    live = aggregate([m1(20, "1.1", "1.105", "1.098", "1.104")], Timeframe.H1)
    out = merge(stored, live, 10)
    assert len(out) == 2
    last = out[-1]
    assert (last.open, last.high, last.low, last.close) == (
        Decimal("1.1"),
        Decimal("1.105"),
        Decimal("1.098"),
        Decimal("1.104"),
    )
    assert merge(stored, live, 1) == [last]
