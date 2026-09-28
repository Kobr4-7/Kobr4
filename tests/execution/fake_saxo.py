"""Faux serveur Saxo OpenAPI (en mémoire) pour tester l'adaptateur sans réseau.

Couvre l'authentification OAuth et les appels utilisés par `SaxoBroker`, avec les formats
de réponse de l'API (simulation : préfixe `/sim/openapi`).
"""

import json
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

import httpx

T = "2026-09-28T08:00:00.1234567Z"
T_CLOSE = "2026-09-28T09:30:00.000000Z"


class FakeSaxo:
    def __init__(self, app_key: str = "appkey-123", app_secret: str = "secret-456") -> None:
        self.app_key = app_key
        self.app_secret = app_secret
        self.n = 1
        self.access = "acc-1"
        self.refresh = "ref-1"
        self.expires_in = 1200
        self.account_key = "AK-1=="
        self.client_key = "CK-1=="
        self.price = {21: (Decimal("1.08000"), Decimal("1.08010"))}
        self.fill_slip = Decimal(0)
        """Écart entre la cotation lue avant l'ordre et le prix d'exécution."""
        self.positions: dict[str, dict[str, Any]] = {}
        self.closed: list[dict[str, Any]] = []
        self.next_id = 1000
        self.reject_next: str | None = None
        self.requests: list[tuple[str, str, Any]] = []
        self.token_requests: list[dict[str, str]] = []
        self.quote_stamp = 0
        self.external_refs = True
        """Recopier `ExternalReference` sur les positions."""

    def _id(self) -> str:
        self.next_id += 1
        return str(self.next_id)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # Authentification

    def _tokens(self) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": self.access,
                "token_type": "Bearer",
                "expires_in": self.expires_in,
                "refresh_token": self.refresh,
                "refresh_token_expires_in": 3600,
            },
        )

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.token_requests.append(form)
        if form.get("client_id") != self.app_key or form.get("client_secret") != self.app_secret:
            return httpx.Response(401, json={"error": "invalid_client"})
        grant = form.get("grant_type")
        if grant == "authorization_code" and form.get("code") == "code-ok":
            return self._tokens()
        if grant == "refresh_token" and form.get("refresh_token") == self.refresh:
            self.n += 1
            self.access, self.refresh = f"acc-{self.n}", f"ref-{self.n}"
            return self._tokens()
        return httpx.Response(400, json={"error": "invalid_grant"})

    # API

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host.endswith("logonvalidation.net"):
            return self._token(request)
        if request.headers.get("Authorization") != f"Bearer {self.access}":
            return httpx.Response(401)
        path = request.url.path.removeprefix("/sim/openapi")
        q = dict(request.url.params)
        body: Any = json.loads(request.content) if request.content else {}
        self.requests.append((request.method, path, body))
        match request.method, path:
            case "GET", "/port/v1/accounts/me":
                return httpx.Response(
                    200,
                    json={
                        "Data": [
                            {
                                "AccountId": "12345",
                                "AccountKey": self.account_key,
                                "ClientKey": self.client_key,
                                "Currency": "USD",
                                "DisplayName": "Simulation",
                                "Active": True,
                            }
                        ]
                    },
                )
            case "GET", "/ref/v1/instruments":
                code = q.get("Keywords", "")
                data = [{"Identifier": 21, "Symbol": "EURUSD", "AssetType": "FxSpot"}]
                return httpx.Response(200, json={"Data": [d for d in data if d["Symbol"] == code]})
            case "GET", "/ref/v1/instruments/details/21/FxSpot":
                return httpx.Response(200, json={"Symbol": "EURUSD", "Uic": 21})
            case "GET", "/trade/v1/infoprices":
                bid, ask = self.price[int(q["Uic"])]
                return httpx.Response(
                    200,
                    json={"Uic": int(q["Uic"]), "Quote": {"Bid": float(bid), "Ask": float(ask)}},
                )
            case "GET", "/trade/v1/infoprices/list":
                self.quote_stamp += 1
                data = []
                for u in q["Uics"].split(","):
                    bid, ask = self.price[int(u)]
                    data.append(
                        {
                            "Uic": int(u),
                            "Quote": {"Bid": float(bid), "Ask": float(ask), "MarketState": "Open"},
                            "LastUpdated": f"2026-09-28T08:00:{self.quote_stamp:02d}.000000Z",
                        }
                    )
                return httpx.Response(200, json={"Data": data})
            case "GET", "/port/v1/positions":
                return httpx.Response(200, json={"Data": list(self.positions.values())})
            case "GET", "/port/v1/closedpositions":
                return httpx.Response(200, json={"Data": self.closed})
            case "GET", "/port/v1/orders":
                return httpx.Response(200, json={"Data": []})
            case "GET", "/port/v1/balances":
                return httpx.Response(
                    200,
                    json={
                        "CashBalance": 10000.0,
                        "TotalValue": 10012.5,
                        "MarginUsedByCurrentPositions": 216.0,
                        "Currency": "USD",
                    },
                )
            case "GET", "/chart/v3/charts":
                return httpx.Response(
                    200,
                    json={
                        "Data": [
                            {
                                "Time": "2026-09-28T06:00:00.000000Z",
                                "OpenBid": 1.0790,
                                "HighBid": 1.0810,
                                "LowBid": 1.0785,
                                "CloseBid": 1.0800,
                                "CloseAsk": 1.0801,
                                "Volume": 120,
                            },
                            {
                                "Time": "2026-09-28T07:00:00.000000Z",
                                "OpenBid": 1.0800,
                                "HighBid": 1.0806,
                                "LowBid": 1.0795,
                                "CloseBid": 1.0802,
                                "CloseAsk": 1.0803,
                            },
                            {
                                "Time": "2026-09-28T08:00:00.000000Z",
                                "OpenBid": 1.0802,
                                "HighBid": 1.0804,
                                "LowBid": 1.0801,
                                "CloseBid": 1.0803,
                            },
                        ]
                    },
                )
            case "POST", "/trade/v2/orders":
                return self._post_order(body)
            case "PATCH", "/trade/v2/orders":
                for p in self.positions.values():
                    for o in p["PositionBase"]["RelatedOpenOrders"]:
                        if o["OrderId"] == body["OrderId"]:
                            o["OrderPrice"] = body["OrderPrice"]
                            return httpx.Response(200, json={"OrderId": body["OrderId"]})
                return httpx.Response(404, json={"ErrorInfo": {"Message": "ordre inconnu"}})
        if request.method == "DELETE" and path.startswith("/trade/v2/orders/"):
            oid = path.rsplit("/", 1)[1]
            for p in self.positions.values():
                rel = p["PositionBase"]["RelatedOpenOrders"]
                p["PositionBase"]["RelatedOpenOrders"] = [o for o in rel if o["OrderId"] != oid]
            return httpx.Response(200, json={"Orders": [{"OrderId": oid}]})
        return httpx.Response(404, json={"ErrorInfo": {"Message": f"inconnu : {path}"}})

    def _related(self, orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "OrderId": self._id(),
                "OpenOrderType": o["OrderType"],
                "OrderPrice": o["OrderPrice"],
                "Amount": o["Amount"],
                "Status": "Working",
            }
            for o in orders
        ]

    def _post_order(self, body: dict[str, Any]) -> httpx.Response:
        if self.reject_next:
            msg, self.reject_next = self.reject_next, None
            return httpx.Response(
                400, json={"ErrorInfo": {"ErrorCode": "InsufficientFunds", "Message": msg}}
            )
        pid = body.get("PositionId")
        if pid and "BuySell" not in body:  # ordres liés ajoutés à une position
            self.positions[pid]["PositionBase"]["RelatedOpenOrders"] += self._related(
                body["Orders"]
            )
            return httpx.Response(200, json={"Orders": []})
        if pid:  # fermeture d'une position
            pos = self.positions[pid]
            bid, ask = self.price[pos["PositionBase"]["Uic"]]
            price = bid if pos["PositionBase"]["Amount"] > 0 else ask
            self.close(pid, price)
            return httpx.Response(200, json={"OrderId": self._id()})
        order_id = self._id()
        bid, ask = self.price[body["Uic"]]
        buy = body["BuySell"] == "Buy"
        price = (ask + self.fill_slip) if buy else (bid - self.fill_slip)
        new_pid = self._id()
        base: dict[str, Any] = {
            "AccountId": "12345",
            "Amount": body["Amount"] if buy else -body["Amount"],
            "AssetType": "FxSpot",
            "ExecutionTimeOpen": T,
            "OpenPrice": float(price),
            "RelatedOpenOrders": self._related(body.get("Orders", [])),
            "SourceOrderId": order_id,
            "Status": "Open",
            "Uic": body["Uic"],
        }
        if self.external_refs:
            base["ExternalReference"] = body.get("ExternalReference")
        self.positions[new_pid] = {
            "NetPositionId": "EURUSD__FxSpot",
            "PositionId": new_pid,
            "PositionBase": base,
        }
        return httpx.Response(200, json={"OrderId": order_id})

    def close(self, pid: str, price: Decimal) -> None:
        """Ferme la position `pid` au prix donné (fermeture manuelle, stop ou objectif)."""
        pos = self.positions.pop(pid)["PositionBase"]
        amount = pos["Amount"]
        pnl = (price - Decimal(str(pos["OpenPrice"]))) * amount
        self.closed.append(
            {
                "ClosedPosition": {
                    "Amount": amount,
                    "AssetType": "FxSpot",
                    "BuyOrSell": "Buy" if amount > 0 else "Sell",
                    "ClosedProfitLoss": float(pnl),
                    "ClosedProfitLossInBaseCurrency": float(pnl),
                    "ClosingPositionId": self._id(),
                    "ClosingPrice": float(price),
                    "CostClosingInBaseCurrency": -1.5,
                    "CostOpeningInBaseCurrency": -1.5,
                    "ExecutionTimeClose": T_CLOSE,
                    "ExecutionTimeOpen": pos["ExecutionTimeOpen"],
                    "OpeningPositionId": pid,
                    "OpenPrice": pos["OpenPrice"],
                    "Uic": pos["Uic"],
                },
                "ClosedPositionUniqueId": f"{pid}-x",
                "NetPositionId": "EURUSD__FxSpot",
            }
        )

    def hit_stop(self, pid: str) -> None:
        stop = next(
            o
            for o in self.positions[pid]["PositionBase"]["RelatedOpenOrders"]
            if o["OpenOrderType"] == "StopIfTraded"
        )
        self.close(pid, Decimal(str(stop["OrderPrice"])))
