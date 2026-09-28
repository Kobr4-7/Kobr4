"""Connexion à la base (PostgreSQL en production, SQLite en développement)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from kobr4.db.models import Base


def _sqlite_pragmas(dbapi_conn: object, _: object) -> None:
    cur = dbapi_conn.cursor()  # type: ignore[attr-defined]
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.close()


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        kw: dict[str, object] = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kw = {}
        self.engine: AsyncEngine = create_async_engine(url, **kw)
        if url.startswith("sqlite"):
            event.listen(self.engine.sync_engine, "connect", _sqlite_pragmas)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_all(self) -> None:
        """Crée les tables manquantes (tests et développement ; en production : alembic)."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as s:
            yield s

    async def close(self) -> None:
        await self.engine.dispose()
