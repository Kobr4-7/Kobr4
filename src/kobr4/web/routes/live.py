"""Temps réel (WebSocket), santé du serveur, métriques Prometheus."""

import asyncio
import contextlib
import json
from typing import Any

from fastapi import (
    APIRouter,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import PlainTextResponse
from prometheus_client import generate_latest
from sqlalchemy import select

from kobr4.db.models import BotRecord
from kobr4.live.metrics import REGISTRY
from kobr4.web.deps import SESSION_COOKIE, AppState, Auth, _auth

router = APIRouter(tags=["temps réel"])


@router.get("/healthz")
async def healthz(request: Request) -> dict[str, Any]:
    st: AppState = request.app.state.kobr4
    return {"ok": True, "bots_running": len(st.supervisor.bots)}


@router.get("/metrics")
async def metrics(
    request: Request, authorization: str | None = Header(default=None)
) -> PlainTextResponse:
    st: AppState = request.app.state.kobr4
    # Jeton obligatoire, même en local : derrière un proxy, tout semble venir de 127.0.0.1.
    token = st.config.metrics_token
    if token is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "métriques désactivées (KOBR4_METRICS_TOKEN)"
        )
    if authorization != f"Bearer {token}":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "accès aux métriques refusé")
    return PlainTextResponse(
        generate_latest(REGISTRY).decode(), media_type="text/plain; version=0.0.4"
    )


@router.websocket("/api/ws")
async def ws(websocket: WebSocket) -> None:
    """Envoie chaque seconde l'état des bots de l'utilisateur connecté."""
    st: AppState = websocket.app.state.kobr4
    if websocket.cookies.get(SESSION_COOKIE) is None:
        await websocket.close(code=4401)
        return
    async with st.db.session() as s:
        try:
            a: Auth = await _auth(websocket, s, require_mfa=True)  # type: ignore[arg-type]
        except HTTPException:
            await websocket.close(code=4401)
            return
        user_id = a.user.id
    await websocket.accept()
    try:
        while True:
            async with st.db.session() as s:
                ids = list(
                    await s.scalars(select(BotRecord.id).where(BotRecord.user_id == user_id))
                )
            payload = {
                "bots": [
                    bot.status() for bot_id in ids if (bot := st.supervisor.get(bot_id)) is not None
                ]
            }
            await websocket.send_text(json.dumps(payload, default=str))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
    except (WebSocketDisconnect, RuntimeError):
        return
