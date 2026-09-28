from datetime import UTC, datetime, timedelta

import pytest

from kobr4.core.clock import LiveClock, SimulatedClock


def test_live_clock_is_utc() -> None:
    assert LiveClock().now().utcoffset() == timedelta(0)


def test_simulated_clock(t0: datetime) -> None:
    clock = SimulatedClock(t0)
    clock.advance(timedelta(hours=1))
    assert clock.now() == t0 + timedelta(hours=1)
    with pytest.raises(ValueError, match="reculer"):
        clock.set(t0)


def test_simulated_clock_rejects_naive() -> None:
    with pytest.raises(ValueError, match="fuseau"):
        SimulatedClock(datetime(2026, 1, 1))  # noqa: DTZ001
    clock = SimulatedClock(datetime(2026, 1, 1, tzinfo=UTC))
    with pytest.raises(ValueError, match="fuseau"):
        clock.set(datetime(2026, 1, 2))  # noqa: DTZ001
