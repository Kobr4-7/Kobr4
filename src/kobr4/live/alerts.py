"""Alertes : messages courts envoyés à l'utilisateur (Telegram) quand quelque chose
mérite son attention."""

import logging
import time
from collections.abc import Callable
from typing import Protocol

import httpx

from kobr4.core.bus import EventBus
from kobr4.core.events import (
    Event,
    KillSwitchActivated,
    MarketDataStale,
    OrderStatusChanged,
    PositionClosed,
    PositionOpened,
    ReconciliationMismatch,
    RiskLimitReached,
)
from kobr4.core.orders import OrderStatus

log = logging.getLogger(__name__)


class Notifier(Protocol):
    async def send(self, text: str) -> None: ...


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, client: httpx.AsyncClient | None = None) -> None:
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.client = client or httpx.AsyncClient(timeout=10)

    async def send(self, text: str) -> None:
        resp = await self.client.post(
            self.url, json={"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Telegram a refusé le message (HTTP {resp.status_code})")


class AlertRouter:
    """Traduit les événements importants en messages, sans répéter la même alerte plus
    d'une fois par `cooldown` secondes."""

    def __init__(
        self,
        bus: EventBus,
        notifier: Notifier,
        bot_name: str,
        notify_trades: bool = True,
        cooldown: float = 900,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.notifier = notifier
        self.name = bot_name
        self.cooldown = cooldown
        self.clock = clock
        self._last: dict[str, float] = {}
        self.sent: list[str] = []
        self.failures = 0
        self._on(bus, KillSwitchActivated, lambda e: ("kill", f"⛔ Arrêt d'urgence : {e.reason}"))
        self._on(
            bus, RiskLimitReached, lambda e: (f"limit:{e.rule}", f"⚠️ Limite atteinte : {e.detail}")
        )
        self._on(
            bus,
            ReconciliationMismatch,
            lambda e: ("recon", f"⚠️ Écart avec le courtier : {e.detail}"),
        )
        self._on(
            bus,
            MarketDataStale,
            lambda e: (
                f"stale:{e.symbol}",
                f"📡 Plus de cotations sur {e.symbol} : nouvelles entrées suspendues",
            ),
        )
        self._on(bus, OrderStatusChanged, self._order_msg)
        if notify_trades:
            self._on(bus, PositionOpened, self._opened_msg)
            self._on(bus, PositionClosed, self._closed_msg)

    def _on[E: Event](
        self, bus: EventBus, kind: type[E], build: Callable[[E], tuple[str, str] | None]
    ) -> None:
        async def handler(e: E) -> None:
            msg = build(e)
            if msg is not None:
                await self.alert(*msg)

        bus.subscribe(kind, handler)

    @staticmethod
    def _order_msg(e: OrderStatusChanged) -> tuple[str, str] | None:
        if e.order.status is not OrderStatus.REJECTED:
            return None
        return (
            f"reject:{e.order.id}",
            f"❌ Ordre refusé par le courtier ({e.order.symbol}) : {e.order.reject_reason}",
        )

    @staticmethod
    def _opened_msg(e: PositionOpened) -> tuple[str, str]:
        p = e.position
        side = "Achat" if p.side == "buy" else "Vente"
        return (
            f"open:{p.id}",
            f"{side} {p.quantity:,} {p.symbol} à {p.entry_price} (stop {p.stop_loss}"
            f"{', objectif ' + str(p.take_profit) if p.take_profit else ''})".replace(",", " ", 1),
        )

    @staticmethod
    def _closed_msg(e: PositionClosed) -> tuple[str, str]:
        t = e.trade
        sign = "✅" if t.pnl > 0 else "🔻"
        return (f"close:{t.position_id}", f"{sign} {t.symbol} fermé ({t.reason}) : {t.pnl:+.2f}")

    async def alert(self, key: str, text: str) -> None:
        now = self.clock()
        last = self._last.get(key)
        if last is not None and now - last < self.cooldown:
            return
        self._last[key] = now
        message = f"[{self.name}] {text}"
        try:
            await self.notifier.send(message)
            self.sent.append(message)
        except Exception as exc:
            self.failures += 1
            log.warning("alerte non envoyée : %s", exc)
