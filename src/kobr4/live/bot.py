"""Un bot en direct : assemble le courtier, les données, les stratégies, le risque,
l'OMS, le journal, les métriques et les alertes, et fait tourner les boucles."""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from kobr4.config.settings import Settings
from kobr4.core.bus import InMemoryEventBus
from kobr4.core.clock import Clock, LiveClock
from kobr4.core.events import Event, KillSwitchActivated, PositionClosed, RiskRejected
from kobr4.core.models import ExitReason, OrderIntent
from kobr4.core.prices import PriceBook, conversion_symbols
from kobr4.db.session import Database
from kobr4.execution.broker import Broker
from kobr4.execution.oms import OrderManager
from kobr4.intel.features import FeatureTracker
from kobr4.intel.ml import build_filters
from kobr4.live.alerts import AlertRouter, Notifier
from kobr4.live.journal import Journal
from kobr4.live.market import MarketData
from kobr4.live.metrics import BotMetrics
from kobr4.portfolio import Portfolio
from kobr4.risk.calendar import EconomicCalendar
from kobr4.risk.manager import RiskManager
from kobr4.strategies import create_strategy
from kobr4.strategies.runner import StrategyRunner

log = logging.getLogger(__name__)

CalendarSource = Callable[[], Awaitable[EconomicCalendar]]


class BotState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    ERROR = "error"


@dataclass
class Intervals:
    check: float = 1.0
    mark: float = 5.0
    reconcile: float = 60.0
    account: float = 30.0
    calendar: float = 6 * 3600.0


@dataclass
class _Recent:
    events: list[dict[str, Any]] = field(default_factory=list)

    def add(self, e: Event) -> None:
        self.events.append({"ts": e.ts.isoformat(), "type": type(e).__name__, **_summary(e)})
        del self.events[:-200]


def _summary(e: Event) -> dict[str, Any]:
    if isinstance(e, RiskRejected):
        return {"message": f"{e.intent.symbol} refusé ({e.rule}) : {e.reason}"}
    if isinstance(e, PositionClosed):
        t = e.trade
        return {"message": f"{t.symbol} fermé ({t.reason}) : {t.pnl:+.2f}"}
    if isinstance(e, KillSwitchActivated):
        return {"message": f"arrêt d'urgence : {e.reason}"}
    return {"message": type(e).__name__}


