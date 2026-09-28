import asyncio
import json
import lzma
import struct
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from kobr4.core.clock import SimulatedClock
from kobr4.core.events import PositionClosed, PositionOpened
from kobr4.core.models import ExitReason, Tick
from kobr4.core.orders import OrderStatus
from kobr4.core.types import Side, Timeframe
from kobr4.execution.broker import BrokerError
from kobr4.execution.brokers.paper import PaperBroker, from_finnhub, make_tick, to_finnhub
from kobr4.execution.oms import OrderManager
from kobr4.marketdata.store import ParquetBarStore
from tests.execution.test_simulated_broker import order
from tests.stack import Stack, make_stack

T0 = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)
MS = int(T0.timestamp() * 1000)


class FakeFinnhub:
    """Faux flux WebSocket Finnhub : envoie les messages mis dans la file."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent: list[dict[str, str]] = []
        self.urls: list[str] = []

    def trade(self, symbol: str, price: str, ms: int = MS) -> None:
        msg = {"type": "trade", "data": [{"s": to_finnhub(symbol), "p": float(price), "t": ms}]}
        self.queue.put_nowait(json.dumps(msg))

    def connect(self, url: str):  # type: ignore[no-untyped-def]
        fake = self

        class Ws:
            async def send(self, message: str) -> None:
                fake.sent.append(json.loads(message))

            def __aiter__(self) -> AsyncIterator[str]:
                return self._gen()

            async def _gen(self) -> AsyncIterator[str]:
                while True:
                    m = await fake.queue.get()
                    if m is None:
                        return
                    yield m

        @asynccontextmanager
        async def ctx() -> AsyncIterator[Ws]:
            fake.urls.append(url)
            yield Ws()

        return ctx()


@pytest.fixture
async def env(tmp_path: Path) -> tuple[FakeFinnhub, PaperBroker, Stack]:
    fake = FakeFinnhub()
    st = make_stack(T0)
    broker = PaperBroker(
        "cle-finnhub-123",
        tmp_path / "paper" / "compte.json",
        clock=SimulatedClock(T0),
        connect_ws=fake.connect,
    )
    st.oms = OrderManager(st.bus, broker, st.portfolio, st.clock)
    await broker.connect()
    return fake, broker, st


async def feed(broker: PaperBroker, fake: FakeFinnhub, *trades: tuple[str, str]) -> list[Tick]:
    """Envoie des cotations et lit les ticks produits."""
    for symbol, price in trades:
        fake.trade(symbol, price)
    fake.queue.put_nowait(None)
    return [t async for t in broker.stream_prices(["EUR/USD", "USD/JPY"])]


def test_helpers() -> None:
    assert to_finnhub("EUR/USD") == "OANDA:EUR_USD"
    assert from_finnhub("OANDA:USD_JPY") == "USD/JPY"
    t = make_tick("EUR/USD", Decimal("1.08000"), T0)
    assert (t.bid, t.ask) == (Decimal("1.07997"), Decimal("1.08003"))


async def test_stream_subscribes_and_builds_ticks(
    env: tuple[FakeFinnhub, PaperBroker, Stack],
) -> None:
    fake, broker, st = env
    ticks = await feed(broker, fake, ("EUR/USD", "1.08"), ("GBP/USD", "1.25"))
    assert fake.urls == ["wss://ws.finnhub.io?token=cle-finnhub-123"]
    assert [m["symbol"] for m in fake.sent] == ["OANDA:EUR_USD", "OANDA:USD_JPY"]
    assert [t.symbol for t in ticks] == ["EUR/USD"]  # GBP/USD non demandé
    assert ticks[0].ts == T0


async def test_error_message_ends_stream(env: tuple[FakeFinnhub, PaperBroker, Stack]) -> None:
    fake, broker, st = env
    fake.queue.put_nowait(json.dumps({"type": "error", "msg": "Invalid API key"}))
    with pytest.raises(BrokerError, match="Invalid API key"):
        async for _ in broker.stream_prices(["EUR/USD"]):
            pass


async def test_order_before_any_price_is_rejected(
    env: tuple[FakeFinnhub, PaperBroker, Stack],
) -> None:
    fake, broker, st = env
    result = await st.oms.submit(order(T0))
    assert result.status is OrderStatus.REJECTED


async def test_stop_hit_on_live_price(env: tuple[FakeFinnhub, PaperBroker, Stack]) -> None:
    fake, broker, st = env
    await feed(broker, fake, ("EUR/USD", "1.08000"))
    result = await st.oms.submit(order(T0, qty=10_000))
    assert result.status is OrderStatus.FILLED
    assert result.avg_fill_price == Decimal("1.08005")  # ask 1.08003 + 0.2 pip
    assert len(st.of(PositionOpened)) == 1
    await feed(broker, fake, ("EUR/USD", "1.07700"))
    (closed,) = st.of(PositionClosed)
    assert closed.trade.reason is ExitReason.STOP_LOSS
    assert closed.trade.exit_price == Decimal("1.07697")
    assert broker.balance < Decimal(10_000)


async def test_take_profit_at_its_level(env: tuple[FakeFinnhub, PaperBroker, Stack]) -> None:
    fake, broker, st = env
    await feed(broker, fake, ("EUR/USD", "1.08000"))
    await st.oms.submit(order(T0, side=Side.SELL, qty=10_000))
    await feed(broker, fake, ("EUR/USD", "1.07400"))
    (closed,) = st.of(PositionClosed)
    assert closed.trade.reason is ExitReason.TAKE_PROFIT
    assert closed.trade.exit_price == Decimal("1.07495")  # 1.07995 - 0.0050


async def test_account_survives_restart(
    env: tuple[FakeFinnhub, PaperBroker, Stack], tmp_path: Path
) -> None:
    fake, broker, st = env
    await feed(broker, fake, ("EUR/USD", "1.08000"))
    await st.oms.submit(order(T0))
    again = PaperBroker("cle-finnhub-123", broker.state_path, clock=SimulatedClock(T0))
    await again.connect()
    (pos,) = await again.positions()
    assert (pos.id, pos.entry_price) == ("o1", Decimal("1.08005"))
    found = await again.find_order("o1")
    assert found is not None and found.status is OrderStatus.FILLED  # noqa: PT018
    await st.oms.close("o1", ExitReason.MANUAL)
    reloaded = PaperBroker("x" * 12, broker.state_path, clock=SimulatedClock(T0))
    await reloaded.connect()
    assert await reloaded.positions() == []
    assert reloaded.balance == broker.balance


def _bi5(rows: list[tuple[int, int, int, int, int, float]]) -> bytes:
    rec = struct.Struct(">5if")
    return lzma.compress(b"".join(rec.pack(*r) for r in rows), format=lzma.FORMAT_ALONE)


async def test_history_downloads_missing_days(tmp_path: Path) -> None:
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        # Une bougie par heure, prix 1.08000 (précision 5 décimales).
        return _bi5([(h * 3600, 108000, 108010, 107990, 108020, 1.0) for h in range(24)])

    store = ParquetBarStore(tmp_path / "data")
    broker = PaperBroker(
        "cle-finnhub-123",
        tmp_path / "compte.json",
        store=store,
        clock=SimulatedClock(T0),
        fetch=fetch,
    )
    bars = await broker.history("EUR/USD", Timeframe.H1, 30)
    assert len(bars) == 30
    assert bars[-1].open_time.date() == date(2026, 9, 27)
    assert bars[-1].close == Decimal("1.0801")
    n = len(calls)
    await broker.history("EUR/USD", Timeframe.H1, 30)
    assert len(calls) == n  # jours déjà stockés : rien à retélécharger
