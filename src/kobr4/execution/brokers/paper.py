"""Compte démo interne : argent fictif, prix réels.

Les ordres sont exécutés par le courtier simulé du backtest (`SimulatedBroker`), mais
sur les cotations en direct de Finnhub (flux WebSocket gratuit, prix OANDA) au lieu
de bougies historiques :
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
from kobr4.core.instruments import get_instrument
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
SPREAD_PIPS = {
    "EUR/USD": Decimal("0.6"),
    "GBP/USD": Decimal("0.9"),
    "USD/JPY": Decimal("0.7"),
    "USD/CHF": Decimal("1.0"),
    "AUD/USD": Decimal("0.8"),
    "USD/CAD": Decimal("1.0"),
    "EUR/GBP": Decimal("0.9"),
}
DEFAULT_SPREAD_PIPS = Decimal("1.2")
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
    spread = inst.from_pips(SPREAD_PIPS.get(symbol, DEFAULT_SPREAD_PIPS))
    bid = inst.round_price(mid - spread / 2)
    return Tick(symbol=symbol, bid=bid, ask=bid + spread, ts=ts)


class PaperBroker(SimulatedBroker):
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
        wanted = set(symbols)
        async with self.connect_ws(f"{FINNHUB_WS}?token={self.api_key}") as ws:
            for s in symbols:
                await ws.send(json.dumps({"type": "subscribe", "symbol": to_finnhub(s)}))
            async for raw in ws:
                msg: dict[str, Any] = json.loads(raw)
                kind = msg.get("type")
                if kind == "error":
                    raise BrokerError(f"Finnhub : {msg.get('msg', 'erreur inconnue')}")
                if kind != "trade":
                    continue  # ping
                for t in msg.get("data") or []:
                    symbol = from_finnhub(str(t.get("s", "")))
                    if symbol not in wanted or t.get("p") is None:
                        continue
                    ts = datetime.fromtimestamp(int(t.get("t", 0)) / 1000, UTC)
                    tick = make_tick(symbol, Decimal(str(t["p"])), ts)
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
