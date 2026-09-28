"""Inscription, connexion, double authentification, sessions, mot de passe."""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select, update

from kobr4.db.models import BotRecord, User, UserSession
from kobr4.web.deps import SESSION_COOKIE, Current, CurrentAny, DbSession, State, audit, client_ip
from kobr4.web.security import (
    hash_password,
    new_recovery_codes,
    new_session_token,
    new_totp_secret,
    password_problem,
    token_digest,
    totp_qr_svg,
    totp_uri,
    verify_password,
    verify_totp,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

MAX_FAILED = 5
LOCK_MINUTES = 15


class SignupIn(BaseModel):
    email: EmailStr
    password: str
    display_name: str = Field(default="", max_length=80)
    invite_code: str | None = None


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class CodeIn(BaseModel):
    code: str = Field(min_length=6, max_length=20)


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str


class DisableMfaIn(BaseModel):
    password: str
    code: str


def user_out(u: User) -> dict[str, object]:
    return {
        "id": u.id,
        "email": u.email,
        "display_name": u.display_name,
        "totp_enabled": u.totp_enabled,
        "is_admin": u.is_admin,
        "onboarding_step": u.onboarding_step,
        "base_currency": u.base_currency,
        "timezone": u.timezone,
        "recovery_codes_left": len(u.recovery_codes or []),
        "created_at": u.created_at.isoformat(),
    }


async def _open_session(
    st: State, s: DbSession, request: Request, response: Response, user: User, mfa_passed: bool
) -> None:
    token, digest = new_session_token()
    now = datetime.now(UTC)
    s.add(
        UserSession(
            id=digest,
            user_id=user.id,
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(days=st.config.session_days),
            user_agent=request.headers.get("user-agent", "")[:255],
            ip=client_ip(request),
            mfa_passed=mfa_passed,
        )
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=st.config.session_days * 86400,
        httponly=True,
        secure=st.config.secure_cookies,
        samesite="lax",
        path="/",
    )


@router.post("/signup", status_code=201)
async def signup(
    body: SignupIn, request: Request, response: Response, st: State, s: DbSession
) -> dict[str, object]:
    first = (await s.scalar(select(func.count()).select_from(User))) == 0
    invited = bool(st.config.invite_code) and body.invite_code == st.config.invite_code
    if not first and not st.config.open_signup and not invited:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "inscription sur invitation : code d'invitation requis"
        )
    email = body.email.lower()
    if await s.scalar(select(User).where(User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "un compte existe déjà avec cette adresse")
    problem = password_problem(body.password, email)
    if problem:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"mot de passe trop faible : {problem}"
        )
    user = User(
        email=email,
        password_hash=hash_password(body.password),
        display_name=body.display_name.strip(),
        is_admin=first,
        recovery_codes=[],
    )
    s.add(user)
    await s.flush()
    await audit(s, request, user.id, "signup")
    await _open_session(st, s, request, response, user, mfa_passed=True)
    await s.commit()
    return user_out(user)


@router.post("/login")
async def login(
    body: LoginIn, request: Request, response: Response, st: State, s: DbSession
) -> dict[str, object]:
    if not st.login_limiter.allow(client_ip(request)):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "trop de tentatives, réessaie dans quelques minutes"
        )
    user = await s.scalar(select(User).where(User.email == body.email.lower()))
    now = datetime.now(UTC)
    if user is not None and user.locked_until and user.locked_until > now:
        raise HTTPException(
            status.HTTP_423_LOCKED, "compte verrouillé temporairement après plusieurs échecs"
        )
    if user is None or not verify_password(user.password_hash, body.password):
        if user is not None:
            user.failed_logins += 1
            if user.failed_logins >= MAX_FAILED:
                user.locked_until = now + timedelta(minutes=LOCK_MINUTES)
                user.failed_logins = 0
            await audit(s, request, user.id, "login_failed")
            await s.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "email ou mot de passe incorrect")
    user.failed_logins = 0
    user.last_login_at = now
    await _open_session(st, s, request, response, user, mfa_passed=not user.totp_enabled)
    await audit(s, request, user.id, "login")
    await s.commit()
    return {"user": user_out(user), "mfa_required": user.totp_enabled}


@router.post("/mfa")
async def mfa(
    body: CodeIn, request: Request, st: State, s: DbSession, a: CurrentAny
) -> dict[str, object]:
    user = a.user
    if not user.totp_enabled or user.totp_secret is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "double authentification non activée")
    if not st.login_limiter.allow(f"mfa:{user.id}"):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "trop de tentatives, réessaie dans quelques minutes"
        )
    secret = st.box.decrypt(user.totp_secret, context=user.id)
    code = body.code.strip()
    ok = verify_totp(secret, code)
    if not ok:
        digest = token_digest(code.lower())
        if digest in (user.recovery_codes or []):
            user.recovery_codes = [c for c in user.recovery_codes if c != digest]
            ok = True
            await audit(s, request, user.id, "recovery_code_used", left=len(user.recovery_codes))
    if not ok:
        await audit(s, request, user.id, "mfa_failed")
        await s.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "code incorrect")
    a.session.mfa_passed = True
    await s.commit()
    return user_out(user)


