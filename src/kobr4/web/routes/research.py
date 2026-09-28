"""Backtests, laboratoire et historique disponible, depuis le site."""

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from kobr4.config.settings import Settings, StrategySettings
from kobr4.db.models import BacktestRun, BotRecord, ProposalRecord
from kobr4.lab.optimizer import LabSettings
from kobr4.lab.proposals import Proposal, ProposalStatus
from kobr4.marketdata.store import ParquetBarStore
from kobr4.web.deps import Current, DbSession, State, audit
from kobr4.web.jobs import backtest_job, optimize_job
from kobr4.web.routes.account import bot_config

router = APIRouter(prefix="/api", tags=["recherche"])


def _day(d: date | None, end: bool = False) -> datetime | None:
    if d is None:
        return None
    return datetime.combine(d + timedelta(days=1) if end else d, time(), UTC)


@router.get("/data/coverage")
async def coverage(st: State, a: Current) -> list[dict[str, Any]]:
    store = ParquetBarStore(st.config.data_dir)
    out = []
    for sym in store.symbols():
        c = store.coverage(sym)
        if c:
            out.append(
                {
                    "symbol": sym,
                    "first": c.first.isoformat(),
                    "last": c.last.isoformat(),
                    "rows": c.rows,
                }
            )
    return out


class BacktestIn(BaseModel):
    bot_id: str | None = None
    instruments: list[str] | None = None
    risk: dict[str, Any] | None = None
    strategies: list[dict[str, Any]] | None = None
    start: date | None = None
    end: date | None = None
    initial_balance: float = Field(default=10_000, gt=0)


def run_out(r: BacktestRun) -> dict[str, Any]:
    return {
        "id": r.id,
        "created_at": r.created_at.isoformat(),
        "status": r.status,
        "config": r.config,
        "result": r.result,
        "error": r.error,
    }


@router.post("/backtests", status_code=202)
async def create_backtest(
    body: BacktestIn, request: Request, st: State, s: DbSession, a: Current
) -> dict[str, Any]:
    base: dict[str, Any] = {}
    if body.bot_id:
        b = await s.get(BotRecord, body.bot_id)
        if b is None or b.user_id != a.user.id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "bot introuvable")
        base = dict(b.config)
    try:
        cfg = bot_config(
            body.instruments or base.get("instruments") or [],
            body.risk if body.risk is not None else base.get("risk", {}),
            body.strategies or base.get("strategies") or [],
            base.get("base_currency", a.user.base_currency),
        )
    except ValueError as e:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"configuration invalide : {e}"
        ) from e
    settings = Settings.model_validate(
        {
            **cfg,
            "mode": "backtest",
            "broker": {"name": "simulated"},
            "backtest": {"initial_balance": body.initial_balance},
        }
    )
    run = BacktestRun(
        user_id=a.user.id,
        status="running",
        config={
            **cfg,
            "start": body.start.isoformat() if body.start else None,
            "end": body.end.isoformat() if body.end else None,
            "bot_id": body.bot_id,
        },
    )
    s.add(run)
    await s.commit()
    run_id = run.id

    async def done(result: dict[str, Any] | None, error: str | None) -> None:
        async with st.db.session() as s2:
            r = await s2.get(BacktestRun, run_id)
            if r is not None:
                r.status = "failed" if error else "done"
                r.result = result
                r.error = error
                await s2.commit()

    st.jobs.submit(
        st.jobs.backtests,
        backtest_job,
        settings.model_dump_json(),
        str(st.config.data_dir),
        _day(body.start),
        _day(body.end, end=True),
        done=done,
    )
    return run_out(run)


@router.get("/backtests")
async def list_backtests(s: DbSession, a: Current) -> list[dict[str, Any]]:
    rows = await s.scalars(
        select(BacktestRun)
        .where(BacktestRun.user_id == a.user.id)
        .order_by(BacktestRun.created_at.desc())
        .limit(50)
    )
    return [
        {**run_out(r), "result": {"metrics": r.result["metrics"]} if r.result else None}
        for r in rows
    ]


@router.get("/backtests/{run_id}")
async def get_backtest(run_id: str, s: DbSession, a: Current) -> dict[str, Any]:
    r = await s.get(BacktestRun, run_id)
    if r is None or r.user_id != a.user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "backtest introuvable")
    return run_out(r)


# Laboratoire


class OptimizeIn(BaseModel):
    bot_id: str
    strategy_id: str
    start: date
    end: date
    train_months: int = Field(default=24, ge=1, le=120)
    test_months: int = Field(default=6, ge=1, le=36)
    holdout_months: int = Field(default=12, ge=0, le=36)
    trials: int = Field(default=60, ge=4, le=500)
    objective: str = Field(default="sharpe", pattern="^(sharpe|calmar|profit_factor)$")


