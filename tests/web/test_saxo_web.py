from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pyotp
import pytest
from sqlalchemy import select

from kobr4.db import Database
from kobr4.db.models import BrokerConnection
from kobr4.execution.brokers.saxo import SaxoBroker, SaxoTokens
from kobr4.security.crypto import SecretBox, generate_master_key
from kobr4.web.app import create_app
from kobr4.web.config import ServerConfig
from kobr4.web.jobs import JobRunner
from kobr4.web.saxo import SaxoSessions
from kobr4.web.supervisor import BotSupervisor
from tests.execution.fake_saxo import FakeSaxo
from tests.web.conftest import PASSWORD, H, first_bot


@dataclass
class SaxoEnv:
    client: httpx.AsyncClient
    fake: FakeSaxo
    db: Database
    box: SecretBox
    supervisor: BotSupervisor

    async def ready_user(self) -> None:
        """Compte créé, profil rempli, double authentification active."""
        c = self.client
        r = await c.post(
            "/api/auth/signup", json={"email": "moi@example.be", "password": PASSWORD}, headers=H
        )
        assert r.status_code in (200, 201), r.text
        await c.patch("/api/profile", json={"display_name": "Moi"}, headers=H)
        secret = (await c.post("/api/auth/2fa/setup", headers=H)).json()["secret"]
        r = await c.post("/api/auth/2fa/enable", json={"code": pyotp.TOTP(secret).now()}, headers=H)
        assert r.status_code == 200, r.text

    async def connect(self) -> str:
        """Parcours complet ; renvoie l'identifiant de la connexion créée."""
        c = self.client
        r = await c.post(
            "/api/brokers/saxo/start",
            json={"app_key": self.fake.app_key, "app_secret": self.fake.app_secret},
            headers=H,
        )
        assert r.status_code == 200, r.text
        state = parse_qs(urlparse(r.json()["authorize_url"]).query)["state"][0]
        r = await c.get("/api/brokers/saxo/callback", params={"state": state, "code": "code-ok"})
        assert r.status_code == 303, r.text
        r = await c.post(
            "/api/brokers/saxo/finish",
            json={"state": state, "account_id": self.fake.account_key},
            headers=H,
        )
        assert r.status_code == 201, r.text
        return str(r.json()["id"])


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[SaxoEnv]:
    fake = FakeSaxo()
    key = generate_master_key()
    box = SecretBox(key)
    config = ServerConfig(
        database_url="sqlite+aiosqlite:///:memory:",
        master_key=key,
        public_url="https://kobr4.test",
        data_dir=tmp_path,
        start_bots=False,
    )
    db = Database(config.database_url)
    await db.create_all()
    sup = BotSupervisor(db, box, saxo=SaxoSessions(db, box, fake.transport()))
    jobs = JobRunner(ThreadPoolExecutor(1), ThreadPoolExecutor(1))
    app = create_app(config, db=db, supervisor=sup, jobs=jobs, broker_transport=fake.transport())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://kobr4.test"
    ) as client:
        yield SaxoEnv(client, fake, db, box, sup)
    await sup.shutdown()
    jobs.shutdown()
    await db.close()


async def test_full_connection_flow(env: SaxoEnv) -> None:
    await env.ready_user()
    c = env.client
    r = await c.get("/api/brokers/saxo/redirect-uri")
    assert r.json()["redirect_uri"] == "https://kobr4.test/api/brokers/saxo/callback"
    r = await c.post(
        "/api/brokers/saxo/start",
        json={"app_key": env.fake.app_key, "app_secret": env.fake.app_secret},
        headers=H,
    )
    url = urlparse(r.json()["authorize_url"])
    q = parse_qs(url.query)
    assert url.netloc == "sim.logonvalidation.net"
    assert q["client_id"] == [env.fake.app_key]
    assert q["redirect_uri"] == ["https://kobr4.test/api/brokers/saxo/callback"]
    state = q["state"][0]

    r = await c.get("/api/brokers/saxo/callback", params={"state": state, "code": "code-ok"})
    assert r.status_code == 303
    assert r.headers["location"] == f"/bienvenue?saxo={state}"  # parcours d'accueil en cours

    r = await c.get(f"/api/brokers/saxo/pending/{state}")
    assert r.status_code == 200, r.text
    (acct,) = r.json()["accounts"]
    assert (acct["id"], acct["currency"]) == (env.fake.account_key, "USD")

    r = await c.post(
        "/api/brokers/saxo/finish", json={"state": state, "account_id": acct["id"]}, headers=H
    )
    assert r.status_code == 201, r.text
    conn = r.json()
    assert (conn["broker"], conn["environment"], conn["label"]) == (
        "saxo",
        "practice",
        "Simulation",
    )
    me = (await c.get("/api/auth/me")).json()
    assert me["onboarding_step"] == "notifications"

    async with env.db.session() as s:
        row = await s.get(BrokerConnection, conn["id"])
        assert row is not None
        stored = SaxoTokens.loads(env.box.decrypt(row.token_encrypted, context=row.user_id))
    assert stored.refresh_token == "ref-1"
    assert stored.app_secret == env.fake.app_secret

    # Le parcours ne peut pas être rejoué.
    r = await c.get(f"/api/brokers/saxo/pending/{state}")
    assert r.status_code == 404


