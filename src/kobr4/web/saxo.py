"""Connexions Saxo : parcours OAuth et jetons partagés entre les bots.

Saxo renouvelle le jeton de rafraîchissement à chaque utilisation, et celui-ci expire
s'il n'est pas utilisé pendant environ une heure. Le registre garde donc une seule
`SaxoSession` par connexion, enregistre chaque nouveau jeton (chiffré) en base, et
renouvelle régulièrement les jetons de toutes les connexions, même sans bot en marche.
"""

import asyncio
import contextlib
import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select

from kobr4.db.models import BrokerConnection
from kobr4.db.session import Database
from kobr4.execution.brokers.saxo import (
    Environment,
    SaxoAuthError,
    SaxoError,
    SaxoSession,
    SaxoTokens,
)
from kobr4.security.crypto import SecretBox

log = logging.getLogger(__name__)

PENDING_TTL = timedelta(minutes=30)


@dataclass
class PendingSaxo:
    """Connexion en cours : entre le départ vers Saxo et le choix du compte."""

    user_id: str
    environment: Environment
    app_key: str
    app_secret: str
    redirect_uri: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    tokens: SaxoTokens | None = None


class SaxoSessions:
    def __init__(
        self,
        db: Database,
        box: SecretBox,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.db = db
        self.box = box
        self.transport = transport
        self.sessions: dict[str, SaxoSession] = {}
        self.pending: dict[str, PendingSaxo] = {}
        self._task: asyncio.Task[None] | None = None

    # Parcours de connexion

    def start(self, pending: PendingSaxo) -> str:
        now = datetime.now(UTC)
        for k in [k for k, p in self.pending.items() if now - p.created_at > PENDING_TTL]:
            del self.pending[k]
        state = secrets.token_urlsafe(24)
        self.pending[state] = pending
        return state

    def get_pending(self, state: str, user_id: str) -> PendingSaxo | None:
        p = self.pending.get(state)
        if p is None or p.user_id != user_id or datetime.now(UTC) - p.created_at > PENDING_TTL:
            return None
        return p

    def pending_session(self, p: PendingSaxo) -> SaxoSession:
        assert p.tokens is not None

        async def keep(tokens: SaxoTokens) -> None:
            p.tokens = tokens

        return SaxoSession(p.environment, p.tokens, on_refresh=keep, transport=self.transport)

    # Sessions des connexions enregistrées

    def session_for(self, conn: BrokerConnection, raw: str) -> SaxoSession:
        s = self.sessions.get(conn.id)
        if s is None:
            env: Environment = "live" if conn.environment == "live" else "practice"
            conn_id, user_id = conn.id, conn.user_id

            async def persist(tokens: SaxoTokens) -> None:
                await self._save(conn_id, user_id, tokens)

            s = SaxoSession(
                env, SaxoTokens.loads(raw), on_refresh=persist, transport=self.transport
            )
            self.sessions[conn.id] = s
        return s

    def forget(self, conn_id: str) -> None:
        self.sessions.pop(conn_id, None)

    async def _save(self, conn_id: str, user_id: str, tokens: SaxoTokens) -> None:
        async with self.db.session() as s:
            conn = await s.get(BrokerConnection, conn_id)
            if conn is None:
                return
            conn.token_encrypted = self.box.encrypt(tokens.dumps(), context=user_id)
            await s.commit()

    async def keep_alive(self, margin: timedelta = timedelta(minutes=30)) -> None:
        """Renouvelle les jetons proches de l'expiration, pour toutes les connexions."""
        async with self.db.session() as s:
            conns = list(
                await s.scalars(select(BrokerConnection).where(BrokerConnection.broker == "saxo"))
            )
            raws = {c.id: self.box.decrypt(c.token_encrypted, context=c.user_id) for c in conns}
        now = datetime.now(UTC)
        for conn in conns:
            session = self.session_for(conn, raws[conn.id])
            if session.expired or session.tokens.refresh_expires - now > margin:
                continue
            try:
                await session.refresh(force=True)
            except SaxoAuthError as e:
                log.error("connexion Saxo %s : %s", conn.id, e)
            except SaxoError as e:
                log.warning("connexion Saxo %s : renouvellement reporté (%s)", conn.id, e)

    async def run(self, every: float = 300.0) -> None:
        while True:
            try:
                await self.keep_alive()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("renouvellement des jetons Saxo impossible (%s)", e)
            await asyncio.sleep(every)

    def start_keep_alive(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="saxo-tokens")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


def pending_out(p: PendingSaxo) -> dict[str, Any]:
    return {"environment": p.environment, "connected": p.tokens is not None}
