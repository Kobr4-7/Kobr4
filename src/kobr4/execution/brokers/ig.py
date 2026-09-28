"""Adaptateur IG (API REST « gateway/deal » et flux Lightstreamer).

Correspondances :
- une position Kobr4 = une position IG (compte CFD) ; la référence de l'ordre
  (`dealReference`, 30 caractères au plus) est dérivée de l'identifiant de l'ordre
  Kobr4 et recopiée sur la position, ce qui permet de la retrouver après un redémarrage ;
- les prix IG des paires de devises sont exprimés en points (`scalingFactor`, par
  exemple 10852.3 pour 1.08523) : tout est converti à l'entrée et à la sortie ;
- la taille IG est un nombre de contrats (`contractSize` unités chacun) : les unités
  Kobr4 sont arrondies vers le bas au pas de taille du marché ;
- le stop et l'objectif sont posés à l'ouverture, par distance au prix d'exécution ;
- IG limite fortement le nombre de requêtes (une dizaine par minute en démo) : les prix
  arrivent par le flux Lightstreamer, les positions sont relues toutes les 30 s, et
  toutes les lectures passent par un limiteur de débit partagé.

Connexion : clé API + identifiant + mot de passe (`POST /session`), qui donnent les
jetons CST et X-SECURITY-TOKEN. Ils sont redemandés automatiquement quand IG les refuse.
"""

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any, Literal
from urllib.parse import quote, unquote

import httpx

from kobr4.core.clock import Clock
from kobr4.core.instruments import get_instrument
from kobr4.core.models import Account, Bar, ClosedTrade, ExitReason, Position, Tick
from kobr4.core.orders import Order, OrderStatus
from kobr4.core.types import OrderType, Side, Timeframe
from kobr4.execution.broker import Broker, BrokerError

log = logging.getLogger(__name__)

Environment = Literal["practice", "live"]

HOSTS = {
    "practice": "https://demo-api.ig.com/gateway/deal",
    "live": "https://api.ig.com/gateway/deal",
}
RESOLUTION = {
    Timeframe.M1: "MINUTE",
    Timeframe.M5: "MINUTE_5",
    Timeframe.M15: "MINUTE_15",
    Timeframe.M30: "MINUTE_30",
    Timeframe.H1: "HOUR",
    Timeframe.H4: "HOUR_4",
    Timeframe.D1: "DAY",
}
REF_MAX = 30
LS_CID = "mgQkwtwdysogQz2BJ4Ji kOj2Bg"
"""Identifiant client public du protocole Lightstreamer (TLCP)."""


class IgError(BrokerError):
    def __init__(self, message: str, status: int | None = None, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class IgAuthError(IgError):
    """Identifiants refusés : clé API, identifiant ou mot de passe à vérifier."""


def dec(value: Any) -> Decimal:
    return Decimal(str(value))


def parse_time(value: str) -> datetime:
    """Heure UTC d'IG (`2026-09-28T08:00:00` ou avec millisecondes) → datetime UTC."""
    main, _, frac = value.rstrip("Z").replace(" ", "T").partition(".")
    micro = (frac + "000000")[:6]
    return datetime.fromisoformat(f"{main}.{micro}").replace(tzinfo=UTC)


def deal_ref(order_id: str) -> str:
    """Référence IG (30 caractères au plus) d'un ordre Kobr4 `<stratégie>-<hex>`."""
    if len(order_id) <= REF_MAX:
        return order_id
    head, _, tail = order_id.rpartition("-")
    return f"{head[: REF_MAX - len(tail) - 1]}-{tail}"


def strategy_of(ref: str) -> str:
    return ref.rpartition("-")[0] or "externe"


def parse_money(value: Any) -> Decimal | None:
    """Montant IG, parfois préfixé d'un symbole de devise (« E-12.50 », « $1,204.10 »)."""
    if isinstance(value, int | float):
        return dec(value)
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(value or ""))
    return Decimal(m.group(0).replace(",", "")) if m else None


# Client partagé


@dataclass
class Market:
    symbol: str
    epic: str
    scaling: Decimal
    contract_size: Decimal
    min_size: Decimal
    currency: str

    def price(self, raw: Any) -> Decimal:
        return dec(raw) / self.scaling

    def raw(self, price: Decimal) -> Decimal:
        return price * self.scaling

    def units(self, size: Any) -> int:
        return int(abs(dec(size)) * self.contract_size)

    def size(self, units: int) -> Decimal:
        exp = max(0, -self.min_size.normalize().as_tuple().exponent)  # type: ignore[operator]
        step = Decimal(1).scaleb(-exp)
        return (Decimal(units) / self.contract_size).quantize(step, rounding=ROUND_DOWN)


