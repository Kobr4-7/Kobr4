"""Supervision des bots de tous les utilisateurs : démarrage, arrêt, reprise après un
redémarrage du serveur, suivi des pannes."""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import httpx
from sqlalchemy import select

from kobr4.config.settings import Settings
from kobr4.core.clock import LiveClock
from kobr4.db.models import BotRecord, BrokerConnection, NotificationSettings
from kobr4.db.session import Database
from kobr4.execution.broker import Broker
from kobr4.execution.brokers.oanda import OandaBroker
from kobr4.execution.brokers.saxo import SaxoBroker
from kobr4.live.alerts import Notifier, TelegramNotifier
from kobr4.live.bot import BotState, LiveBot
from kobr4.risk.calendar import EconomicCalendar
from kobr4.risk.calendar_feed import fetch_calendar
from kobr4.security.crypto import SecretBox
from kobr4.web.saxo import SaxoSessions

log = logging.getLogger(__name__)

BrokerFactory = Callable[[BrokerConnection, str], Broker]
CalendarSource = Callable[[], Awaitable[EconomicCalendar]]


def bot_settings(record: BotRecord, conn: BrokerConnection | None) -> Settings:
    """Configuration complète d'un bot à partir de son enregistrement."""
    cfg: dict[str, Any] = dict(record.config)
    broker: dict[str, Any] = (
        {"name": conn.broker, "environment": conn.environment, "account_id": conn.account_id}
        if conn is not None
        else {"name": "oanda", "environment": "practice"}
    )
    return Settings.model_validate(
        {
            **cfg,
            "mode": record.mode,
            "broker": broker,
            "confirm_live": record.live_confirmed_at is not None,
        }
    )


class BotSupervisor:
    def __init__(
        self,
        db: Database,
        box: SecretBox,
        broker_factory: BrokerFactory | None = None,
        calendar_source: CalendarSource | None = None,
        notifier_factory: Callable[[str, str], Notifier] | None = None,
        saxo: SaxoSessions | None = None,
    ) -> None:
        self.db = db
        self.box = box
        self.saxo = saxo or SaxoSessions(db, box)
        self.broker_factory = broker_factory or self._broker
        self.calendar_source = calendar_source
        self.notifier_factory = notifier_factory or (
            lambda token, chat: TelegramNotifier(token, chat)
        )
        self.bots: dict[str, LiveBot] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._watch: asyncio.Task[None] | None = None

    def _broker(self, conn: BrokerConnection, token: str) -> Broker:
        if conn.broker == "saxo":
            session = self.saxo.session_for(conn, token)
            return SaxoBroker(session, conn.account_id, LiveClock(), self.saxo.transport)
        env: Literal["practice", "live"] = "live" if conn.environment == "live" else "practice"
        return OandaBroker(token, conn.account_id, env, LiveClock())

    def get(self, bot_id: str) -> LiveBot | None:
        return self.bots.get(bot_id)

    def _lock(self, bot_id: str) -> asyncio.Lock:
        return self._locks.setdefault(bot_id, asyncio.Lock())

    async def start(self, bot_id: str) -> LiveBot:
        async with self._lock(bot_id):
            existing = self.bots.get(bot_id)
            if existing is not None and existing.state in (BotState.RUNNING, BotState.STARTING):
                return existing
            async with self.db.session() as s:
                record = await s.get(BotRecord, bot_id)
                if record is None:
                    raise KeyError(bot_id)
                conn = (
                    await s.get(BrokerConnection, record.broker_connection_id)
                    if record.broker_connection_id
                    else None
                )
                if conn is None:
                    raise ValueError("aucune connexion courtier associée à ce bot")
                notif = await s.get(NotificationSettings, record.user_id)
                settings = bot_settings(record, conn)
                token = self.box.decrypt(conn.token_encrypted, context=record.user_id)
                notifier = None
                if notif and notif.telegram_token_encrypted and notif.telegram_chat_id:
                    notifier = self.notifier_factory(
                        self.box.decrypt(notif.telegram_token_encrypted, context=record.user_id),
                        notif.telegram_chat_id,
                    )
                record.status = BotState.STARTING.value
                record.desired_state = "running"
                record.last_error = None
                await s.commit()
                name = record.name
                notify_trades = notif.notify_trades if notif else True

            bot = LiveBot(
                bot_id,
                name,
                settings,
                self.broker_factory(conn, token),
                db=self.db,
                notifier=notifier,
                notify_trades=notify_trades,
                calendar_source=self.calendar_source,
            )
            self.bots[bot_id] = bot
            try:
                await bot.start()
            except Exception as e:
                await self._set_status(bot_id, BotState.ERROR.value, str(e))
                raise
            await self._set_status(bot_id, BotState.RUNNING.value, None)
            return bot

    async def stop(self, bot_id: str, desired: str = "stopped") -> None:
        async with self._lock(bot_id):
            bot = self.bots.pop(bot_id, None)
            if bot is not None:
                await bot.stop()
            async with self.db.session() as s:
                record = await s.get(BotRecord, bot_id)
                if record is not None:
                    record.status = BotState.STOPPED.value
                    record.desired_state = desired
                    await s.commit()

    async def restart(self, bot_id: str) -> LiveBot:
        await self.stop(bot_id, desired="running")
        return await self.start(bot_id)

    async def _set_status(self, bot_id: str, status: str, error: str | None) -> None:
        async with self.db.session() as s:
            record = await s.get(BotRecord, bot_id)
            if record is not None:
                record.status = status
                record.last_error = error
                await s.commit()

    async def restore(self) -> None:
        """Relance les bots qui tournaient avant l'arrêt du serveur."""
        async with self.db.session() as s:
            ids = list(
                await s.scalars(select(BotRecord.id).where(BotRecord.desired_state == "running"))
            )
        for bot_id in ids:
            try:
                await self.start(bot_id)
            except Exception as e:
                log.error("bot %s : reprise impossible (%s)", bot_id, e)

    async def watch(self, every: float = 10.0) -> None:
        """Répercute en base les pannes survenues pendant l'exécution."""
        while True:
            await asyncio.sleep(every)
            for bot_id, bot in list(self.bots.items()):
                if bot.state is BotState.ERROR:
                    await self._set_status(bot_id, BotState.ERROR.value, bot.error)

    def start_watch(self) -> None:
        if self._watch is None:
            self._watch = asyncio.create_task(self.watch(), name="bot-supervisor")

    async def shutdown(self) -> None:
        """Arrête tous les bots en gardant l'état souhaité (ils repartiront au démarrage)."""
        if self._watch is not None:
            self._watch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watch
        for bot_id in list(self.bots):
            bot = self.bots.pop(bot_id)
            await bot.stop()
            await self._set_status(bot_id, BotState.STOPPED.value, None)


async def default_calendar() -> EconomicCalendar:
    async with httpx.AsyncClient() as client:
        return await fetch_calendar(client)
