import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from kobr4.db import Database
from kobr4.db.models import Base

PG_URL = os.environ.get("KOBR4_TEST_PG_URL")
"""Ex. postgresql+asyncpg://kobr4@127.0.0.1:55432/kobr4_test : active les tests sur PostgreSQL."""


@pytest.fixture
def t0() -> datetime:
    return datetime(2026, 9, 28, 8, 0, tzinfo=UTC)


@pytest.fixture(params=["sqlite", "postgres"])
async def db(request: pytest.FixtureRequest) -> AsyncIterator[Database]:
    if request.param == "postgres":
        if not PG_URL:
            pytest.skip("KOBR4_TEST_PG_URL non défini")
        d = Database(PG_URL)
        async with d.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    else:
        d = Database("sqlite+aiosqlite:///:memory:")
    await d.create_all()
    yield d
    await d.close()
