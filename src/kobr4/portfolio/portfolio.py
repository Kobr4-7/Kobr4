"""Vue du portefeuille : positions ouvertes, trades fermés, solde, équité, drawdown."""

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal

from kobr4.core.bus import EventBus
from kobr4.core.clock import Clock
from kobr4.core.events import EquitySnapshot
from kobr4.core.instruments import get_instrument
from kobr4.core.models import ClosedTrade, Position
from kobr4.core.prices import MissingPriceError, PriceBook


class Portfolio:
    """Tenu à jour par l'OMS dès que le courtier signale un changement (et non via la
    file d'événements, pour que le risque voie immédiatement une position fermée) ;
    valorisé avec le `PriceBook`.

    En direct, le solde est recalé périodiquement sur celui du courtier (`sync_balance`).
    """

    def __init__(
        self,
        bus: EventBus,
        prices: PriceBook,
        clock: Clock,
        account_currency: str,
        initial_balance: Decimal,
    ) -> None:
        self.bus = bus
        self.prices = prices
        self.clock = clock
        self.account_currency = account_currency
        self.balance = initial_balance
        self.initial_balance = initial_balance
        self.open_positions: dict[str, Position] = {}
        self.trades: list[ClosedTrade] = []
        self.peak_equity = initial_balance
        self._day: date | None = None
        self.day_start_equity = initial_balance

    # Changements signalés par le courtier

    def apply_opened(self, position: Position) -> None:
        self.open_positions[position.id] = position

    def apply_modified(self, position: Position) -> None:
        self.open_positions[position.id] = position

    def apply_closed(self, trade: ClosedTrade) -> None:
        self.open_positions.pop(trade.position_id, None)
        self.trades.append(trade)
        self.balance += trade.pnl

    # Lecture

    def positions(
        self, strategy_id: str | None = None, symbol: str | None = None
    ) -> Sequence[Position]:
        return [
            p
            for p in self.open_positions.values()
            if (strategy_id is None or p.strategy_id == strategy_id)
            and (symbol is None or p.symbol == symbol)
        ]

    def unrealized(self) -> Decimal:
        total = Decimal(0)
        for pos in self.open_positions.values():
            try:
                tick = self.prices.tick(pos.symbol)
                rate = self.prices.rate(get_instrument(pos.symbol).quote, self.account_currency)
            except MissingPriceError:
                continue
            total += pos.unrealized_pnl(tick) * rate
        return total

    def equity(self) -> Decimal:
        return self.balance + self.unrealized()

    def position_risk(self, pos: Position) -> Decimal:
        """Perte en devise du compte si le stop de la position est touché."""
        rate = self.prices.rate(get_instrument(pos.symbol).quote, self.account_currency)
        return pos.risk_quote() * rate

    def drawdown_pct(self, equity: Decimal | None = None) -> Decimal:
        eq = self.equity() if equity is None else equity
        if self.peak_equity <= 0:
            return Decimal(0)
        return max(Decimal(0), (self.peak_equity - eq) / self.peak_equity * 100)

    def daily_loss_pct(self, equity: Decimal | None = None) -> Decimal:
        eq = self.equity() if equity is None else equity
        if self.day_start_equity <= 0:
            return Decimal(0)
        return max(Decimal(0), (self.day_start_equity - eq) / self.day_start_equity * 100)

    # Mise à jour

    def sync_balance(self, balance: Decimal) -> None:
        self.balance = balance

    def replace_positions(self, positions: Sequence[Position]) -> None:
        """Aligne les positions sur celles du courtier (réconciliation)."""
        self.open_positions = {p.id: p for p in positions}

    async def mark(self, ts: datetime | None = None) -> EquitySnapshot:
        """Valorise le portefeuille, met à jour le plus haut et le début de journée, et
        publie un `EquitySnapshot`."""
        ts = ts or self.clock.now()
        equity = self.equity()
        if self._day != ts.date():
            self._day = ts.date()
            self.day_start_equity = equity
        self.peak_equity = max(self.peak_equity, equity)
        snap = EquitySnapshot(
            ts=ts, balance=self.balance, equity=equity, open_positions=len(self.open_positions)
        )
        await self.bus.publish(snap)
        return snap
