from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from kobr4.core.events import PositionClosed, PositionOpened
from kobr4.core.models import Bar, ExitReason
from kobr4.core.orders import Order, OrderStatus
from kobr4.core.types import OrderType, Side, Timeframe
from tests.stack import Stack, make_stack


def order(
    t0: datetime, side: Side = Side.BUY, qty: int = 10_000, oid: str = "o1", **kw: object
) -> Order:
    base: dict[str, object] = {
        "id": oid,
        "strategy_id": "s1",
        "symbol": "EUR/USD",
        "side": side,
        "order_type": OrderType.MARKET,
        "quantity": qty,
        "stop_loss_distance": Decimal("0.0025"),
        "take_profit_distance": Decimal("0.0050"),
        "created_at": t0,
        "updated_at": t0,
    }
    return Order.model_validate({**base, **kw})


def bar(t0: datetime, hours: int, o: str, h: str, low: str, c: str, spread: str = "0.0001") -> Bar:
    return Bar(
        symbol="EUR/USD",
        timeframe=Timeframe.H1,
        open_time=t0 + timedelta(hours=hours),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        spread=Decimal(spread),
    )


async def opened(st: Stack, t0: datetime, side: Side = Side.BUY, qty: int = 10_000) -> Order:
    st.quote("EUR/USD", "1.08000")
    return await st.oms.submit(order(t0, side, qty=qty))


async def test_market_buy_fills_at_ask_with_slippage(t0: datetime) -> None:
    st = make_stack(t0, slippage_pips=Decimal("0.5"))
    o = await opened(st, t0)
    assert o.status is OrderStatus.FILLED
    pos = st.portfolio.positions()[0]
    assert pos.entry_price == Decimal("1.08015")  # ask 1.08010 + 0,5 pip
    assert pos.stop_loss == Decimal("1.07765")
    assert pos.take_profit == Decimal("1.08515")
    assert len(st.of(PositionOpened)) == 1


async def test_sell_fills_at_bid(t0: datetime) -> None:
    st = make_stack(t0)
    await opened(st, t0, Side.SELL)
    pos = st.portfolio.positions()[0]
    assert pos.entry_price == Decimal("1.08000")
    assert pos.stop_loss == Decimal("1.08250")


async def test_non_market_rejected(t0: datetime) -> None:
    st = make_stack(t0)
    st.quote("EUR/USD", "1.08000")
    o = await st.oms.submit(order(t0, order_type=OrderType.LIMIT, price=Decimal("1.07")))
    assert o.status is OrderStatus.REJECTED
    assert st.portfolio.positions() == []


async def test_duplicate_order_id_rejected(t0: datetime) -> None:
    st = make_stack(t0)
    await opened(st, t0)
    o = await st.oms.submit(order(t0))
    assert o.status is OrderStatus.REJECTED
    assert "déjà envoyé" in (o.reject_reason or "")


@pytest.mark.parametrize(
    ("side", "bar_args", "reason", "exit_price"),
    [
        (Side.BUY, ("1.0801", "1.0805", "1.0770", "1.0790"), ExitReason.STOP_LOSS, "1.07760"),
        (Side.BUY, ("1.0801", "1.0860", "1.0795", "1.0850"), ExitReason.TAKE_PROFIT, "1.08510"),
        # stop et objectif dans la même bougie : le stop l'emporte
        (Side.BUY, ("1.0801", "1.0860", "1.0770", "1.0850"), ExitReason.STOP_LOSS, "1.07760"),
        # gap sous le stop : sortie à l'ouverture
        (Side.BUY, ("1.0750", "1.0760", "1.0740", "1.0755"), ExitReason.STOP_LOSS, "1.0750"),
        # vente : contrôlée sur l'ask (bid + spread)
        (Side.SELL, ("1.0800", "1.0824", "1.0790", "1.0800"), ExitReason.STOP_LOSS, "1.08250"),
        (Side.SELL, ("1.0800", "1.0823", "1.0790", "1.0800"), None, None),
        (Side.SELL, ("1.0800", "1.0805", "1.0749", "1.0760"), ExitReason.TAKE_PROFIT, "1.07500"),
    ],
)
async def test_stops_and_targets(
    t0: datetime,
    side: Side,
    bar_args: tuple[str, str, str, str],
    reason: ExitReason | None,
    exit_price: str | None,
) -> None:
    st = make_stack(t0)
    await opened(st, t0, side)
    await st.broker.on_bar(bar(t0, 0, *bar_args))  # ouverte à t0 : la bougie de t0 compte
    closed = st.of(PositionClosed)
    if reason is None:
        assert closed == []
        return
    (e,) = closed
    assert e.trade.reason is reason
    assert e.trade.exit_price == Decimal(exit_price or "0")
    assert st.portfolio.positions() == []


async def test_bar_before_opening_ignored(t0: datetime) -> None:
    st = make_stack(t0 + timedelta(hours=2))
    await opened(st, t0 + timedelta(hours=2))
    await st.broker.on_bar(bar(t0, 0, "1.08", "1.09", "1.07", "1.08"))
    assert st.of(PositionClosed) == []


async def test_pnl_commission_and_balance(t0: datetime) -> None:
    st = make_stack(t0, commission_per_million=Decimal(30))
    await opened(st, t0)  # achat 10 000 à 1.08010
    st.quote("EUR/USD", "1.08110")
    await st.oms.close("o1", ExitReason.MANUAL)
    (e,) = st.of(PositionClosed)
    t = e.trade
    # +10 pips × 10 000 = 10 $, commission 2 × 10 000 × 1.0816 × 30 / 1e6
    assert t.exit_price == Decimal("1.08110")
    assert t.commission == pytest.approx(Decimal("0.64896"), abs=Decimal("0.001"))
    assert t.pnl == pytest.approx(Decimal("10") - t.commission, abs=Decimal("0.0001"))
    assert st.portfolio.balance == st.broker.balance == Decimal(10000) + t.pnl


async def test_account_equity_and_margin(t0: datetime) -> None:
    st = make_stack(t0)
    await opened(st, t0, qty=30_000)
    st.quote("EUR/USD", "1.08110")
    acc = await st.broker.account()
    assert acc.equity == Decimal(10000) + Decimal("30.00")
    assert acc.margin_used == pytest.approx(Decimal(30_000) * Decimal("1.08115") / 30)
    assert st.portfolio.equity() == acc.equity


async def test_modify_position(t0: datetime) -> None:
    st = make_stack(t0)
    await opened(st, t0)
    await st.oms.modify("o1", Decimal("1.0790"), None)
    assert st.portfolio.positions()[0].stop_loss == Decimal("1.0790")
    assert st.portfolio.positions()[0].take_profit is None
