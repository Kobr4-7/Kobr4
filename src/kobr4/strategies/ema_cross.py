"""Suivi de tendance : croisement de deux moyennes mobiles exponentielles."""

from typing import ClassVar, Self

from pydantic import Field, model_validator

from kobr4.config.settings import StrategySettings
from kobr4.core.models import Bar
from kobr4.core.types import Side
from kobr4.strategies.base import BaseParams, Intent, ParamRange, Strategy, StrategyContext
from kobr4.strategies.indicators import EMA


class EmaCrossParams(BaseParams):
    fast: int = Field(default=20, ge=2, le=200)
    slow: int = Field(default=50, ge=3, le=400)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.fast >= self.slow:
            raise ValueError("la moyenne rapide doit être plus courte que la lente")
        return self


class EmaCross(Strategy):
    """Achète quand l'EMA rapide passe au-dessus de la lente, vend dans le cas inverse.
    Un croisement inverse ferme la position en cours."""

    kind = "ema_cross"
    Params = EmaCrossParams
    search_space: ClassVar[dict[str, ParamRange]] = {
        "fast": ParamRange(5, 50, integer=True),
        "slow": ParamRange(20, 200, integer=True),
        "stop_loss_pips": ParamRange(10, 80, step=5),
        "take_profit_pips": ParamRange(10, 160, step=10),
    }
    params: EmaCrossParams

    def __init__(self, settings: StrategySettings) -> None:
        super().__init__(settings)
        self._fast = {s: EMA(self.params.fast) for s in self.instruments}
        self._slow = {s: EMA(self.params.slow) for s in self.instruments}
        self._prev_diff: dict[str, float | None] = dict.fromkeys(self.instruments)

    @property
    def warmup_bars(self) -> int:
        return self.params.slow + 1

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[Intent]:
        close = float(bar.close)
        fast = self._fast[bar.symbol].update(close)
        slow = self._slow[bar.symbol].update(close)
        if fast is None or slow is None:
            return []
        diff, prev = fast - slow, self._prev_diff[bar.symbol]
        self._prev_diff[bar.symbol] = diff
        if prev is None:
            return []
        if prev <= 0 < diff:
            return self.reverse_to(
                Side.BUY, bar, ctx, f"EMA {self.params.fast} > EMA {self.params.slow}"
            )
        if prev >= 0 > diff:
            return self.reverse_to(
                Side.SELL, bar, ctx, f"EMA {self.params.fast} < EMA {self.params.slow}"
            )
        return []
