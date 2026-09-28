"""Adaptateur Saxo (OpenAPI REST).

Correspondances :
- une position Kobr4 = une position Saxo (compte en « end of day netting », où chaque
  exécution garde sa propre position) ; la référence externe de l'ordre
  (`ExternalReference` = `<id de l'ordre>|<stratégie>`) est recopiée sur la position, ce
  qui permet de la retrouver après un redémarrage ;
- le stop loss (`StopIfTraded`) et le take profit (`Limit`) sont des ordres liés, envoyés
  avec l'ordre au marché, puis recalés sur le prix réel d'exécution ;
- Saxo n'a pas de flux de transactions en REST : les positions sont relues à intervalle
  régulier, et une position disparue est une position fermée (stop, objectif ou
  fermeture manuelle), dont le résultat est lu dans les positions fermées ;
- les cotations sont lues de la même façon (`infoprices`), ce qui suffit pour des
  stratégies en M15 et au-delà.

L'accès passe par OAuth 2 (code d'autorisation) : `SaxoSession` garde le jeton d'accès
(20 min) et le renouvelle avec le jeton de rafraîchissement, qui change à chaque
renouvellement et doit donc être enregistré à chaque fois (`on_refresh`).
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

from kobr4.core.clock import Clock
from kobr4.core.instruments import get_instrument
from kobr4.core.models import Account, Bar, ClosedTrade, ExitReason, Position, Tick
from kobr4.core.orders import Order, OrderStatus
from kobr4.core.types import OrderType, Side, Timeframe
from kobr4.execution.broker import Broker, BrokerError

log = logging.getLogger(__name__)

Environment = Literal["practice", "live"]


@dataclass(frozen=True)
class Hosts:
    api: str
    auth: str


HOSTS: dict[str, Hosts] = {
    "practice": Hosts(
        "https://gateway.saxobank.com/sim/openapi", "https://sim.logonvalidation.net"
    ),
    "live": Hosts("https://gateway.saxobank.com/openapi", "https://live.logonvalidation.net"),
}
HORIZON = {
    Timeframe.M1: 1,
    Timeframe.M5: 5,
    Timeframe.M15: 15,
    Timeframe.M30: 30,
    Timeframe.H1: 60,
    Timeframe.H4: 240,
    Timeframe.D1: 1440,
}
STOP_TYPES = ("StopIfTraded", "Stop")
TP_TYPE = "Limit"
REF_MAX = 50
"""Longueur maximale de `ExternalReference` chez Saxo."""


def to_saxo(symbol: str) -> str:
    return symbol.replace("/", "")


def from_saxo(code: str) -> str:
    code = code.split(":")[0].upper()
    return f"{code[:3]}/{code[3:6]}"


def parse_time(value: str) -> datetime:
    """Heure Saxo (ISO 8601, jusqu'à 7 décimales, suffixe Z) → datetime UTC."""
    main, _, frac = value.rstrip("Z").partition(".")
    micro = (frac + "000000")[:6]
    return datetime.fromisoformat(f"{main}.{micro}").replace(tzinfo=UTC)


def dec(value: Any) -> Decimal:
    return Decimal(str(value))


def external_ref(order_id: str, strategy_id: str) -> str:
    return f"{order_id}|{strategy_id}"[:REF_MAX]


def parse_ref(ref: str | None) -> tuple[str, str] | None:
    if not ref or "|" not in ref:
        return None
    order_id, _, strategy = ref.partition("|")
    return order_id, strategy or "externe"


class SaxoError(BrokerError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SaxoAuthError(SaxoError):
    """Autorisation perdue : il faut se reconnecter chez Saxo depuis le site."""


# Authentification


def authorize_url(environment: Environment, app_key: str, redirect_uri: str, state: str) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": app_key,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"{HOSTS[environment].auth}/authorize?{query}"


@dataclass
class SaxoTokens:
    app_key: str
    app_secret: str
    redirect_uri: str
    access_token: str
    access_expires: datetime
    refresh_token: str
    refresh_expires: datetime

    def dumps(self) -> str:
        return json.dumps(
            {
                "app_key": self.app_key,
                "app_secret": self.app_secret,
                "redirect_uri": self.redirect_uri,
                "access_token": self.access_token,
                "access_expires": self.access_expires.isoformat(),
                "refresh_token": self.refresh_token,
                "refresh_expires": self.refresh_expires.isoformat(),
            }
        )

    @classmethod
    def loads(cls, raw: str) -> "SaxoTokens":
        d = json.loads(raw)
        return cls(
            app_key=d["app_key"],
            app_secret=d["app_secret"],
            redirect_uri=d["redirect_uri"],
            access_token=d["access_token"],
            access_expires=datetime.fromisoformat(d["access_expires"]),
            refresh_token=d["refresh_token"],
            refresh_expires=datetime.fromisoformat(d["refresh_expires"]),
        )


async def _token_request(
    environment: Environment,
    form: dict[str, str],
    transport: httpx.AsyncBaseTransport | None,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15, transport=transport) as c:
        try:
            resp = await c.post(f"{HOSTS[environment].auth}/token", data=form)
        except httpx.HTTPError as e:
            raise SaxoError(f"Saxo injoignable ({e.__class__.__name__})") from e
    if resp.status_code in (400, 401):
        raise SaxoAuthError(
            "autorisation Saxo refusée ou expirée : reconnecte le compte depuis les réglages",
            resp.status_code,
        )
    if resp.status_code >= 400:
        raise SaxoError(f"Saxo a répondu HTTP {resp.status_code}", resp.status_code)
    data: dict[str, Any] = resp.json()
    return data


def _tokens(
    data: dict[str, Any], app_key: str, app_secret: str, redirect_uri: str, now: datetime
) -> SaxoTokens:
    return SaxoTokens(
        app_key=app_key,
        app_secret=app_secret,
        redirect_uri=redirect_uri,
        access_token=data["access_token"],
        access_expires=now + timedelta(seconds=int(data.get("expires_in", 1200))),
        refresh_token=data["refresh_token"],
        refresh_expires=now + timedelta(seconds=int(data.get("refresh_token_expires_in", 3600))),
    )


async def exchange_code(
    environment: Environment,
    app_key: str,
    app_secret: str,
    redirect_uri: str,
    code: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SaxoTokens:
    """Échange le code reçu après la connexion chez Saxo contre des jetons."""
    data = await _token_request(
        environment,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": app_key,
            "client_secret": app_secret,
        },
        transport,
    )
    return _tokens(data, app_key, app_secret, redirect_uri, datetime.now(UTC))


class SaxoSession:
    """Jetons d'une connexion Saxo, partagés par tous les bots qui l'utilisent.

    Le jeton de rafraîchissement change à chaque renouvellement : deux sessions pour la
    même connexion s'invalideraient l'une l'autre. `on_refresh` enregistre les nouveaux
    jetons (chiffrés) pour qu'un redémarrage reprenne avec les bons.
    """

    def __init__(
        self,
        environment: Environment,
        tokens: SaxoTokens,
        on_refresh: Callable[[SaxoTokens], Awaitable[None]] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        margin: timedelta = timedelta(minutes=2),
    ) -> None:
        self.environment = environment
        self.tokens = tokens
        self.on_refresh = on_refresh
        self.transport = transport
        self.margin = margin
        self._lock = asyncio.Lock()

    @property
    def expired(self) -> bool:
        return self.tokens.refresh_expires <= datetime.now(UTC)

    async def access_token(self) -> str:
        if self.tokens.access_expires - self.margin <= datetime.now(UTC):
            await self.refresh()
        return self.tokens.access_token

    async def refresh(self, force: bool = False) -> None:
        async with self._lock:
            now = datetime.now(UTC)
            if not force and self.tokens.access_expires - self.margin > now:
                return  # déjà renouvelé par un autre appel
            if self.tokens.refresh_expires <= now:
                raise SaxoAuthError(
                    "autorisation Saxo expirée : reconnecte le compte depuis les réglages"
                )
            t = self.tokens
            data = await _token_request(
                self.environment,
                {
                    "grant_type": "refresh_token",
                    "refresh_token": t.refresh_token,
                    "redirect_uri": t.redirect_uri,
                    "client_id": t.app_key,
                    "client_secret": t.app_secret,
                },
                self.transport,
            )
            self.tokens = _tokens(data, t.app_key, t.app_secret, t.redirect_uri, now)
            if self.on_refresh is not None:
                await self.on_refresh(self.tokens)


# Client HTTP


class SaxoClient:
    """Appels REST authentifiés, avec renouvellement du jeton si Saxo répond 401."""

    def __init__(
        self,
        session: SaxoSession,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.session = session
        self.http = httpx.AsyncClient(
            base_url=HOSTS[session.environment].api,
            timeout=httpx.Timeout(10.0),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    async def request(self, method: str, path: str, **kw: Any) -> Any:
        for attempt in (1, 2):
            headers = {"Authorization": f"Bearer {await self.session.access_token()}"}
            try:
                resp = await self.http.request(method, path, headers=headers, **kw)
            except httpx.HTTPError as e:
                raise SaxoError(f"Saxo injoignable : {e}") from e
            if resp.status_code == 401 and attempt == 1:
                await self.session.refresh(force=True)
                continue
            break
        try:
            data: Any = resp.json() if resp.content else {}
        except ValueError:
            data = {}
        if resp.status_code >= 400:
            info = (data.get("ErrorInfo") or data) if isinstance(data, dict) else {}
            message = info.get("Message") or resp.text[:200] or resp.reason_phrase
            if resp.status_code == 401:
                raise SaxoAuthError(f"jeton Saxo refusé (HTTP 401) : {message}", 401)
            if resp.status_code == 429:
                message = "trop de requêtes, limite de Saxo atteinte"
            raise SaxoError(f"{message} (HTTP {resp.status_code})", resp.status_code)
        return data

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=params or None)


async def list_accounts(client: SaxoClient) -> list[dict[str, Any]]:
    """Comptes du client, avec leur devise (pour que l'utilisateur choisisse le bon)."""
    data = await client.get("/port/v1/accounts/me")
    return [
        {
            "id": a["AccountKey"],
            "alias": a.get("DisplayName") or a.get("AccountId", ""),
            "currency": a.get("Currency", ""),
            "account_number": a.get("AccountId", ""),
        }
        for a in data.get("Data", [])
        if a.get("Active", True)
    ]


# Courtier


class SaxoBroker(Broker):
    def __init__(
        self,
        session: SaxoSession,
        account_key: str,
        clock: Clock,
        transport: httpx.AsyncBaseTransport | None = None,
        price_interval: float = 1.0,
        position_interval: float = 2.0,
        fill_timeout: float = 5.0,
    ) -> None:
        super().__init__()
        self.client = SaxoClient(session, transport)
        self.account_key = account_key
        self.clock = clock
        self.price_interval = price_interval
        self.position_interval = position_interval
        self.fill_timeout = fill_timeout
        self.account_currency = "USD"
        self.client_key = ""
        self._uics: dict[str, int] = {}
        self._symbols: dict[int, str] = {}
        self._positions: dict[str, Position] = {}
        """Positions ouvertes, par identifiant de position Saxo."""
        self._raw: dict[str, dict[str, Any]] = {}
        self._closing: dict[str, ExitReason] = {}
        self._by_source: dict[str, tuple[str, str]] = {}
        """Ordre Kobr4 (identifiant, stratégie) par identifiant d'ordre Saxo, au cas où la
        position ne porterait pas la référence externe."""
        self._lock = asyncio.Lock()
        self._poll_task: asyncio.Task[None] | None = None

    # Connexion

    async def connect(self) -> None:
        accounts = (await self.client.get("/port/v1/accounts/me")).get("Data", [])
        acct = next((a for a in accounts if a.get("AccountKey") == self.account_key), None)
        if acct is None:
            raise SaxoError("compte Saxo introuvable pour cette autorisation")
        self.account_currency = acct.get("Currency", "USD")
        self.client_key = acct["ClientKey"]
        async with self._lock:
            await self._refresh_positions(notify=False)
        if self._poll_task is None:
            self._poll_task = asyncio.create_task(self._poll_loop(), name="saxo-positions")

    async def close(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None
        await self.client.aclose()

    # Instruments

    async def uic(self, symbol: str) -> int:
        if symbol not in self._uics:
            data = await self.client.get(
                "/ref/v1/instruments", AssetTypes="FxSpot", Keywords=to_saxo(symbol)
            )
            code = to_saxo(symbol)
            match = [d for d in data.get("Data", []) if d.get("Symbol", "").upper() == code]
            if not match:
                raise SaxoError(f"instrument {symbol} introuvable chez Saxo")
            uic = int(match[0]["Identifier"])
            self._uics[symbol] = uic
            self._symbols[uic] = symbol
        return self._uics[symbol]

    async def _symbol(self, uic: int) -> str:
        if uic not in self._symbols:
            data = await self.client.get(f"/ref/v1/instruments/details/{uic}/FxSpot")
            symbol = from_saxo(data["Symbol"])
            self._symbols[uic] = symbol
            self._uics[symbol] = uic
        return self._symbols[uic]

    # Positions

    async def _position(self, raw: dict[str, Any]) -> Position:
        base = raw["PositionBase"]
        pid = raw["PositionId"]
        amount = dec(base["Amount"])
        side = Side.BUY if amount > 0 else Side.SELL
        entry = dec(base["OpenPrice"])
        sl = tp = None
        for o in base.get("RelatedOpenOrders") or []:
            kind = o.get("OpenOrderType")
            if kind in STOP_TYPES:
                sl = dec(o["OrderPrice"])
            elif kind == TP_TYPE:
                tp = dec(o["OrderPrice"])
        if sl is None:
            # Position sans stop (ouverte hors du bot) : stop fictif très éloigné, signalé.
            log.warning("position Saxo %s sans stop loss", pid)
            sl = entry * (Decimal("0.5") if side is Side.BUY else Decimal("1.5"))
        ref = parse_ref(base.get("ExternalReference")) or self._by_source.get(
            str(base.get("SourceOrderId"))
        )
        known = self._positions.get(pid)
        if ref is not None:
            position_id, strategy = ref
        elif known is not None:
            position_id, strategy = known.id, known.strategy_id
        else:
            position_id, strategy = f"saxo-{pid}", "externe"
        return Position(
            id=position_id,
            strategy_id=strategy,
            symbol=await self._symbol(int(base["Uic"])),
            side=side,
            quantity=int(abs(amount)),
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            opened_at=parse_time(base["ExecutionTimeOpen"]),
        )

    async def _fetch_positions(self) -> dict[str, dict[str, Any]]:
        data = await self.client.get(
            "/port/v1/positions",
            ClientKey=self.client_key,
            AccountKey=self.account_key,
            FieldGroups="PositionBase",
        )
        return {
            p["PositionId"]: p
            for p in data.get("Data", [])
            if p["PositionBase"].get("AssetType", "FxSpot") == "FxSpot"
            and p["PositionBase"].get("Status", "Open") == "Open"
        }

    async def _refresh_positions(self, notify: bool = True) -> None:
        """Relit les positions ; signale les ouvertures, modifications et fermetures.

        À appeler sous `self._lock`.
        """
        raw = await self._fetch_positions()
        for pid, r in raw.items():
            pos = await self._position(r)
            before = self._positions.get(pid)
            self._positions[pid] = pos
            self._raw[pid] = r
            if not notify:
                continue
            if before is None:
                await self.listener.on_position_opened(pos)
            elif (before.stop_loss, before.take_profit) != (pos.stop_loss, pos.take_profit):
                await self.listener.on_position_modified(pos)
        for pid in [p for p in self._positions if p not in raw]:
            gone = self._positions.pop(pid)
            self._raw.pop(pid, None)
            if notify:
                await self._report_close(pid, gone)

    async def _report_close(self, pid: str, gone: Position) -> None:
        closed = await self._closed_position(pid)
        if closed is None:
            log.error("position Saxo %s fermée mais introuvable dans l'historique", pid)
            exit_price = gone.entry_price
            pnl = commission = Decimal(0)
            closed_at = self.clock.now()
        else:
            exit_price = dec(closed["ClosingPrice"])
            costs = dec(closed.get("CostOpeningInBaseCurrency", 0)) + dec(
                closed.get("CostClosingInBaseCurrency", 0)
            )
            commission = -costs
            pnl = dec(closed.get("ClosedProfitLossInBaseCurrency", 0)) + costs
            closed_at = parse_time(closed["ExecutionTimeClose"])
        reason = self._closing.pop(pid, None) or self._exit_reason(gone, exit_price)
        await self.listener.on_position_closed(
            ClosedTrade(
                position_id=gone.id,
                strategy_id=gone.strategy_id,
                symbol=gone.symbol,
                side=gone.side,
                quantity=gone.quantity,
                entry_price=gone.entry_price,
                exit_price=exit_price,
                opened_at=gone.opened_at,
                closed_at=closed_at,
                reason=reason,
                pnl=pnl,
                commission=commission,
                financing=Decimal(0),
            )
        )

    @staticmethod
    def _exit_reason(pos: Position, price: Decimal) -> ExitReason:
        """Fermeture décidée par Saxo : stop ou objectif, selon le prix le plus proche."""
        to_sl = abs(price - pos.stop_loss)
        if pos.take_profit is not None and abs(price - pos.take_profit) < to_sl:
            return ExitReason.TAKE_PROFIT
        tick = get_instrument(pos.symbol).tick_size * 20
        return ExitReason.STOP_LOSS if to_sl <= tick else ExitReason.MANUAL

    async def _closed_position(self, pid: str) -> dict[str, Any] | None:
        data = await self.client.get(
            "/port/v1/closedpositions",
            ClientKey=self.client_key,
            AccountKey=self.account_key,
            FieldGroups="ClosedPosition",
        )
        for c in data.get("Data", []):
            cp = c.get("ClosedPosition") or {}
            if cp.get("OpeningPositionId") == pid:
                return dict(cp)
        return None

    def _pid(self, position_id: str) -> str:
        for pid, pos in self._positions.items():
            if pos.id == position_id:
                return pid
        raise BrokerError(f"position inconnue : {position_id}")

    async def positions(self) -> list[Position]:
        async with self._lock:
            await self._refresh_positions()
        return list(self._positions.values())

    async def account(self) -> Account:
        b = await self.client.get(
            "/port/v1/balances", ClientKey=self.client_key, AccountKey=self.account_key
        )
        return Account(
            currency=b.get("Currency", self.account_currency),
            balance=dec(b.get("CashBalance", 0)),
            equity=dec(b.get("TotalValue", b.get("CashBalance", 0))),
            margin_used=max(Decimal(0), dec(b.get("MarginUsedByCurrentPositions", 0))),
            ts=self.clock.now(),
        )

    async def _poll_loop(self) -> None:
        delay = self.position_interval
        while True:
            await asyncio.sleep(delay)
            try:
                async with self._lock:
                    await self._refresh_positions()
                delay = self.position_interval
            except asyncio.CancelledError:
                raise
            except Exception as e:
                delay = min(delay * 2, 60)
                log.warning("lecture des positions Saxo impossible (%s), nouvel essai", e)

    # Ordres

    def _related(self, side: Side, amount: int, order_type: str, price: Decimal) -> dict[str, Any]:
        return {
            "AccountKey": self.account_key,
            "AssetType": "FxSpot",
            "BuySell": "Sell" if side is Side.BUY else "Buy",
            "Amount": amount,
            "OrderType": order_type,
            "OrderPrice": float(price),
            "OrderDuration": {"DurationType": "GoodTillCancel"},
            "ManualOrder": False,
        }

    async def _quote(self, uic: int) -> tuple[Decimal, Decimal]:
        data = await self.client.get(
            "/trade/v1/infoprices",
            AccountKey=self.account_key,
            Uic=uic,
            AssetType="FxSpot",
            FieldGroups="Quote",
        )
        q = data.get("Quote") or {}
        if q.get("Bid") is None or q.get("Ask") is None:
            raise SaxoError("pas de cotation disponible")
        return dec(q["Bid"]), dec(q["Ask"])

    async def submit(self, order: Order) -> Order:
        now = self.clock.now()
        order = order.transition(OrderStatus.SUBMITTED, now)
        if order.order_type is not OrderType.MARKET:
            return order.transition(
                OrderStatus.REJECTED, now, reject_reason="seuls les ordres au marché sont gérés"
            )
        inst = get_instrument(order.symbol)
        try:
            uic = await self.uic(order.symbol)
            bid, ask = await self._quote(uic)
        except SaxoError as e:
            return order.transition(OrderStatus.REJECTED, self.clock.now(), reject_reason=str(e))
        ref_price = ask if order.side is Side.BUY else bid
        sl, tp = order.protection_prices(ref_price)
        related = [self._related(order.side, order.quantity, "StopIfTraded", inst.round_price(sl))]
        if tp is not None:
            related.append(self._related(order.side, order.quantity, TP_TYPE, inst.round_price(tp)))
        body = {
            "AccountKey": self.account_key,
            "Uic": uic,
            "AssetType": "FxSpot",
            "BuySell": "Buy" if order.side is Side.BUY else "Sell",
            "Amount": order.quantity,
            "OrderType": "Market",
            "OrderDuration": {"DurationType": "DayOrder"},
            "ManualOrder": False,
            "ExternalReference": external_ref(order.id, order.strategy_id),
            "Orders": related,
        }
        # Sous verrou : la relève des positions ne doit pas voir l'exécution avant que
        # l'ordre Saxo soit rattaché à l'ordre Kobr4.
        async with self._lock:
            try:
                data = await self.client.request("POST", "/trade/v2/orders", json=body)
            except SaxoError as e:
                return order.transition(
                    OrderStatus.REJECTED, self.clock.now(), reject_reason=str(e)
                )
            saxo_id = str(data.get("OrderId", ""))
            self._by_source[saxo_id] = (order.id, order.strategy_id)
        pos = await self._await_fill(order.id, saxo_id)
        if pos is None:
            with contextlib.suppress(SaxoError):
                await self.client.request(
                    "DELETE", f"/trade/v2/orders/{saxo_id}", params={"AccountKey": self.account_key}
                )
            return order.transition(
                OrderStatus.REJECTED, self.clock.now(), reject_reason="ordre non exécuté"
            )
        result = order.transition(
            OrderStatus.FILLED,
            pos.opened_at,
            filled_quantity=pos.quantity,
            avg_fill_price=pos.entry_price,
            broker_order_id=saxo_id,
        )
        # Stop et objectif recalés sur le prix réel d'exécution.
        want_sl, want_tp = order.protection_prices(pos.entry_price)
        want_sl = inst.round_price(want_sl)
        want_tp = None if want_tp is None else inst.round_price(want_tp)
        if (want_sl, want_tp) != (pos.stop_loss, pos.take_profit):
            try:
                await self.modify_position(order.id, want_sl, want_tp)
            except SaxoError as e:
                log.warning("ordre %s : stop non recalé sur l'exécution (%s)", order.id, e)
        return result

    async def _await_fill(self, order_id: str, saxo_id: str) -> Position | None:
        """Attend que l'ordre au marché devienne une position, et la signale."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.fill_timeout
        while True:
            async with self._lock:
                raw = await self._fetch_positions()
                for pid, r in raw.items():
                    base = r["PositionBase"]
                    ref = parse_ref(base.get("ExternalReference"))
                    if str(base.get("SourceOrderId")) == saxo_id or (ref and ref[0] == order_id):
                        if pid not in self._positions:
                            pos = await self._position(r)
                            self._positions[pid] = pos
                            self._raw[pid] = r
                            await self.listener.on_position_opened(pos)
                        return self._positions[pid]
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(0.5)

    async def modify_position(
        self, position_id: str, stop_loss: Decimal, take_profit: Decimal | None
    ) -> Position:
        async with self._lock:
            pid = self._pid(position_id)
            pos = self._positions[pid]
            inst = get_instrument(pos.symbol)
            related = self._raw[pid]["PositionBase"].get("RelatedOpenOrders") or []
            stop = next((o for o in related if o.get("OpenOrderType") in STOP_TYPES), None)
            target = next((o for o in related if o.get("OpenOrderType") == TP_TYPE), None)
            new: list[dict[str, Any]] = []
            await self._set_order(stop, pos, "StopIfTraded", inst.round_price(stop_loss), new)
            if take_profit is None:
                if target is not None:
                    await self.client.request(
                        "DELETE",
                        f"/trade/v2/orders/{target['OrderId']}",
                        params={"AccountKey": self.account_key},
                    )
            else:
                await self._set_order(target, pos, TP_TYPE, inst.round_price(take_profit), new)
            if new:
                await self.client.request(
                    "POST", "/trade/v2/orders", json={"PositionId": pid, "Orders": new}
                )
            raw = (await self._fetch_positions()).get(pid)
            if raw is None:
                raise SaxoError(f"position {position_id} fermée pendant la modification")
            self._raw[pid] = raw
            self._positions[pid] = await self._position(raw)
            updated = self._positions[pid]
        await self.listener.on_position_modified(updated)
        return updated

    async def _set_order(
        self,
        existing: dict[str, Any] | None,
        pos: Position,
        order_type: str,
        price: Decimal,
        new: list[dict[str, Any]],
    ) -> None:
        if existing is None:
            uic = await self.uic(pos.symbol)
            new.append({**self._related(pos.side, pos.quantity, order_type, price), "Uic": uic})
            return
        if dec(existing["OrderPrice"]) == price:
            return
        await self.client.request(
            "PATCH",
            "/trade/v2/orders",
            json={
                "AccountKey": self.account_key,
                "OrderId": existing["OrderId"],
                "AssetType": "FxSpot",
                "OrderType": existing.get("OpenOrderType", order_type),
                "OrderPrice": float(price),
                "Amount": pos.quantity,
                "OrderDuration": {"DurationType": "GoodTillCancel"},
                "ManualOrder": False,
            },
        )

    async def close_position(self, position_id: str, reason: ExitReason) -> None:
        async with self._lock:
            pid = self._pid(position_id)
            pos = self._positions[pid]
            self._closing[pid] = reason
            try:
                await self.client.request(
                    "POST",
                    "/trade/v2/orders",
                    json={
                        "PositionId": pid,
                        "Uic": await self.uic(pos.symbol),
                        "AssetType": "FxSpot",
                        "BuySell": "Sell" if pos.side is Side.BUY else "Buy",
                        "Amount": pos.quantity,
                        "OrderType": "Market",
                        "OrderDuration": {"DurationType": "DayOrder"},
                        "ManualOrder": False,
                    },
                )
            except SaxoError:
                self._closing.pop(pid, None)
                raise
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.fill_timeout
        while loop.time() < deadline:
            async with self._lock:
                if pid not in self._positions:
                    return  # déjà signalée par la relève
                if pid not in await self._fetch_positions():
                    gone = self._positions.pop(pid)
                    self._raw.pop(pid, None)
                    await self._report_close(pid, gone)
                    return
            await asyncio.sleep(0.5)
        log.warning("fermeture de %s envoyée, en attente de confirmation par Saxo", position_id)

    async def find_order(self, order_id: str) -> Order | None:
        now = self.clock.now()
        async with self._lock:
            raw = await self._fetch_positions()
        for r in raw.values():
            ref = parse_ref(r["PositionBase"].get("ExternalReference"))
            if ref is not None and ref[0] == order_id:
                pos = await self._position(r)
                found = self._found(order_id, pos.strategy_id, pos.symbol, pos.side, now)
                return found.model_copy(update={"quantity": pos.quantity}).transition(
                    OrderStatus.FILLED,
                    now,
                    filled_quantity=pos.quantity,
                    avg_fill_price=pos.entry_price,
                    broker_order_id=str(r["PositionBase"].get("SourceOrderId") or ""),
                )
        data = await self.client.get(
            "/port/v1/orders", ClientKey=self.client_key, AccountKey=self.account_key
        )
        for o in data.get("Data", []):
            ref = parse_ref(o.get("ExternalReference"))
            if ref is not None and ref[0] == order_id:
                symbol = await self._symbol(int(o["Uic"]))
                side = Side.BUY if o.get("BuySell") == "Buy" else Side.SELL
                found = self._found(order_id, ref[1], symbol, side, now)
                amount = max(int(abs(dec(o.get("Amount", 1)))), 1)
                return found.model_copy(update={"quantity": amount}).transition(
                    OrderStatus.ACCEPTED, now, broker_order_id=str(o.get("OrderId"))
                )
        return None

    @staticmethod
    def _found(order_id: str, strategy: str, symbol: str, side: Side, now: datetime) -> Order:
        return Order(
            id=order_id,
            strategy_id=strategy,
            symbol=symbol,
            side=side,
            order_type=OrderType.MARKET,
            quantity=1,
            stop_loss_distance=Decimal("0.0001"),
            status=OrderStatus.SUBMITTED,
            created_at=now,
            updated_at=now,
        )

    # Données de marché

    async def stream_prices(self, symbols: list[str]) -> AsyncIterator[Tick]:
        uics = {await self.uic(s): s for s in symbols}
        last: dict[int, tuple[Decimal, Decimal, str]] = {}
        while True:
            data = await self.client.get(
                "/trade/v1/infoprices/list",
                AccountKey=self.account_key,
                Uics=",".join(str(u) for u in uics),
                AssetType="FxSpot",
                FieldGroups="Quote",
            )
            for item in data.get("Data", []):
                uic = int(item["Uic"])
                q = item.get("Quote") or {}
                if uic not in uics or q.get("Bid") is None or q.get("Ask") is None:
                    continue
                if q.get("MarketState") == "Closed":
                    continue
                stamp = item.get("LastUpdated") or ""
                key = (dec(q["Bid"]), dec(q["Ask"]), stamp)
                if last.get(uic) == key:
                    continue
                last[uic] = key
                ts = parse_time(stamp) if stamp else self.clock.now()
                yield Tick(symbol=uics[uic], bid=key[0], ask=key[1], ts=ts)
            await asyncio.sleep(self.price_interval)

    async def history(self, symbol: str, timeframe: Timeframe, count: int) -> list[Bar]:
        uic = await self.uic(symbol)
        params = {
            "AssetType": "FxSpot",
            "Uic": uic,
            "Horizon": HORIZON[timeframe],
            "Count": min(count, 1200),
        }
        try:
            data = await self.client.get("/chart/v3/charts", **params)
        except SaxoError as e:
            if e.status != 404:
                raise
            data = await self.client.get("/chart/v1/charts", **params)
        now = self.clock.now()
        bars = []
        for c in data.get("Data", []):
            open_time = parse_time(c["Time"])
            if open_time + timeframe.duration > now:
                continue  # bougie en cours
            spread = None
            if c.get("CloseAsk") is not None:
                spread = max(Decimal(0), dec(c["CloseAsk"]) - dec(c["CloseBid"]))
            bars.append(
                Bar(
                    symbol=symbol,
                    timeframe=timeframe,
                    open_time=open_time,
                    open=dec(c["OpenBid"]),
                    high=dec(c["HighBid"]),
                    low=dec(c["LowBid"]),
                    close=dec(c["CloseBid"]),
                    spread=spread,
                    volume=dec(c.get("Volume", 0)),
                )
            )
        return bars
