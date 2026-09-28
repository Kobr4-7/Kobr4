from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest

from kobr4.core.clock import SimulatedClock
from kobr4.core.events import PositionClosed, PositionOpened
from kobr4.core.models import ExitReason, Tick
from kobr4.core.orders import OrderStatus
from kobr4.core.types import Side, Timeframe
from kobr4.execution.brokers.saxo import (
    SaxoAuthError,
    SaxoBroker,
    SaxoSession,
    SaxoTokens,
    authorize_url,
    exchange_code,
    external_ref,
    from_saxo,
    parse_ref,
    parse_time,
)
from kobr4.execution.oms import OrderManager
from tests.execution.fake_saxo import FakeSaxo
from tests.execution.test_simulated_broker import order
from tests.stack import Stack, make_stack

T0 = datetime(2026, 9, 28, 8, 30, tzinfo=UTC)


def tokens(fake: FakeSaxo, access_in: timedelta = timedelta(minutes=20)) -> SaxoTokens:
    now = datetime.now(UTC)
    return SaxoTokens(
        app_key=fake.app_key,
        app_secret=fake.app_secret,
        redirect_uri="https://kobr4.test/api/brokers/saxo/callback",
        access_token=fake.access,
        access_expires=now + access_in,
        refresh_token=fake.refresh,
        refresh_expires=now + timedelta(hours=1),
    )


def make_broker(fake: FakeSaxo, session: SaxoSession | None = None) -> SaxoBroker:
    session = session or SaxoSession("practice", tokens(fake), transport=fake.transport())
    return SaxoBroker(
        session,
        fake.account_key,
        SimulatedClock(T0),
        transport=fake.transport(),
        price_interval=0,
        position_interval=3600,
        fill_timeout=1,
    )


@pytest.fixture
async def env() -> AsyncIterator[tuple[FakeSaxo, SaxoBroker, Stack]]:
    fake = FakeSaxo()
    st = make_stack(T0)
    broker = make_broker(fake)
    st.oms = OrderManager(st.bus, broker, st.portfolio, st.clock)
    await broker.connect()
    yield fake, broker, st
    await broker.close()


def test_helpers() -> None:
    assert parse_time("2026-09-28T08:00:00.1234567Z") == datetime(
        2026, 9, 28, 8, 0, 0, 123456, tzinfo=UTC
    )
    assert from_saxo("EURUSD") == "EUR/USD"
    assert parse_ref(external_ref("o1", "ema-cross")) == ("o1", "ema-cross")
    assert parse_ref(None) is None
    assert len(external_ref("x" * 40, "y" * 40)) == 50
    url = authorize_url("practice", "key", "https://a.b/cb", "st4te")
    assert url.startswith("https://sim.logonvalidation.net/authorize?")
    assert "client_id=key" in url
    assert "state=st4te" in url
    assert "response_type=code" in url


async def test_exchange_code_and_refresh_rotation() -> None:
    fake = FakeSaxo()
    t = await exchange_code(
        "practice", fake.app_key, fake.app_secret, "https://x/cb", "code-ok", fake.transport()
    )
    assert (t.access_token, t.refresh_token) == ("acc-1", "ref-1")
    saved: list[SaxoTokens] = []

    async def keep(new: SaxoTokens) -> None:
        saved.append(new)

    session = SaxoSession("practice", t, on_refresh=keep, transport=fake.transport())
    await session.refresh(force=True)
    assert session.tokens.refresh_token == "ref-2"
    assert saved[-1].access_token == "acc-2"
    assert SaxoTokens.loads(saved[-1].dumps()) == saved[-1]
    with pytest.raises(SaxoAuthError):
        await exchange_code(
            "practice", fake.app_key, fake.app_secret, "https://x/cb", "mauvais", fake.transport()
        )


async def test_expired_access_token_is_renewed_before_call() -> None:
    fake = FakeSaxo()
    session = SaxoSession(
        "practice", tokens(fake, timedelta(seconds=30)), transport=fake.transport()
    )
    broker = make_broker(fake, session)
    await broker.connect()
    assert session.tokens.access_token == "acc-2"
    await broker.close()


async def test_401_forces_a_refresh() -> None:
    fake = FakeSaxo()
    session = SaxoSession("practice", tokens(fake), transport=fake.transport())
    fake.access = "révoqué-côté-saxo"  # le jeton local n'est plus accepté
    broker = make_broker(fake, session)
    await broker.connect()
    assert session.tokens.access_token == "acc-2"
    await broker.close()


async def test_expired_refresh_token_asks_to_reconnect() -> None:
    fake = FakeSaxo()
    t = tokens(fake, timedelta(seconds=0))
    t.refresh_expires = datetime.now(UTC) - timedelta(seconds=1)
    broker = make_broker(fake, SaxoSession("practice", t, transport=fake.transport()))
    with pytest.raises(SaxoAuthError, match="reconnecte"):
        await broker.connect()
    await broker.close()


async def test_market_order_with_stop_and_target_recalibrated_on_fill(
    env: tuple[FakeSaxo, SaxoBroker, Stack],
) -> None:
    fake, broker, st = env
    fake.fill_slip = Decimal("0.00002")
    result = await st.oms.submit(order(T0, qty=10_000))
    assert result.status is OrderStatus.FILLED
    assert result.avg_fill_price == Decimal("1.08012")
    body = next(b for m, p, b in fake.requests if m == "POST" and p == "/trade/v2/orders")
    assert body["ExternalReference"] == "o1|s1"
    assert body["BuySell"] == "Buy"
    assert body["Amount"] == 10_000
    assert body["ManualOrder"] is False
    assert [o["OrderType"] for o in body["Orders"]] == ["StopIfTraded", "Limit"]
    assert all(o["BuySell"] == "Sell" for o in body["Orders"])
    (pos,) = st.portfolio.positions()
    assert (pos.id, pos.strategy_id) == ("o1", "s1")
    assert pos.stop_loss == Decimal("1.07762")  # 1.08012 - 0.0025
    assert pos.take_profit == Decimal("1.08512")
    assert sum(1 for m, *_ in fake.requests if m == "PATCH") == 2
    assert len(st.of(PositionOpened)) == 1


