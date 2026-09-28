import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from kobr4.core.clock import SimulatedClock
from kobr4.core.events import PositionClosed, PositionOpened
from kobr4.core.models import ExitReason
from kobr4.core.orders import OrderStatus
from kobr4.core.types import Side, Timeframe
from kobr4.execution.brokers.oanda import OandaBroker, OandaError, parse_time
from kobr4.execution.oms import OrderManager
from tests.execution.fake_oanda import FakeOanda
from tests.execution.test_simulated_broker import order
from tests.stack import Stack, make_stack

T0 = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)


@pytest.fixture
async def env() -> AsyncIterator[tuple[FakeOanda, OandaBroker, Stack]]:
    fake = FakeOanda()
    st = make_stack(T0)
    broker = OandaBroker(
        "tok", fake.account_id, "practice", SimulatedClock(T0), transport=fake.transport()
    )
    st.oms = OrderManager(st.bus, broker, st.portfolio, st.clock)
    await broker.connect()
    yield fake, broker, st
    await fake.tx_queue.put("__END__")
    await broker.close()


def test_parse_time_nanoseconds() -> None:
    assert parse_time("2026-09-28T08:00:00.123456789Z") == datetime(
        2026, 9, 28, 8, 0, 0, 123456, tzinfo=UTC
    )
    assert parse_time("2026-09-28T08:00:00Z") == datetime(2026, 9, 28, 8, 0, tzinfo=UTC)


async def test_bad_token() -> None:
    fake = FakeOanda()
    broker = OandaBroker(
        "wrong", fake.account_id, "practice", SimulatedClock(T0), transport=fake.transport()
    )
    with pytest.raises(OandaError, match="jeton API refusé"):
        await broker.connect()
    await broker.close()


async def test_market_order_sets_stop_by_distance_and_exact_target(
    env: tuple[FakeOanda, OandaBroker, Stack],
) -> None:
    fake, broker, st = env
    result = await st.oms.submit(order(T0, qty=10_000))
    assert result.status is OrderStatus.FILLED
    assert result.avg_fill_price == Decimal("1.08010")
    method, _, body = next(r for r in fake.requests if r[0] == "POST")
    assert body["order"]["units"] == "10000"
    assert body["order"]["stopLossOnFill"]["distance"] == "0.00250"
    assert body["order"]["clientExtensions"] == {"id": "o1", "tag": "s1"}
    (pos,) = st.portfolio.positions()
    assert pos.id == "o1"
    assert pos.stop_loss == Decimal("1.07760")
    assert pos.take_profit == Decimal("1.08510")  # 1.08010 + 0.0050, posé après l'exécution
    assert len(st.of(PositionOpened)) == 1


async def test_sell_units_negative(env: tuple[FakeOanda, OandaBroker, Stack]) -> None:
    fake, broker, st = env
    await st.oms.submit(order(T0, side=Side.SELL, qty=5_000))
    body = next(r[2] for r in fake.requests if r[0] == "POST")
    assert body["order"]["units"] == "-5000"
    assert st.portfolio.positions()[0].side is Side.SELL


async def test_broker_rejection(env: tuple[FakeOanda, OandaBroker, Stack]) -> None:
    fake, broker, st = env
    fake.reject_next = "INSUFFICIENT_MARGIN"
    result = await st.oms.submit(order(T0))
    assert result.status is OrderStatus.REJECTED
    assert result.reject_reason == "INSUFFICIENT_MARGIN"
    assert st.portfolio.positions() == []


async def test_close_by_client_id(env: tuple[FakeOanda, OandaBroker, Stack]) -> None:
    fake, broker, st = env
    await st.oms.submit(order(T0))
    fake.price["EUR_USD"] = (Decimal("1.08110"), Decimal("1.08120"))
    await st.oms.close("o1", ExitReason.SIGNAL)
    (e,) = st.of(PositionClosed)
    assert e.trade.reason is ExitReason.SIGNAL
    assert e.trade.exit_price == Decimal("1.08110")
    assert e.trade.financing == Decimal("-0.12")
    assert e.trade.pnl == Decimal("10.0000") - Decimal("0.12")
    assert st.portfolio.positions() == []


