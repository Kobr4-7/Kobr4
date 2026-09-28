from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pyotp
import pytest

from kobr4.core.clock import LiveClock
from kobr4.db import Database
from kobr4.execution.brokers.oanda import OandaBroker
from kobr4.marketdata.sources.synthetic import MARKER, generate_m1
from kobr4.marketdata.store import ParquetBarStore
from kobr4.security.crypto import SecretBox, generate_master_key
from kobr4.web.app import create_app
from kobr4.web.config import ServerConfig
from kobr4.web.jobs import JobRunner
from kobr4.web.supervisor import BotSupervisor
from tests.execution.fake_oanda import FakeOanda

H = {"X-Kobr4": "1"}
PASSWORD = "correct horse battery"


@dataclass
class Env:
    client: httpx.AsyncClient
    fake: FakeOanda
    db: Database
    supervisor: BotSupervisor
    config: ServerConfig

    async def post(self, url: str, json: Any = None, **kw: Any) -> httpx.Response:
        return await self.client.post(url, json=json, headers=H, **kw)

    async def put(self, url: str, json: Any = None) -> httpx.Response:
        return await self.client.put(url, json=json, headers=H)

    async def patch(self, url: str, json: Any = None) -> httpx.Response:
        return await self.client.patch(url, json=json, headers=H)

    async def delete(self, url: str) -> httpx.Response:
        return await self.client.delete(url, headers=H)

    async def signup(self, email: str = "moi@example.com", **kw: Any) -> httpx.Response:
        return await self.post("/api/auth/signup", {"email": email, "password": PASSWORD, **kw})

    async def enable_mfa(self) -> tuple[str, list[str]]:
        r = await self.post("/api/auth/2fa/setup")
        secret = r.json()["secret"]
        r = await self.post("/api/auth/2fa/enable", {"code": pyotp.TOTP(secret).now()})
        assert r.status_code == 200, r.text
        return secret, r.json()["recovery_codes"]

    async def onboard(self) -> str:
        """Compte complet jusqu'au premier bot (non démarré). Renvoie l'id du bot."""
        await self.signup()
        await self.patch("/api/profile", {"display_name": "Moi"})
        await self.enable_mfa()
        r = await self.post(
            "/api/brokers", {"token": "tok-valide-123", "account_id": self.fake.account_id}
        )
        assert r.status_code == 201, r.text
        await self.post("/api/onboarding/skip-notifications")
        r = await self.post("/api/onboarding/finish", first_bot(r.json()["id"], start=False))
        assert r.status_code == 201, r.text
        return str(r.json()["bot_id"])


def first_bot(conn_id: str, start: bool = True) -> dict[str, Any]:
    return {
        "name": "EUR tendance",
        "broker_connection_id": conn_id,
        "instruments": ["EUR/USD"],
        "risk": {"risk_per_trade_pct": 1},
        "strategies": [
            {
                "id": "ema",
                "kind": "ema_cross",
                "instruments": ["EUR/USD"],
                "timeframe": "H1",
                "params": {"fast": 10, "slow": 30},
            }
        ],
        "start": start,
    }


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    fake = FakeOanda(token="tok-valide-123")
    key = generate_master_key()
    store = ParquetBarStore(tmp_path / "data")
    store.write_m1("EUR/USD", generate_m1("EUR/USD", date(2024, 1, 1), date(2024, 3, 31), seed=2))
    (store.root / MARKER).write_text("x", encoding="utf-8")
    config = ServerConfig(
        # Base dans un fichier : en mémoire, toutes les sessions partagent une seule
        # connexion et le rollback de l'une peut effacer l'écriture en cours d'une autre.
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'kobr4.db'}",
        master_key=key,
        data_dir=store.root,
        invite_code="bienvenue",
        start_bots=False,
        min_paper_days=28,
    )
    db = Database(config.database_url)
    await db.create_all()
    sup = BotSupervisor(
        db,
        SecretBox(key),
        broker_factory=lambda conn, token: OandaBroker(
            token, conn.account_id, "practice", LiveClock(), transport=fake.transport()
        ),
    )
    jobs = JobRunner(ThreadPoolExecutor(2), ThreadPoolExecutor(1))
    app = create_app(config, db=db, supervisor=sup, jobs=jobs, broker_transport=fake.transport())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield Env(client, fake, db, sup, config)
    await fake.tx_queue.put("__END__")
    await sup.shutdown()
    jobs.shutdown()
    await db.close()