async def test_callback_errors_are_sent_back_to_the_page(env: SaxoEnv) -> None:
    await env.ready_user()
    c = env.client
    r = await c.get("/api/brokers/saxo/callback", params={"state": "inconnu", "code": "code-ok"})
    assert r.status_code == 303
    assert "saxo_error=" in r.headers["location"]
    r = await c.post(
        "/api/brokers/saxo/start",
        json={"app_key": env.fake.app_key, "app_secret": "mauvais-secret"},
        headers=H,
    )
    state = parse_qs(urlparse(r.json()["authorize_url"]).query)["state"][0]
    r = await c.get("/api/brokers/saxo/callback", params={"state": state, "code": "code-ok"})
    assert r.status_code == 303
    assert "saxo_error=" in r.headers["location"]
    r = await c.get("/api/brokers/saxo/callback", params={"state": state, "error": "access_denied"})
    assert "saxo_error=" in r.headers["location"]


async def test_start_requires_two_factor(env: SaxoEnv) -> None:
    c = env.client
    await c.post(
        "/api/auth/signup", json={"email": "moi@example.be", "password": PASSWORD}, headers=H
    )
    r = await c.post(
        "/api/brokers/saxo/start",
        json={"app_key": env.fake.app_key, "app_secret": env.fake.app_secret},
        headers=H,
    )
    assert r.status_code == 403


async def test_keep_alive_renews_and_saves_tokens(env: SaxoEnv) -> None:
    await env.ready_user()
    conn_id = await env.connect()
    saxo = env.supervisor.saxo
    await saxo.keep_alive()  # jeton encore frais : rien à faire
    assert env.fake.refresh == "ref-1"
    await saxo.keep_alive(margin=timedelta(hours=2))
    assert env.fake.refresh == "ref-2"
    async with env.db.session() as s:
        row = await s.get(BrokerConnection, conn_id)
        assert row is not None
        stored = SaxoTokens.loads(env.box.decrypt(row.token_encrypted, context=row.user_id))
    assert stored.refresh_token == "ref-2"
    assert stored.refresh_expires > datetime.now(UTC) + timedelta(minutes=50)


async def test_supervisor_builds_a_saxo_broker_sharing_the_session(env: SaxoEnv) -> None:
    await env.ready_user()
    await env.connect()
    async with env.db.session() as s:
        (conn,) = list(await s.scalars(select(BrokerConnection)))
        raw = env.box.decrypt(conn.token_encrypted, context=conn.user_id)
    a = env.supervisor.broker_factory(conn, raw)
    b = env.supervisor.broker_factory(conn, raw)
    assert isinstance(a, SaxoBroker)
    assert isinstance(b, SaxoBroker)
    assert a.client.session is b.client.session
    await a.connect()
    assert a.client_key == env.fake.client_key
    await a.close()
    await b.close()


async def test_deleting_the_connection_forgets_the_session(env: SaxoEnv) -> None:
    await env.ready_user()
    conn_id = await env.connect()
    await env.supervisor.saxo.keep_alive(margin=timedelta(hours=2))
    assert conn_id in env.supervisor.saxo.sessions
    r = await env.client.delete(f"/api/brokers/{conn_id}", headers=H)
    assert r.status_code == 200, r.text
    assert conn_id not in env.supervisor.saxo.sessions


async def test_first_bot_runs_on_saxo(env: SaxoEnv) -> None:
    await env.ready_user()
    conn_id = await env.connect()
    c = env.client
    await c.post("/api/onboarding/skip-notifications", headers=H)
    r = await c.post("/api/onboarding/finish", json=first_bot(conn_id, start=True), headers=H)
    assert r.status_code == 201, r.text
    assert r.json()["start_error"] is None
    bot_id = r.json()["bot_id"]
    bot = env.supervisor.get(bot_id)
    assert bot is not None
    assert bot.state.value == "running"
    await env.supervisor.stop(bot_id)
