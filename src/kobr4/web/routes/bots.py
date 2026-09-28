"""Bots : création, réglages, marche/arrêt, arrêt d'urgence, historique, passage en réel."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from kobr4.core.instruments import INSTRUMENTS
from kobr4.core.types import Timeframe
from kobr4.db.models import BotEvent, BotRecord, BrokerConnection, EquityPoint, TradeRecord
from kobr4.live.bot import LiveBot
from kobr4.marketdata.store import ParquetBarStore
from kobr4.web.chart import aggregate, merge
from kobr4.web.deps import Auth, Current, DbSession, State, audit
from kobr4.web.routes.account import bot_config
from kobr4.web.security import verify_password, verify_totp

router = APIRouter(prefix="/api/bots", tags=["bots"])

LIVE_CONFIRMATION = "PASSER EN RÉEL"


def bot_out(b: BotRecord, live: LiveBot | None) -> dict[str, Any]:
    return {
        "id": b.id,
        "name": b.name,
        "mode": b.mode,
        "status": live.state.value if live else b.status,
        "desired_state": b.desired_state,
        "broker_connection_id": b.broker_connection_id,
        "config": b.config,
        "last_error": (live.error if live and live.error else None) or b.last_error,
        "live_confirmed_at": b.live_confirmed_at.isoformat() if b.live_confirmed_at else None,
        "auto_optimize": b.auto_optimize,
        "last_auto_optimize_at": b.last_auto_optimize_at.isoformat()
        if b.last_auto_optimize_at
        else None,
        "created_at": b.created_at.isoformat(),
        "updated_at": b.updated_at.isoformat(),
    }


async def _own(s: DbSession, a: Auth, bot_id: str) -> BotRecord:
    b = await s.get(BotRecord, bot_id)
    if b is None or b.user_id != a.user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "bot introuvable")
    return b


def _running(st: State, bot_id: str) -> LiveBot:
    bot = st.supervisor.get(bot_id)
    if bot is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "le bot n'est pas en marche")
    return bot


@router.get("")
async def list_bots(st: State, s: DbSession, a: Current) -> list[dict[str, Any]]:
    rows = await s.scalars(
        select(BotRecord).where(BotRecord.user_id == a.user.id).order_by(BotRecord.created_at)
    )
    return [bot_out(b, st.supervisor.get(b.id)) for b in rows]


class BotIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    broker_connection_id: str
    instruments: list[str] = Field(min_length=1)
    risk: dict[str, Any] = Field(default_factory=dict)
    strategies: list[dict[str, Any]] = Field(min_length=1)


@router.post("", status_code=201)
async def create_bot(
    body: BotIn, request: Request, st: State, s: DbSession, a: Current
) -> dict[str, Any]:
    conn = await s.get(BrokerConnection, body.broker_connection_id)
    if conn is None or conn.user_id != a.user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "connexion courtier introuvable")
    if conn.environment != "practice":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "un nouveau bot démarre toujours sur un compte démo",
        )
    try:
        cfg = bot_config(
            body.instruments,
            body.risk,
            body.strategies,
            conn.account_currency or a.user.base_currency,
        )
    except ValueError as e:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"configuration invalide : {e}"
        ) from e
    b = BotRecord(
        user_id=a.user.id, broker_connection_id=conn.id, name=body.name, mode="paper", config=cfg
    )
    s.add(b)
    await s.flush()
    await audit(s, request, a.user.id, "bot_created", bot=b.id)
    await s.commit()
    return bot_out(b, None)


@router.get("/{bot_id}")
async def get_bot(bot_id: str, st: State, s: DbSession, a: Current) -> dict[str, Any]:
    b = await _own(s, a, bot_id)
    live = st.supervisor.get(bot_id)
    return {**bot_out(b, live), "live": live.status() if live else None}


class BotUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    instruments: list[str] | None = None
    risk: dict[str, Any] | None = None
    strategies: list[dict[str, Any]] | None = None
    auto_optimize: bool | None = None


@router.patch("/{bot_id}")
async def update_bot(
    bot_id: str, body: BotUpdate, request: Request, st: State, s: DbSession, a: Current
) -> dict[str, Any]:
    b = await _own(s, a, bot_id)
    if body.name is not None:
        b.name = body.name
    if body.auto_optimize is not None:
        b.auto_optimize = body.auto_optimize
        await audit(s, request, a.user.id, "auto_optimize", bot=bot_id, enabled=body.auto_optimize)
    if body.instruments is not None or body.risk is not None or body.strategies is not None:
        if st.supervisor.get(bot_id) is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "arrête le bot avant de changer ses réglages"
            )
        cfg = b.config
        try:
            b.config = bot_config(
                body.instruments or cfg["instruments"],
                body.risk if body.risk is not None else cfg["risk"],
                body.strategies or cfg["strategies"],
                cfg.get("base_currency", "USD"),
            )
        except ValueError as e:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"configuration invalide : {e}"
            ) from e
        await audit(s, request, a.user.id, "bot_config_changed", bot=bot_id)
    await s.commit()
    return bot_out(b, st.supervisor.get(bot_id))


@router.delete("/{bot_id}")
async def delete_bot(
    bot_id: str, request: Request, st: State, s: DbSession, a: Current
) -> dict[str, bool]:
    b = await _own(s, a, bot_id)
    if st.supervisor.get(bot_id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "arrête le bot avant de le supprimer")
    await s.delete(b)
    await audit(s, request, a.user.id, "bot_deleted", bot=bot_id)
    await s.commit()
    return {"ok": True}


# Commandes


@router.post("/{bot_id}/start")
async def start_bot(
    bot_id: str, request: Request, st: State, s: DbSession, a: Current
) -> dict[str, Any]:
    b = await _own(s, a, bot_id)
    await audit(s, request, a.user.id, "bot_start", bot=bot_id, mode=b.mode)
    await s.commit()
    try:
        bot = await st.supervisor.start(bot_id)
    except Exception as e:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"démarrage impossible : {e}"
        ) from e
    return bot.status()


@router.post("/{bot_id}/stop")
async def stop_bot(
    bot_id: str, request: Request, st: State, s: DbSession, a: Current
) -> dict[str, bool]:
    await _own(s, a, bot_id)
    await audit(s, request, a.user.id, "bot_stop", bot=bot_id)
    await s.commit()
    await st.supervisor.stop(bot_id)
    return {"ok": True}


class KillIn(BaseModel):
    close_positions: bool = True
    reason: str = Field(default="arrêt manuel", max_length=200)


@router.post("/{bot_id}/kill")
async def kill_bot(
    bot_id: str, body: KillIn, request: Request, st: State, s: DbSession, a: Current
) -> dict[str, Any]:
    await _own(s, a, bot_id)
    bot = _running(st, bot_id)
    await audit(
        s, request, a.user.id, "kill_switch", bot=bot_id, close_positions=body.close_positions
    )
    await s.commit()
    await bot.kill(body.reason, body.close_positions)
    return bot.status()


@router.post("/{bot_id}/release")
async def release_bot(
    bot_id: str, request: Request, st: State, s: DbSession, a: Current
) -> dict[str, Any]:
    await _own(s, a, bot_id)
    bot = _running(st, bot_id)
    await audit(s, request, a.user.id, "kill_switch_released", bot=bot_id)
    await s.commit()
    await bot.release("levé par l'utilisateur")
    return bot.status()


@router.post("/{bot_id}/strategies/{strategy_id}/{action}")
async def toggle_strategy(
    bot_id: str,
    strategy_id: str,
    action: str,
    request: Request,
    st: State,
    s: DbSession,
    a: Current,
) -> dict[str, Any]:
    if action not in ("pause", "resume"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "action inconnue")
    await _own(s, a, bot_id)
    bot = _running(st, bot_id)
    try:
        bot.set_strategy_enabled(strategy_id, action == "resume")
    except KeyError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "stratégie introuvable") from e
    await audit(s, request, a.user.id, f"strategy_{action}", bot=bot_id, strategy=strategy_id)
    await s.commit()
    return bot.status()


@router.post("/{bot_id}/positions/{position_id}/close")
async def close_position(
    bot_id: str, position_id: str, request: Request, st: State, s: DbSession, a: Current
) -> dict[str, Any]:
    await _own(s, a, bot_id)
    bot = _running(st, bot_id)
    if position_id not in bot.portfolio.open_positions:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "position introuvable")
    await audit(s, request, a.user.id, "position_closed_manually", bot=bot_id, position=position_id)
    await s.commit()
    await bot.close_position(position_id)
    return bot.status()


# Historique


@router.get("/{bot_id}/trades")
async def trades(
    bot_id: str, s: DbSession, a: Current, limit: int = Query(200, le=2000)
) -> list[dict[str, Any]]:
    await _own(s, a, bot_id)
    rows = await s.scalars(
        select(TradeRecord)
        .where(TradeRecord.bot_id == bot_id)
        .order_by(TradeRecord.closed_at.desc())
        .limit(limit)
    )
    return [
        {
            "position_id": t.position_id,
            "strategy_id": t.strategy_id,
            "symbol": t.symbol,
            "side": t.side,
            "quantity": t.quantity,
            "entry_price": str(t.entry_price),
            "exit_price": str(t.exit_price),
            "opened_at": t.opened_at.isoformat(),
            "closed_at": t.closed_at.isoformat(),
            "reason": t.reason,
            "pnl": str(t.pnl),
        }
        for t in rows
    ]


@router.get("/{bot_id}/equity")
async def equity(
    bot_id: str, s: DbSession, a: Current, days: int = Query(30, ge=1, le=3650)
) -> list[dict[str, Any]]:
    await _own(s, a, bot_id)
    since = datetime.now(UTC) - timedelta(days=days)
    rows = list(
        await s.scalars(
            select(EquityPoint)
            .where(EquityPoint.bot_id == bot_id, EquityPoint.ts >= since)
            .order_by(EquityPoint.ts)
        )
    )
    step = max(1, len(rows) // 1000)
    picked = rows[::step] + ([rows[-1]] if rows and (len(rows) - 1) % step else [])
    return [
        {"ts": p.ts.isoformat(), "equity": str(p.equity), "balance": str(p.balance)} for p in picked
    ]


@router.get("/{bot_id}/events")
async def events(
    bot_id: str, s: DbSession, a: Current, limit: int = Query(200, le=1000)
) -> list[dict[str, Any]]:
    await _own(s, a, bot_id)
    rows = await s.scalars(
        select(BotEvent).where(BotEvent.bot_id == bot_id).order_by(BotEvent.id.desc()).limit(limit)
    )
    return [{"ts": e.ts.isoformat(), "type": e.type, "payload": e.payload} for e in rows]


@router.get("/{bot_id}/candles")
async def candles(
    bot_id: str,
    st: State,
    s: DbSession,
    a: Current,
    symbol: str,
    timeframe: Timeframe = Timeframe.H1,
    count: int = Query(200, ge=10, le=1000),
) -> list[dict[str, Any]]:
    await _own(s, a, bot_id)
    if symbol not in INSTRUMENTS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "instrument inconnu")
    store = ParquetBarStore(st.config.data_dir)
    since = datetime.now(UTC) - timeframe.duration * count * 2 - timedelta(days=4)
    try:
        stored = await asyncio.to_thread(store.bars, symbol, timeframe, since, None)
    except Exception:
        stored = []  # fichier en cours d'écriture par un téléchargement : on réessaiera
    bot = st.supervisor.get(bot_id)
    live = aggregate(bot.market.recent_m1(symbol), timeframe) if bot is not None else []
    bars = merge(stored, live, count)
    return [
        {
            "time": int(b.open_time.timestamp()),
            "open": float(b.open),
            "high": float(b.high),
            "low": float(b.low),
            "close": float(b.close),
        }
        for b in bars
    ]


# Passage en réel


class LiveIn(BaseModel):
    broker_connection_id: str
    password: str
    code: str
    confirmation: str


async def paper_days(s: DbSession, bot_id: str) -> float:
    first = await s.scalar(select(func.min(EquityPoint.ts)).where(EquityPoint.bot_id == bot_id))
    if first is None:
        return 0.0
    return (datetime.now(UTC) - first) / timedelta(days=1)


@router.get("/{bot_id}/live-readiness")
async def live_readiness(bot_id: str, st: State, s: DbSession, a: Current) -> dict[str, Any]:
    b = await _own(s, a, bot_id)
    days = await paper_days(s, bot_id)
    trades_count = (
        await s.scalar(
            select(func.count()).select_from(TradeRecord).where(TradeRecord.bot_id == bot_id)
        )
        or 0
    )
    checks = [
        {"label": "Double authentification activée", "ok": a.user.totp_enabled},
        {
            "label": f"Au moins {st.config.min_paper_days} jours en démo",
            "ok": days >= st.config.min_paper_days,
            "value": round(days, 1),
        },
        {"label": "Au moins 20 trades en démo", "ok": trades_count >= 20, "value": trades_count},
        {"label": "Bot actuellement en démo", "ok": b.mode == "paper"},
    ]
    return {
        "ready": all(c["ok"] for c in checks),
        "checks": checks,
        "confirmation": LIVE_CONFIRMATION,
    }


@router.post("/{bot_id}/live")
async def go_live(
    bot_id: str, body: LiveIn, request: Request, st: State, s: DbSession, a: Current
) -> dict[str, Any]:
    b = await _own(s, a, bot_id)
    user = a.user
    if b.mode == "live":
        raise HTTPException(status.HTTP_409_CONFLICT, "ce bot est déjà en réel")
    if not user.totp_enabled or user.totp_secret is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "double authentification obligatoire")
    if body.confirmation.strip() != LIVE_CONFIRMATION:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"recopie exactement : {LIVE_CONFIRMATION}"
        )
    if not verify_password(user.password_hash, body.password) or not verify_totp(
        st.box.decrypt(user.totp_secret, context=user.id), body.code
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "mot de passe ou code incorrect")
    readiness = await live_readiness(bot_id, st, s, a)
    if not readiness["ready"]:
        missing = ", ".join(c["label"] for c in readiness["checks"] if not c["ok"])
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"conditions non remplies : {missing}")
    conn = await s.get(BrokerConnection, body.broker_connection_id)
    if conn is None or conn.user_id != user.id or conn.environment != "live":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "choisis une connexion à un compte réel"
        )
    was_running = st.supervisor.get(bot_id) is not None
    if was_running:
        await st.supervisor.stop(bot_id)
    b.mode = "live"
    b.broker_connection_id = conn.id
    b.live_confirmed_at = datetime.now(UTC)
    await audit(s, request, user.id, "bot_live", bot=bot_id, account=conn.account_id)
    await s.commit()
    return bot_out(b, None)


@router.post("/{bot_id}/paper")
async def back_to_paper(
    bot_id: str, body: dict[str, str], request: Request, st: State, s: DbSession, a: Current
) -> dict[str, Any]:
    """Retour en démo : toujours possible, sans condition (les positions réelles restent
    ouvertes chez le courtier avec leurs stops)."""
    b = await _own(s, a, bot_id)
    conn = await s.get(BrokerConnection, body.get("broker_connection_id", ""))
    if conn is None or conn.user_id != a.user.id or conn.environment != "practice":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "choisis une connexion à un compte démo"
        )
    if st.supervisor.get(bot_id) is not None:
        await st.supervisor.stop(bot_id)
    b.mode = "paper"
    b.broker_connection_id = conn.id
    b.live_confirmed_at = None
    await audit(s, request, a.user.id, "bot_paper", bot=bot_id)
    await s.commit()
    return bot_out(b, None)
