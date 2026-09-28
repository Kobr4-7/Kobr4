from datetime import timedelta
from decimal import Decimal

import polars as pl

from kobr4.core.instruments import get_instrument
from kobr4.core.models import Tick
from kobr4.core.types import Timeframe
from kobr4.marketdata import frames
from kobr4.marketdata.bar_builder import BarBuilder
from tests.marketdata.helpers import T0, m1_frame

EURUSD = get_instrument("EUR/USD")


def test_resample_h1() -> None:
    m1 = m1_frame(T0, list(range(108400, 108520)))  # 120 minutes, hausse régulière
    h1 = frames.resample(m1, Timeframe.H1)
    assert h1.height == 2
    first = h1.row(0, named=True)
    assert first["open_time"] == T0
    assert (first["open"], first["close"]) == (108400, 108459)
    assert (first["low"], first["high"]) == (108397, 108462)
    assert first["volume"] == 60.0
    assert h1.row(1, named=True)["open_time"] == T0 + timedelta(hours=1)


def test_resample_aligns_h4_and_d1_on_utc() -> None:
    m1 = m1_frame(T0 + timedelta(hours=5, minutes=17), [108400] * 10)
    assert frames.resample(m1, Timeframe.H4).row(0, named=True)["open_time"] == T0 + timedelta(
        hours=4
    )
    assert frames.resample(m1, Timeframe.D1).row(0, named=True)["open_time"] == T0


def test_resample_spread_is_ceiled_mean() -> None:
    m1 = pl.concat(
        [m1_frame(T0, [108400], spread=2), m1_frame(T0 + timedelta(minutes=1), [108400], spread=3)]
    )
    assert frames.resample(m1, Timeframe.H1).row(0, named=True)["spread"] == 3  # 2,5 → 3


def test_resample_without_spread() -> None:
    m1 = m1_frame(T0, [108400, 108401], spread=None)
    assert frames.resample(m1, Timeframe.H1).row(0, named=True)["spread"] is None


def test_bars_roundtrip() -> None:
    m1 = m1_frame(T0, [108400, 108425, 108391])
    bars = frames.to_bars(m1, EURUSD, Timeframe.M1)
    assert bars[1].close == Decimal("1.08425")
    assert bars[1].spread == Decimal("0.00002")
    assert frames.from_bars(bars, EURUSD).equals(m1)


def test_polars_resample_matches_live_builder() -> None:
    """Le backtest (agrégation de M1) et le réel (agrégation des cotations) doivent
    produire les mêmes prix OHLC."""
    prices = [108400 + (i * 37) % 23 - (i // 50) for i in range(300)]
    ticks = [
        Tick(
            symbol="EUR/USD",
            bid=Decimal(p).scaleb(-5),
            ask=Decimal(p + 2).scaleb(-5),
            ts=T0 + timedelta(seconds=20 * i),
        )
        for i, p in enumerate(prices)
    ]

    def build(tf: Timeframe) -> list[tuple[object, ...]]:
        b = BarBuilder(EURUSD, tf)
        out = [bar for t in ticks for bar in b.on_tick(t)]
        out += b.flush(T0 + timedelta(days=1))
        return [(x.open_time, x.open, x.high, x.low, x.close) for x in out]

    m1 = frames.from_bars(
        [
            bar
            for bar in (
                lambda b: [x for t in ticks for x in b.on_tick(t)] + b.flush(T0 + timedelta(days=1))
            )(BarBuilder(EURUSD, Timeframe.M1))
        ],
        EURUSD,
    )
    for tf in (Timeframe.M15, Timeframe.H1):
        via_m1 = [
            (b.open_time, b.open, b.high, b.low, b.close)
            for b in frames.to_bars(frames.resample(m1, tf), EURUSD, tf)
        ]
        assert via_m1 == build(tf)
