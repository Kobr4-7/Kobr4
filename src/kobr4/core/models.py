"""Modèles du domaine : cotations, bougies, intentions d'ordre, exécutions, positions.

Tous les modèles sont immuables. Les prix et montants sont des `Decimal`.
"""

from decimal import Decimal
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


class Position(_Frozen):
    """Position ouverte d'une stratégie sur un instrument. `quantity` en unités."""

    strategy_id: str
    symbol: str
    side: Side
    quantity: int = Field(gt=0)
    avg_price: Price = Field(gt=0)
    stop_loss: Price | None = None
    take_profit: Price | None = None
    opened_at: UtcDatetime

    def unrealized_pnl(self, tick: Tick) -> Money:
        """P&L latent en devise de cotation, valorisé au prix de clôture (bid ou ask)."""
        exit_price = tick.price_for(self.side.opposite)
        return (exit_price - self.avg_price) * self.side.sign * self.quantity


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
