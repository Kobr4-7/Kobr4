"""Faux serveur OANDA v20 (en mémoire) pour tester l'adaptateur sans réseau.

Couvre les appels utilisés par `OandaBroker`, avec les formats de réponse de l'API.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import httpx

T = "2026-09-28T08:00:00.123456789Z"


class _Stream(httpx.AsyncByteStream):
    def __init__(self, lines: list[str], queue: "asyncio.Queue[str] | None" = None) -> None:
        self.lines = lines
        self.queue = queue

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for line in self.lines:
            yield (line + "\n").encode()
        if self.queue is not None:
            while True:
                line = await self.queue.get()
                if line == "__END__":
                    return
                yield (line + "\n").encode()


class FakeOanda:
    def __init__(self, account_id: str = "001-001-1-001", token: str = "tok") -> None:
        self.account_id = account_id
        self.token = token
        self.balance = Decimal("10000")
        self.price = {"EUR_USD": (Decimal("1.08000"), Decimal("1.08010"))}
        self.trades: dict[str, dict[str, Any]] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.next_id = 100
        self.tx_queue: asyncio.Queue[str] = asyncio.Queue()
        self.reject_next: str | None = None
        self.requests: list[tuple[str, str, Any]] = []
        self.price_lines: list[str] = []

    def _id(self) -> str:
        self.next_id += 1
        return str(self.next_id)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"errorMessage": "Insufficient authorization"})
        body: Any = json.loads(request.content) if request.content else {}
        path = request.url.path
        self.requests.append((request.method, path, body))
        base = f"/v3/accounts/{self.account_id}"
        m = request.method
        if path == "/v3/accounts":
            return httpx.Response(200, json={"accounts": [{"id": self.account_id, "tags": []}]})
        if path.startswith("/v3/accounts/") and not path.startswith(base):
            return httpx.Response(403, json={"errorMessage": "Unauthorized access to account"})
        if path == f"{base}/summary":
            return httpx.Response(200, json={"account": self.summary()})
        if path == f"{base}/openTrades":
            return httpx.Response(200, json={"trades": list(self.trades.values())})
        if m == "POST" and path == f"{base}/orders":
            return self.market_order(body["order"])
        if path.startswith(f"{base}/trades/"):
            rest = path[len(f"{base}/trades/") :]
            spec, _, action = rest.partition("/")
            trade = self.find_trade(spec)
            if trade is None:
                return httpx.Response(404, json={"errorMessage": "Trade not found"})
            if action == "":
                return httpx.Response(200, json={"trade": trade})
            if action == "orders" and m == "PUT":
                if "stopLoss" in body:
                    trade["stopLossOrder"] = {"price": body["stopLoss"]["price"]}
                if "takeProfit" in body:
                    if body["takeProfit"] is None:
                        trade.pop("takeProfitOrder", None)
                    else:
                        trade["takeProfitOrder"] = {"price": body["takeProfit"]["price"]}
                return httpx.Response(200, json={})
            if action == "close" and m == "PUT":
                return httpx.Response(
                    200,
                    json={"orderFillTransaction": self.close_tx(trade, "MARKET_ORDER_TRADE_CLOSE")},
                )
        if path.startswith(f"{base}/orders/@"):
            cid = path.split("@", 1)[1]
            if cid not in self.orders:
                return httpx.Response(404, json={"errorMessage": "Order not found"})
            return httpx.Response(200, json={"order": self.orders[cid]})
        if path == f"{base}/pricing/stream":
            return httpx.Response(200, stream=_Stream(self.price_lines))
        if path == f"{base}/transactions/stream":
            return httpx.Response(200, stream=_Stream([], self.tx_queue))
        if path.startswith("/v3/instruments/") and path.endswith("/candles"):
            return httpx.Response(200, json={"candles": self.candles()})
        return httpx.Response(404, json={"errorMessage": f"route inconnue {m} {path}"})

    def summary(self) -> dict[str, Any]:
        return {
            "currency": "USD",
            "balance": str(self.balance),
            "NAV": str(self.balance + Decimal("12.5")),
            "marginUsed": "150.0",
            "lastTransactionID": "99",
        }

    def find_trade(self, spec: str) -> dict[str, Any] | None:
        if spec.startswith("@"):
            return next(
                (t for t in self.trades.values() if t["clientExtensions"]["id"] == spec[1:]), None
            )
        return self.trades.get(spec)

    def market_order(self, o: dict[str, Any]) -> httpx.Response:
        cid = o["clientExtensions"]["id"]
        if self.reject_next:
            reason, self.reject_next = self.reject_next, None
            self.orders[cid] = {
                "id": self._id(),
                "state": "CANCELLED",
                "units": o["units"],
                "instrument": o["instrument"],
            }
            return httpx.Response(201, json={"orderCancelTransaction": {"reason": reason}})
        units = int(o["units"])
        bid, ask = self.price[o["instrument"]]
        price = ask if units > 0 else bid
        dist = Decimal(o["stopLossOnFill"]["distance"])
        sl = price - dist if units > 0 else price + dist
        trade_id = self._id()
        order_id = self._id()
        self.trades[trade_id] = {
            "id": trade_id,
            "instrument": o["instrument"],
            "price": str(price),
            "openTime": T,
            "currentUnits": str(units),
            "clientExtensions": o["tradeClientExtensions"],
            "stopLossOrder": {"price": f"{sl:.5f}"},
        }
        self.orders[cid] = {
            "id": order_id,
            "state": "FILLED",
            "units": str(units),
            "instrument": o["instrument"],
            "clientExtensions": o["clientExtensions"],
        }
        fill = {
            "type": "ORDER_FILL",
            "id": self._id(),
            "orderID": order_id,
            "time": T,
            "price": str(price),
            "reason": "MARKET_ORDER",
            "tradeOpened": {"tradeID": trade_id, "units": str(units), "price": str(price)},
        }
        return httpx.Response(
            201, json={"orderCreateTransaction": {}, "orderFillTransaction": fill}
        )

    def close_tx(
        self, trade: dict[str, Any], reason: str, price: str | None = None
    ) -> dict[str, Any]:
        units = int(trade["currentUnits"])
        bid, ask = self.price[trade["instrument"]]
        exit_price = Decimal(price) if price else (bid if units > 0 else ask)
        pl = (exit_price - Decimal(trade["price"])) * units
        del self.trades[trade["id"]]
        self.balance += pl
        return {
            "type": "ORDER_FILL",
            "id": self._id(),
            "time": T,
            "reason": reason,
            "price": str(exit_price),
            "commission": "0.0000",
            "tradesClosed": [
                {
                    "tradeID": trade["id"],
                    "units": str(-units),
                    "price": str(exit_price),
                    "realizedPL": f"{pl:.4f}",
                    "financing": "-0.1200",
                }
            ],
        }

    async def trigger_stop(self, trade_id: str) -> None:
        trade = self.trades[trade_id]
        tx = self.close_tx(trade, "STOP_LOSS_ORDER", trade["stopLossOrder"]["price"])
        await self.tx_queue.put(json.dumps(tx))

    def candles(self) -> list[dict[str, Any]]:
        out = []
        for i in range(3):
            out.append(
                {
                    "time": f"2026-09-28T0{i}:00:00.000000000Z",
                    "complete": i < 2,
                    "volume": 100 + i,
                    "bid": {"o": "1.08000", "h": "1.08100", "l": "1.07900", "c": "1.08050"},
                    "ask": {"o": "1.08010", "h": "1.08110", "l": "1.07910", "c": "1.08060"},
                }
            )
        return out
