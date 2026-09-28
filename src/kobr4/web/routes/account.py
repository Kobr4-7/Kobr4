"""Profil, parcours d'inscription, connexions courtier, notifications."""

import json
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from kobr4.config.settings import RiskSettings, Settings, StrategySettings
from kobr4.db.models import BotRecord, BrokerConnection, NotificationSettings
from kobr4.execution.brokers.paper import FINNHUB_API
from kobr4.live.alerts import TelegramNotifier
from kobr4.strategies import create_strategy
from kobr4.web.brokers import BrokerCheckError, oanda_accounts, verify_oanda
from kobr4.web.catalog import catalog
from kobr4.web.deps import Current, DbSession, State, audit
from kobr4.web.routes.auth import user_out

router = APIRouter(prefix="/api", tags=["compte"])

STEPS = ["profile", "security", "broker", "notifications", "setup", "done"]


class ProfileIn(BaseModel):
    display_name: str | None = Field(default=None, max_length=80)
    base_currency: str | None = Field(default=None, pattern="^[A-Z]{3}$")
    timezone: str | None = Field(default=None, max_length=64)


@router.get("/catalog")
async def get_catalog() -> dict[str, Any]:
    return catalog()


@router.patch("/profile")
async def update_profile(body: ProfileIn, s: DbSession, a: Current) -> dict[str, object]:
    u = a.user
    if body.display_name is not None:
        u.display_name = body.display_name.strip()
    if body.base_currency is not None:
        u.base_currency = body.base_currency
    if body.timezone is not None:
        u.timezone = body.timezone
    if u.onboarding_step == "profile":
        u.onboarding_step = "broker" if u.totp_enabled else "security"
    await s.commit()
    return user_out(u)


# Courtier


class TokenIn(BaseModel):
    broker: Literal["oanda"] = "oanda"
    environment: Literal["practice", "live"] = "practice"
    token: str = Field(min_length=10, max_length=200)


class BrokerIn(TokenIn):
    account_id: str = Field(min_length=3, max_length=64)
    label: str = Field(default="", max_length=80)


def broker_out(c: BrokerConnection) -> dict[str, object]:
    return {
        "id": c.id,
        "broker": c.broker,
        "environment": c.environment,
        "account_id": c.account_id,
        "label": c.label,
        "account_currency": c.account_currency,
        "verified_at": c.verified_at.isoformat() if c.verified_at else None,
    }


def _require_mfa(a: Current) -> None:
    if not a.user.totp_enabled:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "active d'abord la double authentification pour protéger l'accès à ton courtier",
        )


@router.post("/brokers/accounts")
async def list_broker_accounts(body: TokenIn, st: State, a: Current) -> list[dict[str, Any]]:
    """Comptes accessibles avec un jeton, pour que l'utilisateur choisisse le bon."""
    _require_mfa(a)
    try:
        return await oanda_accounts(body.token.strip(), body.environment, st.broker_transport)
    except BrokerCheckError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e


class DemoIn(BaseModel):
    finnhub_key: str = Field(min_length=10, max_length=100)
    initial_balance: int = Field(default=10_000, ge=1_000, le=1_000_000)
    currency: Literal["USD", "EUR"] = "USD"
    label: str = Field(default="", max_length=80)


async def check_finnhub_key(key: str, transport: Any = None) -> None:
    """Vérifie la clé Finnhub par une requête gratuite (cotation d'une action US)."""
    async with httpx.AsyncClient(timeout=10, transport=transport) as c:
        try:
            resp = await c.get(f"{FINNHUB_API}/quote", params={"symbol": "AAPL", "token": key})
        except httpx.HTTPError as e:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, f"Finnhub injoignable ({e.__class__.__name__})"
            ) from e
    if resp.status_code in (401, 403):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "clé Finnhub refusée : copie-la à nouveau depuis ton tableau de bord Finnhub",
        )
    if resp.status_code >= 400:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Finnhub a répondu HTTP {resp.status_code}"
        )


