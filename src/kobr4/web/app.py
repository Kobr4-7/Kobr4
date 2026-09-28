"""Application web : API, fichiers du site, démarrage et arrêt propres des bots."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from kobr4 import __version__
from kobr4.db.session import Database
from kobr4.security.crypto import SecretBox
from kobr4.web.config import ServerConfig
from kobr4.web.deps import AppState
from kobr4.web.ig import IgClients
from kobr4.web.jobs import JobRunner
from kobr4.web.routes import account, auth, bots, live, research, saxo
from kobr4.web.saxo import SaxoSessions
from kobr4.web.scheduler import LabScheduler
from kobr4.web.security import RateLimiter
from kobr4.web.supervisor import BotSupervisor, default_calendar

log = logging.getLogger("kobr4.web")

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
CSRF_HEADER = "x-kobr4"


def create_app(
    config: ServerConfig,
    db: Database | None = None,
    supervisor: BotSupervisor | None = None,
    jobs: JobRunner | None = None,
    broker_transport: Any = None,
    create_tables: bool = False,
) -> FastAPI:
    box = SecretBox(config.master_key)
    database = db or Database(config.database_url)
    sup = supervisor or BotSupervisor(
        database,
        box,
        calendar_source=default_calendar,
        saxo=SaxoSessions(database, box, broker_transport),
        ig=IgClients(broker_transport),
    )
    state = AppState(
        config=config,
        db=database,
        box=box,
        supervisor=sup,
        login_limiter=RateLimiter(limit=10, window=300),
        jobs=jobs or JobRunner(),
        broker_transport=broker_transport,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if create_tables:
            await database.create_all()
        scheduler = LabScheduler(database, box, state.jobs, str(config.data_dir))
        if config.start_bots:
            await sup.restore()
            sup.start_watch()
            sup.saxo.start_keep_alive()
            scheduler.start()
        yield
        await scheduler.stop()
        await sup.saxo.stop()
        await sup.shutdown()
        state.jobs.shutdown()
        await database.close()

    app = FastAPI(
        title="Kobr4 FX",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.state.kobr4 = state

    @app.middleware("http")
    async def protect(request: Request, call_next: Any) -> Response:
        # Toute requête qui modifie quelque chose doit porter l'en-tête X-Kobr4 : un autre
        # site ne peut pas l'ajouter sans autorisation CORS, ce qui bloque les attaques CSRF.
        if (
            request.method in UNSAFE
            and request.url.path.startswith("/api/")
            and request.headers.get(CSRF_HEADER) != "1"
        ):
            return JSONResponse({"detail": "en-tête X-Kobr4 manquant"}, status_code=403)
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if config.secure_cookies:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    for r in (
        auth.router,
        account.router,
        saxo.router,
        bots.router,
        research.router,
        live.router,
    ):
        app.include_router(r)

    static = config.static_dir
    if static is not None and (static / "index.html").exists():
        mount_frontend(app, static)
    return app


def mount_frontend(app: FastAPI, root: Path) -> None:
    """Sert le site (application monopage) : les chemins inconnus renvoient index.html."""
    assets = root / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")
    index = root / "index.html"

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> Response:
        if path.startswith("api/"):
            return JSONResponse({"detail": "route inconnue"}, status_code=404)
        candidate = (root / path).resolve()
        if path and candidate.is_file() and root.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
