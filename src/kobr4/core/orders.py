"""Ordres et leur cycle de vie (voir docs/ARCHITECTURE.md §5.2)."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kobr4.core.models import Fill, Price
from kobr4.core.types import OrderType, Side, UtcDatetime


class OrderStatus(StrEnum):
    CREATED = "created"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    UNKNOWN = "unknown"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        return not TRANSITIONS[self]


S = OrderStatus
TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    S.CREATED: frozenset({S.SUBMITTED}),
    # Un ordre au marché peut être exécuté dès la réponse à l'envoi.
    S.SUBMITTED: frozenset({S.ACCEPTED, S.REJECTED, S.UNKNOWN, S.PARTIALLY_FILLED, S.FILLED}),
    # Après un délai dépassé, seule la réconciliation avec le courtier tranche.
    S.UNKNOWN: frozenset({S.ACCEPTED, S.REJECTED, S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED}),
    S.ACCEPTED: frozenset({S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED, S.EXPIRED}),
    S.PARTIALLY_FILLED: frozenset({S.PARTIALLY_FILLED, S.FILLED, S.CANCELLED}),
    S.FILLED: frozenset(),
    S.REJECTED: frozenset(),
    S.CANCELLED: frozenset(),
    S.EXPIRED: frozenset(),
}
del S


class InvalidTransitionError(ValueError):
    def __init__(self, order_id: str, current: OrderStatus, target: OrderStatus) -> None:
        super().__init__(f"ordre {order_id} : transition interdite {current} → {target}")
        self.current = current
        self.target = target


class Order(BaseModel):
    """Ordre dimensionné par le risque, prêt à être envoyé au courtier.

    `id` est l'identifiant client : il permet de retrouver l'ordre chez le courtier
    sans jamais le renvoyer deux fois.

    Le stop loss et le take profit sont exprimés en distance au prix d'exécution : le
    courtier les pose au moment de l'exécution (`stopLossOnFill` chez OANDA), ce qui évite
    un stop mal placé si le prix a bougé entre la décision et l'exécution.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    strategy_id: str
    symbol: str
    side: Side
    order_type: OrderType
    quantity: int = Field(gt=0)
    price: Price | None = Field(default=None, gt=0)
    stop_loss_distance: Price = Field(gt=0)
    take_profit_distance: Price | None = Field(default=None, gt=0)
    status: OrderStatus = OrderStatus.CREATED
    broker_order_id: str | None = None
    filled_quantity: int = Field(default=0, ge=0)
    avg_fill_price: Price | None = None
    reject_reason: str | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.filled_quantity > self.quantity:
            raise ValueError("quantité exécutée supérieure à la quantité de l'ordre")
        if self.order_type is not OrderType.MARKET and self.price is None:
            raise ValueError(f"un ordre {self.order_type} exige un prix")
        return self

    @property
    def remaining_quantity(self) -> int:
        return self.quantity - self.filled_quantity

    def protection_prices(self, fill_price: Price) -> tuple[Price, Price | None]:
        """Prix du stop loss et du take profit pour une exécution à `fill_price`."""
        sl = fill_price - self.side.sign * self.stop_loss_distance
        tp = (
            None
            if self.take_profit_distance is None
            else fill_price + self.side.sign * self.take_profit_distance
        )
        return sl, tp

    def transition(self, target: OrderStatus, ts: datetime, **changes: object) -> "Order":
        """Renvoie une copie de l'ordre dans le nouvel état, ou lève InvalidTransitionError."""
        if target not in TRANSITIONS[self.status]:
            raise InvalidTransitionError(self.id, self.status, target)
        return self.model_validate(
            {**self.model_dump(), **changes, "status": target, "updated_at": ts}
        )

    def apply_fill(self, fill: Fill) -> "Order":
        """Intègre une exécution : met à jour la quantité, le prix moyen et l'état."""
        if fill.order_id != self.id:
            raise ValueError(f"l'exécution {fill.fill_id} ne concerne pas l'ordre {self.id}")
        if fill.quantity > self.remaining_quantity:
            raise ValueError(
                f"exécution de {fill.quantity} unités pour {self.remaining_quantity} restantes"
            )
        filled = self.filled_quantity + fill.quantity
        prev_notional = (self.avg_fill_price or Decimal(0)) * self.filled_quantity
        avg = (prev_notional + fill.price * fill.quantity) / filled
        target = OrderStatus.FILLED if filled == self.quantity else OrderStatus.PARTIALLY_FILLED
        return self.transition(target, fill.ts, filled_quantity=filled, avg_fill_price=avg)
