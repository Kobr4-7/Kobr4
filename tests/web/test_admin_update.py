from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
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
from tests.web.conftest import PASSWORD, H


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    key = generate_master_key()
    ctl = tmp_path / "control"
    ctl.mkdir()
    config = ServerConfig(
        # Base dans un fichier : en mémoire, toutes les sessions partagent une seule
        # connexion et le rollback de l'une peut effacer l'écriture en cours d'une autre.
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'kobr4.db'}",
        master_key=key,
        data_dir=tmp_path,
        control_dir=ctl,
        start_bots=False,
        open_signup=True,
    )
    db = Database(config.database_url)
    await db.create_all()
    sup = BotSupervisor(db, SecretBox(key))
    jobs = JobRunner(ThreadPoolExecutor(1), ThreadPoolExecutor(1))
    app = create_app(config, db=db, supervisor=sup, jobs=jobs)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=H
    ) as c:
        yield c
    await sup.shutdown()
    jobs.shutdown()
    await db.close()


async def signup(c: httpx.AsyncClient, email: str, mfa: bool = True) -> None:
    r = await c.post("/api/auth/signup", json={"email": email, "password": PASSWORD})
    assert r.status_code in (200, 201), r.text
    if mfa:
        secret = (await c.post("/api/auth/2fa/setup")).json()["secret"]
        r = await c.post("/api/auth/2fa/enable", json={"code": pyotp.TOTP(secret).now()})
        assert r.status_code == 200, r.text


async def test_status_and_request(client: httpx.AsyncClient, tmp_path: Path) -> None:
    ctl = tmp_path / "control"
    (ctl / "version").write_text("abc1234\n")
    (ctl / "pending").write_text("def5678 Ajoute l'or\n9876fed Refonte du site\n")
    await signup(client, "admin@example.com")
    r = await client.get("/api/admin/update")
    body = r.json()
    assert body["available"] is True
    assert body["version"] == "abc1234"
    assert [p["subject"] for p in body["pending"]] == ["Ajoute l'or", "Refonte du site"]
    assert body["state"] == "idle"
    assert body["requested_at"] is None

    r = await client.post("/api/admin/update")
    assert r.status_code == 202, r.text
    assert (ctl / "request").exists()
    assert r.json()["requested_at"] is not None

    (ctl / "state").write_text("running\n")
    r = await client.post("/api/admin/update")
    assert r.status_code == 409


async def test_only_the_admin_can_update(client: httpx.AsyncClient) -> None:
    await signup(client, "admin@example.com", mfa=False)
    await client.post("/api/auth/logout")
    client.cookies.clear()
    await signup(client, "autre@example.com")
    r = await client.get("/api/admin/update")
    assert r.status_code == 403
    r = await client.post("/api/admin/update")
    assert r.status_code == 403
