"""Connexion d'un compte Saxo (OAuth 2, code d'autorisation).

1. `POST /api/brokers/saxo/start` : clé et secret de l'application Saxo de l'utilisateur,
   renvoie l'adresse de connexion chez Saxo ;
2. Saxo renvoie le navigateur sur `GET /api/brokers/saxo/callback`, qui échange le code
   contre des jetons puis redirige vers le site ;
3. `GET /api/brokers/saxo/pending/{state}` liste les comptes, et
   `POST /api/brokers/saxo/finish` enregistre la connexion pour le compte choisi.
"""

from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from kobr4.db.models import BrokerConnection
from kobr4.execution.brokers.saxo import (
    SaxoClient,
    SaxoError,
    authorize_url,
    exchange_code,
    list_accounts,
)
from kobr4.web.deps import Current, DbSession, State, audit
from kobr4.web.routes.account import _require_mfa, broker_out
from kobr4.web.saxo import PendingSaxo

router = APIRouter(prefix="/api/brokers/saxo", tags=["compte"])


def redirect_uri(st: State) -> str:
    return f"{st.config.public_url}/api/brokers/saxo/callback"


class StartIn(BaseModel):
    environment: Literal["practice", "live"] = "practice"
    app_key: str = Field(min_length=8, max_length=200)
    app_secret: str = Field(min_length=8, max_length=200)


@router.get("/redirect-uri")
async def get_redirect_uri(st: State, a: Current) -> dict[str, str]:
    """Adresse de retour à déclarer dans l'application Saxo."""
    return {"redirect_uri": redirect_uri(st)}


@router.post("/start")
async def start(body: StartIn, st: State, a: Current) -> dict[str, str]:
    _require_mfa(a)
    uri = redirect_uri(st)
    state = st.supervisor.saxo.start(
        PendingSaxo(
            user_id=a.user.id,
            environment=body.environment,
            app_key=body.app_key.strip(),
            app_secret=body.app_secret.strip(),
            redirect_uri=uri,
        )
    )
    return {
        "authorize_url": authorize_url(body.environment, body.app_key.strip(), uri, state),
        "redirect_uri": uri,
    }


@router.get("/callback")
async def callback(
    st: State,
    a: Current,
    state: str = "",
    code: str = "",
    error: str = "",
    error_description: str = "",
) -> RedirectResponse:
    page = "/bienvenue" if a.user.onboarding_step == "broker" else "/reglages"
    p = st.supervisor.saxo.get_pending(state, a.user.id)
    problem = None
    if p is None:
        problem = "connexion Saxo expirée ou inconnue, recommence"
    elif error or not code:
        problem = f"Saxo a refusé la connexion ({error_description or error or 'sans code'})"
    else:
        try:
            p.tokens = await exchange_code(
                p.environment, p.app_key, p.app_secret, p.redirect_uri, code, st.broker_transport
            )
        except SaxoError as e:
            problem = str(e)
    if problem is not None:
        return RedirectResponse(f"{page}?saxo_error={quote(problem)}", status_code=303)
    return RedirectResponse(f"{page}?saxo={quote(state)}", status_code=303)


def _pending(st: State, a: Current, state: str) -> PendingSaxo:
    p = st.supervisor.saxo.get_pending(state, a.user.id)
    if p is None or p.tokens is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "connexion Saxo expirée, recommence")
    return p


@router.get("/pending/{state}")
async def pending_accounts(state: str, st: State, a: Current) -> dict[str, Any]:
    p = _pending(st, a, state)
    client = SaxoClient(st.supervisor.saxo.pending_session(p), st.broker_transport)
    try:
        accounts = await list_accounts(client)
    except SaxoError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    finally:
        await client.aclose()
    return {"environment": p.environment, "accounts": accounts}


class FinishIn(BaseModel):
    state: str = Field(min_length=10, max_length=100)
    account_id: str = Field(min_length=3, max_length=64)
    label: str = Field(default="", max_length=80)


@router.post("/finish", status_code=201)
async def finish(
    body: FinishIn, request: Request, st: State, s: DbSession, a: Current
) -> dict[str, object]:
    _require_mfa(a)
    p = _pending(st, a, body.state)
    client = SaxoClient(st.supervisor.saxo.pending_session(p), st.broker_transport)
    try:
        accounts = await list_accounts(client)
    except SaxoError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    finally:
        await client.aclose()
    acct = next((x for x in accounts if x["id"] == body.account_id), None)
    if acct is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "compte Saxo introuvable")
    assert p.tokens is not None
    conn = BrokerConnection(
        user_id=a.user.id,
        broker="saxo",
        environment=p.environment,
        account_id=body.account_id,
        token_encrypted=st.box.encrypt(p.tokens.dumps(), context=a.user.id),
        label=body.label
        or acct["alias"]
        or ("Saxo simulation" if p.environment == "practice" else "Saxo réel"),
        account_currency=acct["currency"] or None,
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
        broker="saxo",
        environment=p.environment,
        account=acct["account_number"],
    )
    await s.commit()
    st.supervisor.saxo.pending.pop(body.state, None)
    return broker_out(conn)
