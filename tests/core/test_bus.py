from datetime import datetime
from decimal import Decimal

import pytest

from kobr4.core.bus import InMemoryEventBus
from kobr4.core.events import Event, KillSwitchActivated, TickReceived
from kobr4.core.models import Tick


def tick_event(t0: datetime) -> TickReceived:
    return TickReceived(
        ts=t0, tick=Tick(symbol="EUR/USD", bid=Decimal("1.1"), ask=Decimal("1.1"), ts=t0)
    )


async def test_sync_and_async_handlers(t0: datetime) -> None:
    bus = InMemoryEventBus()
    seen: list[str] = []

    def sync_handler(e: TickReceived) -> None:
        seen.append("sync")

    async def async_handler(e: TickReceived) -> None:
        seen.append("async")

    bus.subscribe(TickReceived, sync_handler)
    bus.subscribe(TickReceived, async_handler)
    await bus.publish(tick_event(t0))
    assert seen == ["sync", "async"]


async def test_base_class_subscription_receives_everything(t0: datetime) -> None:
    bus = InMemoryEventBus()
    seen: list[type[Event]] = []
    bus.subscribe(Event, lambda e: seen.append(type(e)))
    await bus.publish(tick_event(t0))
    await bus.publish(KillSwitchActivated(ts=t0, reason="test", close_positions=False))
    assert seen == [TickReceived, KillSwitchActivated]


async def test_nested_publish_is_fifo(t0: datetime) -> None:
    """Un événement publié par un abonné est traité après l'événement en cours."""
    bus = InMemoryEventBus()
    order: list[str] = []

    async def on_tick(e: TickReceived) -> None:
        order.append("tick:1")
        await bus.publish(KillSwitchActivated(ts=t0, reason="x", close_positions=False))
        order.append("tick:1-fin")

    bus.subscribe(TickReceived, on_tick)
    bus.subscribe(TickReceived, lambda e: order.append("tick:2"))
    bus.subscribe(KillSwitchActivated, lambda e: order.append("kill"))
    await bus.publish(tick_event(t0))
    assert order == ["tick:1", "tick:1-fin", "tick:2", "kill"]


async def test_failing_handler_is_isolated(t0: datetime, caplog: pytest.LogCaptureFixture) -> None:
    bus = InMemoryEventBus()
    seen: list[str] = []

    def boom(e: TickReceived) -> None:
        raise RuntimeError("panne")

    bus.subscribe(TickReceived, boom)
    bus.subscribe(TickReceived, lambda e: seen.append("ok"))
    await bus.publish(tick_event(t0))
    assert seen == ["ok"]
    assert bus.handler_errors == 1
    assert "panne" in caplog.text


async def test_unsubscribe(t0: datetime) -> None:
    bus = InMemoryEventBus()
    seen: list[str] = []
    unsubscribe = bus.subscribe(TickReceived, lambda e: seen.append("x"))
    unsubscribe()
    unsubscribe()  # idempotent
    await bus.publish(tick_event(t0))
    assert seen == []
