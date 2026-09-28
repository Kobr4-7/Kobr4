from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from kobr4.config.settings import StrategySettings
from kobr4.core.models import Bar, CloseIntent, OrderIntent, Position
from kobr4.core.types import Side, Timeframe
from kobr4.strategies import STRATEGIES, create_strategy
from kobr4.strategies.sessions import Session, in_sessions


class Ctx:
    def __init__(self, t0: datetime) -> None:
        self.t0 = t0
        self.open: list[Position] = []

    def now(self) -> datetime:
        return self.t0

    def positions(self, strategy_id: str, symbol: str) -> Sequence[Position]:
        return [p for p in self.open if p.strategy_id == strategy_id and p.symbol == symbol]


def settings(kind: str, **params: Any) -> StrategySettings:
    return StrategySettings(
        id="s", kind=kind, instruments=["EUR/USD"], timeframe=Timeframe.H1, params=params
    )


def bars(t0: datetime, closes: list[float], spread: str = "0.0001") -> list[Bar]:
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = Decimal(str(round(prev, 5)))
        cl = Decimal(str(round(c, 5)))
        out.append(
            Bar(
                symbol="EUR/USD",
                timeframe=Timeframe.H1,
                open_time=t0 + timedelta(hours=i),
                open=o,
                high=max(o, cl) + Decimal("0.0002"),
                low=min(o, cl) - Decimal("0.0002"),
                close=cl,
                spread=Decimal(spread),
            )
        )
        prev = c
    return out


def feed(strategy: Any, ctx: Ctx, bs: list[Bar]) -> list[list[Any]]:
    return [strategy.on_bar(b, ctx) for b in bs]


def position(t0: datetime, side: Side) -> Position:
    sl = Decimal("1.0") if side is Side.BUY else Decimal("1.5")
    return Position(
        id="p",
        strategy_id="s",
        symbol="EUR/USD",
        side=side,
        quantity=1000,
        entry_price=Decimal("1.1"),
        stop_loss=sl,
        opened_at=t0,
    )


def test_registry() -> None:
    assert set(STRATEGIES) == {"ema_cross", "rsi_reversion", "breakout"}
    with pytest.raises(ValueError, match="type inconnu"):
        create_strategy(settings("martingale"))


def test_ema_params_validated() -> None:
    with pytest.raises(ValidationError, match="plus courte"):
        create_strategy(settings("ema_cross", fast=50, slow=20))
    with pytest.raises(ValidationError):
        create_strategy(settings("ema_cross", unknown=1))


def test_ema_cross_buys_on_golden_cross(t0: datetime) -> None:
    s = create_strategy(
        settings("ema_cross", fast=3, slow=6, stop_loss_pips=20, take_profit_pips=40)
    )
    ctx = Ctx(t0)
    closes = [1.10 - i * 0.001 for i in range(10)] + [1.09 + i * 0.003 for i in range(10)]
    out = [i for step in feed(s, ctx, bars(t0, closes)) for i in step]
    buys = [i for i in out if isinstance(i, OrderIntent)]
    assert buys[0].side is Side.BUY
    assert buys[0].stop_loss_pips == 20
    assert buys[0].take_profit_pips == 40
    assert "EMA 3 > EMA 6" in buys[0].reason


def test_reverse_closes_opposite_and_skips_duplicate(t0: datetime) -> None:
    s = create_strategy(settings("ema_cross", fast=3, slow=6))
    ctx = Ctx(t0)
    ctx.open = [position(t0, Side.SELL)]
    b = bars(t0, [1.1])[0]
    out = s.reverse_to(Side.BUY, b, ctx, "test")
    assert isinstance(out[0], CloseIntent)
    assert out[0].side is Side.SELL
    assert isinstance(out[1], OrderIntent)
    assert out[1].side is Side.BUY
    ctx.open = [position(t0, Side.BUY)]
    assert s.reverse_to(Side.BUY, b, ctx, "test") == []


def test_sessions(t0: datetime) -> None:
    assert in_sessions(t0.replace(hour=3), [])
    assert in_sessions(t0.replace(hour=8), [Session.LONDON])
    assert not in_sessions(t0.replace(hour=22), [Session.LONDON, Session.NEW_YORK])
    s = create_strategy(settings("ema_cross", fast=3, slow=6, sessions=["tokyo"]))
    b = bars(t0.replace(hour=12), [1.1])[0]
    assert s.reverse_to(Side.BUY, b, Ctx(t0), "x") == []


def test_rsi_reversion_buys_out_of_oversold(t0: datetime) -> None:
    s = create_strategy(settings("rsi_reversion", period=5, adx_max=0))
    ctx = Ctx(t0)
    closes = [1.10 - i * 0.002 for i in range(15)] + [1.072, 1.075, 1.079]
    out = [i for step in feed(s, ctx, bars(t0, closes)) for i in step]
    assert any(isinstance(i, OrderIntent) and i.side is Side.BUY for i in out)


def test_rsi_exit_at_mid(t0: datetime) -> None:
    s = create_strategy(settings("rsi_reversion", period=5, adx_max=0))
    ctx = Ctx(t0)
    ctx.open = [position(t0, Side.BUY)]
    closes = [1.10 - i * 0.002 for i in range(12)] + [1.08 + i * 0.004 for i in range(8)]
    out = [i for step in feed(s, ctx, bars(t0, closes)) for i in step]
    assert any(isinstance(i, CloseIntent) and i.side is Side.BUY for i in out)


def test_breakout_with_atr_stop(t0: datetime) -> None:
    s = create_strategy(settings("breakout", lookback=5, atr_stop=2))
    ctx = Ctx(t0)
    closes = [
        1.10,
        1.101,
        1.099,
        1.1005,
        1.1,
        1.1002,
        1.0998,
        1.1001,
        1.1,
        1.0999,
        1.1001,
        1.1,
        1.0999,
        1.1,
        1.1001,
        1.1002,
        1.12,
    ]
    out = feed(s, ctx, bars(t0, closes))
    last = [i for i in out[-1] if isinstance(i, OrderIntent)]
    assert last
    assert last[0].side is Side.BUY
    assert last[0].stop_loss_pips != 25  # stop calculé sur l'ATR
    assert all(step == [] for step in out[:5])