@router.post("/logout")
async def logout(response: Response, s: DbSession, a: CurrentAny) -> dict[str, bool]:
    a.session.revoked = True
    await s.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(a: Current) -> dict[str, object]:
    return user_out(a.user)


@router.post("/password")
async def change_password(
    body: PasswordChangeIn, request: Request, s: DbSession, a: Current
) -> dict[str, bool]:
    if not verify_password(a.user.password_hash, body.current_password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "mot de passe actuel incorrect")
    problem = password_problem(body.new_password, a.user.email)
    if problem:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"mot de passe trop faible : {problem}"
        )
    a.user.password_hash = hash_password(body.new_password)
    # Toutes les autres sessions sont fermées.
    await s.execute(
        update(UserSession)
        .where(UserSession.user_id == a.user.id, UserSession.id != a.session.id)
        .values(revoked=True)
    )
    await audit(s, request, a.user.id, "password_changed")
    await s.commit()
    return {"ok": True}


# Double authentification


@router.post("/2fa/setup")
async def mfa_setup(st: State, s: DbSession, a: Current) -> dict[str, str]:
    if a.user.totp_enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "double authentification déjà activée")
    secret = new_totp_secret()
    a.user.totp_secret = st.box.encrypt(secret, context=a.user.id)
    await s.commit()
    uri = totp_uri(secret, a.user.email)
    return {"secret": secret, "uri": uri, "qr_svg": totp_qr_svg(uri)}


@router.post("/2fa/enable")
async def mfa_enable(
    body: CodeIn, request: Request, st: State, s: DbSession, a: Current
) -> dict[str, object]:
    user = a.user
    if user.totp_enabled or user.totp_secret is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "lancer d'abord la configuration")
    if not verify_totp(st.box.decrypt(user.totp_secret, context=user.id), body.code):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "code incorrect : vérifie l'heure de ton téléphone"
        )
    codes, digests = new_recovery_codes()
    user.totp_enabled = True
    user.recovery_codes = digests
    a.session.mfa_passed = True
    if user.onboarding_step == "security":
        user.onboarding_step = "broker"
    await audit(s, request, user.id, "mfa_enabled")
    await s.commit()
    return {"recovery_codes": codes, "user": user_out(user)}


@router.post("/2fa/disable")
async def mfa_disable(
    body: DisableMfaIn, request: Request, st: State, s: DbSession, a: Current
) -> dict[str, object]:
    user = a.user
    if not user.totp_enabled or user.totp_secret is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "double authentification non activée")
    if not verify_password(user.password_hash, body.password) or not verify_totp(
        st.box.decrypt(user.totp_secret, context=user.id), body.code
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "mot de passe ou code incorrect")
    live = await s.scalar(
        select(func.count())
        .select_from(BotRecord)
        .where(BotRecord.user_id == user.id, BotRecord.mode == "live")
    )
    if live:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "impossible tant qu'un bot est en argent réel"
        )
    user.totp_enabled = False
    user.totp_secret = None
    user.recovery_codes = []
    await audit(s, request, user.id, "mfa_disabled")
    await s.commit()
    return user_out(user)


@router.post("/2fa/recovery-codes")
async def regenerate_codes(
    body: CodeIn, request: Request, st: State, s: DbSession, a: Current
) -> dict[str, list[str]]:
    user = a.user
    if not user.totp_enabled or user.totp_secret is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "double authentification non activée")
    if not verify_totp(st.box.decrypt(user.totp_secret, context=user.id), body.code):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "code incorrect")
    codes, digests = new_recovery_codes()
    user.recovery_codes = digests
    await audit(s, request, user.id, "recovery_codes_regenerated")
    await s.commit()
    return {"recovery_codes": codes}


# Sessions


@router.get("/sessions")
async def sessions(s: DbSession, a: Current) -> list[dict[str, object]]:
    now = datetime.now(UTC)
    rows = await s.scalars(
        select(UserSession)
        .where(
            UserSession.user_id == a.user.id,
            UserSession.revoked.is_(False),
            UserSession.expires_at > now,
        )
        .order_by(UserSession.last_seen_at.desc())
    )
    return [
        {
            "id": r.id[:12],
            "current": r.id == a.session.id,
            "created_at": r.created_at.isoformat(),
            "last_seen_at": r.last_seen_at.isoformat(),
            "user_agent": r.user_agent,
            "ip": r.ip,
        }
        for r in rows
    ]


@router.delete("/sessions/{prefix}")
async def revoke_session(
    prefix: str, request: Request, s: DbSession, a: Current
) -> dict[str, bool]:
    rows = list(
        await s.scalars(
            select(UserSession).where(
                UserSession.user_id == a.user.id, UserSession.id.startswith(prefix)
            )
        )
    )
    if len(rows) != 1:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session introuvable")
    rows[0].revoked = True
    await audit(s, request, a.user.id, "session_revoked")
    await s.commit()
    return {"ok": True}
