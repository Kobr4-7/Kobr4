import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pyotp
from sqlalchemy import select

from kobr4.db.models import AuditEntry, BrokerConnection, ProposalRecord, User
from tests.web.conftest import PASSWORD, Env, first_bot


async def test_onboarding_steps(env: Env) -> None:
    r = await env.signup()
    assert r.json()["onboarding_step"] == "profile"
    r = await env.patch("/api/profile", {"display_name": "Moi", "timezone": "Europe/Paris"})
    assert r.json()["onboarding_step"] == "security"
    # Pas de courtier sans double authentification.
    r = await env.post(
        "/api/brokers", {"token": "tok-valide-123", "account_id": env.fake.account_id}
    )
    assert r.status_code == 403
    await env.enable_mfa()
    assert (await env.client.get("/api/auth/me")).json()["onboarding_step"] == "broker"
    r = await env.post("/api/brokers/accounts", {"token": "tok-valide-123"})
    assert r.status_code == 200
    assert r.json() == [
        {"id": env.fake.account_id, "alias": "", "currency": "USD", "balance": "10000"}
    ]
    r = await env.post(
        "/api/brokers", {"token": "mauvais-jeton-000", "account_id": env.fake.account_id}
    )
    assert r.status_code == 422
    assert "jeton refusé" in r.json()["detail"]
    r = await env.post(
        "/api/brokers", {"token": "tok-valide-123", "account_id": env.fake.account_id}
    )
    assert r.status_code == 201
    conn_id = r.json()["id"]
    assert r.json()["account_currency"] == "USD"
    assert "tok" not in json.dumps(r.json())
    r = await env.put("/api/notifications", {"telegram_token": "123:abc", "telegram_chat_id": "42"})
    assert r.json()["telegram"] is True
    assert (await env.client.get("/api/auth/me")).json()["onboarding_step"] == "setup"
    r = await env.post("/api/onboarding/finish", first_bot(conn_id))
    assert r.status_code == 201, r.text
    assert r.json()["start_error"] is None
    assert r.json()["user"]["onboarding_step"] == "done"
    bot_id = r.json()["bot_id"]
    r = await env.client.get(f"/api/bots/{bot_id}")
    assert r.json()["status"] == "running"
    assert r.json()["live"]["mode"] == "paper"
    # Le jeton du courtier est chiffré en base.
    async with env.db.session() as s:
        conn = (await s.scalars(select(BrokerConnection))).one()
        assert "tok-valide-123" not in conn.token_encrypted
        actions = [a.action for a in (await s.scalars(select(AuditEntry))).all()]
    assert {"signup", "mfa_enabled", "broker_added", "bot_created"} <= set(actions)


async def test_bot_commands(env: Env) -> None:
    bot_id = await env.onboard()
    assert (
        await env.post(f"/api/bots/{bot_id}/kill", {"close_positions": True})
    ).status_code == 409
    r = await env.post(f"/api/bots/{bot_id}/start")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "running"
    r = await env.post(f"/api/bots/{bot_id}/strategies/ema/pause")
    assert r.json()["strategies"][0]["enabled"] is False
    r = await env.patch(f"/api/bots/{bot_id}", {"risk": {"risk_per_trade_pct": 2}})
    assert r.status_code == 409  # pas de changement de réglages en marche
    r = await env.post(f"/api/bots/{bot_id}/kill", {"close_positions": False, "reason": "test"})
    assert r.json()["kill_switch"] == "test"
    r = await env.post(f"/api/bots/{bot_id}/release")
    assert r.json()["kill_switch"] is None
    assert (await env.post(f"/api/bots/{bot_id}/stop")).status_code == 200
    r = await env.patch(
        f"/api/bots/{bot_id}", {"risk": {"risk_per_trade_pct": 2, "max_daily_loss_pct": 4}}
    )
    assert r.status_code == 200
    assert r.json()["config"]["risk"]["risk_per_trade_pct"] == "2"
    r = await env.patch(f"/api/bots/{bot_id}", {"risk": {"risk_per_trade_pct": 9}})
    assert r.status_code == 422
    assert (await env.client.get(f"/api/bots/{bot_id}/trades")).json() == []


