"""Moteur de backtest.

Rejoue l'historique à travers exactement les mêmes composants qu'en réel (stratégies,
risque, OMS, portefeuille), avec une horloge simulée et le courtier simulé.

Déroulé pour chaque instant de l'historique (toutes paires confondues) :
1. le courtier contrôle stops et objectifs sur la bougie qui vient de se former ;
2. les prix sont mis à jour avec la clôture ;
3. les bougies sont publiées (et agrégées dans les unités plus longues si besoin) ;
   les stratégies réagissent, le risque valide, l'OMS exécute ;
4. le portefeuille est valorisé.
"""

import heapq
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from itertools import groupby
from typing import Any

from kobr4.config.settings import Settings
from kobr4.core.bus import InMemoryEventBus
from kobr4.core.clock import SimulatedClock
from kobr4.core.events import BarClosed, RiskApproved, RiskRejected
from kobr4.core.models import Bar, ClosedTrade, ExitReason, OrderIntent
from kobr4.core.prices import PriceBook, conversion_symbols
from kobr4.core.types import Timeframe
from kobr4.execution.brokers.simulated import SimulatedBroker
from kobr4.execution.oms import OrderManager
from kobr4.intel.features import FeatureTracker
from kobr4.marketdata.resampler import BarResampler
from kobr4.marketdata.store import ParquetBarStore
from kobr4.portfolio import Portfolio
from kobr4.risk.calendar import EconomicCalendar
from kobr4.risk.manager import RiskManager, SignalFilter
from kobr4.strategies import create_strategy
from kobr4.strategies.base import Strategy
from kobr4.strategies.runner import StrategyRunner


@dataclass
class BacktestResult:
    initial_balance: Decimal
    start: datetime | None
    end: datetime | None
    trades: list[ClosedTrade]
    equity_curve: list[tuple[datetime, Decimal]]
    rejections: dict[str, int]
    bars_processed: int
    rejected_intents: list[RiskRejected] = field(default_factory=list)
    trade_context: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Par position : régime de marché et caractéristiques au moment de la décision."""
    regime_skips: dict[str, int] = field(default_factory=dict)

    @property
    def final_equity(self) -> Decimal:
        return self.equity_curve[-1][1] if self.equity_curve else self.initial_balance


def execution_timeframe(strategies: Iterable[Strategy]) -> Timeframe:
    """Unité la plus courte utilisée par les stratégies : c'est sur elle que les stops
    sont contrôlés."""
    return min((s.timeframe for s in strategies), key=lambda tf: tf.duration)


def required_symbols(settings: Settings) -> list[str]:
    symbols = sorted({s for st in settings.strategies if st.enabled for s in st.instruments})
    return symbols + sorted(conversion_symbols(symbols, settings.base_currency))


def load_bars(
    store: ParquetBarStore,
    settings: Settings,
    start: datetime | None,
    end: datetime | None,
) -> dict[str, list[Bar]]:
    strategies = [create_strategy(s) for s in settings.strategies if s.enabled]
    tf = execution_timeframe(strategies)
    return {sym: store.bars(sym, tf, start, end) for sym in required_symbols(settings)}


def _timeline(bars: dict[str, list[Bar]]) -> Iterator[tuple[datetime, list[Bar]]]:
    merged = heapq.merge(*bars.values(), key=lambda b: (b.open_time, b.symbol))
    for open_time, group in groupby(merged, key=lambda b: b.open_time):
        yield open_time, list(group)


async def run_backtest(
    settings: Settings,
    bars: dict[str, list[Bar]],
    calendar: EconomicCalendar | None = None,
    strategies: list[Strategy] | None = None,
    signal_filters: list[SignalFilter] | None = None,
    use_ml_filters: bool = True,
) -> BacktestResult:
    strategies = strategies or [create_strategy(s) for s in settings.strategies if s.enabled]
    if not strategies:
        raise ValueError("aucune stratégie active")
    first = min((b[0].open_time for b in bars.values() if b), default=None)
    if first is None:
        raise ValueError("aucune bougie à rejouer")

    exec_tf = execution_timeframe(strategies)
    bt = settings.backtest
    bus = InMemoryEventBus()
    clock = SimulatedClock(first)
    prices = PriceBook()
    broker = SimulatedBroker(
        prices,
        clock,
        account_currency=settings.base_currency,
        initial_balance=bt.initial_balance,
        slippage_pips=bt.slippage_pips,
        commission_per_million=bt.commission_per_million,
        leverage=settings.risk.leverage,
    )
    portfolio = Portfolio(bus, prices, clock, settings.base_currency, bt.initial_balance)
    counter = 0

    def next_id(intent: OrderIntent) -> str:
        nonlocal counter
        counter += 1
        return f"{intent.strategy_id}-{counter:06d}"

    features = FeatureTracker()
    risk = RiskManager(
        settings.risk,
        bus,
        portfolio,
        prices,
        clock,
        next_id,
        calendar,
        signal_filters=signal_filters,
    )
    oms = OrderManager(bus, broker, portfolio, clock)
    runner = StrategyRunner(bus, clock, portfolio, strategies, features)
    timeframe_of = {s.id: s.timeframe for s in strategies}
    context: dict[str, dict[str, Any]] = {}

    def remember(e: RiskApproved) -> None:
        tf = timeframe_of[e.intent.strategy_id]
        context[e.order.id] = {
            "regime": features.regime(e.intent.symbol, tf).value,
            "features": features.features(e.intent.symbol, tf),
            "strategy_id": e.intent.strategy_id,
        }

    bus.subscribe(RiskApproved, remember)

    rejected: list[RiskRejected] = []
    bus.subscribe(RiskRejected, rejected.append)

    higher = sorted(
        {s.timeframe for s in strategies if s.timeframe is not exec_tf}, key=lambda t: t.duration
    )
    resamplers = {
        (sym, tf): BarResampler(sym, tf)
        for sym in bars
        for tf in higher
        if any(sym in s.instruments and s.timeframe is tf for s in strategies)
    }

    curve: list[tuple[datetime, Decimal]] = []
    processed = 0
    for open_time, group in _timeline(bars):
        clock.set(open_time)
        for bar in group:
            await broker.on_bar(bar)
        close_time = group[0].close_time
        clock.set(close_time)
        for bar in group:
            prices.update_from_bar(bar)
        for bar in group:
            await bus.publish(BarClosed(ts=close_time, bar=bar))
            for (sym, _), resampler in resamplers.items():
                if sym == bar.symbol:
                    for big in resampler.on_bar(bar):
                        await bus.publish(BarClosed(ts=close_time, bar=big))
        snap = await portfolio.mark(close_time)
        curve.append((close_time, snap.equity))
        processed += len(group)

    await oms.close_all(ExitReason.END_OF_TEST)
    if curve:
        snap = await portfolio.mark(clock.now())
        curve[-1] = (curve[-1][0], snap.equity)

    return BacktestResult(
        initial_balance=bt.initial_balance,
        start=first,
        end=clock.now(),
        trades=portfolio.trades,
        equity_curve=curve,
        rejections=dict(risk.rejections),
        bars_processed=processed,
        rejected_intents=rejected,
        trade_context=context,
        regime_skips=dict(runner.regime_skips),
    )
