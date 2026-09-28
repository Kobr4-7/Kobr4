"""Modèles du domaine : cotations, bougies, intentions d'ordre, exécutions, positions.

Tous les modèles sont immuables. Les prix et montants sont des `Decimal`.
"""

from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kobr4.core.types import OrderType, Side, Timeframe, UtcDatetime

Price = Decimal
Money = Decimal


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Tick(_Frozen):
    """Cotation bid/ask à un instant donné."""

    symbol: str
    bid: Price = Field(gt=0)
    ask: Price = Field(gt=0)
    ts: UtcDatetime

    @model_validator(mode="after")
    def _check_spread(self) -> Self:
        if self.ask < self.bid:
            raise ValueError(f"ask ({self.ask}) inférieur au bid ({self.bid})")
        return self

    @property
    def spread(self) -> Price:
        return self.ask - self.bid

    @property
    def mid(self) -> Price:
        return (self.bid + self.ask) / 2

    def price_for(self, side: Side) -> Price:
        """Prix d'exécution d'un ordre au marché : ask à l'achat, bid à la vente."""
        return self.ask if side is Side.BUY else self.bid


class Bar(_Frozen):
    """Bougie OHLC au prix bid. `open_time` est le début de la période.

    `spread` est l'écart moyen ask - bid sur la période, quand la source le fournit.
    Le prix ask s'en déduit : ask ≈ bid + spread.
    """

    symbol: str
    timeframe: Timeframe
    open_time: UtcDatetime
    open: Price = Field(gt=0)
    high: Price = Field(gt=0)
    low: Price = Field(gt=0)
    close: Price = Field(gt=0)
    spread: Price | None = Field(default=None, ge=0)
    volume: Decimal = Field(default=Decimal(0), ge=0)

    @model_validator(mode="after")
    def _check_range(self) -> Self:
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("high/low incohérents avec open/close")
        return self

    @property
    def close_time(self) -> UtcDatetime:
        return self.open_time + self.timeframe.duration


class OrderIntent(_Frozen):
    """Ce qu'une stratégie veut faire, sans taille.

    La taille est calculée par le gestionnaire de risque. Le stop loss est obligatoire.
    """

    strategy_id: str = Field(min_length=1)
    symbol: str
    side: Side
    order_type: OrderType = OrderType.MARKET
    entry_price: Price | None = Field(default=None, gt=0)
    stop_loss_pips: Decimal = Field(gt=0)
    take_profit_pips: Decimal | None = Field(default=None, gt=0)
    ts: UtcDatetime
    reason: str = ""

    @model_validator(mode="after")
    def _check_entry(self) -> Self:
        if self.order_type is OrderType.MARKET and self.entry_price is not None:
            raise ValueError("un ordre au marché ne prend pas de prix d'entrée")
        if self.order_type is not OrderType.MARKET and self.entry_price is None:
            raise ValueError(f"un ordre {self.order_type} exige un prix d'entrée")
        return self


class Fill(_Frozen):
    """Exécution (totale ou partielle) d'un ordre chez le courtier."""

    fill_id: str
    order_id: str
    symbol: str
    side: Side
    quantity: int = Field(gt=0)
    price: Price = Field(gt=0)
    commission: Money = Decimal(0)
    ts: UtcDatetime


class CloseIntent(_Frozen):
    """Demande d'une stratégie de fermer ses positions sur un instrument.

    Une fermeture n'est jamais bloquée par le gestionnaire de risque.
    """

    strategy_id: str = Field(min_length=1)
    symbol: str
    side: Side | None = None
    """Ne ferme que les positions de ce sens ; toutes si absent."""
    ts: UtcDatetime
    reason: str = ""


class Position(_Frozen):
    """Position ouverte, issue de l'exécution d'un ordre. `quantity` en unités.

    `id` est l'identifiant de l'ordre qui l'a ouverte.
    """

    id: str
    strategy_id: str
    symbol: str
    side: Side
    quantity: int = Field(gt=0)
    entry_price: Price = Field(gt=0)
    stop_loss: Price = Field(gt=0)
    take_profit: Price | None = Field(default=None, gt=0)
    opened_at: UtcDatetime

    @model_validator(mode="after")
    def _check_protection(self) -> Self:
        s = self.side.sign
        if (self.stop_loss - self.entry_price) * s >= 0:
            raise ValueError(
                f"stop loss {self.stop_loss} du mauvais côté pour un {self.side}"
                f" à {self.entry_price}"
            )
        if self.take_profit is not None and (self.take_profit - self.entry_price) * s <= 0:
            raise ValueError(
                f"take profit {self.take_profit} du mauvais côté pour un {self.side}"
                f" à {self.entry_price}"
            )
        return self

    def exit_price(self, tick: Tick) -> Price:
        """Prix de fermeture : bid pour un achat, ask pour une vente."""
        return tick.price_for(self.side.opposite)

    def pnl_quote(self, exit_price: Price) -> Money:
        """P&L en devise de cotation pour une sortie à `exit_price`."""
        return (exit_price - self.entry_price) * self.side.sign * self.quantity

    def unrealized_pnl(self, tick: Tick) -> Money:
        """P&L latent en devise de cotation, valorisé au prix de fermeture."""
        return self.pnl_quote(self.exit_price(tick))

    def risk_quote(self) -> Money:
        """Perte en devise de cotation si le stop loss est touché."""
        return abs(self.entry_price - self.stop_loss) * self.quantity


class ExitReason(StrEnum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    SIGNAL = "signal"
    MANUAL = "manual"
    KILL_SWITCH = "kill_switch"
    END_OF_TEST = "end_of_test"


class ClosedTrade(_Frozen):
    """Position fermée, avec son résultat en devise du compte."""

    position_id: str
    strategy_id: str
    symbol: str
    side: Side
    quantity: int = Field(gt=0)
    entry_price: Price
    exit_price: Price
    opened_at: UtcDatetime
    closed_at: UtcDatetime
    reason: ExitReason
    pnl: Money
    """Résultat net en devise du compte, commissions et financement déduits."""
    commission: Money = Decimal(0)
    financing: Money = Decimal(0)


class Account(_Frozen):
    """Instantané du compte chez le courtier."""

    currency: str = Field(min_length=3, max_length=3)
    balance: Money
    equity: Money
    margin_used: Money = Field(default=Decimal(0), ge=0)
    ts: UtcDatetime

    @property
    def margin_free(self) -> Money:
        return self.equity - self.margin_used
