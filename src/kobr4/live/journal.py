"""Journal d'un bot : événements, trades et courbe d'équité enregistrés en base.

Les écritures sont regroupées (toutes les 2 secondes ou par 200) pour ne pas ralentir
le bot. Les cotations et les bougies ne sont pas journalisées (trop nombreuses et déjà
disponibles chez le courtier) ; l'équité est gardée une fois par minute.
"""

import asyncio
import contextlib
import logging
from datetime import datetime
from typing import Any

from kobr4.core.bus import EventBus
from kobr4.core.events import BarClosed, EquitySnapshot, Event, PositionClosed, TickReceived
from kobr4.db.models import BotEvent, EquityPoint, TradeRecord
from kobr4.db.session import Database

log = logging.getLogger(__name__)

_SKIPPED = (TickReceived, BarClosed, EquitySnapshot)


class Journal:
    def __init__(self, db: Database, bot_id: str, bus: EventBus, flush_every: float = 2.0) -> None:
        self.db = db
        self.bot_id = bot_id
        self.flush_every = flush_every
        self._events: list[BotEvent] = []
        self._trades: list[TradeRecord] = []
        self._equity: list[EquityPoint] = []
        self._last_minute: datetime | None = None
        self._task: asyncio.Task[None] | None = None
        self.write_errors = 0
        bus.subscribe(Event, self._on_event)

    def _on_event(self, e: Event) -> None:
        if isinstance(e, EquitySnapshot):
            minute = e.ts.replace(second=0, microsecond=0)
            if minute != self._last_minute:
                self._last_minute = minute
                self._equity.append(
                    EquityPoint(
                        bot_id=self.bot_id,
                        ts=minute,
                        balance=e.balance,
                        equity=e.equity,
                        open_positions=e.open_positions,
                    )
                )
            return
        if isinstance(e, _SKIPPED):
            return
        payload: dict[str, Any] = e.model_dump(mode="json", exclude={"ts"})
        self._events.append(
            BotEvent(bot_id=self.bot_id, ts=e.ts, type=type(e).__name__, payload=payload)
        )
        if isinstance(e, PositionClosed):
            t = e.trade
            self._trades.append(
                TradeRecord(
                    bot_id=self.bot_id,
                    position_id=t.position_id,
                    strategy_id=t.strategy_id,
                    symbol=t.symbol,
                    side=str(t.side),
                    quantity=t.quantity,
                    entry_price=t.entry_price,
                    exit_price=t.exit_price,
                    opened_at=t.opened_at,
                    closed_at=t.closed_at,
                    reason=str(t.reason),
                    pnl=t.pnl,
                    commission=t.commission,
                    financing=t.financing,
                )
            )
        if len(self._events) >= 200:
            self._schedule_flush()

    def _schedule_flush(self) -> None:
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(self.flush())

    async def flush(self) -> int:
        events, trades, equity = self._events, self._trades, self._equity
        self._events, self._trades, self._equity = [], [], []
        if not (events or trades or equity):
            return 0
        try:
            async with self.db.session() as s:
                s.add_all(events)
                s.add_all(trades)
                for point in equity:
                    await s.merge(point)
                await s.commit()
        except Exception:
            self.write_errors += 1
            log.exception("journal du bot %s : écriture impossible", self.bot_id)
            # On garde les données pour la prochaine tentative (dans la limite du raisonnable).
            self._events = (events + self._events)[-10_000:]
            self._trades = trades + self._trades
            self._equity = (equity + self._equity)[-1_000:]
            return 0
        return len(events) + len(trades) + len(equity)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.flush_every)
            await self.flush()

    async def aclose(self) -> None:
        await self.flush()
