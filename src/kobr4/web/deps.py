"""Dépendances des routes : base, utilisateur connecté, audit."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from kobr4.db.models import AuditEntry, User, UserSession
from kobr4.db.session import Database
from kobr4.security.crypto import SecretBox
from kobr4.web.config import ServerConfig
from kobr4.web.jobs import JobRunner
from kobr4.web.security import RateLimiter, token_digest
from kobr4.web.supervisor import BotSupervisor

SESSION_COOKIE = "kobr4_session"


@dataclass
class AppState:
    config: ServerConfig
    db: Database
    box: SecretBox
    supervisor: BotSupervisor
    login_limiter: RateLimiter
    jobs: JobRunner
    broker_transport: Any = None
    """Transport HTTP injecté pour les tests (faux serveur OANDA)."""


def state(request: Request) -> AppState:
    st: AppState = request.app.state.kobr4
    return st


State = Annotated[AppState, Depends(state)]


async def session(st: State) -> AsyncIterator[AsyncSession]:
    async with st.db.session() as s:
        yield s


DbSession = Annotated[AsyncSession, Depends(session)]


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


@dataclass
class Auth:
    user: User
    session: UserSession


async def _auth(request: Request, s: DbSession, require_mfa: bool) -> Auth:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "connexion requise")
    sess = await s.get(UserSession, token_digest(token))
    now = datetime.now(UTC)
    if sess is None or sess.revoked or sess.expires_at <= now:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session expirée, reconnecte-toi")
    user = await s.get(User, sess.user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "compte introuvable")
    if require_mfa and user.totp_enabled and not sess.mfa_passed:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "code de double authentification requis")
    if now - sess.last_seen_at > timedelta(minutes=5):
        sess.last_seen_at = now
        await s.commit()
    return Auth(user, sess)


async def auth_any(request: Request, s: DbSession) -> Auth:
    """Session valide, double authentification pas encore forcément validée."""
    return await _auth(request, s, require_mfa=False)


async def auth(request: Request, s: DbSession) -> Auth:
    return await _auth(request, s, require_mfa=True)


CurrentAny = Annotated[Auth, Depends(auth_any)]
Current = Annotated[Auth, Depends(auth)]


async def audit(
    s: AsyncSession, request: Request, user_id: str | None, action: str, **detail: Any
) -> None:
    s.add(AuditEntry(user_id=user_id, action=action, detail=detail, ip=client_ip(request)))
