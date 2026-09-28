"""Assemblage des composants réels (courtier simulé) pour les tests."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from kobr4.config.settings import RiskSettings
from kobr4.core.bus import InMemoryEventBus
from kobr4.core.clock import SimulatedClock
from kobr4.core.events import Event
from kobr4.core.models import OrderIntent, Tick
from kobr4.core.prices import PriceBook
from kobr4.core.types import Side
from kobr4.execution.brokers.simulated import SimulatedBroker
from kobr4.execution.oms import OrderManager
from kobr4.portfolio import Portfolio
from kobr4.risk.calendar import EconomicCalendar
from kobr4.risk.manager import RiskManager


@dataclass
class Stack:
    bus: InMemoryEventBus
    clock: SimulatedClock
    prices: PriceBook
    broker: SimulatedBroker
    portfolio: Portfolio
    oms: OrderManager
    risk: RiskManager
    events: list[Event] = field(default_factory=list)

    def quote(self, symbol: str, bid: str, spread: str = "0.00010") -> Tick:
        tick = Tick(
            symbol=symbol,
            bid=Decimal(bid),
            ask=Decimal(bid) + Decimal(spread),
            ts=self.clock.now(),
        )
        self.prices.update(tick)
        return tick

    def of(self, kind: type[Event]) -> list[Any]:
        return [e for e in self.events if isinstance(e, kind)]


def make_stack(
    t0: datetime,
    risk: RiskSettings | None = None,
    balance: str = "10000",
    calendar: EconomicCalendar | None = None,
    **broker_kw: Any,
) -> Stack:
    bus = InMemoryEventBus()
    clock = SimulatedClock(t0)
    prices = PriceBook()
    broker = SimulatedBroker(prices, clock, initial_balance=Decimal(balance), **broker_kw)
    portfolio = Portfolio(bus, prices, clock, "USD", Decimal(balance))
    n = 0

    def ids(intent: OrderIntent) -> str:
        nonlocal n
        n += 1
        return f"o{n}"

    rm = RiskManager(risk or RiskSettings(), bus, portfolio, prices, clock, ids, calendar)
    oms = OrderManager(bus, broker, portfolio, clock)
    stack = Stack(bus, clock, prices, broker, portfolio, oms, rm)
    bus.subscribe(Event, stack.events.append)
    return stack


def intent(
    t0: datetime,
    symbol: str = "EUR/USD",
    side: Side = Side.BUY,
    sl: str = "25",
    tp: str | None = "50",
    strategy: str = "s1",
) -> OrderIntent:
    return OrderIntent(
        strategy_id=strategy,
        symbol=symbol,
        side=side,
        stop_loss_pips=Decimal(sl),
        take_profit_pips=None if tp is None else Decimal(tp),
        ts=t0,
    )
