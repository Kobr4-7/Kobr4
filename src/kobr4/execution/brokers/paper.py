"""Compte démo interne : argent fictif, prix réels.

Les ordres sont exécutés par le courtier simulé du backtest (`SimulatedBroker`), mais
sur les cotations en direct de Finnhub (flux WebSocket gratuit, prix OANDA) au lieu
de bougies historiques :
- une seule connexion Finnhub par clé, partagée par tous les bots du serveur (Finnhub
  n'accepte qu'une connexion à la fois par clé : deux bots se déconnecteraient l'un
  l'autre et les cotations s'arrêteraient) ;
- chaque cotation met à jour les prix, puis déclenche les stops et objectifs touchés
  (stop exécuté au prix du moment, donc au-delà du stop en cas de décalage ; objectif
  exécuté à son niveau) ;
- Finnhub donne un prix milieu : le bid et l'ask sont reconstitués avec un spread
  typique de chaque paire ;
- le compte (solde, positions, derniers ordres) est enregistré dans un fichier JSON
  après chaque changement, et retrouvé au redémarrage ;
- l'historique d'amorçage des indicateurs vient des bougies M1 de Dukascopy, stockées
  avec les données de backtest (les jours manquants sont téléchargés au démarrage).
"""

import asyncio
import json
import logging
import math
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import polars as pl

from kobr4.core.clock import Clock, LiveClock
from kobr4.core.instruments import get_instrument, typical_spread_pips
from kobr4.core.models import Bar, ExitReason, Position, Tick
from kobr4.core.orders import Order, OrderStatus
from kobr4.core.prices import MissingPriceError, PriceBook
from kobr4.core.types import Timeframe
from kobr4.execution.broker import BrokerError
from kobr4.execution.brokers.simulated import SimulatedBroker
from kobr4.marketdata.sources import dukascopy
from kobr4.marketdata.store import ParquetBarStore

log = logging.getLogger(__name__)

FINNHUB_WS = "wss://ws.finnhub.io"
FINNHUB_API = "https://finnhub.io/api/v1"
KEEP_ORDERS = 200


class WebSocket(Protocol):
    async def send(self, message: str) -> None: ...

    def __aiter__(self) -> AsyncIterator[str | bytes]: ...


Connector = Callable[[str], AbstractAsyncContextManager[WebSocket]]


def _default_connect(url: str) -> AbstractAsyncContextManager[WebSocket]:
    from websockets.asyncio.client import connect

    return connect(url, open_timeout=15, ping_interval=20)


def to_finnhub(symbol: str) -> str:
    return "OANDA:" + symbol.replace("/", "_")


def from_finnhub(code: str) -> str:
    return code.removeprefix("OANDA:").replace("_", "/")


def make_tick(symbol: str, mid: Decimal, ts: datetime) -> Tick:
    inst = get_instrument(symbol)
    spread = inst.from_pips(typical_spread_pips(symbol))
    bid = inst.round_price(mid - spread / 2)
    return Tick(symbol=symbol, bid=bid, ask=bid + spread, ts=ts)


END = object()
"""Fin normale du flux (connexion fermée par Finnhub)."""

type Quote = tuple[str, Decimal, datetime]


class FinnhubFeed:
    """Connexion WebSocket Finnhub partagée entre tous les abonnés d'une même clé.

    Chaque abonné reçoit les cotations de ses paires ; la connexion s'ouvre avec le
    premier abonné et se ferme avec le dernier. Les paires d'un nouvel abonné sont
    ajoutées à la connexion déjà ouverte."""

    def __init__(self, url: str, connect_ws: Connector) -> None:
        self.url = url
        self.connect_ws = connect_ws
        self.subscribers: dict[int, tuple[set[str], asyncio.Queue[Any]]] = {}
        self.task: asyncio.Task[None] | None = None
        self.ws: WebSocket | None = None
        self.subscribed: set[str] = set()
        self._next = 0

    async def quotes(self, symbols: list[str]) -> AsyncIterator[Quote]:
        key = self._next
        self._next += 1
        queue: asyncio.Queue[Any] = asyncio.Queue()
        self.subscribers[key] = (set(symbols), queue)
        try:
            if self.task is None or self.task.done():
                self.task = asyncio.create_task(self._run())
            elif self.ws is not None:
                await self._sync(self.ws)
            while True:
                item = await queue.get()
                if item is END:
                    return
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            del self.subscribers[key]
            if not self.subscribers and self.task is not None:
                self.task.cancel()
                self.task = None

    async def _sync(self, ws: WebSocket) -> None:
        wanted = set().union(*(s for s, _ in self.subscribers.values()))
        for sym in sorted(wanted - self.subscribed):
            self.subscribed.add(sym)
            await ws.send(json.dumps({"type": "subscribe", "symbol": to_finnhub(sym)}))

    def _broadcast(self, item: Any, symbol: str | None = None) -> None:
        for symbols, queue in self.subscribers.values():
            if symbol is None or symbol in symbols:
                queue.put_nowait(item)

    async def _run(self) -> None:
        self.subscribed = set()
        try:
            async with self.connect_ws(self.url) as ws:
                self.ws = ws
                await self._sync(ws)
                async for raw in ws:
                    msg: dict[str, Any] = json.loads(raw)
                    kind = msg.get("type")
                    if kind == "error":
                        raise BrokerError(f"Finnhub : {msg.get('msg', 'erreur inconnue')}")
                    if kind != "trade":
                        continue  # ping
                    for t in msg.get("data") or []:
                        if t.get("p") is None:
                            continue
                        symbol = from_finnhub(str(t.get("s", "")))
                        ts = datetime.fromtimestamp(int(t.get("t", 0)) / 1000, UTC)
                        self._broadcast((symbol, Decimal(str(t["p"])), ts), symbol)
            self._broadcast(END)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._broadcast(e)
        finally:
            self.ws = None


