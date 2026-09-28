"""`kobr4 run` : un bot seul, sans la plateforme web, configuré par fichier et variables
d'environnement. Utile sur un serveur sans interface, ou pour tester."""

import asyncio
import contextlib
import logging
import os
import signal

import httpx

from kobr4.config.settings import Mode, Settings
from kobr4.core.clock import LiveClock
from kobr4.db import Database
from kobr4.execution.brokers.oanda import OandaBroker
from kobr4.live.alerts import Notifier, TelegramNotifier
from kobr4.live.bot import LiveBot
from kobr4.risk.calendar import EconomicCalendar
from kobr4.risk.calendar_feed import fetch_calendar

log = logging.getLogger("kobr4")


def build_bot(settings: Settings) -> tuple[LiveBot, Database | None]:
    b = settings.broker
    if b.name != "oanda":
        raise ValueError(
            "kobr4 run exige un courtier réel (oanda) ; utiliser `kobr4 backtest` sinon"
        )
    account_id = b.account_id or os.environ.get("KOBR4_OANDA_ACCOUNT_ID")
    if not account_id:
        raise ValueError(
            "identifiant de compte absent : broker.account_id ou KOBR4_OANDA_ACCOUNT_ID"
        )
    clock = LiveClock()
    broker = OandaBroker(b.api_key().get_secret_value(), account_id, b.environment, clock)
    db_url = os.environ.get("KOBR4_DATABASE_URL")
    db = Database(db_url) if db_url else None
    notifier: Notifier | None = None
    token, chat = os.environ.get("KOBR4_TELEGRAM_TOKEN"), os.environ.get("KOBR4_TELEGRAM_CHAT_ID")
    if token and chat:
        notifier = TelegramNotifier(token, chat)

    async def calendar_source() -> EconomicCalendar:
        async with httpx.AsyncClient() as client:
            return await fetch_calendar(client)

    bot = LiveBot(
        os.environ.get("KOBR4_BOT_ID", "standalone"),
        os.environ.get("KOBR4_BOT_NAME", "Kobr4"),
        settings,
        broker,
        db=db,
        notifier=notifier,
        calendar_source=calendar_source,
        clock=clock,
    )
    return bot, db


async def run_standalone(settings: Settings) -> int:
    bot, db = build_bot(settings)
    if db is not None:
        await db.create_all()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    await bot.start()
    if settings.mode is Mode.LIVE:
        log.warning("MODE RÉEL : les ordres engagent de l'argent réel.")
    try:
        await stop.wait()
    finally:
        await bot.stop()
        if db is not None:
            await db.close()
    return 0