class RateLimiter:
    """Espace les requêtes de lecture pour rester sous la limite d'IG.

    Le débit baisse de moitié chaque fois qu'IG signale un dépassement.
    """

    def __init__(self, per_minute: float, floor: float = 4.0) -> None:
        self.per_minute = per_minute
        self.floor = floor
        self._next = 0.0
        self._lock = asyncio.Lock()

    @property
    def interval(self) -> float:
        return 60.0 / self.per_minute if self.per_minute > 0 else 0.0

    def slow_down(self) -> None:
        self.per_minute = max(self.floor, self.per_minute / 2)

    async def wait(self) -> None:
        if self.interval <= 0:
            return
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self.interval


class IgClient:
    """Session IG d'une connexion, partagée par tous les bots qui l'utilisent."""

    def __init__(
        self,
        environment: Environment,
        api_key: str,
        identifier: str,
        password: str,
        account_id: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        reads_per_minute: float = 25.0,
        exceeded_pause: float = 30.0,
    ) -> None:
        self.environment = environment
        self.api_key = api_key
        self.identifier = identifier
        self.password = password
        self.account_id = account_id
        self.transport = transport
        self.http = httpx.AsyncClient(
            base_url=HOSTS[environment], timeout=httpx.Timeout(15.0), transport=transport
        )
        self.cst = ""
        self.xst = ""
        self.lightstreamer = ""
        self.accounts: list[dict[str, Any]] = []
        self.limiter = RateLimiter(reads_per_minute)
        self.exceeded_pause = exceeded_pause
        self.markets: dict[str, Market] = {}
        """Marché choisi pour chaque paire."""
        self.by_epic: dict[str, Market] = {}
        self._login_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self.http.aclose()

    def _headers(self, version: str) -> dict[str, str]:
        h = {
            "X-IG-API-KEY": self.api_key,
            "Accept": "application/json; charset=UTF-8",
            "Content-Type": "application/json; charset=UTF-8",
            "Version": version,
        }
        if self.cst:
            h["CST"] = self.cst
            h["X-SECURITY-TOKEN"] = self.xst
        return h

    @staticmethod
    def _error(resp: httpx.Response) -> IgError:
        try:
            code = str(resp.json().get("errorCode", ""))
        except ValueError:
            code = resp.text[:200]
        if "exceeded" in code:
            return IgError("limite de requêtes IG atteinte, nouvel essai plus tard", 429, code)
        if resp.status_code in (401, 403) and (
            "invalid.details" in code or "invalid.api.key" in code or "apikey" in code.lower()
        ):
            return IgAuthError(
                "identifiants IG refusés : vérifie la clé API, l'identifiant et le mot de "
                "passe du compte démo",
                resp.status_code,
                code,
            )
        return IgError(
            f"IG a refusé la requête : {code or resp.reason_phrase}", resp.status_code, code
        )

    async def login(self) -> dict[str, Any]:
        async with self._login_lock:
            self.cst = self.xst = ""
            try:
                resp = await self.http.post(
                    "/session",
                    headers=self._headers("2"),
                    json={"identifier": self.identifier, "password": self.password},
                )
            except httpx.HTTPError as e:
                raise IgError(f"IG injoignable ({e.__class__.__name__})") from e
            if resp.status_code >= 400:
                raise self._error(resp)
            self.cst = resp.headers.get("CST", "")
            self.xst = resp.headers.get("X-SECURITY-TOKEN", "")
            data: dict[str, Any] = resp.json()
            self.lightstreamer = data.get("lightstreamerEndpoint", "")
            self.accounts = data.get("accounts", [])
            current = data.get("currentAccountId")
            if self.account_id and current != self.account_id:
                resp = await self.http.put(
                    "/session",
                    headers=self._headers("1"),
                    json={"accountId": self.account_id, "defaultAccount": False},
                )
                if resp.status_code >= 400:
                    raise IgError(f"compte IG {self.account_id} introuvable pour cet identifiant")
                self.cst = resp.headers.get("CST", self.cst)
                self.xst = resp.headers.get("X-SECURITY-TOKEN", self.xst)
            return data

    async def request(
        self,
        method: str,
        path: str,
        version: str = "1",
        trading: bool = False,
        **kw: Any,
    ) -> Any:
        if not self.cst:
            await self.login()
        relogged = False
        exceeded = 0
        while True:
            if not trading:
                await self.limiter.wait()
            headers = self._headers(version)
            if method == "DELETE":  # IG n'accepte pas de corps avec DELETE
                headers["_method"] = "DELETE"
            try:
                resp = await self.http.request(
                    "POST" if method == "DELETE" else method, path, headers=headers, **kw
                )
            except httpx.HTTPError as e:
                raise IgError(f"IG injoignable : {e}") from e
            if resp.status_code == 401 and not relogged:
                relogged = True
                await self.login()  # jetons expirés : nouvelle session
                continue
            if resp.status_code >= 400:
                err = self._error(resp)
                if err.status == 429 and exceeded < 3 and not trading:
                    exceeded += 1
                    self.limiter.slow_down()
                    log.warning("limite IG atteinte : %.0f lectures/min", self.limiter.per_minute)
                    await asyncio.sleep(self.exceeded_pause)
                    continue
                raise err
            return resp.json() if resp.content else {}

    # Marchés

    def _parse_market(self, d: dict[str, Any], symbol: str) -> Market:
        inst, rules, snap = d["instrument"], d.get("dealingRules") or {}, d.get("snapshot") or {}
        currencies = inst.get("currencies") or [{"code": symbol[4:], "isDefault": True}]
        return Market(
            symbol=symbol,
            epic=inst["epic"],
            scaling=dec(snap.get("scalingFactor") or 1),
            contract_size=dec(inst.get("contractSize") or 1),
            min_size=dec((rules.get("minDealSize") or {}).get("value") or 1),
            currency=next(
                (c["code"] for c in currencies if c.get("isDefault")), currencies[0]["code"]
            ),
        )

    async def _details(self, epics: list[str]) -> list[Market]:
        data = await self.request(
            "GET", "/markets", version="2", params={"epics": ",".join(epics), "filter": "ALL"}
        )
        out = []
        for d in data.get("marketDetails", []):
            code = d["instrument"]["epic"].split(".")[2]
            m = self._parse_market(d, f"{code[:3]}/{code[3:6]}")
            self.by_epic[m.epic] = m
            out.append(m)
        return out

    async def market(self, symbol: str) -> Market:
        """Marché IG le plus fin (plus petite taille minimale) pour une paire."""
        if symbol in self.markets:
            return self.markets[symbol]
        code = symbol.replace("/", "")
        found = await self.request("GET", "/markets", params={"searchTerm": code})
        epics = [
            m["epic"]
            for m in found.get("markets", [])
            if m.get("epic", "").startswith(f"CS.D.{code}.") and m.get("expiry", "-") == "-"
        ]
        if not epics:
            raise IgError(f"marché {symbol} introuvable chez IG")
        candidates = await self._details(epics[:5])
        best = min(candidates, key=lambda m: m.min_size * m.contract_size, default=None)
        if best is None:
            raise IgError(f"marché {symbol} introuvable chez IG")
        self.markets[symbol] = best
        return best

    async def market_for_epic(self, epic: str) -> Market:
        if epic not in self.by_epic:
            await self._details([epic])
        if epic not in self.by_epic:
            raise IgError(f"marché IG {epic} introuvable")
        return self.by_epic[epic]


