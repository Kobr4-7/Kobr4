"""Gestionnaire de risque : seul chemin entre une intention de stratégie et le marché.

Chaque intention est soit transformée en ordre dimensionné (`RiskApproved`), soit refusée
avec la règle et le motif (`RiskRejected`). Les fermetures ne passent pas par ici : elles
ne sont jamais bloquées.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from kobr4.config.settings import RiskSettings
from kobr4.core.bus import EventBus
from kobr4.core.clock import Clock
from kobr4.core.events import (
    BarClosed,
    EquitySnapshot,
    KillSwitchActivated,
    KillSwitchReleased,
    MarketDataResumed,
    MarketDataStale,
    RiskApproved,
    RiskLimitReached,
    RiskRejected,
    SignalEmitted,
    TickReceived,
)
from kobr4.core.instruments import get_instrument
from kobr4.core.models import OrderIntent
from kobr4.core.orders import Order
from kobr4.core.prices import MissingPriceError, PriceBook
from kobr4.intel.allocation import risk_multiplier
from kobr4.portfolio import Portfolio
from kobr4.risk.calendar import EconomicCalendar
from kobr4.risk.spreads import SpreadMonitor

log = logging.getLogger(__name__)

IdFactory = Callable[[OrderIntent], str]
SignalFilter = Callable[[OrderIntent], "Rejection | None"]


@dataclass(frozen=True)
class Rejection:
    rule: str
    reason: str


class RiskManager:
    def __init__(
        self,
        settings: RiskSettings,
        bus: EventBus,
        portfolio: Portfolio,
        prices: PriceBook,
        clock: Clock,
        id_factory: IdFactory,
        calendar: EconomicCalendar | None = None,
        spreads: SpreadMonitor | None = None,
        signal_filters: list[SignalFilter] | None = None,
    ) -> None:
        self.settings = settings
        self.bus = bus
        self.portfolio = portfolio
        self.prices = prices
        self.clock = clock
        self.id_factory = id_factory
        self.calendar = calendar or EconomicCalendar()
        self.spreads = spreads or SpreadMonitor()
        self.signal_filters = signal_filters or []
        self.kill_switch: str | None = None
        self.halted_until_day: object | None = None
        self.stale: set[str] = set()
        self.rejections: dict[str, int] = {}

        bus.subscribe(SignalEmitted, self._on_signal)
        bus.subscribe(EquitySnapshot, self._on_equity)
        bus.subscribe(TickReceived, lambda e: self.spreads.observe(e.tick.symbol, e.tick.spread))
        bus.subscribe(BarClosed, self._on_bar)
        bus.subscribe(MarketDataStale, lambda e: self.stale.add(e.symbol))
        bus.subscribe(MarketDataResumed, lambda e: self.stale.discard(e.symbol))

    def _on_bar(self, e: BarClosed) -> None:
        if e.bar.spread is not None:
            self.spreads.observe(e.bar.symbol, e.bar.spread)

    # Arrêt d'urgence

    async def activate_kill_switch(self, reason: str, close_positions: bool) -> None:
        self.kill_switch = reason
        log.warning("arrêt d'urgence : %s", reason)
        await self.bus.publish(
            KillSwitchActivated(ts=self.clock.now(), reason=reason, close_positions=close_positions)
        )

    async def release_kill_switch(self, reason: str) -> None:
        self.kill_switch = None
        await self.bus.publish(KillSwitchReleased(ts=self.clock.now(), reason=reason))

    # Limites globales

    async def _on_equity(self, e: EquitySnapshot) -> None:
        s = self.settings
        if self.kill_switch is None:
            dd = self.portfolio.drawdown_pct(e.equity)
            if dd >= s.max_drawdown_pct:
                detail = f"drawdown {dd:.2f} % ≥ {s.max_drawdown_pct} %"
                await self.bus.publish(
                    RiskLimitReached(ts=e.ts, rule="max_drawdown", detail=detail)
                )
                await self.activate_kill_switch(
                    f"{detail} : intervention manuelle requise", close_positions=False
                )
                return
        if self.halted_until_day != e.ts.date():
            loss = self.portfolio.daily_loss_pct(e.equity)
            if loss >= s.max_daily_loss_pct:
                self.halted_until_day = e.ts.date()
                await self.bus.publish(
                    RiskLimitReached(
                        ts=e.ts,
                        rule="max_daily_loss",
                        detail=f"perte du jour {loss:.2f} % ≥ {s.max_daily_loss_pct} %,"
                        " entrées suspendues jusqu'à demain 0h UTC",
                    )
                )

    # Intentions

    async def _on_signal(self, e: SignalEmitted) -> None:
        result = self.evaluate(e.intent)
        now = self.clock.now()
        if isinstance(result, Rejection):
            self.rejections[result.rule] = self.rejections.get(result.rule, 0) + 1
            await self.bus.publish(
                RiskRejected(ts=now, intent=e.intent, rule=result.rule, reason=result.reason)
            )
        else:
            await self.bus.publish(RiskApproved(ts=now, intent=e.intent, order=result))

    def evaluate(self, intent: OrderIntent) -> Order | Rejection:
        s = self.settings
        now = self.clock.now()
        if self.kill_switch is not None:
            return Rejection("kill_switch", f"arrêt d'urgence actif ({self.kill_switch})")
        if self.halted_until_day == now.date():
            return Rejection("max_daily_loss", "perte journalière maximale atteinte")
        if intent.symbol in self.stale:
            return Rejection("stale_data", "cotations interrompues sur cet instrument")

        for flt in self.signal_filters:
            refusal = flt(intent)
            if refusal is not None:
                return refusal

        news = self.calendar.blackout(intent.symbol, now, s.news_blackout_minutes)
        if news is not None:
            return Rejection(
                "news_blackout", f"annonce {news.currency} à {news.ts:%H:%M} UTC : {news.title}"
            )

        try:
            tick = self.prices.tick(intent.symbol)
        except MissingPriceError:
            return Rejection("no_price", f"aucun prix connu pour {intent.symbol}")
        typical = self.spreads.typical(intent.symbol)
        if typical is not None and typical > 0 and tick.spread > typical * s.max_spread_multiplier:
            return Rejection(
                "spread", f"spread {tick.spread} > {s.max_spread_multiplier} × habituel ({typical})"
            )

        open_positions = self.portfolio.positions()
        if len(open_positions) >= s.max_open_positions:
            return Rejection("max_open_positions", f"{len(open_positions)} positions ouvertes")
        same = [
            p
            for p in open_positions
            if p.strategy_id == intent.strategy_id and p.symbol == intent.symbol
        ]
        if len(same) >= s.max_positions_per_symbol_strategy:
            return Rejection(
                "max_positions_per_symbol_strategy",
                f"déjà {len(same)} position(s) {intent.symbol} pour {intent.strategy_id}",
            )

        instrument = get_instrument(intent.symbol)
        ccy = self.portfolio.account_currency
        try:
            pip_value = self.prices.pip_value_per_unit(instrument, ccy)
            base_rate = self.prices.rate(instrument.base, ccy)
        except MissingPriceError as exc:
            return Rejection("no_price", str(exc))

        equity = self.portfolio.equity()
        mult = risk_multiplier(s.allocation, intent.strategy_id, self.portfolio.trades)
        risk_budget = equity * s.risk_per_trade_pct / 100 * mult
        raw_units = risk_budget / (intent.stop_loss_pips * pip_value)
        units = int((raw_units / s.min_units).to_integral_value(ROUND_FLOOR)) * s.min_units
        if units < s.min_units:
            return Rejection(
                "size_too_small",
                f"risque {risk_budget:.2f} {ccy} insuffisant pour {s.min_units} unités"
                f" avec un stop de {intent.stop_loss_pips} pips",
            )
        new_risk = intent.stop_loss_pips * pip_value * units

        limit = equity * s.max_currency_risk_pct / 100
        for currency in (instrument.base, instrument.quote):
            existing = sum(
                (
                    self.portfolio.position_risk(p)
                    for p in open_positions
                    if currency in (p.symbol[:3], p.symbol[4:])
                ),
                Decimal(0),
            )
            if existing + new_risk > limit:
                return Rejection(
                    "currency_exposure",
                    f"risque cumulé sur {currency} {existing + new_risk:.2f} > {limit:.2f} {ccy}",
                )

        margin_used = sum(
            (
                p.quantity * self.prices.rate(get_instrument(p.symbol).base, ccy) / s.leverage
                for p in open_positions
            ),
            Decimal(0),
        )
        margin = units * base_rate / s.leverage
        if margin_used + margin > equity:
            return Rejection(
                "margin", f"marge requise {margin:.2f}, disponible {equity - margin_used:.2f}"
            )

        return Order(
            id=self.id_factory(intent),
            strategy_id=intent.strategy_id,
            symbol=intent.symbol,
            side=intent.side,
            order_type=intent.order_type,
            quantity=units,
            price=intent.entry_price,
            stop_loss_distance=instrument.from_pips(intent.stop_loss_pips),
            take_profit_distance=None
            if intent.take_profit_pips is None
            else instrument.from_pips(intent.take_profit_pips),
            created_at=now,
            updated_at=now,
        )