@router.post("/brokers/demo", status_code=201)
async def add_demo_account(
    body: DemoIn, request: Request, st: State, s: DbSession, a: Current
) -> dict[str, object]:
    """Compte démo interne : argent fictif, prix réels fournis par Finnhub."""
    _require_mfa(a)
    key = body.finnhub_key.strip()
    await check_finnhub_key(key, st.broker_transport)
    secret = json.dumps({"finnhub_key": key, "initial_balance": body.initial_balance})
    conn = BrokerConnection(
        user_id=a.user.id,
        broker="paper",
        environment="practice",
        account_id="demo",
        token_encrypted=st.box.encrypt(secret, context=a.user.id),
        label=body.label or "Compte démo",
        account_currency=body.currency,
        verified_at=datetime.now(UTC),
    )
    s.add(conn)
    if a.user.onboarding_step == "broker":
        a.user.onboarding_step = "notifications"
    await s.flush()
    conn.account_id = f"demo-{conn.id[:8]}"
    await audit(s, request, a.user.id, "broker_added", broker="paper", environment="practice")
    await s.commit()
    return broker_out(conn)


@router.get("/brokers")
async def list_brokers(s: DbSession, a: Current) -> list[dict[str, object]]:
    rows = await s.scalars(select(BrokerConnection).where(BrokerConnection.user_id == a.user.id))
    return [broker_out(c) for c in rows]


@router.post("/brokers", status_code=201)
async def add_broker(
    body: BrokerIn, request: Request, st: State, s: DbSession, a: Current
) -> dict[str, object]:
    _require_mfa(a)
    token = body.token.strip()
    try:
        info = await verify_oanda(
            token, body.account_id.strip(), body.environment, st.broker_transport
        )
    except BrokerCheckError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    conn = BrokerConnection(
        user_id=a.user.id,
        broker=body.broker,
        environment=body.environment,
        account_id=body.account_id.strip(),
        token_encrypted=st.box.encrypt(token, context=a.user.id),
        label=body.label
        or info.get("alias")
        or ("Compte démo" if body.environment == "practice" else "Compte réel"),
        account_currency=info["currency"],
        verified_at=datetime.now(UTC),
    )
    s.add(conn)
    if a.user.onboarding_step == "broker":
        a.user.onboarding_step = "notifications"
    await s.flush()
    await audit(
        s,
        request,
        a.user.id,
        "broker_added",
        broker=body.broker,
        environment=body.environment,
        account=conn.account_id,
    )
    await s.commit()
    return broker_out(conn)


@router.delete("/brokers/{conn_id}")
async def delete_broker(
    conn_id: str, request: Request, s: DbSession, a: Current
) -> dict[str, bool]:
    conn = await s.get(BrokerConnection, conn_id)
    if conn is None or conn.user_id != a.user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "connexion introuvable")
    used = await s.scalar(
        select(BotRecord.id).where(
            BotRecord.broker_connection_id == conn_id, BotRecord.status != "stopped"
        )
    )
    if used:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "un bot en marche utilise cette connexion : arrête-le d'abord"
        )
    await s.delete(conn)
    await audit(s, request, a.user.id, "broker_removed", account=conn.account_id)
    await s.commit()
    return {"ok": True}


# Notifications


class NotificationsIn(BaseModel):
    telegram_token: str | None = Field(default=None, max_length=100)
    telegram_chat_id: str | None = Field(default=None, max_length=64)
    notify_trades: bool = True
    notify_risk: bool = True
    notify_daily_summary: bool = True
    clear_telegram: bool = False


def notif_out(n: NotificationSettings | None) -> dict[str, object]:
    if n is None:
        return {
            "telegram": False,
            "telegram_chat_id": None,
            "notify_trades": True,
            "notify_risk": True,
            "notify_daily_summary": True,
        }
    return {
        "telegram": bool(n.telegram_token_encrypted and n.telegram_chat_id),
        "telegram_chat_id": n.telegram_chat_id,
        "notify_trades": n.notify_trades,
        "notify_risk": n.notify_risk,
        "notify_daily_summary": n.notify_daily_summary,
    }


