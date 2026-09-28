"""Événements échangés sur le bus. Chaque événement est enregistré dans le journal."""

from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from kobr4.core.models import Account, Bar, Fill, OrderIntent, Tick
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


# Stratégies et risque
class SignalEmitted(Event):
    intent: OrderIntent


class RiskRejected(Event):
    intent: OrderIntent
    rule: str
    reason: str


class KillSwitchActivated(Event):
    reason: str
    close_positions: bool


# Exécution
class OrderSubmitted(Event):
    order: Order


class OrderStatusChanged(Event):
    order: Order
    previous_status: OrderStatus


class FillReceived(Event):
    fill: Fill


class ReconciliationMismatch(Event):
    detail: str


# Compte
class AccountUpdated(Event):
    account: Account
