from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from kobr4.core.bus import InMemoryEventBus
from kobr4.core.clock import SimulatedClock
from kobr4.core.events import (
    BarClosed,
    EquitySnapshot,
    Event,
    KillSwitchActivated,
    MarketDataResumed,
    MarketDataStale,
    PositionClosed,
    PositionOpened,
    RiskRejected,
    TickReceived,
)
from kobr4.core.models import ClosedTrade, ExitReason, OrderIntent, Position, Tick
from kobr4.core.prices import PriceBook
from kobr4.core.types import Side, Timeframe
from kobr4.db import Database
from kobr4.db.models import BotEvent, EquityPoint, TradeRecord
from kobr4.execution.brokers.simulated import SimulatedBroker
from kobr4.live.alerts import AlertRouter
from kobr4.live.journal import Journal
from kobr4.live.market import MarketData, forex_open
from kobr4.risk.calendar_feed import parse_feed

T0 = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)  # lundi


def tick(sec: int, bid: str = "1.08000", symbol: str = "EUR/USD") -> Tick:
    return Tick(
        symbol=symbol,
        bid=Decimal(bid),
        ask=Decimal(bid) + Decimal("0.0001"),
        ts=T0 + timedelta(seconds=sec),
    )


def market(clock: SimulatedClock, bus: InMemoryEventBus) -> MarketData:
    prices = PriceBook()
    broker = SimulatedBroker(prices, clock)
    return MarketData(
        bus,
        broker,
        prices,
        clock,
        ["EUR/USD", "GBP/USD"],
        {"EUR/USD": {Timeframe.M1, Timeframe.M5}},
        stale_after=30,
    )


def test_forex_hours() -> None:
    assert forex_open(datetime(2026, 9, 28, 3, tzinfo=UTC))  # lundi
    assert forex_open(datetime(2026, 10, 2, 20, tzinfo=UTC))  # vendredi 20h
    assert not forex_open(datetime(2026, 10, 2, 21, tzinfo=UTC))
    assert not forex_open(datetime(2026, 10, 3, 12, tzinfo=UTC))  # samedi
    assert not forex_open(datetime(2026, 10, 4, 21, tzinfo=UTC))  # dimanche 21h
    assert forex_open(datetime(2026, 10, 4, 22, tzinfo=UTC))


async def test_ticks_build_bars_and_flush() -> None:
    clock, bus = SimulatedClock(T0), InMemoryEventBus()
    md = market(clock, bus)
    seen: list[Event] = []
    bus.subscribe(Event, seen.append)
    for s, px in ((0, "1.08000"), (30, "1.08050"), (61, "1.08020")):
        clock.set(T0 + timedelta(seconds=s))
        await md.on_tick(tick(s, px))
    bars = [e.bar for e in seen if isinstance(e, BarClosed)]
    assert [(b.timeframe, b.high) for b in bars] == [(Timeframe.M1, Decimal("1.08050"))]
    assert len([e for e in seen if isinstance(e, TickReceived)]) == 3
    clock.set(T0 + timedelta(minutes=5))
    await md.check()
    bars = [e.bar for e in seen if isinstance(e, BarClosed)]
    assert [b.timeframe for b in bars] == [Timeframe.M1, Timeframe.M1, Timeframe.M5]


async def test_stale_and_resume() -> None:
    clock, bus = SimulatedClock(T0), InMemoryEventBus()
    md = market(clock, bus)
    seen: list[Event] = []
    bus.subscribe(Event, seen.append)
    await md.check()
    assert not md.stale  # aucune cotation encore reçue : pas d'alerte au démarrage
    await md.on_tick(tick(0))
    await md.on_tick(tick(0, "1.27", symbol="GBP/USD"))
    clock.advance(timedelta(seconds=45))
    await md.on_tick(tick(45))
    await md.check()
    assert md.stale == {"GBP/USD"}
    await md.check()
    assert len([e for e in seen if isinstance(e, MarketDataStale)]) == 1
    await md.on_tick(tick(46, "1.27", symbol="GBP/USD"))
    assert md.stale == set()
    assert any(isinstance(e, MarketDataResumed) for e in seen)


async def test_no_stale_alert_when_market_closed() -> None:
    saturday = datetime(2026, 10, 3, 12, tzinfo=UTC)
    clock, bus = SimulatedClock(saturday), InMemoryEventBus()
    md = market(clock, bus)
    md.ticks_received = 1
    await md.check()
    assert md.stale == set()


def trade(pid: str = "p1") -> ClosedTrade:
    return ClosedTrade(
        position_id=pid,
        strategy_id="s",
        symbol="EUR/USD",
        side=Side.BUY,
        quantity=1000,
        entry_price=Decimal("1.08"),
        exit_price=Decimal("1.09"),
        opened_at=T0,
        closed_at=T0 + timedelta(hours=1),
        reason=ExitReason.TAKE_PROFIT,
        pnl=Decimal("10.5"),
    )


