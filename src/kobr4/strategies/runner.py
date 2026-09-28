"""Fait tourner les stratégies : distribue les bougies, publie leurs intentions."""

import logging
from collections.abc import Sequence
from datetime import datetime

from kobr4.core.bus import EventBus
from kobr4.core.clock import Clock
from kobr4.core.events import BarClosed, CloseRequested, SignalEmitted
from kobr4.core.models import CloseIntent, Position
from kobr4.portfolio import Portfolio
from kobr4.strategies.base import Strategy

log = logging.getLogger(__name__)


class _Context:
    def __init__(self, clock: Clock, portfolio: Portfolio) -> None:
        self._clock = clock
        self._portfolio = portfolio

    def now(self) -> datetime:
        return self._clock.now()

    def positions(self, strategy_id: str, symbol: str) -> Sequence[Position]:
        return self._portfolio.positions(strategy_id, symbol)


class StrategyRunner:
    def __init__(
        self, bus: EventBus, clock: Clock, portfolio: Portfolio, strategies: list[Strategy]
    ) -> None:
        self.bus = bus
        self.clock = clock
        self.strategies = strategies
        self.ctx = _Context(clock, portfolio)
        self.enabled: dict[str, bool] = {s.id: True for s in strategies}
        bus.subscribe(BarClosed, self._on_bar)

    async def _on_bar(self, e: BarClosed) -> None:
        bar = e.bar
        for strategy in self.strategies:
            if bar.timeframe is not strategy.timeframe or bar.symbol not in strategy.instruments:
                continue
            try:
                intents = strategy.on_bar(bar, self.ctx)
            except Exception:
                log.exception("stratégie %s : erreur sur %s", strategy.id, bar.symbol)
                continue
            if not self.enabled[strategy.id]:
                intents = [i for i in intents if isinstance(i, CloseIntent)]
            now = self.clock.now()
            for intent in intents:
                if isinstance(intent, CloseIntent):
                    await self.bus.publish(CloseRequested(ts=now, intent=intent))
                else:
                    await self.bus.publish(SignalEmitted(ts=now, intent=intent))
