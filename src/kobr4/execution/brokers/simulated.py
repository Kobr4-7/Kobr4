"""Courtier simulé pour le backtest.

Modèle d'exécution :
- les ordres au marché sont exécutés immédiatement au dernier prix connu (ask à
  l'achat, bid à la vente), dégradé du glissement ;
- à chaque bougie, les stops et objectifs des positions ouvertes avant cette bougie
  sont contrôlés sur son plus haut et son plus bas (bid pour un achat, ask = bid +
  spread pour une vente) ;
- si le stop et l'objectif sont tous deux atteints dans la même bougie, on suppose
  que le stop a été touché en premier (hypothèse prudente) ;
- si la bougie ouvre déjà au-delà du stop (gap), la sortie se fait à l'ouverture ;
- la commission est facturée par million d'unités de devise de base échangées, à
  l'ouverture et à la fermeture, et déduite à la fermeture.
"""

from datetime import datetime
from decimal import Decimal

from kobr4.core.clock import Clock
from kobr4.core.instruments import get_instrument
from kobr4.core.models import Account, Bar, ClosedTrade, ExitReason, Position
from kobr4.core.orders import Order, OrderStatus
from kobr4.core.prices import PriceBook
from kobr4.core.types import OrderType, Side
from kobr4.execution.broker import Broker, BrokerError


class SimulatedBroker(Broker):
    def __init__(
        self,
        prices: PriceBook,
        clock: Clock,
        account_currency: str = "USD",
        initial_balance: Decimal = Decimal(10_000),
        slippage_pips: Decimal = Decimal(0),
        commission_per_million: Decimal = Decimal(0),
        leverage: Decimal = Decimal(30),
    ) -> None:
        super().__init__()
        self.prices = prices
        self.clock = clock
        self.account_currency = account_currency
        self.balance = initial_balance
        self.slippage_pips = slippage_pips
        self.commission_per_million = commission_per_million
        self.leverage = leverage
        self._positions: dict[str, Position] = {}
        self._orders: dict[str, Order] = {}

    # Ordres

    async def submit(self, order: Order) -> Order:
        now = self.clock.now()
        order = order.transition(OrderStatus.SUBMITTED, now)
        if order.order_type is not OrderType.MARKET:
            return self._store(
                order.transition(
                    OrderStatus.REJECTED,
                    now,
                    reject_reason="seuls les ordres au marché sont simulés",
                )
            )
        if order.id in self._orders:
            raise BrokerError(f"ordre {order.id} déjà envoyé")
        instrument = get_instrument(order.symbol)
        tick = self.prices.tick(order.symbol)
        slip = instrument.from_pips(self.slippage_pips) * order.side.sign
        price = instrument.round_price(tick.price_for(order.side) + slip)
        sl, tp = order.protection_prices(price)
        position = Position(
            id=order.id,
            strategy_id=order.strategy_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            entry_price=price,
            stop_loss=instrument.round_price(sl),
            take_profit=None if tp is None else instrument.round_price(tp),
            opened_at=now,
        )
        self._positions[position.id] = position
        order = order.transition(
            OrderStatus.FILLED,
            now,
            filled_quantity=order.quantity,
            avg_fill_price=price,
            broker_order_id=f"SIM-{len(self._orders) + 1}",
        )
        self._store(order)
        await self.listener.on_position_opened(position)
        return order

    def _store(self, order: Order) -> Order:
        self._orders[order.id] = order
        return order

    async def find_order(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    # Positions

    async def positions(self) -> list[Position]:
        return list(self._positions.values())

    async def modify_position(
        self, position_id: str, stop_loss: Decimal, take_profit: Decimal | None
    ) -> Position:
        pos = self._get(position_id)
        updated = Position.model_validate(
            {**pos.model_dump(), "stop_loss": stop_loss, "take_profit": take_profit}
        )
        self._positions[position_id] = updated
        await self.listener.on_position_modified(updated)
        return updated

    async def close_position(self, position_id: str, reason: ExitReason) -> None:
        pos = self._get(position_id)
        tick = self.prices.tick(pos.symbol)
        instrument = get_instrument(pos.symbol)
        slip = instrument.from_pips(self.slippage_pips) * pos.side.opposite.sign
        exit_price = instrument.round_price(pos.exit_price(tick) + slip)
        await self._close(pos, exit_price, reason, self.clock.now())

    def _get(self, position_id: str) -> Position:
        try:
            return self._positions[position_id]
        except KeyError:
            raise BrokerError(f"position inconnue : {position_id}") from None

    async def _close(
        self, pos: Position, exit_price: Decimal, reason: ExitReason, ts: datetime
    ) -> None:
        del self._positions[pos.id]
        instrument = get_instrument(pos.symbol)
        to_acct = self.prices.rate(instrument.quote, self.account_currency)
        gross = pos.pnl_quote(exit_price) * to_acct
        commission = self._commission(pos)
        pnl = gross - commission
        self.balance += pnl
        trade = ClosedTrade(
            position_id=pos.id,
            strategy_id=pos.strategy_id,
            symbol=pos.symbol,
            side=pos.side,
            quantity=pos.quantity,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            opened_at=pos.opened_at,
            closed_at=ts,
            reason=reason,
            pnl=pnl,
            commission=commission,
        )
        await self.listener.on_position_closed(trade)

    def _commission(self, pos: Position) -> Decimal:
        if not self.commission_per_million:
            return Decimal(0)
        base = get_instrument(pos.symbol).base
        notional = Decimal(pos.quantity) * self.prices.rate(base, self.account_currency)
        return 2 * notional * self.commission_per_million / 1_000_000

    # Bougies

    async def on_bar(self, bar: Bar) -> None:
        """Contrôle les stops et objectifs des positions ouvertes avant cette bougie."""
        spread = bar.spread or Decimal(0)
        for pos in [
            p
            for p in self._positions.values()
            if p.symbol == bar.symbol and p.opened_at <= bar.open_time
        ]:
            # Prix auxquels la position se fermerait : bid pour un achat, ask pour une vente.
            off = Decimal(0) if pos.side is Side.BUY else spread
            o, h, low = bar.open + off, bar.high + off, bar.low + off
            s = pos.side.sign
            sl, tp = pos.stop_loss, pos.take_profit
            ts = bar.close_time
            if (o - sl) * s <= 0:
                await self._close(pos, o, ExitReason.STOP_LOSS, bar.open_time)
            elif tp is not None and (o - tp) * s >= 0:
                await self._close(pos, o, ExitReason.TAKE_PROFIT, bar.open_time)
            elif (low if s > 0 else h) * s <= sl * s:
                await self._close(pos, sl, ExitReason.STOP_LOSS, ts)
            elif tp is not None and (h if s > 0 else low) * s >= tp * s:
                await self._close(pos, tp, ExitReason.TAKE_PROFIT, ts)

    # Compte

    def unrealized(self) -> Decimal:
        total = Decimal(0)
        for pos in self._positions.values():
            q = get_instrument(pos.symbol).quote
            total += pos.unrealized_pnl(self.prices.tick(pos.symbol)) * self.prices.rate(
                q, self.account_currency
            )
        return total

    async def account(self) -> Account:
        margin = Decimal(0)
        for pos in self._positions.values():
            base = get_instrument(pos.symbol).base
            margin += pos.quantity * self.prices.rate(base, self.account_currency) / self.leverage
        return Account(
            currency=self.account_currency,
            balance=self.balance,
            equity=self.balance + self.unrealized(),
            margin_used=margin,
            ts=self.clock.now(),
        )
