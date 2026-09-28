import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from kobr4.config.settings import AllocationSettings, StrategySettings
from kobr4.core.bus import InMemoryEventBus
from kobr4.core.clock import SimulatedClock
from kobr4.core.events import BarClosed, SignalEmitted
from kobr4.core.models import Bar, ClosedTrade, ExitReason, OrderIntent
from kobr4.core.prices import PriceBook
from kobr4.core.types import Regime, Side, Timeframe
from kobr4.intel.allocation import risk_multiplier
from kobr4.intel.features import FEATURES, FeatureTracker, classify
from kobr4.intel.ml import Sample, SignalModel, auc, ml_filter, train_filter
from kobr4.portfolio import Portfolio
from kobr4.strategies import create_strategy
from kobr4.strategies.runner import StrategyRunner

T0 = datetime(2024, 1, 1, tzinfo=UTC)


def bars(closes: Sequence[float], wick: float = 0.0005) -> list[Bar]:
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o, cl = Decimal(str(round(prev, 5))), Decimal(str(round(c, 5)))
        out.append(
            Bar(
                symbol="EUR/USD",
                timeframe=Timeframe.H1,
                open_time=T0 + timedelta(hours=i),
                open=o,
                high=max(o, cl) + Decimal(str(wick)),
                low=min(o, cl) - Decimal(str(wick)),
                close=cl,
                spread=Decimal("0.0001"),
            )
        )
        prev = c
    return out


def test_regimes_trend_vs_range() -> None:
    trend, rng = FeatureTracker(), FeatureTracker()
    for b in bars([1.10 + i * 0.001 for i in range(120)], wick=0.0002):
        trend.update(b)
    r = random.Random(1)
    for b in bars([1.10 + r.uniform(-0.001, 0.001) for _ in range(120)], wick=0.0004):
        rng.update(b)
    assert trend.regime("EUR/USD", Timeframe.H1) is Regime.TREND
    assert rng.regime("EUR/USD", Timeframe.H1) in (Regime.RANGE, Regime.VOLATILE)
    f = trend.features("EUR/USD", Timeframe.H1)
    assert f is not None
    assert set(f) == set(FEATURES)
    assert FeatureTracker().regime("EUR/USD", Timeframe.H1) is Regime.UNKNOWN


def test_classify() -> None:
    base = dict.fromkeys(FEATURES, 0.0)
    assert classify({**base, "adx": 30}) is Regime.TREND
    assert classify({**base, "adx": 15, "atr_rank": 0.9}) is Regime.VOLATILE
    assert classify({**base, "adx": 15, "atr_rank": 0.4}) is Regime.RANGE


def trade(pnl: float, sid: str = "s") -> ClosedTrade:
    return ClosedTrade(
        position_id="p",
        strategy_id=sid,
        symbol="EUR/USD",
        side=Side.BUY,
        quantity=1000,
        entry_price=Decimal(1),
        exit_price=Decimal(1),
        opened_at=T0,
        closed_at=T0,
        reason=ExitReason.SIGNAL,
        pnl=Decimal(str(pnl)),
    )


def test_allocation_multiplier() -> None:
    on = AllocationSettings(enabled=True, lookback_trades=20, min_trades=10)
    assert risk_multiplier(AllocationSettings(), "s", [trade(10)] * 30) == 1
    assert risk_multiplier(on, "s", [trade(10)] * 5) == 1  # pas assez de trades
    good = [trade(20), trade(-10)] * 10  # profit factor 2 → ×1,5
    assert risk_multiplier(on, "s", good) == Decimal("1.5")
    bad = [trade(5), trade(-10)] * 10  # profit factor 0,5 → ×0,75
    assert risk_multiplier(on, "s", bad) == Decimal("0.75")
    assert risk_multiplier(on, "s", [trade(-10)] * 20) == Decimal("0.5")
    assert risk_multiplier(on, "autre", good) == 1