_feeds: dict[tuple[str, Connector], FinnhubFeed] = {}


def finnhub_feed(api_key: str, connect_ws: Connector) -> FinnhubFeed:
    """La connexion partagée de cette clé (créée au premier appel)."""
    feed = _feeds.get((api_key, connect_ws))
    if feed is None:
        feed = _feeds[(api_key, connect_ws)] = FinnhubFeed(
            f"{FINNHUB_WS}?token={api_key}", connect_ws
        )
    return feed


class PaperBroker(SimulatedBroker):
    stale_after = 300.0
    """Finnhub n'envoie une cotation que quand le prix change : la nuit, une paire
    calme peut rester plus d'une minute sans cotation sans que le flux soit coupé."""

    def __init__(
        self,
        api_key: str,
        state_path: Path,
        store: ParquetBarStore | None = None,
        clock: Clock | None = None,
        currency: str = "USD",
        initial_balance: Decimal = Decimal(10_000),
        slippage_pips: Decimal = Decimal("0.2"),
        connect_ws: Connector | None = None,
        fetch: dukascopy.Fetcher | None = None,
    ) -> None:
        super().__init__(
            PriceBook(),
            clock or LiveClock(),
            account_currency=currency,
            initial_balance=initial_balance,
            slippage_pips=slippage_pips,
        )
        self.api_key = api_key
        self.state_path = state_path
        self.store = store
        self.connect_ws = connect_ws or _default_connect
        self.fetch = fetch

    # Compte enregistré

    async def connect(self) -> None:
        if self.state_path.exists():
            self._load()
        else:
            self._save()

    def _load(self) -> None:
        d = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.account_currency = d.get("currency", self.account_currency)
        self.balance = Decimal(d["balance"])
        self._positions = {p["id"]: Position.model_validate(p) for p in d.get("positions", [])}
        self._orders = {o["id"]: Order.model_validate(o) for o in d.get("orders", [])}

    def _save(self) -> None:
        orders = list(self._orders.values())[-KEEP_ORDERS:]
        self._orders = {o.id: o for o in orders}
        data = {
            "currency": self.account_currency,
            "balance": str(self.balance),
            "positions": [p.model_dump(mode="json") for p in self._positions.values()],
            "orders": [o.model_dump(mode="json") for o in orders],
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(self.state_path)

    async def submit(self, order: Order) -> Order:
        if self.prices.get(order.symbol) is None:
            now = self.clock.now()
            return order.transition(OrderStatus.SUBMITTED, now).transition(
                OrderStatus.REJECTED,
                now,
                reject_reason=f"pas encore de cotation pour {order.symbol}",
            )
        result = await super().submit(order)
        self._save()
        return result

    async def modify_position(
        self, position_id: str, stop_loss: Decimal, take_profit: Decimal | None
    ) -> Position:
        pos = await super().modify_position(position_id, stop_loss, take_profit)
        self._save()
        return pos

    async def _close(
        self, pos: Position, exit_price: Decimal, reason: ExitReason, ts: datetime
    ) -> None:
        await super()._close(pos, exit_price, reason, ts)
        self._save()

    # Prix en direct

    async def stream_prices(self, symbols: list[str]) -> AsyncIterator[Tick]:
        async for symbol, mid, ts in finnhub_feed(self.api_key, self.connect_ws).quotes(symbols):
            tick = make_tick(symbol, mid, ts)
            self.prices.update(tick)
            await self._check_protections(tick)
            yield tick

    async def _check_protections(self, tick: Tick) -> None:
        for pos in [p for p in self._positions.values() if p.symbol == tick.symbol]:
            try:
                self.prices.rate(get_instrument(pos.symbol).quote, self.account_currency)
            except MissingPriceError:
                continue  # conversion impossible pour l'instant : on attend un prix
            s = pos.side.sign
            px = pos.exit_price(tick)
            if (px - pos.stop_loss) * s <= 0:
                await self._close(pos, px, ExitReason.STOP_LOSS, tick.ts)
            elif pos.take_profit is not None and (px - pos.take_profit) * s >= 0:
                await self._close(pos, pos.take_profit, ExitReason.TAKE_PROFIT, tick.ts)

    # Historique d'amorçage

    async def history(self, symbol: str, timeframe: Timeframe, count: int) -> list[Bar]:
        if self.store is None:
            return []
        today = self.clock.now().date()
        days = math.ceil(count * timeframe.duration / timedelta(days=1) * 7 / 5) + 3
        start = today - timedelta(days=days)
        await asyncio.to_thread(self._fill, symbol, start, today - timedelta(days=1))
        since = datetime.combine(start, time(), UTC)
        bars = await asyncio.to_thread(self.store.bars, symbol, timeframe, since, None)
        return bars[-count:]

    def _fill(self, symbol: str, start: date, end: date) -> None:
        """Télécharge chez Dukascopy les jours manquants de [start, end]."""
        assert self.store is not None
        present = self.store.days_present(symbol, start, end)
        batch: list[pl.DataFrame] = []
        try:
            for _, df in dukascopy.download(
                symbol, start, end, skip=present, workers=2, fetch=self.fetch
            ):
                batch.append(df)
        except (OSError, dukascopy.DataFormatError) as e:
            log.warning("%s : historique récent incomplet (%s)", symbol, e)
        if batch:
            self.store.write_m1(symbol, pl.concat(batch))
