from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import httpx
import pyotp
import pytest

from kobr4.db import Database
from kobr4.security.crypto import SecretBox, generate_master_key
from kobr4.web.app import create_app
from kobr4.web.config import ServerConfig
from kobr4.web.jobs import JobRunner
from kobr4.web.supervisor import BotSupervisor
from tests.execution.test_paper import FakeFinnhub
from tests.web.conftest import PASSWORD, H, first_bot

GOOD_KEY = "cle-finnhub-valide"


def finnhub(request: httpx.Request) -> httpx.Response:
    assert request.url.host == "finnhub.io"
    if request.url.params.get("token") != GOOD_KEY:
        return httpx.Response(401, json={"error": "Invalid API key."})
    return httpx.Response(200, json={"c": 230.1})


@dataclass
class DemoEnv:
    client: httpx.AsyncClient
    fake: FakeFinnhub
    supervisor: BotSupervisor
    data_dir: Path

    async def ready_user(self) -> None:
        c = self.client
        r = await c.post(
            "/api/auth/signup", json={"email": "moi@example.be", "password": PASSWORD}, headers=H
        )
        assert r.status_code in (200, 201), r.text
        await c.patch("/api/profile", json={"display_name": "Moi"}, headers=H)
        secret = (await c.post("/api/auth/2fa/setup", headers=H)).json()["secret"]
        r = await c.post("/api/auth/2fa/enable", json={"code": pyotp.TOTP(secret).now()}, headers=H)
        assert r.status_code == 200, r.text


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[DemoEnv]:
    fake = FakeFinnhub()
    key = generate_master_key()
    config = ServerConfig(
        # Base dans un fichier : en mémoire, toutes les sessions partagent une seule
        # connexion et le rollback de l'une peut effacer l'écriture en cours d'une autre.
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'kobr4.db'}",
        master_key=key,
        data_dir=tmp_path,
        start_bots=False,
    )
    db = Database(config.database_url)
    await db.create_all()
    sup = BotSupervisor(
        db,
        SecretBox(key),
        data_dir=tmp_path,
        paper_options={"connect_ws": fake.connect, "fetch": lambda url: b""},
    )
    jobs = JobRunner(ThreadPoolExecutor(1), ThreadPoolExecutor(1))
    app = create_app(
        config, db=db, supervisor=sup, jobs=jobs, broker_transport=httpx.MockTransport(finnhub)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield DemoEnv(client, fake, sup, tmp_path)
    fake.queue.put_nowait(None)
    await sup.shutdown()
    jobs.shutdown()
    await db.close()


async def test_demo_account_and_first_bot(env: DemoEnv) -> None:
    await env.ready_user()
    c = env.client
    r = await c.post(
        "/api/brokers/demo",
        json={"finnhub_key": GOOD_KEY, "initial_balance": 25_000, "currency": "EUR"},
        headers=H,
    )
    assert r.status_code == 201, r.text
    conn = r.json()
    assert (conn["broker"], conn["environment"], conn["account_currency"]) == (
        "paper",
        "practice",
        "EUR",
    )
    assert (await c.get("/api/auth/me")).json()["onboarding_step"] == "notifications"

    await c.post("/api/onboarding/skip-notifications", headers=H)
    r = await c.post("/api/onboarding/finish", json=first_bot(conn["id"], start=True), headers=H)
    assert r.status_code == 201, r.text
    assert r.json()["start_error"] is None
    bot_id = r.json()["bot_id"]
    bot = env.supervisor.get(bot_id)
    assert bot is not None
    assert bot.state.value == "running"
    state = env.data_dir / "paper" / f"{conn['id']}.json"
    assert state.exists()
    assert '"balance": "25000"' in state.read_text()

    # Un compte démo ne sert qu'à un bot en marche à la fois.
    payload = {**first_bot(conn["id"]), "name": "Deuxième"}
    payload.pop("start")
    r = await c.post("/api/bots", json=payload, headers=H)
    assert r.status_code == 201, r.text
    r = await c.post(f"/api/bots/{r.json()['id']}/start", headers=H)
    assert r.status_code >= 400
    assert "déjà utilisé" in r.text
    await env.supervisor.stop(bot_id)


async def test_bad_finnhub_key_is_refused(env: DemoEnv) -> None:
    await env.ready_user()
    r = await env.client.post("/api/brokers/demo", json={"finnhub_key": "mauvaise-cle"}, headers=H)
    assert r.status_code == 422
    assert "Finnhub" in r.json()["detail"]
