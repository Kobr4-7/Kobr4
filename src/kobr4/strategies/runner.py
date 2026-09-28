"""Fait tourner les stratégies : distribue les bougies, publie leurs intentions."""

import logging
from collections.abc import Sequence
from datetime import datetime

from kobr4.core.bus import EventBus
from kobr4.core.clock import Clock
from kobr4.core.events import BarClosed, CloseRequested, SignalEmitted
from kobr4.core.models import Bar, CloseIntent, Position
from kobr4.intel.features import FeatureTracker
from kobr4.portfolio import Portfolio
from kobr4.strategies.base import Intent, Strategy

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
        self,
        bus: EventBus,
        clock: Clock,
        portfolio: Portfolio,
        strategies: list[Strategy],
        features: FeatureTracker | None = None,
    ) -> None:
        self.bus = bus
        self.clock = clock
        self.strategies = strategies
        self.ctx = _Context(clock, portfolio)
        self.enabled: dict[str, bool] = {s.id: True for s in strategies}
        self.features = features or FeatureTracker()
        self.regime_skips: dict[str, int] = {}
        """Entrées écartées parce que le régime de marché n'est pas celui voulu."""
        bus.subscribe(BarClosed, self._on_bar)

    def warmup(self, bars: list[Bar]) -> int:
        """Fait passer l'historique dans les stratégies pour amorcer leurs indicateurs,
        sans publier d'intention. Renvoie le nombre de bougies utilisées."""
        used = 0
        for bar in sorted(bars, key=lambda b: (b.open_time, b.symbol)):
            self.features.update(bar)
            for strategy in self.strategies:
                if bar.timeframe is strategy.timeframe and bar.symbol in strategy.instruments:
                    strategy.on_bar(bar, self.ctx)
                    used += 1
        return used

    def regime_allows(self, strategy: Strategy, symbol: str) -> bool:
        wanted = strategy.params.regimes
        return not wanted or self.features.regime(symbol, strategy.timeframe) in wanted

    async def _on_bar(self, e: BarClosed) -> None:
        bar = e.bar
        self.features.update(bar)
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
            elif not self.regime_allows(strategy, bar.symbol):
                kept: list[Intent] = [i for i in intents if isinstance(i, CloseIntent)]
                if len(kept) < len(intents):
                    self.regime_skips[strategy.id] = self.regime_skips.get(strategy.id, 0) + 1
                intents = kept
            now = self.clock.now()
            for intent in intents:
                if isinstance(intent, CloseIntent):
                    await self.bus.publish(CloseRequested(ts=now, intent=intent))
                else:
                    await self.bus.publish(SignalEmitted(ts=now, intent=intent))