async def test_users_are_isolated(env: Env) -> None:
    bot_id = await env.onboard()
    env.client.cookies.clear()
    await env.signup("autre@example.com", invite_code="bienvenue")
    assert (await env.client.get(f"/api/bots/{bot_id}")).status_code == 404
    assert (await env.post(f"/api/bots/{bot_id}/start")).status_code == 404
    assert (await env.client.get("/api/bots")).json() == []


async def test_live_switch_guarded(env: Env) -> None:
    bot_id = await env.onboard()
    r = await env.client.get(f"/api/bots/{bot_id}/live-readiness")
    assert r.json()["ready"] is False
    async with env.db.session() as s:
        user = (await s.scalars(select(User))).one()
        secret = env.config and user.totp_secret
    assert secret
    body = {
        "broker_connection_id": "x",
        "password": PASSWORD,
        "code": "000000",
        "confirmation": "oui",
    }
    r = await env.post(f"/api/bots/{bot_id}/live", body)
    assert r.status_code == 422  # phrase de confirmation
    body["confirmation"] = "PASSER EN RÉEL"
    r = await env.post(f"/api/bots/{bot_id}/live", body)
    assert r.status_code == 401  # code faux


async def test_backtest_from_site(env: Env) -> None:
    bot_id = await env.onboard()
    r = await env.post(
        "/api/backtests", {"bot_id": bot_id, "start": "2024-01-01", "end": "2024-03-31"}
    )
    assert r.status_code == 202, r.text
    run_id = r.json()["id"]
    for _ in range(1200):  # jusqu'à 60 s sur une machine chargée
        r = await env.client.get(f"/api/backtests/{run_id}")
        if r.json()["status"] != "running":
            break
        await asyncio.sleep(0.05)
    data = r.json()
    assert data["status"] == "done", data["error"]
    assert data["result"]["synthetic"] is True
    assert data["result"]["metrics"]["trades"] >= 0
    assert data["result"]["equity"]
    assert (await env.client.get("/api/backtests")).json()[0]["id"] == run_id


async def test_proposal_decision_and_apply(env: Env) -> None:
    bot_id = await env.onboard()
    async with env.db.session() as s:
        user = (await s.scalars(select(User))).one()
        payload: dict[str, Any] = {
            "id": "p1",
            "created_at": datetime.now(UTC).isoformat(),
            "strategy_id": "ema",
            "kind": "ema_cross",
            "base_version": 1,
            "current_params": {"fast": 10, "slow": 30},
            "proposed_params": {"fast": 8, "slow": 40},
            "objective": "sharpe",
            "trials": 10,
            "windows": [],
            "oos_total_return_pct": 4.0,
            "oos_sharpe": 0.9,
            "oos_max_drawdown_pct": 5.0,
            "oos_trades": 40,
            "holdout": None,
            "holdout_baseline": None,
            "stability": 0.8,
            "deflated_sharpe": 0.97,
            "criteria": {},
            "failures": [],
            "status": "proposed",
        }
        s.add(
            ProposalRecord(
                id="p1",
                user_id=user.id,
                bot_id=bot_id,
                strategy_id="ema",
                status="proposed",
                payload=payload,
            )
        )
        await s.commit()
    assert (await env.post("/api/proposals/p1/apply")).status_code == 409
    r = await env.post("/api/proposals/p1/decision", {"approve": True, "note": "ok"})
    assert r.json()["status"] == "approved"
    r = await env.post("/api/proposals/p1/apply")
    assert r.status_code == 200, r.text
    bot = (await env.client.get(f"/api/bots/{bot_id}")).json()
    (st,) = bot["config"]["strategies"]
    assert st["version"] == 2
    assert st["params"]["fast"] == 8
    assert (await env.client.get("/api/proposals")).json()[0]["status"] == "applied"


async def test_catalog_and_coverage(env: Env) -> None:
    await env.signup()
    cat = (await env.client.get("/api/catalog")).json()
    assert {s["kind"] for s in cat["strategies"]} == {"ema_cross", "rsi_reversion", "breakout"}
    assert "fast" in next(s for s in cat["strategies"] if s["kind"] == "ema_cross")["params"]
    assert set(cat["risk_presets"]) == {"prudent", "equilibre", "dynamique"}
    cov = (await env.client.get("/api/data/coverage")).json()
    assert cov[0]["symbol"] == "EUR/USD"
    assert pyotp
