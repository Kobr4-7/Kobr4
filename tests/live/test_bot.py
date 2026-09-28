import asyncio
import json
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest

from kobr4.config.settings import Settings
from kobr4.core.clock import SimulatedClock
from kobr4.core.models import Bar
from kobr4.core.types import Side
from kobr4.db import Database
from kobr4.execution.brokers.oanda import OandaBroker
from kobr4.live.bot import BotState, Intervals, LiveBot
from kobr4.strategies import STRATEGIES
from kobr4.strategies.base import Intent, Strategy, StrategyContext
from tests.execution.fake_oanda import FakeOanda

T0 = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)


class BuyOnce(Strategy):
    """Achète au premier signal, une seule fois (pour les tests)."""

    kind = "test_buy_once"
    warmed: ClassVar[list[int]] = []

    @property
    def warmup_bars(self) -> int:
        return 2

    def __init__(self, *a: Any) -> None:
        super().__init__(*a)
        self.bars = 0
        self.done = False

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[Intent]:
        self.bars += 1
        if self.done:
            return []
        self.done = True
        return [self.open(Side.BUY, bar, "test")]


@pytest.fixture(autouse=True)
def register() -> Any:
    STRATEGIES[BuyOnce.kind] = BuyOnce
    yield
    del STRATEGIES[BuyOnce.kind]


def price(sec: str, bid: str) -> str:
    return json.dumps(
        {
            "type": "PRICE",
            "instrument": "EUR_USD",
            "time": f"2026-09-28T08:{sec}Z",
            "tradeable": True,
            "bids": [{"price": bid}],
            "asks": [{"price": f"{float(bid) + 0.0001:.5f}"}],
        }
    )


def settings() -> Settings:
    return Settings.model_validate(
        {
            "mode": "paper",
            "instruments": ["EUR/USD"],
            "broker": {"name": "oanda", "environment": "practice"},
            "strategies": [
                {"id": "t", "kind": "test_buy_once", "instruments": ["EUR/USD"], "timeframe": "M1"}
            ],
        }
    )


async def wait_for(cond: Any, timeout: float = 3.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition jamais remplie")


async def test_bot_end_to_end_with_fake_oanda() -> None:
    fake = FakeOanda()
    fake.price_lines = [
        price("00:01", "1.08000"),
        price("00:30", "1.08040"),
        price("01:05", "1.08020"),
    ]
    clock = SimulatedClock(T0)
    broker = OandaBroker("tok", fake.account_id, "practice", clock, transport=fake.transport())
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.create_all()
    bot = LiveBot(
        "bot1",
        "Test",
        settings(),
        broker,
        db=db,
        clock=clock,
        intervals=Intervals(check=0.02, mark=0.02, reconcile=0.05, account=0.05),
    )
    await bot.start()
    try:
        assert bot.state is BotState.RUNNING
        # L'amorçage a consommé l'historique sans passer d'ordre.
        strategy = bot.strategies[0]
        assert isinstance(strategy, BuyOnce)
        assert strategy.bars == 2
        assert strategy.done
        assert not any(r[0] == "POST" for r in fake.requests)
        strategy.done = False  # autorise un achat sur la prochaine bougie en direct
        await wait_for(lambda: bot.portfolio.positions())
        (pos,) = bot.portfolio.positions()
        assert pos.symbol == "EUR/USD"
        assert pos.side is Side.BUY
        status = bot.status()
        assert status["state"] == "running"
        assert status["positions"][0]["id"] == pos.id
        assert status["quotes"]["EUR/USD"]["bid"]
        await bot.kill("test", close_positions=True)
        assert bot.portfolio.positions() == []
        assert bot.status()["kill_switch"] == "test"
    finally:
        await fake.tx_queue.put("__END__")
        await bot.stop()
    assert bot.status()["state"] == "stopped"
    from sqlalchemy import select

    from kobr4.db.models import BotEvent, TradeRecord

    async with db.session() as s:
        types = {e.type for e in (await s.scalars(select(BotEvent))).all()}
        assert {
            "SignalEmitted",
            "RiskApproved",
            "PositionOpened",
            "PositionClosed",
            "KillSwitchActivated",
        } <= types
        assert len((await s.scalars(select(TradeRecord))).all()) == 1
    await db.close()


async def test_bot_refuses_currency_mismatch() -> None:
    fake = FakeOanda()
    s = settings().model_copy(update={"base_currency": "EUR"})
    broker = OandaBroker(
        "tok", fake.account_id, "practice", SimulatedClock(T0), transport=fake.transport()
    )
    bot = LiveBot("b", "B", s, broker, clock=SimulatedClock(T0))
    with pytest.raises(ValueError, match="compte est en USD"):
        await bot.start()
    assert bot.state is BotState.ERROR
    assert "USD" in (bot.error or "")


async def test_strategy_pause_blocks_entries_only() -> None:
    fake = FakeOanda()
    broker = OandaBroker(
        "tok", fake.account_id, "practice", SimulatedClock(T0), transport=fake.transport()
    )
    bot = LiveBot("b", "B", settings(), broker, clock=SimulatedClock(T0))
    bot.set_strategy_enabled("t", False)
    assert bot.status()["strategies"] == [{"id": "t", "kind": "test_buy_once", "enabled": False}]
    with pytest.raises(KeyError):
        bot.set_strategy_enabled("nope", True)
