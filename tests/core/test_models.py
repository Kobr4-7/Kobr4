from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from kobr4.core.models import Bar, OrderIntent, Position, Tick
from kobr4.core.types import OrderType, Side, Timeframe


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValidationError, match="fuseau"):
        Tick(symbol="EUR/USD", bid=Decimal("1.1"), ask=Decimal("1.1"), ts=datetime(2026, 1, 1))  # noqa: DTZ001


def test_datetime_normalized_to_utc() -> None:
    paris = timezone(timedelta(hours=2))
    tick = Tick(
        symbol="EUR/USD",
        bid=Decimal("1.1"),
        ask=Decimal("1.1"),
        ts=datetime(2026, 9, 28, 10, 0, tzinfo=paris),
    )
    assert tick.ts.utcoffset() == timedelta(0)
    assert tick.ts.hour == 8


def test_tick_prices(t0: datetime) -> None:
    tick = Tick(symbol="EUR/USD", bid=Decimal("1.08419"), ask=Decimal("1.08425"), ts=t0)
    assert tick.spread == Decimal("0.00006")
    assert tick.price_for(Side.BUY) == Decimal("1.08425")
    assert tick.price_for(Side.SELL) == Decimal("1.08419")


def test_tick_rejects_crossed_quote(t0: datetime) -> None:
    with pytest.raises(ValidationError, match="ask"):
        Tick(symbol="EUR/USD", bid=Decimal("1.2"), ask=Decimal("1.1"), ts=t0)


def test_tick_is_immutable(t0: datetime) -> None:
    tick = Tick(symbol="EUR/USD", bid=Decimal("1.1"), ask=Decimal("1.1"), ts=t0)
    with pytest.raises(ValidationError):
        tick.bid = Decimal("1.2")  # type: ignore[misc]


def test_bar_consistency(t0: datetime) -> None:
    bar = Bar(
        symbol="EUR/USD",
        timeframe=Timeframe.H1,
        open_time=t0,
        open=Decimal("1.0840"),
        high=Decimal("1.0850"),
        low=Decimal("1.0830"),
        close=Decimal("1.0845"),
    )
    assert bar.close_time == t0 + timedelta(hours=1)
    with pytest.raises(ValidationError, match="high/low"):
        Bar(
            symbol="EUR/USD",
            timeframe=Timeframe.H1,
            open_time=t0,
            open=Decimal("1.0840"),
            high=Decimal("1.0842"),
            low=Decimal("1.0830"),
            close=Decimal("1.0845"),
        )


def test_intent_requires_stop_loss(t0: datetime) -> None:
    with pytest.raises(ValidationError, match="stop_loss_pips"):
        OrderIntent(strategy_id="s", symbol="EUR/USD", side=Side.BUY, ts=t0)  # type: ignore[call-arg]


def test_intent_entry_price_rules(t0: datetime) -> None:
    common: dict[str, Any] = {"strategy_id": "s", "symbol": "EUR/USD", "side": Side.BUY, "ts": t0}
    with pytest.raises(ValidationError, match="marché"):
        OrderIntent(**common, stop_loss_pips=Decimal(25), entry_price=Decimal("1.08"))
    with pytest.raises(ValidationError, match="exige un prix"):
        OrderIntent(**common, stop_loss_pips=Decimal(25), order_type=OrderType.LIMIT)


@pytest.mark.parametrize(
    ("side", "bid", "ask", "expected"),
    [
        (Side.BUY, "1.08500", "1.08506", Decimal("120.00")),  # sort au bid
        (Side.SELL, "1.08500", "1.08506", Decimal("-122.40")),  # rachète à l'ask
    ],
)
def test_position_unrealized_pnl(
    t0: datetime, side: Side, bid: str, ask: str, expected: Decimal
) -> None:
    pos = Position(
        id="p1",
        strategy_id="s",
        symbol="EUR/USD",
        side=side,
        quantity=40_000,
        entry_price=Decimal("1.08200"),
        stop_loss=Decimal("1.07") if side is Side.BUY else Decimal("1.09"),
        opened_at=t0,
    )
    tick = Tick(symbol="EUR/USD", bid=Decimal(bid), ask=Decimal(ask), ts=t0)
    assert pos.unrealized_pnl(tick) == expected


@pytest.mark.parametrize(
    ("side", "sl", "tp", "match"),
    [
        (Side.BUY, "1.0830", None, "stop loss"),
        (Side.BUY, "1.0800", "1.0810", "take profit"),
        (Side.SELL, "1.0800", None, "stop loss"),
        (Side.SELL, "1.0840", "1.0830", "take profit"),
    ],
)
def test_position_protection_on_wrong_side(
    t0: datetime, side: Side, sl: str, tp: str | None, match: str
) -> None:
    with pytest.raises(ValidationError, match=match):
        Position(
            id="p",
            strategy_id="s",
            symbol="EUR/USD",
            side=side,
            quantity=1000,
            entry_price=Decimal("1.0820"),
            stop_loss=Decimal(sl),
            take_profit=Decimal(tp) if tp else None,
            opened_at=t0,
        )


def test_position_risk(t0: datetime) -> None:
    pos = Position(
        id="p",
        strategy_id="s",
        symbol="EUR/USD",
        side=Side.BUY,
        quantity=40_000,
        entry_price=Decimal("1.08200"),
        stop_loss=Decimal("1.07950"),
        opened_at=t0,
    )
    assert pos.risk_quote() == Decimal("100")
