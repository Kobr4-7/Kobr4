from datetime import UTC, datetime

import pytest


@pytest.fixture
def t0() -> datetime:
    return datetime(2026, 9, 28, 8, 0, tzinfo=UTC)