async def test_allocation_changes_position_size(t0: datetime) -> None:
    from kobr4.config.settings import RiskSettings
    from tests.stack import intent, make_stack

    st = make_stack(t0, RiskSettings(allocation=AllocationSettings(enabled=True, min_trades=10)))
    st.quote("EUR/USD", "1.08")
    base = st.risk.evaluate(intent(t0))
    st.portfolio.trades.extend([trade(-10, "s1")] * 20)
    reduced = st.risk.evaluate(intent(t0))
    assert base.quantity == 40_000  # type: ignore[union-attr]
    assert reduced.quantity == 20_000  # type: ignore[union-attr]


async def test_runner_regime_filter() -> None:
    bus, clock = InMemoryEventBus(), SimulatedClock(T0)
    portfolio = Portfolio(bus, PriceBook(), clock, "USD", Decimal(10_000))
    s = create_strategy(
        StrategySettings(
            id="s",
            kind="breakout",
            instruments=["EUR/USD"],
            timeframe=Timeframe.H1,
            params={"lookback": 5, "regimes": ["range"]},
        )
    )
    runner = StrategyRunner(bus, clock, portfolio, [s])
    signals: list[SignalEmitted] = []
    bus.subscribe(SignalEmitted, signals.append)
    for b in bars([1.10 + i * 0.001 for i in range(120)], wick=0.0002):  # tendance nette
        await bus.publish(BarClosed(ts=b.close_time, bar=b))
    assert signals == []
    assert runner.regime_skips["s"] > 0


def synthetic_samples(n: int, seed: int = 3) -> list[Sample]:
    """Les trades réussissent surtout quand l'ADX est élevé : un signal que le modèle doit trouver."""
    r = random.Random(seed)
    out = []
    for i in range(n):
        f = {k: r.uniform(0, 1) for k in FEATURES}
        f["adx"] = r.uniform(5, 50)
        win = r.random() < (0.75 if f["adx"] > 25 else 0.25)
        out.append(Sample(ts=T0 + timedelta(hours=i), features=f, pnl=20.0 if win else -10.0))
    return out


def test_train_filter_finds_real_signal(tmp_path: Path) -> None:
    model = train_filter(synthetic_samples(600))
    r = model.report
    assert r.useful, r.reason
    assert r.test_auc > 0.65
    assert r.test_pf_kept > r.test_pf_all
    loaded = SignalModel.load(model.save(tmp_path / "m"))
    f = dict.fromkeys(FEATURES, 0.5)
    assert loaded.probability({**f, "adx": 45}) > loaded.probability({**f, "adx": 8})


def test_train_filter_rejects_noise() -> None:
    r = random.Random(9)
    noise = [
        Sample(
            ts=T0 + timedelta(hours=i),
            features={k: r.random() for k in FEATURES},
            pnl=r.choice([15.0, -10.0]),
        )
        for i in range(400)
    ]
    assert not train_filter(noise).report.useful


def test_train_filter_needs_enough_trades() -> None:
    with pytest.raises(ValueError, match="au moins"):
        train_filter(synthetic_samples(20))


def test_auc() -> None:
    assert auc([True, True, False, False], [0.9, 0.8, 0.2, 0.1]) == 1.0
    assert auc([True, False], [0.1, 0.9]) == 0.0
    assert auc([True, True], [0.1, 0.2]) == 0.5


def test_ml_filter_rule() -> None:
    model = train_filter(synthetic_samples(600))
    tracker = FeatureTracker()
    check = ml_filter(model, "s", Timeframe.H1, tracker)
    it = OrderIntent(
        strategy_id="s", symbol="EUR/USD", side=Side.BUY, stop_loss_pips=Decimal(20), ts=T0
    )
    assert check(it) is None  # pas encore de caractéristiques : pas de filtrage
    r = random.Random(2)
    for b in bars([1.10 + r.uniform(-0.0008, 0.0008) for _ in range(150)]):
        tracker.update(b)
    refusal = check(it)
    assert refusal is not None
    assert refusal.rule == "ml_filter"
    assert check(it.model_copy(update={"strategy_id": "autre"})) is None
