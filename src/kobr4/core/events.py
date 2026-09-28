"""Événements échangés sur le bus. Chaque événement est enregistré dans le journal."""

from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from kobr4.core.models import (
    Account,
    Bar,
    ClosedTrade,
    CloseIntent,
    Fill,
    OrderIntent,
    Position,
    Tick,
)
from kobr4.core.orders import Order, OrderStatus
from kobr4.core.types import UtcDatetime


class Event(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID = Field(default_factory=uuid4)
    ts: UtcDatetime


# Données de marché
class TickReceived(Event):
    tick: Tick


class BarClosed(Event):
    bar: Bar


class MarketDataStale(Event):
    symbol: str
    seconds_since_last_tick: float


class MarketDataResumed(Event):
    symbol: str


# Stratégies et risque
class SignalEmitted(Event):
    intent: OrderIntent


class CloseRequested(Event):
    intent: CloseIntent


class RiskApproved(Event):
    intent: OrderIntent
    order: Order


class RiskRejected(Event):
    intent: OrderIntent
    rule: str
    reason: str


class RiskLimitReached(Event):
    """Une limite globale est atteinte : perte journalière ou drawdown."""

    rule: str
    detail: str


class KillSwitchActivated(Event):
    reason: str
    close_positions: bool


class KillSwitchReleased(Event):
    reason: str


# Exécution
class OrderSubmitted(Event):
    order: Order


class OrderStatusChanged(Event):
    order: Order
    previous_status: OrderStatus


class FillReceived(Event):
    fill: Fill


class PositionOpened(Event):
    position: Position


class PositionModified(Event):
    position: Position


class PositionClosed(Event):
    trade: ClosedTrade


class ReconciliationMismatch(Event):
    detail: str


# Compte
class AccountUpdated(Event):
    account: Account


class EquitySnapshot(Event):
    """Valorisation du portefeuille, publiée à chaque bougie en backtest et chaque
    seconde en direct."""

    balance: Decimal
    equity: Decimal
    open_positions: int