async def test_sell_order(env: tuple[FakeSaxo, SaxoBroker, Stack]) -> None:
    fake, broker, st = env
    await st.oms.submit(order(T0, side=Side.SELL, qty=5_000))
    (pos,) = st.portfolio.positions()
    assert pos.side is Side.SELL
    assert pos.entry_price == Decimal("1.08000")
    assert pos.stop_loss == Decimal("1.08250")
    assert sum(1 for m, *_ in fake.requests if m == "PATCH") == 0  # déjà au bon prix


async def test_rejection(env: tuple[FakeSaxo, SaxoBroker, Stack]) -> None:
    fake, broker, st = env
    fake.reject_next = "marge insuffisante"
    result = await st.oms.submit(order(T0))
    assert result.status is OrderStatus.REJECTED
    assert "marge insuffisante" in (result.reject_reason or "")
    assert st.portfolio.positions() == []


async def test_modify_moves_stop_and_manages_target(
    env: tuple[FakeSaxo, SaxoBroker, Stack],
) -> None:
    fake, broker, st = env
    await st.oms.submit(order(T0))
    pos = await broker.modify_position("o1", Decimal("1.07900"), None)
    assert pos.stop_loss == Decimal("1.07900")
    assert pos.take_profit is None
    assert any(m == "DELETE" for m, *_ in fake.requests)
    pos = await broker.modify_position("o1", Decimal("1.07950"), Decimal("1.09000"))
    assert pos.take_profit == Decimal("1.09000")
    add = [b for m, p, b in fake.requests if m == "POST" and "PositionId" in b]
    assert add
    assert add[-1]["Orders"][0]["OrderType"] == "Limit"


async def test_close_reports_net_result(env: tuple[FakeSaxo, SaxoBroker, Stack]) -> None:
    fake, broker, st = env
    await st.oms.submit(order(T0))
    fake.price[21] = (Decimal("1.08110"), Decimal("1.08120"))
    await st.oms.close("o1", ExitReason.SIGNAL)
    (closed,) = st.of(PositionClosed)
    trade = closed.trade
    assert trade.reason is ExitReason.SIGNAL
    assert trade.exit_price == Decimal("1.08110")
    assert trade.commission == Decimal("3.0")
    assert trade.pnl == Decimal("10.0") - Decimal("3.0")  # 0.0010 × 10 000, frais déduits
    assert st.portfolio.positions() == []


async def test_stop_hit_at_saxo_is_detected(env: tuple[FakeSaxo, SaxoBroker, Stack]) -> None:
    fake, broker, st = env
    await st.oms.submit(order(T0))
    (pid,) = fake.positions
    fake.hit_stop(pid)
    assert await broker.positions() == []
    (closed,) = st.of(PositionClosed)
    assert closed.trade.reason is ExitReason.STOP_LOSS
    assert closed.trade.exit_price == Decimal("1.07760")


async def test_restart_recovers_positions_by_reference(
    env: tuple[FakeSaxo, SaxoBroker, Stack],
) -> None:
    fake, broker, st = env
    await st.oms.submit(order(T0))
    again = make_broker(fake)
    await again.connect()
    (pos,) = await again.positions()
    assert (pos.id, pos.strategy_id, pos.symbol) == ("o1", "s1", "EUR/USD")
    found = await again.find_order("o1")
    assert found is not None
    assert found.status is OrderStatus.FILLED
    assert await again.find_order("inconnu") is None
    await again.close()


async def test_without_reference_on_positions_order_is_still_matched(
    env: tuple[FakeSaxo, SaxoBroker, Stack],
) -> None:
    fake, broker, st = env
    fake.external_refs = False
    await st.oms.submit(order(T0))
    (pos,) = st.portfolio.positions()
    assert (pos.id, pos.strategy_id) == ("o1", "s1")


async def test_account(env: tuple[FakeSaxo, SaxoBroker, Stack]) -> None:
    fake, broker, st = env
    a = await broker.account()
    assert (a.currency, a.balance, a.equity, a.margin_used) == (
        "USD",
        Decimal("10000.0"),
        Decimal("10012.5"),
        Decimal("216.0"),
    )


async def test_price_polling(env: tuple[FakeSaxo, SaxoBroker, Stack]) -> None:
    fake, broker, st = env
    stream = cast(AsyncGenerator[Tick, None], broker.stream_prices(["EUR/USD"]))
    first = await anext(stream)
    assert (first.symbol, first.bid, first.ask) == ("EUR/USD", Decimal("1.08"), Decimal("1.0801"))
    fake.price[21] = (Decimal("1.08050"), Decimal("1.08060"))
    second = await anext(stream)
    assert second.bid == Decimal("1.0805")
    assert second.ts > first.ts
    await stream.aclose()


async def test_history_skips_the_bar_in_progress(env: tuple[FakeSaxo, SaxoBroker, Stack]) -> None:
    fake, broker, st = env
    bars = await broker.history("EUR/USD", Timeframe.H1, 3)
    assert [b.open_time.hour for b in bars] == [6, 7]
    assert bars[0].spread == Decimal("0.0001")
    assert bars[0].volume == Decimal(120)
