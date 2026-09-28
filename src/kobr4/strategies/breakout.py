"""Cassure de range : entrée quand le prix sort du plus haut / plus bas des N dernières
bougies (canal de Donchian)."""

from decimal import Decimal
from typing import ClassVar

from pydantic import Field

from kobr4.config.settings import StrategySettings
from kobr4.core.instruments import get_instrument
from kobr4.core.models import Bar
from kobr4.core.types import Side
from kobr4.strategies.base import BaseParams, Intent, ParamRange, Strategy, StrategyContext
from kobr4.strategies.indicators import ATR, Donchian


class BreakoutParams(BaseParams):
    lookback: int = Field(default=20, ge=5, le=200)
    atr_stop: float = Field(default=0, ge=0, le=10)
    """Si > 0, le stop loss vaut atr_stop × ATR(14) au lieu de stop_loss_pips."""


class Breakout(Strategy):
    kind = "breakout"
    Params = BreakoutParams
    search_space: ClassVar[dict[str, ParamRange]] = {
        "lookback": ParamRange(10, 100, integer=True),
        "atr_stop": ParamRange(0.5, 4, step=0.25),
        "take_profit_pips": ParamRange(20, 200, step=10),
    }
    params: BreakoutParams

    def __init__(self, settings: StrategySettings) -> None:
        super().__init__(settings)
        self._channel = {s: Donchian(self.params.lookback) for s in self.instruments}
        self._atr = {s: ATR(14) for s in self.instruments}

    @property
    def warmup_bars(self) -> int:
        return max(self.params.lookback, 14) + 1

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[Intent]:
        channel = self._channel[bar.symbol].update(float(bar.high), float(bar.low))
        atr = self._atr[bar.symbol].update(float(bar.high), float(bar.low), float(bar.close))
        if channel is None:
            return []
        upper, lower = channel
        close = float(bar.close)
        if close > upper:
            side, reason = Side.BUY, f"cassure du plus haut {self.params.lookback} bougies"
        elif close < lower:
            side, reason = Side.SELL, f"cassure du plus bas {self.params.lookback} bougies"
        else:
            return []
        intents = self.reverse_to(side, bar, ctx, reason)
        if self.params.atr_stop > 0 and atr is not None:
            pip = float(get_instrument(bar.symbol).pip_size)
            sl = Decimal(str(round(max(atr * self.params.atr_stop / pip, 1.0), 1)))
            intents = [
                i.model_copy(update={"stop_loss_pips": sl})
                if i.__class__.__name__ == "OrderIntent"
                else i
                for i in intents
            ]
        return intents
