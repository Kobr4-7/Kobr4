import asyncio
from datetime import datetime
from decimal import Decimal

from kobr4.core.events import (
    CloseRequested,
    OrderStatusChanged,
    PositionClosed,
    ReconciliationMismatch,
    SignalEmitted,
)
from kobr4.core.models import CloseIntent, ExitReason, Position
from kobr4.core.orders import Order, OrderStatus
from kobr4.core.types import Side
from kobr4.execution.broker import BrokerError
from tests.execution.test_simulated_broker import order
from tests.stack import intent, make_stack


async def test_close_then_reopen_in_same_step(t0: datetime) -> None:
    """Un retournement (fermer la vente, ouvrir l'achat) doit passer malgré la limite d'une
    position par paire et par stratégie : la fermeture est prise en compte immédiatement."""
    st = make_stack(t0)
    st.quote("EUR/USD", "1.08")
    await st.bus.publish(SignalEmitted(ts=t0, intent=intent(t0, side=Side.SELL)))
    await st.bus.publish(
        CloseRequested(
            ts=t0, intent=CloseIntent(strategy_id="s1", symbol="EUR/USD", side=Side.SELL, ts=t0)
        )
    )
    await st.bus.publish(SignalEmitted(ts=t0, intent=intent(t0, side=Side.BUY)))
    (pos,) = st.portfolio.positions()
    assert pos.side is Side.BUY
    (closed,) = st.of(PositionClosed)
    assert closed.trade.reason is ExitReason.SIGNAL


async def test_close_request_filters_side(t0: datetime) -> None:
    st = make_stack(t0)
    st.quote("EUR/USD", "1.08")
    await st.bus.publish(SignalEmitted(ts=t0, intent=intent(t0, side=Side.BUY)))
    await st.bus.publish(
        CloseRequested(
            ts=t0, intent=CloseIntent(strategy_id="s1", symbol="EUR/USD", side=Side.SELL, ts=t0)
        )
    )
    assert len(st.portfolio.positions()) == 1
    await st.bus.publish(
        CloseRequested(ts=t0, intent=CloseIntent(strategy_id="s1", symbol="EUR/USD", ts=t0))
    )
    assert st.portfolio.positions() == []


async def test_order_status_events(t0: datetime) -> None:
    st = make_stack(t0)
    st.quote("EUR/USD", "1.08")
    await st.oms.submit(order(t0))
    (ev,) = st.of(OrderStatusChanged)
    assert ev.order.status is OrderStatus.FILLED
    assert ev.previous_status is OrderStatus.CREATED


async def test_broker_error_becomes_rejection(t0: datetime) -> None:
    st = make_stack(t0)

    async def boom(o: Order) -> Order:
        raise BrokerError("marché fermé")

    st.broker.submit = boom  # type: ignore[method-assign, assignment]
    result = await st.oms.submit(order(t0))
    assert result.status is OrderStatus.REJECTED
    assert result.reject_reason == "marché fermé"


async def test_timeout_then_reconciliation(t0: datetime) -> None:
    st = make_stack(t0)
    st.quote("EUR/USD", "1.08")
    st.oms.submit_timeout = 0.01
    real_submit = st.broker.submit

    async def slow(o: Order) -> Order:
        filled = await real_submit(o)  # le courtier exécute…
        await asyncio.sleep(1)  # … mais la réponse se perd
        return filled

    st.broker.submit = slow  # type: ignore[method-assign, assignment]
    result = await st.oms.submit(order(t0))
    assert result.status is OrderStatus.UNKNOWN
    issues = await st.oms.reconcile()
    assert st.oms.orders["o1"].status is OrderStatus.FILLED
    assert any("inconnu" in i for i in issues)


async def test_reconciliation_detects_and_adopts_broker_view(t0: datetime) -> None:
    st = make_stack(t0)
    st.quote("EUR/USD", "1.08")
    await st.oms.submit(order(t0))
    assert await st.oms.reconcile() == []
    ghost = Position(
        id="ghost",
        strategy_id="s1",
        symbol="EUR/USD",
        side=Side.BUY,
        quantity=1000,
        entry_price=Decimal("1.08"),
        stop_loss=Decimal("1.07"),
        opened_at=t0,
    )
    st.portfolio.apply_opened(ghost)
    issues = await st.oms.reconcile()
    assert issues == ["position ghost fermée chez le courtier à notre insu"]
    assert [p.id for p in st.portfolio.positions()] == ["o1"]
    assert len(st.of(ReconciliationMismatch)) == 1


async def test_close_unknown_position_is_logged(t0: datetime) -> None:
    st = make_stack(t0)
    await st.oms.close("nope", ExitReason.MANUAL)
    assert st.of(PositionClosed) == []
