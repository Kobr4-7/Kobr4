from datetime import timedelta
from decimal import Decimal

import pytest

from kobr4.core.instruments import get_instrument
from kobr4.core.models import Tick
from kobr4.core.types import Timeframe
from kobr4.marketdata.bar_builder import BarBuilder, bucket_start
from tests.marketdata.helpers import T0


def tick(seconds: int, bid: str, spread: str = "0.00002", symbol: str = "EUR/USD") -> Tick:
    b = Decimal(bid)
    return Tick(symbol=symbol, bid=b, ask=b + Decimal(spread), ts=T0 + timedelta(seconds=seconds))


def test_bucket_start() -> None:
    assert bucket_start(T0 + timedelta(hours=6, minutes=59), Timeframe.H4) == T0 + timedelta(
        hours=4
    )
    assert bucket_start(T0 + timedelta(minutes=14, seconds=59), Timeframe.M15) == T0


def test_builds_and_closes_on_next_period() -> None:
    b = BarBuilder(get_instrument("EUR/USD"), Timeframe.M1)
    assert b.on_tick(tick(0, "1.08400")) == []
    assert b.on_tick(tick(20, "1.08430", "0.00003")) == []
    assert b.on_tick(tick(40, "1.08390")) == []
    (bar,) = b.on_tick(tick(61, "1.08410"))
    assert bar.open_time == T0
    assert (bar.open, bar.high, bar.low, bar.close) == (
        Decimal("1.08400"),
        Decimal("1.08430"),
        Decimal("1.08390"),
        Decimal("1.08390"),
    )
    assert bar.spread == Decimal("0.00003")  # moyenne 0,0000233 arrondie au tick supérieur
    assert bar.volume == 3


def test_flush_closes_only_after_period_end() -> None:
    b = BarBuilder(get_instrument("EUR/USD"), Timeframe.M5)
    b.on_tick(tick(10, "1.08400"))
    assert b.flush(T0 + timedelta(minutes=4, seconds=59)) == []
    (bar,) = b.flush(T0 + timedelta(minutes=5))
    assert bar.open_time == T0
    assert b.flush(T0 + timedelta(hours=1)) == []


def test_late_tick_ignored() -> None:
    b = BarBuilder(get_instrument("EUR/USD"), Timeframe.M1)
    b.on_tick(tick(70, "1.08400"))
    assert b.on_tick(tick(30, "1.09000")) == []
    (bar,) = b.flush(T0 + timedelta(minutes=5))
    assert bar.high == Decimal("1.08400")


def test_wrong_symbol() -> None:
    b = BarBuilder(get_instrument("EUR/USD"), Timeframe.M1)
    with pytest.raises(ValueError, match="GBP/USD"):
        b.on_tick(tick(0, "1.27000", symbol="GBP/USD"))
