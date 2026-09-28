from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from kobr4.core.models import Fill
from kobr4.core.orders import TRANSITIONS, InvalidTransitionError, Order, OrderStatus
from kobr4.core.types import OrderType, Side


def make_order(t0: datetime, **kw: Any) -> Order:
    base: dict[str, Any] = {
        "id": "o-1",
        "strategy_id": "ema-cross-h1",
        "symbol": "EUR/USD",
        "side": Side.BUY,
        "order_type": OrderType.MARKET,
        "quantity": 40_000,
        "stop_loss_distance": Decimal("0.0025"),
        "take_profit_distance": Decimal("0.0050"),
        "created_at": t0,
        "updated_at": t0,
    }
    return Order(**{**base, **kw})


def fill(t0: datetime, qty: int, price: str, order_id: str = "o-1") -> Fill:
    return Fill(
        fill_id=f"f-{qty}-{price}",
        order_id=order_id,
        symbol="EUR/USD",
        side=Side.BUY,
        quantity=qty,
        price=Decimal(price),
        ts=t0 + timedelta(seconds=1),
    )


def test_happy_path(t0: datetime) -> None:
    o = make_order(t0)
    o = o.transition(OrderStatus.SUBMITTED, t0)
    o = o.transition(OrderStatus.ACCEPTED, t0, broker_order_id="B42")
    o = o.apply_fill(fill(t0, 40_000, "1.08195"))
    assert o.status is OrderStatus.FILLED
    assert o.broker_order_id == "B42"
    assert o.avg_fill_price == Decimal("1.08195")
    assert o.remaining_quantity == 0
    assert o.status.is_terminal


def test_partial_fills_average_price(t0: datetime) -> None:
    o = make_order(t0).transition(OrderStatus.SUBMITTED, t0)
    o = o.apply_fill(fill(t0, 10_000, "1.08200"))
    assert o.status is OrderStatus.PARTIALLY_FILLED
    o = o.apply_fill(fill(t0, 30_000, "1.08240"))
    assert o.status is OrderStatus.FILLED
    assert o.avg_fill_price == Decimal("1.08230")


def test_overfill_rejected(t0: datetime) -> None:
    o = make_order(t0).transition(OrderStatus.SUBMITTED, t0)
    with pytest.raises(ValueError, match="restantes"):
        o.apply_fill(fill(t0, 50_000, "1.08200"))


def test_fill_for_other_order_rejected(t0: datetime) -> None:
    o = make_order(t0).transition(OrderStatus.SUBMITTED, t0)
    with pytest.raises(ValueError, match="ne concerne pas"):
        o.apply_fill(fill(t0, 1_000, "1.08200", order_id="o-2"))


def test_unknown_is_resolved_by_reconciliation(t0: datetime) -> None:
    o = make_order(t0).transition(OrderStatus.SUBMITTED, t0)
    o = o.transition(OrderStatus.UNKNOWN, t0)
    # Jamais de renvoi à l'aveugle : pas de retour vers SUBMITTED.
    with pytest.raises(InvalidTransitionError):
        o.transition(OrderStatus.SUBMITTED, t0)
    assert o.transition(OrderStatus.REJECTED, t0).status is OrderStatus.REJECTED


def test_transition_does_not_mutate(t0: datetime) -> None:
    o = make_order(t0)
    o.transition(OrderStatus.SUBMITTED, t0 + timedelta(seconds=1))
    assert o.status is OrderStatus.CREATED
    assert o.updated_at == t0


@given(
    current=st.sampled_from(list(OrderStatus)),
    target=st.sampled_from(list(OrderStatus)),
)
def test_only_declared_transitions_allowed(current: OrderStatus, target: OrderStatus) -> None:
    from datetime import UTC

    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    filled = 40_000 if current is OrderStatus.FILLED else 0
    o = make_order(t0, status=current, filled_quantity=filled)
    if target in TRANSITIONS[current]:
        assert o.transition(target, t0).status is target
    else:
        with pytest.raises(InvalidTransitionError):
            o.transition(target, t0)


def test_terminal_states() -> None:
    terminal = {s for s in OrderStatus if s.is_terminal}
    assert terminal == {
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
    }


@pytest.mark.parametrize(
    ("side", "sl", "tp"),
    [(Side.BUY, "1.07750", "1.08500"), (Side.SELL, "1.08250", "1.07500")],
)
def test_protection_prices(t0: datetime, side: Side, sl: str, tp: str) -> None:
    o = make_order(t0, side=side)
    assert o.protection_prices(Decimal("1.08000")) == (Decimal(sl), Decimal(tp))
    no_tp = make_order(t0, side=side, take_profit_distance=None)
    assert no_tp.protection_prices(Decimal("1.08000"))[1] is None


def test_pending_order_requires_price(t0: datetime) -> None:
    with pytest.raises(ValidationError, match="exige un prix"):
        make_order(t0, order_type=OrderType.STOP)