async def test_journal_records_events_trades_and_equity(db: Database) -> None:
    bus = InMemoryEventBus()
    j = Journal(db, "bot1", bus)
    await bus.publish(TickReceived(ts=T0, tick=tick(0)))
    await bus.publish(PositionClosed(ts=T0, trade=trade()))
    for sec in (0, 20, 70):
        await bus.publish(
            EquitySnapshot(
                ts=T0 + timedelta(seconds=sec),
                balance=Decimal(100),
                equity=Decimal(101),
                open_positions=0,
            )
        )
    assert await j.flush() == 4  # 1 événement, 1 trade, 2 minutes d'équité
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(BotEvent)) == 1
        (t,) = (await s.scalars(select(TradeRecord))).all()
        assert t.pnl == Decimal("10.5")
        assert t.closed_at == T0 + timedelta(hours=1)
        pts = (await s.scalars(select(EquityPoint).order_by(EquityPoint.ts))).all()
        assert [p.ts for p in pts] == [T0, T0 + timedelta(minutes=1)]
        ev = (await s.scalars(select(BotEvent))).one()
        assert ev.type == "PositionClosed"
        assert ev.payload["trade"]["pnl"] == "10.5"


async def test_journal_keeps_data_when_db_fails(db: Database) -> None:
    bus = InMemoryEventBus()
    j = Journal(db, "bot1", bus)
    await bus.publish(PositionClosed(ts=T0, trade=trade()))
    broken = Database("sqlite+aiosqlite:////nonexistent/dir/x.db")
    j.db = broken
    assert await j.flush() == 0
    assert j.write_errors == 1
    j.db = db
    assert await j.flush() == 2
    await broken.close()


class FakeNotifier:
    def __init__(self, fail: bool = False) -> None:
        self.messages: list[str] = []
        self.fail = fail

    async def send(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("réseau")
        self.messages.append(text)


async def test_alerts_with_cooldown() -> None:
    bus = InMemoryEventBus()
    now = [0.0]
    n = FakeNotifier()
    AlertRouter(bus, n, "Bot EUR", cooldown=60, clock=lambda: now[0])
    ks = KillSwitchActivated(ts=T0, reason="drawdown 10 %", close_positions=False)
    await bus.publish(ks)
    await bus.publish(ks)
    now[0] = 61
    await bus.publish(ks)
    assert n.messages == ["[Bot EUR] ⛔ Arrêt d'urgence : drawdown 10 %"] * 2
    pos = Position(
        id="o1",
        strategy_id="s",
        symbol="EUR/USD",
        side=Side.BUY,
        quantity=40_000,
        entry_price=Decimal("1.08010"),
        stop_loss=Decimal("1.07760"),
        opened_at=T0,
    )
    await bus.publish(PositionOpened(ts=T0, position=pos))
    await bus.publish(PositionClosed(ts=T0, trade=trade()))
    assert n.messages[2] == "[Bot EUR] Achat 40 000 EUR/USD à 1.08010 (stop 1.07760)"
    assert n.messages[3] == "[Bot EUR] ✅ EUR/USD fermé (take_profit) : +10.50"
    intent = OrderIntent(
        strategy_id="s", symbol="EUR/USD", side=Side.BUY, stop_loss_pips=Decimal(25), ts=T0
    )
    await bus.publish(RiskRejected(ts=T0, intent=intent, rule="spread", reason="x"))
    assert len(n.messages) == 4  # les refus du risque ne sont pas notifiés


async def test_alerts_trades_optional_and_failures_counted() -> None:
    bus = InMemoryEventBus()
    n = FakeNotifier(fail=True)
    router = AlertRouter(bus, n, "B", notify_trades=False)
    await bus.publish(PositionClosed(ts=T0, trade=trade()))
    assert router.failures == 0
    await bus.publish(KillSwitchActivated(ts=T0, reason="x", close_positions=True))
    assert router.failures == 1


def test_calendar_feed_parsing() -> None:
    events = parse_feed(
        [
            {
                "title": "Non-Farm Employment Change",
                "country": "USD",
                "date": "2026-10-02T08:30:00-04:00",
                "impact": "High",
            },
            {
                "title": "Bank Holiday",
                "country": "JPY",
                "date": "2026-10-02T00:00:00+09:00",
                "impact": "Holiday",
            },
            {
                "title": "German PMI",
                "country": "EUR",
                "date": "2026-10-01T03:30:00-04:00",
                "impact": "Medium",
            },
            {"title": "broken", "country": "USD", "date": "demain", "impact": "High"},
        ]
    )
    assert [(e.currency, e.impact.value) for e in events] == [("USD", "high"), ("EUR", "medium")]
    assert events[0].ts == datetime(2026, 10, 2, 12, 30, tzinfo=UTC)