async def list_accounts(client: IgClient) -> list[dict[str, Any]]:
    """Comptes CFD accessibles avec ces identifiants (les comptes « spread bet » ne
    conviennent pas : leur taille est en devise par point)."""
    await client.login()
    data = await client.request("GET", "/accounts")
    return [
        {
            "id": a["accountId"],
            "alias": a.get("accountAlias") or a.get("accountName") or "",
            "currency": a.get("currency", ""),
            "balance": str((a.get("balance") or {}).get("balance", "")),
        }
        for a in data.get("accounts", [])
        if a.get("accountType") == "CFD"
    ]


# Flux Lightstreamer


def _ls_decode(value: str) -> str | None:
    if value == "#":
        return None
    if value == "$":
        return ""
    return unquote(value)


class LightstreamerPrices:
    """Abonnement aux prix (BID, OFFER) de plusieurs marchés, protocole TLCP 2.1."""

    def __init__(self, client: IgClient, markets: list[Market]) -> None:
        self.client = client
        self.markets = markets

    async def ticks(self) -> AsyncIterator[tuple[Market, Decimal, Decimal]]:
        c = self.client
        if not c.cst:
            await c.login()
        base = c.lightstreamer.rstrip("/")
        if not base:
            raise IgError("adresse du flux de prix IG absente")
        form = {
            "LS_user": c.account_id or "",
            "LS_password": f"CST-{c.cst}|XST-{c.xst}",
            "LS_adapter_set": "DEFAULT",
            "LS_cid": LS_CID,
            "LS_send_sync": "false",
        }
        async with (
            httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, read=90.0), transport=c.transport
            ) as http,
            http.stream(
                "POST",
                f"{base}/lightstreamer/create_session.txt?LS_protocol=TLCP-2.1.0",
                content="&".join(f"{k}={quote(v)}" for k, v in form.items()),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as resp,
        ):
            if resp.status_code >= 400:
                raise IgError(f"flux de prix IG refusé (HTTP {resp.status_code})")
            values: dict[int, list[str | None]] = {}
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                kind, _, rest = line.partition(",")
                if kind == "CONOK":
                    session_id, _, _, control = (rest.split(",") + ["*"] * 4)[:4]
                    host = base if control in ("*", "") else f"https://{control}"
                    await self._subscribe(http, host, session_id)
                elif kind == "U":
                    _sub, item, fields = rest.split(",", 2)
                    idx = int(item) - 1
                    prev = values.get(idx, [None, None])
                    cur = self._apply(prev, fields)
                    values[idx] = cur
                    bid, offer = cur
                    if bid and offer and 0 <= idx < len(self.markets):
                        m = self.markets[idx]
                        yield m, m.price(bid), m.price(offer)
                elif kind in ("CONERR", "END", "REQERR", "ERROR"):
                    raise IgError(f"flux de prix IG : {line}")
                elif kind == "LOOP":
                    return  # le serveur demande de rouvrir la session

    async def _subscribe(self, http: httpx.AsyncClient, host: str, session_id: str) -> None:
        form = {
            "LS_reqId": "1",
            "LS_op": "add",
            "LS_subId": "1",
            "LS_mode": "MERGE",
            "LS_group": " ".join(f"MARKET:{m.epic}" for m in self.markets),
            "LS_schema": "BID OFFER",
            "LS_snapshot": "true",
        }
        resp = await http.post(
            f"{host}/lightstreamer/control.txt?LS_protocol=TLCP-2.1.0&LS_session={session_id}",
            content="&".join(f"{k}={quote(v)}" for k, v in form.items()),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code >= 400 or resp.text.startswith("REQERR"):
            raise IgError(f"abonnement aux prix IG refusé : {resp.text[:200]}")

    @staticmethod
    def _apply(prev: list[str | None], fields: str) -> list[str | None]:
        out: list[str | None] = []
        i = 0
        for part in fields.split("|"):
            if part.startswith("^") and part[1:].isdigit():
                n = int(part[1:])
                out += prev[i : i + n]
                i += n
                continue
            out.append(prev[i] if part == "" and i < len(prev) else _ls_decode(part))
            i += 1
        out += prev[len(out) :]
        return out[: len(prev)]


# Courtier


class IgBroker(Broker):
    def __init__(
        self,
        client: IgClient,
        account_id: str,
        clock: Clock,
        position_interval: float = 30.0,
        rest_price_interval: float = 20.0,
        confirm_timeout: float = 10.0,
    ) -> None:
        super().__init__()
        self.client = client
        self.account_id = account_id
        self.clock = clock
        self.position_interval = position_interval
        self.rest_price_interval = rest_price_interval
        self.confirm_timeout = confirm_timeout
        self.account_currency = "EUR"
        self._positions: dict[str, Position] = {}
        """Positions ouvertes, par identifiant de deal IG."""
        self._epics: dict[str, str] = {}
        self._refs: dict[str, tuple[str, str]] = {}
        """Ordre Kobr4 (identifiant, stratégie) par référence IG."""
        self._closing: dict[str, ExitReason] = {}
        self._lock = asyncio.Lock()
        self._poll_task: asyncio.Task[None] | None = None

    # Connexion

    async def connect(self) -> None:
        self.client.account_id = self.account_id
        await self.client.login()
        acct = next(
            (a for a in self.client.accounts if a.get("accountId") == self.account_id), None
        )
        if acct is None:
            raise IgError("compte IG introuvable pour ces identifiants")
        a = await self.account()
        self.account_currency = a.currency
        async with self._lock:
            await self._refresh_positions(notify=False)
        if self._poll_task is None:
            self._poll_task = asyncio.create_task(self._poll_loop(), name="ig-positions")

    async def close(self) -> None:
        """Arrête la relève ; la session IG, partagée, reste ouverte."""
        if self._poll_task is not None:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None

    # Positions

    async def _position(self, raw: dict[str, Any]) -> Position:
        p, mk = raw["position"], raw["market"]
        m = await self.client.market_for_epic(mk["epic"])
        deal_id = p["dealId"]
        self._epics[deal_id] = m.epic
        side = Side.BUY if p["direction"] == "BUY" else Side.SELL
        entry = m.price(p["level"])
        sl = m.price(p["stopLevel"]) if p.get("stopLevel") is not None else None
        tp = m.price(p["limitLevel"]) if p.get("limitLevel") is not None else None
        if sl is None:
            log.warning("position IG %s sans stop loss", deal_id)
            sl = entry * (Decimal("0.5") if side is Side.BUY else Decimal("1.5"))
        ref = p.get("dealReference") or ""
        known = self._positions.get(deal_id)
        if ref in self._refs:
            position_id, strategy = self._refs[ref]
        elif known is not None:
            position_id, strategy = known.id, known.strategy_id
        elif ref:
            position_id, strategy = ref, strategy_of(ref)
        else:
            position_id, strategy = f"ig-{deal_id}", "externe"
        opened = p.get("createdDateUTC") or p.get("createdDate")
        return Position(
            id=position_id,
            strategy_id=strategy,
            symbol=m.symbol,
            side=side,
            quantity=m.units(p["size"]),
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            opened_at=parse_time(opened) if opened else self.clock.now(),
        )

    async def _fetch_positions(self) -> dict[str, dict[str, Any]]:
        data = await self.client.request("GET", "/positions", version="2")
        return {
            p["position"]["dealId"]: p
            for p in data.get("positions", [])
            if p["market"].get("epic", "").startswith("CS.D.")
        }

    async def _refresh_positions(self, notify: bool = True) -> None:
        """Relit les positions ; signale ouvertures, modifications et fermetures.

        À appeler sous `self._lock`.
        """
        raw = await self._fetch_positions()
        for deal_id, r in raw.items():
            pos = await self._position(r)
            before = self._positions.get(deal_id)
            self._positions[deal_id] = pos
            if not notify:
                continue
            if before is None:
                await self.listener.on_position_opened(pos)
            elif (before.stop_loss, before.take_profit) != (pos.stop_loss, pos.take_profit):
                await self.listener.on_position_modified(pos)
        for deal_id in [d for d in self._positions if d not in raw]:
            gone = self._positions.pop(deal_id)
            if notify:
                await self._report_close(deal_id, gone, None)

    async def _report_close(
        self, deal_id: str, gone: Position, confirm: dict[str, Any] | None
    ) -> None:
        exit_price: Decimal | None = None
        pnl: Decimal | None = None
        closed_at = self.clock.now()
        m = await self.client.market_for_epic(self._epics[deal_id])
        if confirm is not None and confirm.get("level") is not None:
            exit_price = m.price(confirm["level"])
            pnl = parse_money(confirm.get("profit"))
            if confirm.get("date"):
                closed_at = parse_time(confirm["date"])
        if exit_price is None or pnl is None:
            found = await self._closed_transaction(gone, m)
            if found is not None:
                exit_price, pnl, closed_at = found
        if exit_price is None:
            log.error("position IG %s fermée mais introuvable dans l'historique", deal_id)
            exit_price = gone.entry_price
        if pnl is None:
            # Repli : résultat en devise de cotation (exact si c'est la devise du compte).
            pnl = (exit_price - gone.entry_price) * gone.side.sign * gone.quantity
            log.warning("position IG %s : résultat recalculé, en %s", deal_id, gone.symbol[4:])
        reason = self._closing.pop(deal_id, None) or self._exit_reason(gone, exit_price)
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
                commission=Decimal(0),
                financing=Decimal(0),
            )
        )

    async def _closed_transaction(
        self, pos: Position, m: Market
    ) -> tuple[Decimal, Decimal, datetime] | None:
        since = (pos.opened_at - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
        data = await self.client.request(
            "GET", "/history/transactions", version="2", params={"type": "ALL_DEAL", "from": since}
        )
        for t in data.get("transactions", []):
            try:
                open_level = m.price(t["openLevel"])
            except (KeyError, ArithmeticError, ValueError):
                continue
            if open_level != pos.entry_price:
                continue
            pnl = parse_money(t.get("profitAndLoss"))
            close = t.get("closeLevel")
            if pnl is None or close in (None, "", "-"):
                continue
            when = t.get("dateUtc") or t.get("date")
            return m.price(close), pnl, parse_time(when) if when else self.clock.now()
        return None

    @staticmethod
    def _exit_reason(pos: Position, price: Decimal) -> ExitReason:
        to_sl = abs(price - pos.stop_loss)
        if pos.take_profit is not None and abs(price - pos.take_profit) < to_sl:
            return ExitReason.TAKE_PROFIT
        tick = get_instrument(pos.symbol).tick_size * 20
        return ExitReason.STOP_LOSS if to_sl <= tick else ExitReason.MANUAL

    def _deal_id(self, position_id: str) -> str:
        for deal_id, pos in self._positions.items():
            if pos.id == position_id:
                return deal_id
        raise BrokerError(f"position inconnue : {position_id}")

    async def positions(self) -> list[Position]:
        async with self._lock:
            await self._refresh_positions()
        return list(self._positions.values())

    async def account(self) -> Account:
        data = await self.client.request("GET", "/accounts")
        a = next((x for x in data.get("accounts", []) if x["accountId"] == self.account_id), None)
        if a is None:
            raise IgError("compte IG introuvable")
        b = a.get("balance") or {}
        balance = dec(b.get("balance", 0))
        return Account(
            currency=a.get("currency", self.account_currency),
            balance=balance,
            equity=balance + dec(b.get("profitLoss", 0)),
            margin_used=max(Decimal(0), dec(b.get("deposit", 0))),
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
                delay = min(delay * 2, 300)
                log.warning("lecture des positions IG impossible (%s), nouvel essai", e)

    # Ordres

    async def _confirm(self, ref: str) -> dict[str, Any] | None:
        """Confirmation d'une opération (quelques instants après l'envoi)."""
        if not ref:
            return None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.confirm_timeout
        while True:
            try:
                data: dict[str, Any] = await self.client.request(
                    "GET", f"/confirms/{ref}", trading=True
                )
                return data
            except IgError as e:
                if e.status != 404:
                    raise
                if loop.time() >= deadline:
                    return None
            await asyncio.sleep(0.5)

    async def submit(self, order: Order) -> Order:
        now = self.clock.now()
        order = order.transition(OrderStatus.SUBMITTED, now)
        if order.order_type is not OrderType.MARKET:
            return order.transition(
                OrderStatus.REJECTED, now, reject_reason="seuls les ordres au marché sont gérés"
            )
        try:
            m = await self.client.market(order.symbol)
        except IgError as e:
            return order.transition(OrderStatus.REJECTED, self.clock.now(), reject_reason=str(e))
        size = m.size(order.quantity)
        if size < m.min_size:
            return order.transition(
                OrderStatus.REJECTED,
                self.clock.now(),
                reject_reason=(
                    f"taille trop petite pour IG : {order.quantity} unités, minimum "
                    f"{m.units(m.min_size)} unités"
                ),
            )
        ref = deal_ref(order.id)
        body: dict[str, Any] = {
            "epic": m.epic,
            "expiry": "-",
            "direction": "BUY" if order.side is Side.BUY else "SELL",
            "size": float(size),
            "orderType": "MARKET",
            "timeInForce": "FILL_OR_KILL",
            "guaranteedStop": False,
            "forceOpen": True,
            "currencyCode": m.currency,
            "stopDistance": float(m.raw(order.stop_loss_distance)),
            "dealReference": ref,
        }
        if order.take_profit_distance is not None:
            body["limitDistance"] = float(m.raw(order.take_profit_distance))
        async with self._lock:
            self._refs[ref] = (order.id, order.strategy_id)
            try:
                await self.client.request(
                    "POST", "/positions/otc", version="2", trading=True, json=body
                )
                conf = await self._confirm(ref)
            except IgError as e:
                return order.transition(
                    OrderStatus.REJECTED, self.clock.now(), reject_reason=str(e)
                )
            if conf is None or conf.get("dealStatus") != "ACCEPTED":
                reason = (conf or {}).get("reason") or "ordre non confirmé par IG"
                return order.transition(
                    OrderStatus.REJECTED, self.clock.now(), reject_reason=str(reason)
                )
            deal_id = conf["dealId"]
            self._epics[deal_id] = m.epic
            raw = (await self._fetch_positions()).get(deal_id)
            if raw is not None:
                pos = await self._position(raw)
            else:  # pas encore visible dans la liste : construite depuis la confirmation
                pos = Position(
                    id=order.id,
                    strategy_id=order.strategy_id,
                    symbol=order.symbol,
                    side=order.side,
                    quantity=m.units(conf.get("size", size)),
                    entry_price=m.price(conf["level"]),
                    stop_loss=m.price(conf["stopLevel"])
                    if conf.get("stopLevel") is not None
                    else order.protection_prices(m.price(conf["level"]))[0],
                    take_profit=m.price(conf["limitLevel"])
                    if conf.get("limitLevel") is not None
                    else None,
                    opened_at=parse_time(conf["date"]) if conf.get("date") else self.clock.now(),
                )
            self._positions[deal_id] = pos
            await self.listener.on_position_opened(pos)
        return order.transition(
            OrderStatus.FILLED,
            pos.opened_at,
            filled_quantity=min(pos.quantity, order.quantity),
            avg_fill_price=pos.entry_price,
            broker_order_id=deal_id,
        )

    async def modify_position(
        self, position_id: str, stop_loss: Decimal, take_profit: Decimal | None
    ) -> Position:
        async with self._lock:
            deal_id = self._deal_id(position_id)
            pos = self._positions[deal_id]
            m = await self.client.market_for_epic(self._epics[deal_id])
            inst = get_instrument(pos.symbol)
            body = {
                "stopLevel": float(m.raw(inst.round_price(stop_loss))),
                "limitLevel": None
                if take_profit is None
                else float(m.raw(inst.round_price(take_profit))),
                "guaranteedStop": False,
                "trailingStop": False,
            }
            res = await self.client.request(
                "PUT", f"/positions/otc/{deal_id}", version="2", trading=True, json=body
            )
            conf = await self._confirm(res.get("dealReference", ""))
            if conf is not None and conf.get("dealStatus") == "REJECTED":
                raise IgError(f"modification refusée par IG : {conf.get('reason')}")
            updated = pos.model_copy(
                update={
                    "stop_loss": inst.round_price(stop_loss),
                    "take_profit": None if take_profit is None else inst.round_price(take_profit),
                }
            )
            self._positions[deal_id] = updated
        await self.listener.on_position_modified(updated)
        return updated

    async def close_position(self, position_id: str, reason: ExitReason) -> None:
        async with self._lock:
            deal_id = self._deal_id(position_id)
            pos = self._positions[deal_id]
            m = await self.client.market_for_epic(self._epics[deal_id])
            self._closing[deal_id] = reason
            try:
                res = await self.client.request(
                    "DELETE",
                    "/positions/otc",
                    version="1",
                    trading=True,
                    json={
                        "dealId": deal_id,
                        "direction": "SELL" if pos.side is Side.BUY else "BUY",
                        "size": float(m.size(pos.quantity)),
                        "orderType": "MARKET",
                        "timeInForce": "FILL_OR_KILL",
                    },
                )
                conf = await self._confirm(res.get("dealReference", ""))
            except IgError:
                self._closing.pop(deal_id, None)
                raise
            if conf is not None and conf.get("dealStatus") == "REJECTED":
                self._closing.pop(deal_id, None)
                raise IgError(f"fermeture refusée par IG : {conf.get('reason')}")
            gone = self._positions.pop(deal_id)
            await self._report_close(deal_id, gone, conf)

    async def find_order(self, order_id: str) -> Order | None:
        ref = deal_ref(order_id)
        now = self.clock.now()
        async with self._lock:
            raw = await self._fetch_positions()
        for r in raw.values():
            if r["position"].get("dealReference") == ref:
                pos = await self._position(r)
                return self._found(order_id, pos, now).transition(
                    OrderStatus.FILLED,
                    now,
                    filled_quantity=pos.quantity,
                    avg_fill_price=pos.entry_price,
                    broker_order_id=r["position"]["dealId"],
                )
        try:
            conf = await self.client.request("GET", f"/confirms/{ref}", trading=True)
        except IgError as e:
            if e.status == 404:
                return None
            raise
        if conf.get("dealStatus") == "REJECTED":
            base = Order(
                id=order_id,
                strategy_id=strategy_of(ref),
                symbol="EUR/USD",
                side=Side.BUY,
                order_type=OrderType.MARKET,
                quantity=1,
                stop_loss_distance=Decimal("0.0001"),
                status=OrderStatus.SUBMITTED,
                created_at=now,
                updated_at=now,
            )
            return base.transition(OrderStatus.REJECTED, now, reject_reason=conf.get("reason"))
        return None

    @staticmethod
    def _found(order_id: str, pos: Position, now: datetime) -> Order:
        return Order(
            id=order_id,
            strategy_id=pos.strategy_id,
            symbol=pos.symbol,
            side=pos.side,
            order_type=OrderType.MARKET,
            quantity=pos.quantity,
            stop_loss_distance=Decimal("0.0001"),
            status=OrderStatus.SUBMITTED,
            created_at=now,
            updated_at=now,
        )

    # Données de marché

    async def stream_prices(self, symbols: list[str]) -> AsyncIterator[Tick]:
        markets = [await self.client.market(s) for s in symbols]
        received = False
        try:
            async for m, bid, ask in LightstreamerPrices(self.client, markets).ticks():
                received = True
                if ask >= bid:
                    yield Tick(symbol=m.symbol, bid=bid, ask=ask, ts=self.clock.now())
        except IgError as e:
            if received:
                raise
            log.warning("flux de prix IG indisponible (%s) : relecture lente des prix", e)
        if received:
            return
        async for tick in self._poll_prices(markets):
            yield tick

    async def _poll_prices(self, markets: list[Market]) -> AsyncIterator[Tick]:
        """Repli sans flux : prix relus toutes les `rest_price_interval` secondes."""
        by_epic = {m.epic: m for m in markets}
        while True:
            data = await self.client.request(
                "GET",
                "/markets",
                version="2",
                params={"epics": ",".join(by_epic), "filter": "SNAPSHOT_ONLY"},
            )
            for d in data.get("marketDetails", []):
                epic = (d.get("instrument") or {}).get("epic") or d.get("epic")
                snap = d.get("snapshot") or {}
                m = by_epic.get(epic or "")
                if m is None or snap.get("bid") is None or snap.get("offer") is None:
                    continue
                if snap.get("marketStatus", "TRADEABLE") != "TRADEABLE":
                    continue
                bid, ask = m.price(snap["bid"]), m.price(snap["offer"])
                if ask >= bid:
                    yield Tick(symbol=m.symbol, bid=bid, ask=ask, ts=self.clock.now())
            await asyncio.sleep(self.rest_price_interval)

    async def history(self, symbol: str, timeframe: Timeframe, count: int) -> list[Bar]:
        m = await self.client.market(symbol)
        data = await self.client.request(
            "GET",
            f"/prices/{m.epic}",
            version="3",
            params={"resolution": RESOLUTION[timeframe], "max": min(count, 1000), "pageSize": 0},
        )
        now = self.clock.now()
        bars = []
        for p in data.get("prices", []):
            open_time = parse_time(p.get("snapshotTimeUTC") or p["snapshotTime"])
            if open_time + timeframe.duration > now:
                continue  # bougie en cours
            o, h, lo, c = p["openPrice"], p["highPrice"], p["lowPrice"], p["closePrice"]
            if None in (o.get("bid"), h.get("bid"), lo.get("bid"), c.get("bid")):
                continue
            spread = None
            if c.get("ask") is not None:
                spread = max(Decimal(0), m.price(c["ask"]) - m.price(c["bid"]))
            bars.append(
                Bar(
                    symbol=symbol,
                    timeframe=timeframe,
                    open_time=open_time,
                    open=m.price(o["bid"]),
                    high=m.price(h["bid"]),
                    low=m.price(lo["bid"]),
                    close=m.price(c["bid"]),
                    spread=spread,
                    volume=dec(p.get("lastTradedVolume") or 0),
                )
            )
        return bars


def credentials_dumps(api_key: str, identifier: str, password: str) -> str:
    return json.dumps({"api_key": api_key, "identifier": identifier, "password": password})


def credentials_loads(raw: str) -> tuple[str, str, str]:
    d = json.loads(raw)
    return d["api_key"], d["identifier"], d["password"]
