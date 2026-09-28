from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from kobr4.db.models import BotRecord, ProposalRecord
from kobr4.web.jobs import JobRunner
from kobr4.web.scheduler import LabScheduler, due
from tests.web.conftest import Env


def d(day: int, hour: int) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=UTC)  # le 3 octobre 2026 est un samedi


def test_due() -> None:
    assert not due(d(2, 12), None)  # vendredi
    assert not due(d(3, 3), None)  # samedi avant 4h
    assert due(d(3, 5), None)
    assert not due(d(3, 6), d(3, 5))  # déjà fait cette semaine
    assert due(d(4, 10), datetime(2026, 9, 26, 5, tzinfo=UTC))  # fait la semaine précédente


async def test_scheduler_creates_proposals(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    bot_id = await env.onboard()
    r = await env.patch(f"/api/bots/{bot_id}", {"auto_optimize": True})
    assert r.json()["auto_optimize"] is True

    calls: list[Any] = []

    def fake_optimize(
        settings_json: str,
        strategy_id: str,
        store: str,
        lab_json: str,
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]:
        calls.append((strategy_id, start, end))
        return {"id": "auto-1", "strategy_id": strategy_id, "status": "failed"}

    jobs = JobRunner(ThreadPoolExecutor(1), ThreadPoolExecutor(1))
    monkeypatch.setattr("kobr4.web.scheduler.optimize_job", fake_optimize)
    try:
        s = LabScheduler(
            env.db, env.supervisor.box, jobs, str(env.config.data_dir), clock=lambda: d(3, 6)
        )
        assert await s.tick() == 1
        for t in list(jobs.tasks):
            await t
        assert await s.tick() == 0  # une seule fois par semaine
    finally:
        jobs.shutdown()
    assert calls[0][0] == "ema"
    async with env.db.session() as db:
        assert (await db.scalars(select(ProposalRecord))).one().bot_id == bot_id
        assert (await db.get(BotRecord, bot_id)).last_auto_optimize_at is not None  # type: ignore[union-attr]