class LiveBot:
    def __init__(
        self,
        bot_id: str,
        name: str,
        settings: Settings,
        broker: Broker,
        db: Database | None = None,
        notifier: Notifier | None = None,
        notify_trades: bool = True,
        calendar_source: CalendarSource | None = None,
        clock: Clock | None = None,
        intervals: Intervals | None = None,
    ) -> None:
        self.id = bot_id
        self.name = name
        self.settings = settings
        self.broker = broker
        self.clock = clock or LiveClock()
        self.intervals = intervals or Intervals()
        self.state = BotState.STOPPED
        self.error: str | None = None
        self.started_at: datetime | None = None

        self.bus = InMemoryEventBus()
        self.prices = PriceBook()
        self.strategies = [create_strategy(s) for s in settings.strategies if s.enabled]
        self.portfolio = Portfolio(
            self.bus, self.prices, self.clock, settings.base_currency, Decimal(0)
        )
        self.calendar = EconomicCalendar()
        self.features = FeatureTracker()
        self.risk = RiskManager(
            settings.risk,
            self.bus,
            self.portfolio,
            self.prices,
            self.clock,
            self._order_id,
            self.calendar,
            signal_filters=build_filters(self.strategies, settings.strategies, self.features),
        )
        self.oms = OrderManager(self.bus, broker, self.portfolio, self.clock)
        self.runner = StrategyRunner(
            self.bus, self.clock, self.portfolio, self.strategies, self.features
        )

        traded = sorted({s for st in self.strategies for s in st.instruments})
        self.symbols = traded + sorted(conversion_symbols(traded, settings.base_currency))
        timeframes: dict[str, set[Any]] = {}
        for st in self.strategies:
            for sym in st.instruments:
                timeframes.setdefault(sym, set()).add(st.timeframe)
        self.market = MarketData(
            self.bus,
            broker,
            self.prices,
            self.clock,
            self.symbols,
            timeframes,
            stale_after=getattr(broker, "stale_after", 60.0),
        )
        self.metrics = BotMetrics(bot_id, self.bus)
        self.journal = Journal(db, bot_id, self.bus) if db is not None else None
        self.alerts = (
            AlertRouter(self.bus, notifier, name, notify_trades=notify_trades) if notifier else None
        )
        self.calendar_source = calendar_source
        self.recent = _Recent()
        self.bus.subscribe(RiskRejected, self.recent.add)
        self.bus.subscribe(PositionClosed, self.recent.add)
        self.bus.subscribe(KillSwitchActivated, self.recent.add)
        self._tasks: list[asyncio.Task[None]] = []

    @staticmethod
    def _order_id(intent: OrderIntent) -> str:
        return f"{intent.strategy_id[:20]}-{uuid.uuid4().hex[:12]}"

    # Cycle de vie

    async def start(self) -> None:
        if self.state in (BotState.RUNNING, BotState.STARTING):
            return
        self.state = BotState.STARTING
        self.error = None
        try:
            await self.broker.connect()
            account = await self.broker.account()
            if account.currency != self.settings.base_currency:
                raise ValueError(
                    f"le compte est en {account.currency}, la configuration en"
                    f" {self.settings.base_currency}"
                )
            self.portfolio.balance = account.balance
            self.portfolio.initial_balance = account.balance
            self.portfolio.replace_positions(await self.broker.positions())
            await self._warmup()
            # Le plus haut de référence part de l'équité telle que le bot la calcule, pour
            # ne pas afficher un drawdown fictif au démarrage.
            self.portfolio.peak_equity = self.portfolio.equity()
            await self._refresh_calendar()
        except Exception as e:
            self.state = BotState.ERROR
            self.error = str(e)
            log.exception("bot %s : démarrage impossible", self.id)
            with contextlib.suppress(Exception):
                await self.broker.close()
            raise
        self.started_at = self.clock.now()
        loops: list[tuple[str, Callable[[], Awaitable[None]]]] = [
            ("market", self.market.run),
            ("check", self._check_loop),
            ("reconcile", self._reconcile_loop),
            ("account", self._account_loop),
        ]
        if self.journal is not None:
            loops.append(("journal", self.journal.run))
        if self.calendar_source is not None:
            loops.append(("calendar", self._calendar_loop))
        self._tasks = [
            asyncio.create_task(self._guard(name, fn), name=f"{self.id}-{name}")
            for name, fn in loops
        ]
        self.state = BotState.RUNNING
        self.metrics.set_up(True)
        log.info("bot %s démarré : %s", self.id, ", ".join(self.symbols))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks = []
        if self.journal is not None:
            await self.journal.aclose()
        with contextlib.suppress(Exception):
            await self.broker.close()
        self.metrics.set_up(False)
        if self.state is not BotState.ERROR:
            self.state = BotState.STOPPED
        log.info(
            "bot %s arrêté (les positions ouvertes gardent leurs stops chez le courtier)", self.id
        )

    async def _guard(self, name: str, fn: Callable[[], Awaitable[None]]) -> None:
        """Une boucle qui plante met le bot en erreur et bloque les nouvelles entrées ;
        les positions restent protégées par leurs stops chez le courtier."""
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.state = BotState.ERROR
            self.error = f"boucle {name} : {e}"
            log.exception("bot %s : boucle %s arrêtée", self.id, name)
            await self.risk.activate_kill_switch(f"panne interne ({name})", close_positions=False)

    async def _warmup(self) -> None:
        bars = []
        for st in self.strategies:
            for sym in st.instruments:
                try:
                    bars += await self.broker.history(sym, st.timeframe, st.warmup_bars + 50)
                except NotImplementedError:
                    return
        used = self.runner.warmup(bars)
        for bar in sorted(bars, key=lambda b: b.close_time):
            self.prices.update_from_bar(bar)
            if bar.spread is not None:
                self.risk.spreads.observe(bar.symbol, bar.spread)
        log.info("bot %s : %d bougies d'amorçage", self.id, used)

    async def _refresh_calendar(self) -> None:
        if self.calendar_source is None:
            return
        try:
            cal = await self.calendar_source()
        except Exception as e:
            log.warning("calendrier économique indisponible : %s", e)
            return
        self.calendar.events = cal.events

    # Boucles

    async def _check_loop(self) -> None:
        last_mark = 0.0
        loop = asyncio.get_running_loop()
        while True:
            await self.market.check()
            if loop.time() - last_mark >= self.intervals.mark:
                last_mark = loop.time()
                await self.portfolio.mark()
            await asyncio.sleep(self.intervals.check)

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(self.intervals.reconcile)
            try:
                await self.oms.reconcile()
            except Exception as e:
                log.warning("réconciliation impossible : %s", e)

    async def _account_loop(self) -> None:
        while True:
            await asyncio.sleep(self.intervals.account)
            try:
                self.portfolio.sync_balance((await self.broker.account()).balance)
            except Exception as e:
                log.warning("lecture du compte impossible : %s", e)

    async def _calendar_loop(self) -> None:
        while True:
            await asyncio.sleep(self.intervals.calendar)
            await self._refresh_calendar()

    # Commandes

    def set_strategy_enabled(self, strategy_id: str, enabled: bool) -> None:
        if strategy_id not in self.runner.enabled:
            raise KeyError(f"stratégie inconnue : {strategy_id}")
        self.runner.enabled[strategy_id] = enabled

    async def kill(self, reason: str, close_positions: bool) -> None:
        await self.risk.activate_kill_switch(reason, close_positions)

    async def release(self, reason: str) -> None:
        await self.risk.release_kill_switch(reason)

    async def close_position(self, position_id: str) -> None:
        await self.oms.close(position_id, ExitReason.MANUAL)

    # État

    def status(self) -> dict[str, Any]:
        equity = self.portfolio.equity()
        return {
            "id": self.id,
            "name": self.name,
            "state": self.state.value,
            "error": self.error,
            "mode": self.settings.mode.value,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "connected": self.market.connected,
            "kill_switch": self.risk.kill_switch,
            "balance": str(self.portfolio.balance),
            "equity": str(equity),
            "drawdown_pct": str(round(self.portfolio.drawdown_pct(equity), 2)),
            "daily_loss_pct": str(round(self.portfolio.daily_loss_pct(equity), 2)),
            "weekly_pnl_pct": str(round(self.portfolio.weekly_pnl_pct(equity), 2)),
            "weekly_loss_pct": str(round(self.portfolio.weekly_loss_pct(equity), 2)),
            "stale": sorted(self.market.stale),
            "strategies": [
                {
                    "id": s.id,
                    "kind": s.kind,
                    "enabled": self.runner.enabled[s.id],
                    "regime": {
                        sym: self.features.regime(sym, s.timeframe).value for sym in s.instruments
                    },
                }
                for s in self.strategies
            ],
            "positions": [
                {**p.model_dump(mode="json"), "unrealized": _unrealized(self, p.id)}
                for p in self.portfolio.positions()
            ],
            "quotes": {
                s: {"bid": str(t.bid), "ask": str(t.ask), "ts": t.ts.isoformat()}
                for s in self.symbols
                if (t := self.prices.get(s)) is not None
            },
            "recent": self.recent.events[-50:],
        }


def _unrealized(bot: LiveBot, position_id: str) -> str | None:
    pos = bot.portfolio.open_positions.get(position_id)
    tick = bot.prices.get(pos.symbol) if pos else None
    if pos is None or tick is None:
        return None
    try:
        rate = bot.prices.rate(pos.symbol[4:], bot.portfolio.account_currency)
    except LookupError:
        return None
    return f"{pos.unrealized_pnl(tick) * rate:.2f}"