@router.get("/notifications")
async def get_notifications(s: DbSession, a: Current) -> dict[str, object]:
    return notif_out(await s.get(NotificationSettings, a.user.id))


@router.put("/notifications")
async def put_notifications(
    body: NotificationsIn, st: State, s: DbSession, a: Current
) -> dict[str, object]:
    n = await s.get(NotificationSettings, a.user.id)
    if n is None:
        n = NotificationSettings(user_id=a.user.id)
        s.add(n)
    if body.clear_telegram:
        n.telegram_token_encrypted = None
        n.telegram_chat_id = None
    if body.telegram_token:
        n.telegram_token_encrypted = st.box.encrypt(body.telegram_token.strip(), context=a.user.id)
    if body.telegram_chat_id:
        n.telegram_chat_id = body.telegram_chat_id.strip()
    n.notify_trades = body.notify_trades
    n.notify_risk = body.notify_risk
    n.notify_daily_summary = body.notify_daily_summary
    if a.user.onboarding_step == "notifications":
        a.user.onboarding_step = "setup"
    await s.commit()
    return notif_out(n)


@router.post("/notifications/test")
async def test_notification(st: State, s: DbSession, a: Current) -> dict[str, bool]:
    n = await s.get(NotificationSettings, a.user.id)
    if n is None or not n.telegram_token_encrypted or not n.telegram_chat_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "renseigne d'abord le jeton et l'identifiant de chat Telegram",
        )
    notifier = TelegramNotifier(
        st.box.decrypt(n.telegram_token_encrypted, context=a.user.id), n.telegram_chat_id
    )
    try:
        await notifier.send("✅ Kobr4 FX : les alertes arrivent bien ici.")
    except Exception as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"envoi impossible : {e}") from e
    return {"ok": True}


@router.post("/onboarding/skip-notifications")
async def skip_notifications(s: DbSession, a: Current) -> dict[str, object]:
    if a.user.onboarding_step == "notifications":
        a.user.onboarding_step = "setup"
        await s.commit()
    return user_out(a.user)


# Fin du parcours : premier bot


class FirstBotIn(BaseModel):
    name: str = Field(default="Mon premier bot", min_length=1, max_length=80)
    broker_connection_id: str
    instruments: list[str] = Field(min_length=1)
    risk: dict[str, Any] = Field(default_factory=dict)
    strategies: list[dict[str, Any]] = Field(min_length=1)
    start: bool = True


def bot_config(
    instruments: list[str],
    risk: dict[str, Any],
    strategies: list[dict[str, Any]],
    base_currency: str,
) -> dict[str, Any]:
    """Valide et normalise la configuration d'un bot (lève ValueError sinon)."""
    r = RiskSettings.model_validate(risk)
    sts = [StrategySettings.model_validate(x) for x in strategies]
    for x in sts:
        create_strategy(x)  # valide les paramètres
    cfg = {
        "base_currency": base_currency,
        "instruments": instruments,
        "risk": r.model_dump(mode="json"),
        "strategies": [x.model_dump(mode="json") for x in sts],
    }
    Settings.model_validate(
        {**cfg, "mode": "paper", "broker": {"name": "oanda", "environment": "practice"}}
    )
    return cfg


@router.post("/onboarding/finish", status_code=201)
async def finish_onboarding(
    body: FirstBotIn, request: Request, st: State, s: DbSession, a: Current
) -> dict[str, object]:
    _require_mfa(a)
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
    bot = BotRecord(
        user_id=a.user.id, broker_connection_id=conn.id, name=body.name, mode="paper", config=cfg
    )
    s.add(bot)
    a.user.onboarding_step = "done"
    await s.flush()
    await audit(s, request, a.user.id, "bot_created", bot=bot.id)
    await s.commit()
    started_error = None
    if body.start:
        try:
            await st.supervisor.start(bot.id)
        except Exception as e:
            started_error = str(e)
    return {"bot_id": bot.id, "start_error": started_error, "user": user_out(a.user)}
