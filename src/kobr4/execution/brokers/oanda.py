"""Adaptateur OANDA (API REST v20).

Correspondances :
- une position Kobr4 = un « trade » OANDA ; l'identifiant client du trade est
  l'identifiant de l'ordre Kobr4 (`clientExtensions.id`), ce qui permet de le retrouver
  et de le fermer par `@<id>` même après un redémarrage ;
- le stop loss est posé à l'exécution par distance (`stopLossOnFill.distance`), donc
  toujours du bon côté du prix réel ; le take profit est ensuite posé au prix exact
  (prix d'exécution ± distance) ;
- les fermetures par stop ou objectif, décidées par OANDA, arrivent par le flux des
  transactions. Chaque exécution est traitée une seule fois, qu'elle arrive par la
  réponse HTTP ou par le flux.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

import httpx

from kobr4.core.clock import Clock
from kobr4.core.instruments import get_instrument
from kobr4.core.models import Account, Bar, ClosedTrade, ExitReason, Position, Tick
from kobr4.core.orders import Order, OrderStatus
from kobr4.core.types import OrderType, Side, Timeframe
from kobr4.execution.broker import Broker, BrokerError

log = logging.getLogger(__name__)

HOSTS = {
    "practice": ("https://api-fxpractice.oanda.com", "https://stream-fxpractice.oanda.com"),
    "live": ("https://api-fxtrade.oanda.com", "https://stream-fxtrade.oanda.com"),
}
GRANULARITY = {
    Timeframe.M1: "M1",
    Timeframe.M5: "M5",
    Timeframe.M15: "M15",
    Timeframe.M30: "M30",
    Timeframe.H1: "H1",
    Timeframe.H4: "H4",
    Timeframe.D1: "D",
}
_EXIT_REASONS = {
    "STOP_LOSS_ORDER": ExitReason.STOP_LOSS,
    "TRAILING_STOP_LOSS_ORDER": ExitReason.STOP_LOSS,
    "TAKE_PROFIT_ORDER": ExitReason.TAKE_PROFIT,
    "MARKET_ORDER_TRADE_CLOSE": ExitReason.MANUAL,
    "MARKET_ORDER_POSITION_CLOSEOUT": ExitReason.MANUAL,
    "MARKET_ORDER_MARGIN_CLOSEOUT": ExitReason.KILL_SWITCH,
}


def to_oanda(symbol: str) -> str:
    return symbol.replace("/", "_")


def from_oanda(instrument: str) -> str:
    return instrument.replace("_", "/")


def parse_time(value: str) -> datetime:
    """Heure OANDA (RFC 3339, nanosecondes) → datetime UTC."""
    main, _, frac = value.rstrip("Z").partition(".")
    micro = (frac + "000000")[:6]
    return datetime.fromisoformat(f"{main}.{micro}").replace(tzinfo=UTC)


class OandaError(BrokerError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class OandaBroker(Broker):
    def __init__(
        self,
        token: str,
        account_id: str,
        environment: Literal["practice", "live"],
        clock: Clock,
        transport: httpx.AsyncBaseTransport | None = None,
        stream_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__()
        api, stream = HOSTS[environment]
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept-Datetime-Format": "RFC3339",
            "Content-Type": "application/json",
        }
        self.http = httpx.AsyncClient(
            base_url=api, headers=headers, timeout=httpx.Timeout(10.0), transport=transport
        )
        self.stream_http = httpx.AsyncClient(
            base_url=stream,
            headers=headers,
            timeout=httpx.Timeout(10.0, read=30.0),
            transport=stream_transport or transport,
        )
        self.account_id = account_id
        self.environment = environment
        self.clock = clock
        self.account_currency = "USD"
        self._trades: dict[str, Position] = {}
        """Positions ouvertes, par identifiant de trade OANDA."""
        self._tx_task: asyncio.Task[None] | None = None
        self._fill_lock = asyncio.Lock()
        """Une exécution peut arriver deux fois (réponse HTTP et flux) : traitement en série."""
        self._last_tx_id: str | None = None

    # Connexion

    async def connect(self) -> None:
        summary = (await self._get(f"/v3/accounts/{self.account_id}/summary"))["account"]
        self.account_currency = summary["currency"]
        self._last_tx_id = summary.get("lastTransactionID")
        await self._load_trades()
        if self._tx_task is None:
            self._tx_task = asyncio.create_task(
                self._transactions_loop(), name="oanda-transactions"
            )

    async def close(self) -> None:
        if self._tx_task is not None:
            self._tx_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._tx_task
            self._tx_task = None
        await self.http.aclose()
        await self.stream_http.aclose()

    # HTTP

    async def _request(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        try:
            resp = await self.http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise OandaError(f"OANDA injoignable : {e}") from e
        try:
            data: dict[str, Any] = resp.json() if resp.content else {}
        except ValueError:
            data = {}
        if resp.status_code >= 400:
            message = data.get("errorMessage") or resp.text[:200] or resp.reason_phrase
            if resp.status_code == 401:
                message = "jeton API refusé par OANDA"
            raise OandaError(f"{message} (HTTP {resp.status_code})", resp.status_code)
        return data

    async def _get(self, path: str, **kw: Any) -> dict[str, Any]:
        return await self._request("GET", path, **kw)

    # Positions

    def _position(self, trade: dict[str, Any]) -> Position:
        symbol = from_oanda(trade["instrument"])
        units = int(Decimal(trade["currentUnits"]))
        client_id = (trade.get("clientExtensions") or {}).get("id")
        tag = (trade.get("clientExtensions") or {}).get("tag") or "externe"
        sl = (trade.get("stopLossOrder") or {}).get("price")
        tp = (trade.get("takeProfitOrder") or {}).get("price")
        entry = Decimal(trade["price"])
        side = Side.BUY if units > 0 else Side.SELL
        if sl is None:
            # Trade sans stop (ouvert hors du bot) : stop fictif très éloigné, signalé.
            log.warning("trade OANDA %s sans stop loss", trade["id"])
            sl = str(entry * (Decimal("0.5") if side is Side.BUY else Decimal("1.5")))
        return Position(
            id=client_id or f"oanda-{trade['id']}",
            strategy_id=tag,
            symbol=symbol,
            side=side,
            quantity=abs(units),
            entry_price=entry,
            stop_loss=Decimal(sl),
            take_profit=Decimal(tp) if tp else None,
            opened_at=parse_time(trade["openTime"]),
        )

    async def _load_trades(self) -> None:
        data = await self._get(f"/v3/accounts/{self.account_id}/openTrades")
        self._trades = {t["id"]: self._position(t) for t in data.get("trades", [])}

    def _trade_id(self, position_id: str) -> str:
        for trade_id, pos in self._trades.items():
            if pos.id == position_id:
                return trade_id
        raise BrokerError(f"position inconnue : {position_id}")

    async def positions(self) -> list[Position]:
        await self._load_trades()
        return list(self._trades.values())

    async def account(self) -> Account:
        a = (await self._get(f"/v3/accounts/{self.account_id}/summary"))["account"]
        return Account(
            currency=a["currency"],
            balance=Decimal(a["balance"]),
            equity=Decimal(a["NAV"]),
            margin_used=Decimal(a["marginUsed"]),
            ts=self.clock.now(),
        )

    # Ordres

    async def submit(self, order: Order) -> Order:
        now = self.clock.now()
        order = order.transition(OrderStatus.SUBMITTED, now)
        if order.order_type is not OrderType.MARKET:
            return order.transition(
                OrderStatus.REJECTED, now, reject_reason="seuls les ordres au marché sont gérés"
            )
        inst = get_instrument(order.symbol)
        ext = {"id": order.id, "tag": order.strategy_id}
        body = {
            "order": {
                "type": "MARKET",
                "instrument": to_oanda(order.symbol),
                "units": str(order.quantity * order.side.sign),
                "timeInForce": "FOK",
                "positionFill": "OPEN_ONLY",
                "clientExtensions": ext,
                "tradeClientExtensions": ext,
                "stopLossOnFill": {
                    "distance": f"{inst.round_price(order.stop_loss_distance)}",
                    "timeInForce": "GTC",
                },
            }
        }
        try:
            data = await self._request("POST", f"/v3/accounts/{self.account_id}/orders", json=body)
        except OandaError as e:
            return order.transition(OrderStatus.REJECTED, self.clock.now(), reject_reason=str(e))
        cancel = data.get("orderCancelTransaction")
        fill = data.get("orderFillTransaction")
        if cancel or not fill:
            reason = (cancel or {}).get("reason", "ordre non exécuté")
            return order.transition(OrderStatus.REJECTED, self.clock.now(), reject_reason=reason)
        await self._handle_fill(fill)
        opened = fill.get("tradeOpened") or {}
        price = Decimal(fill["price"])
        result = order.transition(
            OrderStatus.FILLED,
            parse_time(fill["time"]),
            filled_quantity=abs(int(Decimal(opened.get("units", order.quantity)))),
            avg_fill_price=price,
            broker_order_id=fill.get("orderID"),
        )
        if order.take_profit_distance is not None and opened.get("tradeID"):
            _, tp = order.protection_prices(price)
            assert tp is not None
            try:
                await self._set_protection(opened["tradeID"], None, inst.round_price(tp))
            except OandaError as e:
                log.error("ordre %s : objectif non posé (%s), le stop reste en place", order.id, e)
        return result

    async def _set_protection(
        self,
        trade_id: str,
        stop_loss: Decimal | None,
        take_profit: Decimal | None,
        clear_tp: bool = False,
    ) -> None:
        body: dict[str, Any] = {}
        if stop_loss is not None:
            body["stopLoss"] = {"price": str(stop_loss), "timeInForce": "GTC"}
        if take_profit is not None:
            body["takeProfit"] = {"price": str(take_profit), "timeInForce": "GTC"}
        elif clear_tp:
            body["takeProfit"] = None
        await self._request(
            "PUT", f"/v3/accounts/{self.account_id}/trades/{trade_id}/orders", json=body
        )
        data = await self._get(f"/v3/accounts/{self.account_id}/trades/{trade_id}")
        pos = self._position(data["trade"])
        self._trades[trade_id] = pos
        await self.listener.on_position_modified(pos)

    async def modify_position(
        self, position_id: str, stop_loss: Decimal, take_profit: Decimal | None
    ) -> Position:
        trade_id = self._trade_id(position_id)
        inst = get_instrument(self._trades[trade_id].symbol)
        await self._set_protection(
            trade_id,
            inst.round_price(stop_loss),
            None if take_profit is None else inst.round_price(take_profit),
            clear_tp=take_profit is None,
        )
        return self._trades[trade_id]

    async def close_position(self, position_id: str, reason: ExitReason) -> None:
        trade_id = self._trade_id(position_id)
        data = await self._request(
            "PUT", f"/v3/accounts/{self.account_id}/trades/{trade_id}/close", json={"units": "ALL"}
        )
        fill = data.get("orderFillTransaction")
        if fill is None:
            cancel = data.get("orderCancelTransaction") or {}
            raise OandaError(f"fermeture refusée : {cancel.get('reason', 'inconnue')}")
        await self._handle_fill(fill, reason)

    async def find_order(self, order_id: str) -> Order | None:
        try:
            data = await self._get(f"/v3/accounts/{self.account_id}/orders/@{order_id}")
        except OandaError as e:
            if e.status == 404:
                return None
            raise
        o = data["order"]
        state = o.get("state")
        now = self.clock.now()
        units = int(Decimal(o.get("units", "0")))
        base = Order(
            id=order_id,
            strategy_id=(o.get("clientExtensions") or {}).get("tag", "?"),
            symbol=from_oanda(o.get("instrument", "EUR_USD")),
            side=Side.BUY if units >= 0 else Side.SELL,
            order_type=OrderType.MARKET,
            quantity=max(abs(units), 1),
            stop_loss_distance=Decimal("0.0001"),
            status=OrderStatus.SUBMITTED,
            broker_order_id=o.get("id"),
            created_at=now,
            updated_at=now,
        )
        match state:
            case "FILLED":
                return base.transition(OrderStatus.FILLED, now, filled_quantity=base.quantity)
            case "CANCELLED":
                return base.transition(
                    OrderStatus.REJECTED, now, reject_reason=o.get("cancellingTransactionID")
                )
            case _:
                return base.transition(OrderStatus.ACCEPTED, now)

    # Exécutions (réponse HTTP ou flux des transactions)

    async def _handle_fill(self, tx: dict[str, Any], reason: ExitReason | None = None) -> None:
        async with self._fill_lock:
            await self._apply_fill(tx, reason)

    async def _apply_fill(self, tx: dict[str, Any], reason: ExitReason | None) -> None:
        ts = parse_time(tx["time"])
        opened = tx.get("tradeOpened")
        if opened and opened["tradeID"] not in self._trades:
            data = await self._get(f"/v3/accounts/{self.account_id}/trades/{opened['tradeID']}")
            pos = self._position(data["trade"])
            self._trades[opened["tradeID"]] = pos
            await self.listener.on_position_opened(pos)
        # Kobr4 ne ferme jamais partiellement : seuls les trades fermés entièrement comptent.
        for closed in tx.get("tradesClosed") or []:
            gone = self._trades.pop(closed["tradeID"], None)
            if gone is None:
                continue  # déjà traité
            why = reason or _EXIT_REASONS.get(tx.get("reason", ""), ExitReason.MANUAL)
            financing = Decimal(closed.get("financing", "0"))
            commission = Decimal(tx.get("commission", "0"))
            await self.listener.on_position_closed(
                ClosedTrade(
                    position_id=gone.id,
                    strategy_id=gone.strategy_id,
                    symbol=gone.symbol,
                    side=gone.side,
                    quantity=abs(int(Decimal(closed["units"]))),
                    entry_price=gone.entry_price,
                    exit_price=Decimal(closed["price"]),
                    opened_at=gone.opened_at,
                    closed_at=ts,
                    reason=why,
                    pnl=Decimal(closed["realizedPL"]) + financing - commission,
                    commission=commission,
                    financing=financing,
                )
            )

    async def _transactions_loop(self) -> None:
        delay = 1.0
        while True:
            try:
                async with self.stream_http.stream(
                    "GET", f"/v3/accounts/{self.account_id}/transactions/stream"
                ) as resp:
                    if resp.status_code >= 400:
                        raise OandaError(f"flux des transactions refusé (HTTP {resp.status_code})")
                    delay = 1.0
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        tx = json.loads(line)
                        if tx.get("type") == "ORDER_FILL":
                            await self._handle_fill(tx)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(
                    "flux des transactions interrompu (%s), reconnexion dans %.0f s", e, delay
                )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
            with contextlib.suppress(OandaError):
                await self._load_trades()

    # Données de marché

    async def stream_prices(self, symbols: list[str]) -> AsyncIterator[Tick]:
        params = {"instruments": ",".join(to_oanda(s) for s in symbols)}
        async with self.stream_http.stream(
            "GET", f"/v3/accounts/{self.account_id}/pricing/stream", params=params
        ) as resp:
            if resp.status_code >= 400:
                raise OandaError(
                    f"flux des prix refusé (HTTP {resp.status_code})", resp.status_code
                )
            async for line in resp.aiter_lines():
                if not line:
                    continue
                msg = json.loads(line)
                if msg.get("type") != "PRICE" or not msg.get("tradeable", True):
                    continue
                if not msg.get("bids") or not msg.get("asks"):
                    continue
                yield Tick(
                    symbol=from_oanda(msg["instrument"]),
                    bid=Decimal(msg["bids"][0]["price"]),
                    ask=Decimal(msg["asks"][0]["price"]),
                    ts=parse_time(msg["time"]),
                )

    async def history(self, symbol: str, timeframe: Timeframe, count: int) -> list[Bar]:
        data = await self._get(
            f"/v3/instruments/{to_oanda(symbol)}/candles",
            params={
                "granularity": GRANULARITY[timeframe],
                "count": str(min(count, 5000)),
                "price": "BA",
                "alignmentTimezone": "UTC",
                "dailyAlignment": "0",
            },
        )
        bars = []
        for c in data.get("candles", []):
            if not c.get("complete"):
                continue
            b, a = c["bid"], c["ask"]
            bars.append(
                Bar(
                    symbol=symbol,
                    timeframe=timeframe,
                    open_time=parse_time(c["time"]),
                    open=Decimal(b["o"]),
                    high=Decimal(b["h"]),
                    low=Decimal(b["l"]),
                    close=Decimal(b["c"]),
                    spread=max(Decimal(0), Decimal(a["c"]) - Decimal(b["c"])),
                    volume=Decimal(c.get("volume", 0)),
                )
            )
        return bars
