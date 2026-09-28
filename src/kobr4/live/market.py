"""Données de marché en direct : flux de cotations, bougies, détection des coupures."""

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from datetime import datetime

from kobr4.core.bus import EventBus
from kobr4.core.clock import Clock
from kobr4.core.events import BarClosed, MarketDataResumed, MarketDataStale, TickReceived
from kobr4.core.instruments import get_instrument
from kobr4.core.models import Bar, Tick
from kobr4.core.prices import PriceBook
from kobr4.core.types import Timeframe
from kobr4.execution.broker import Broker
from kobr4.marketdata.bar_builder import BarBuilder

log = logging.getLogger(__name__)


def forex_open(ts: datetime) -> bool:
    """Marché forex ouvert : du dimanche 22h au vendredi 21h UTC (marge d'une heure pour
    les changements d'heure)."""
    wd, h = ts.weekday(), ts.hour
    if wd == 5:
        return False
    if wd == 4:
        return h < 21
    if wd == 6:
        return h >= 22
    return True


class MarketData:
    """Reçoit les cotations du courtier et les diffuse, construit les bougies des unités
    utilisées par les stratégies, et signale les interruptions."""

    def __init__(
        self,
        bus: EventBus,
        broker: Broker,
        prices: PriceBook,
        clock: Clock,
        symbols: list[str],
        timeframes: dict[str, set[Timeframe]],
        stale_after: float = 60.0,
        is_open: Callable[[datetime], bool] = forex_open,
    ) -> None:
        self.bus = bus
        self.broker = broker
        self.prices = prices
        self.clock = clock
        self.symbols = symbols
        self.builders = [
            BarBuilder(get_instrument(sym), tf)
            for sym, tfs in timeframes.items()
            for tf in sorted(tfs, key=lambda t: t.duration)
        ]
        self.chart = {s: BarBuilder(get_instrument(s), Timeframe.M1) for s in symbols}
        """Bougies M1 construites pour les graphiques du site (toutes les paires suivies)."""
        self.chart_bars: dict[str, deque[Bar]] = {s: deque(maxlen=3 * 1440) for s in symbols}
        self.stale_after = stale_after
        self.is_open = is_open
        self.last_tick: dict[str, datetime] = {}
        self.stale: set[str] = set()
        self.connected = False
        self.ticks_received = 0

    async def on_tick(self, tick: Tick) -> None:
        self.ticks_received += 1
        self.last_tick[tick.symbol] = self.clock.now()
        self.prices.update(tick)
        if tick.symbol in self.stale:
            self.stale.discard(tick.symbol)
            await self.bus.publish(MarketDataResumed(ts=self.clock.now(), symbol=tick.symbol))
        await self.bus.publish(TickReceived(ts=self.clock.now(), tick=tick))
        chart = self.chart.get(tick.symbol)
        if chart is not None:
            self.chart_bars[tick.symbol].extend(chart.on_tick(tick))
        for b in self.builders:
            if b.instrument.symbol == tick.symbol:
                for bar in b.on_tick(tick):
                    await self.bus.publish(BarClosed(ts=self.clock.now(), bar=bar))

    def recent_m1(self, symbol: str) -> list[Bar]:
        """Bougies M1 reçues en direct depuis le démarrage (la dernière est en cours)."""
        bars = list(self.chart_bars.get(symbol, ()))
        chart = self.chart.get(symbol)
        current = chart.current() if chart is not None else None
        if current is not None:
            bars.append(current)
        return bars

    async def check(self) -> None:
        """Clôt les bougies échues sans nouvelle cotation et détecte les coupures.

        À appeler régulièrement (chaque seconde)."""
        now = self.clock.now()
        for b in self.builders:
            for bar in b.flush(now):
                await self.bus.publish(BarClosed(ts=now, bar=bar))
        if not self.is_open(now):
            return
        for sym in self.symbols:
            last = self.last_tick.get(sym)
            age = (now - last).total_seconds() if last else None
            if (
                (age is None or age > self.stale_after)
                and sym not in self.stale
                and self.ticks_received
            ):
                self.stale.add(sym)
                log.warning(
                    "%s : aucune cotation depuis %s s", sym, f"{age:.0f}" if age else "le démarrage"
                )
                await self.bus.publish(
                    MarketDataStale(ts=now, symbol=sym, seconds_since_last_tick=age or -1.0)
                )

    async def run(self) -> None:
        """Boucle du flux de prix, avec reconnexion (1 s, 2 s, 4 s… jusqu'à 60 s)."""
        delay = 1.0
        while True:
            try:
                self.connected = True
                async for tick in self.broker.stream_prices(self.symbols):
                    delay = 1.0
                    await self.on_tick(tick)
                log.warning("flux des prix terminé, reconnexion")
            except asyncio.CancelledError:
                self.connected = False
                raise
            except Exception as e:
                log.warning("flux des prix interrompu (%s), reconnexion dans %.0f s", e, delay)
            self.connected = False
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)