@router.post("/lab/optimize", status_code=202)
async def optimize(
    body: OptimizeIn, request: Request, st: State, s: DbSession, a: Current
) -> dict[str, Any]:
    b = await s.get(BotRecord, body.bot_id)
    if b is None or b.user_id != a.user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "bot introuvable")
    if not any(x["id"] == body.strategy_id for x in b.config["strategies"]):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "stratégie introuvable dans ce bot")
    settings = Settings.model_validate(
        {**b.config, "mode": "backtest", "broker": {"name": "simulated"}}
    )
    lab = LabSettings(
        train_months=body.train_months,
        test_months=body.test_months,
        holdout_months=body.holdout_months,
        trials=body.trials,
        objective=body.objective,
    )
    user_id, bot_id = a.user.id, b.id

    async def done(result: dict[str, Any] | None, error: str | None) -> None:
        if result is None:
            return
        async with st.db.session() as s2:
            s2.add(
                ProposalRecord(
                    id=result["id"],
                    user_id=user_id,
                    bot_id=bot_id,
                    strategy_id=result["strategy_id"],
                    status=result["status"],
                    payload=result,
                )
            )
            await s2.commit()

    await audit(s, request, a.user.id, "lab_optimize", bot=b.id, strategy=body.strategy_id)
    await s.commit()
    st.jobs.submit(
        st.jobs.lab,
        optimize_job,
        settings.model_dump_json(),
        body.strategy_id,
        str(st.config.data_dir),
        lab.model_dump_json(),
        _day(body.start),
        _day(body.end, end=True),
        done=done,
    )
    return {"queued": True}


@router.get("/proposals")
async def proposals(s: DbSession, a: Current) -> list[dict[str, Any]]:
    rows = await s.scalars(
        select(ProposalRecord)
        .where(ProposalRecord.user_id == a.user.id)
        .order_by(ProposalRecord.created_at.desc())
    )
    return [{**r.payload, "status": r.status, "bot_id": r.bot_id} for r in rows]


class DecisionIn(BaseModel):
    approve: bool
    note: str = Field(default="", max_length=500)


@router.post("/proposals/{proposal_id}/decision")
async def decide(
    proposal_id: str, body: DecisionIn, request: Request, s: DbSession, a: Current
) -> dict[str, Any]:
    r = await s.get(ProposalRecord, proposal_id)
    if r is None or r.user_id != a.user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "proposition introuvable")
    if r.status != ProposalStatus.PROPOSED.value:
        raise HTTPException(status.HTTP_409_CONFLICT, f"proposition déjà traitée ({r.status})")
    r.status = (ProposalStatus.APPROVED if body.approve else ProposalStatus.REJECTED).value
    r.payload = {
        **r.payload,
        "status": r.status,
        "decided_by": a.user.email,
        "decided_at": datetime.now(UTC).isoformat(),
        "note": body.note,
    }
    await audit(
        s, request, a.user.id, "proposal_decision", proposal=proposal_id, approve=body.approve
    )
    await s.commit()
    return {**r.payload, "bot_id": r.bot_id}


@router.post("/proposals/{proposal_id}/apply")
async def apply_proposal(
    proposal_id: str, request: Request, st: State, s: DbSession, a: Current
) -> dict[str, Any]:
    r = await s.get(ProposalRecord, proposal_id)
    if r is None or r.user_id != a.user.id or r.bot_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "proposition introuvable")
    if r.status != ProposalStatus.APPROVED.value:
        raise HTTPException(status.HTTP_409_CONFLICT, "accepte d'abord la proposition")
    b = await s.get(BotRecord, r.bot_id)
    if b is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "bot introuvable")
    if st.supervisor.get(b.id) is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "arrête le bot avant d'appliquer de nouveaux réglages"
        )
    p = Proposal.model_validate(r.payload)
    strategies = []
    for raw in b.config["strategies"]:
        st_ = StrategySettings.model_validate(raw)
        if st_.id == p.strategy_id:
            try:
                st_ = p.apply_to(st_)
            except ValueError as e:
                raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
        strategies.append(st_.model_dump(mode="json"))
    b.config = {**b.config, "strategies": strategies}
    r.status = ProposalStatus.APPLIED.value
    r.payload = {**r.payload, "status": r.status}
    await audit(s, request, a.user.id, "proposal_applied", proposal=proposal_id, bot=b.id)
    await s.commit()
    return {"ok": True, "bot_id": b.id, "mode": b.mode}