async def test_stop_from_transaction_stream_counted_once(
    env: tuple[FakeOanda, OandaBroker, Stack],
) -> None:
    fake, broker, st = env
    await st.oms.submit(order(T0))
    trade_id = next(iter(fake.trades))
    await fake.trigger_stop(trade_id)
    for _ in range(50):
        if st.of(PositionClosed):
            break
        await asyncio.sleep(0.01)
    (e,) = st.of(PositionClosed)
    assert e.trade.reason is ExitReason.STOP_LOSS
    assert e.trade.exit_price == Decimal("1.07760")
    # Le même événement rejoué (reconnexion du flux) n'est pas compté deux fois.
    await broker._handle_fill(
        json.loads(
            json.dumps(
                {
                    "type": "ORDER_FILL",
                    "time": "2026-09-28T08:00:00Z",
                    "reason": "STOP_LOSS_ORDER",
                    "tradesClosed": [
                        {
                            "tradeID": trade_id,
                            "units": "-10000",
                            "price": "1.07760",
                            "realizedPL": "-25",
                        }
                    ],
                }
            )
        )
    )
    assert len(st.of(PositionClosed)) == 1


async def test_modify_and_clear_target(env: tuple[FakeOanda, OandaBroker, Stack]) -> None:
    fake, broker, st = env
    await st.oms.submit(order(T0))
    await st.oms.modify("o1", Decimal("1.07900"), None)
    (pos,) = st.portfolio.positions()
    assert pos.stop_loss == Decimal("1.07900")
    assert pos.take_profit is None


async def test_account_and_positions_after_restart(
    env: tuple[FakeOanda, OandaBroker, Stack],
) -> None:
    fake, broker, st = env
    await st.oms.submit(order(T0))
    fresh = OandaBroker(
        "tok", fake.account_id, "practice", SimulatedClock(T0), transport=fake.transport()
    )
    (pos,) = await fresh.positions()
    assert pos.id == "o1"
    assert pos.strategy_id == "s1"
    acc = await fresh.account()
    assert acc.balance == Decimal("10000")
    assert acc.equity == Decimal("10012.5")
    await fresh.close()


async def test_find_order(env: tuple[FakeOanda, OandaBroker, Stack]) -> None:
    fake, broker, st = env
    await st.oms.submit(order(T0))
    found = await broker.find_order("o1")
    assert found is not None
    assert found.status is OrderStatus.FILLED
    assert await broker.find_order("absent") is None


async def test_price_stream_and_history(env: tuple[FakeOanda, OandaBroker, Stack]) -> None:
    fake, broker, st = env
    fake.price_lines = [
        json.dumps({"type": "HEARTBEAT", "time": "2026-09-28T08:00:00Z"}),
        json.dumps(
            {
                "type": "PRICE",
                "instrument": "EUR_USD",
                "time": "2026-09-28T08:00:01.5Z",
                "tradeable": True,
                "bids": [{"price": "1.08001"}],
                "asks": [{"price": "1.08009"}],
            }
        ),
        json.dumps(
            {
                "type": "PRICE",
                "instrument": "EUR_USD",
                "time": "2026-09-28T08:00:02Z",
                "tradeable": False,
                "bids": [{"price": "1.1"}],
                "asks": [{"price": "1.1"}],
            }
        ),
    ]
    ticks = [t async for t in broker.stream_prices(["EUR/USD"])]
    assert len(ticks) == 1
    assert ticks[0].bid == Decimal("1.08001")
    assert ticks[0].spread == Decimal("0.00008")
    bars = await broker.history("EUR/USD", Timeframe.H1, 3)
    assert len(bars) == 2  # la bougie en cours est ignorée
    assert bars[0].spread == Decimal("0.00010")
