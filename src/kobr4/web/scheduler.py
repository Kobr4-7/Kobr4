"""Optimisation automatique du week-end.

Chaque samedi (marché fermé), pour les bots où l'utilisateur l'a activée, le laboratoire
relance l'optimisation de chaque stratégie sur l'historique disponible. Les propositions
apparaissent dans le site et, si Telegram est configuré, un message prévient l'utilisateur.
Rien n'est appliqué sans validation.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from kobr4.config.settings import Settings
from kobr4.db.models import BotRecord, NotificationSettings, ProposalRecord
from kobr4.db.session import Database
from kobr4.lab.optimizer import LabSettings
from kobr4.live.alerts import Notifier, TelegramNotifier
from kobr4.marketdata.store import ParquetBarStore
from kobr4.security.crypto import SecretBox
from kobr4.strategies import STRATEGIES
from kobr4.web.jobs import JobRunner, optimize_job

log = logging.getLogger(__name__)

WEEKDAY = 5  # samedi
HOUR = 4  # UTC
HISTORY_YEARS = 6


def due(now: datetime, last: datetime | None) -> bool:
    """Vrai si l'optimisation de la semaine n'a pas encore tourné (samedi 4h UTC passé)."""
    if now.weekday() < WEEKDAY or (now.weekday() == WEEKDAY and now.hour < HOUR):
        return False
    week_start = (now - timedelta(days=now.weekday() - WEEKDAY)).replace(
        hour=HOUR, minute=0, second=0, microsecond=0
    )
    return last is None or last < week_start


class LabScheduler:
    def __init__(
        self,
        db: Database,
        box: SecretBox,
        jobs: JobRunner,
        data_dir: str,
        notifier_factory: Callable[[str, str], Notifier] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.db = db
        self.box = box
        self.jobs = jobs
        self.data_dir = data_dir
        self.notifier_factory = notifier_factory or (lambda t, c: TelegramNotifier(t, c))
        self.clock = clock
        self._task: asyncio.Task[None] | None = None

    def _range(self, symbols: list[str]) -> tuple[datetime, datetime] | None:
        store = ParquetBarStore(self.data_dir)
        covs = [store.coverage(s) for s in symbols]
        if not covs or any(c is None for c in covs):
            return None
        first = max(c.first for c in covs if c)
        last = min(c.last for c in covs if c)
        start = max(first, last - timedelta(days=365 * HISTORY_YEARS))
        return start, last

    async def tick(self) -> int:
        """Lance les optimisations dues. Renvoie le nombre de stratégies lancées."""
        now = self.clock()
        launched = 0
        async with self.db.session() as s:
            bots = list(await s.scalars(select(BotRecord).where(BotRecord.auto_optimize.is_(True))))
            for bot in bots:
                if not due(now, bot.last_auto_optimize_at):
                    continue
                bot.last_auto_optimize_at = now
                settings = Settings.model_validate(
                    {**bot.config, "mode": "backtest", "broker": {"name": "simulated"}}
                )
                for st in settings.strategies:
                    if not st.enabled or not STRATEGIES[st.kind].search_space:
                        continue
                    period = self._range(sorted(set(st.instruments)))
                    if period is None:
                        log.warning(
                            "bot %s : historique absent, optimisation de %s sautée", bot.id, st.id
                        )
                        continue
                    self._launch(bot.id, bot.user_id, bot.name, settings, st.id, *period)
                    launched += 1
            await s.commit()
        return launched

    def _launch(
        self,
        bot_id: str,
        user_id: str,
        bot_name: str,
        settings: Settings,
        strategy_id: str,
        start: datetime,
        end: datetime,
    ) -> None:
        lab = LabSettings()

        async def done(result: dict[str, Any] | None, error: str | None) -> None:
            if result is None:
                log.warning(
                    "optimisation automatique %s/%s en échec : %s", bot_id, strategy_id, error
                )
                return
            async with self.db.session() as s:
                s.add(
                    ProposalRecord(
                        id=result["id"],
                        user_id=user_id,
                        bot_id=bot_id,
                        strategy_id=strategy_id,
                        status=result["status"],
                        payload=result,
                    )
                )
                notif = await s.get(NotificationSettings, user_id)
                await s.commit()
            if notif and notif.telegram_token_encrypted and notif.telegram_chat_id:
                verdict = (
                    "à valider dans le Labo"
                    if result["status"] == "proposed"
                    else "critères non remplis, réglages actuels conservés"
                )
                try:
                    notifier = self.notifier_factory(
                        self.box.decrypt(notif.telegram_token_encrypted, context=user_id),
                        notif.telegram_chat_id,
                    )
                    await notifier.send(
                        f"[{bot_name}] 🧪 Optimisation de {strategy_id} terminée : {verdict}."
                    )
                except Exception as e:
                    log.warning("message Telegram non envoyé : %s", e)

        self.jobs.submit(
            self.jobs.lab,
            optimize_job,
            settings.model_dump_json(),
            strategy_id,
            self.data_dir,
            lab.model_dump_json(),
            start,
            end,
            done=done,
        )

    async def run(self, every: float = 3600) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("planificateur du laboratoire")
            await asyncio.sleep(every)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="lab-scheduler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
