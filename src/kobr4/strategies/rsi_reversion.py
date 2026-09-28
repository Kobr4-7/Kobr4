"""Retour à la moyenne : achat en survente, vente en surachat, hors tendance forte."""

from typing import ClassVar, Self

from pydantic import Field, model_validator

from kobr4.config.settings import StrategySettings
from kobr4.core.models import Bar, CloseIntent
from kobr4.core.types import Side
from kobr4.strategies.base import BaseParams, Intent, ParamRange, Strategy, StrategyContext
from kobr4.strategies.indicators import ADX, RSI
from kobr4.strategies.sessions import in_sessions


class RsiReversionParams(BaseParams):
    period: int = Field(default=14, ge=2, le=50)
    oversold: float = Field(default=30, gt=0, lt=50)
    overbought: float = Field(default=70, gt=50, lt=100)
    adx_max: float = Field(default=25, ge=0, le=100)
    """Pas d'entrée si l'ADX dépasse ce seuil (tendance trop forte). 0 : filtre désactivé."""
    exit_at_mid: bool = True
    """Ferme la position quand le RSI revient à 50."""

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.oversold >= self.overbought:
            raise ValueError("le seuil de survente doit être sous le seuil de surachat")
        return self


class RsiReversion(Strategy):
    """Entre quand le RSI ressort d'une zone extrême : au-dessus de `oversold` (achat) ou
    sous `overbought` (vente)."""

    kind = "rsi_reversion"
    Params = RsiReversionParams
    search_space: ClassVar[dict[str, ParamRange]] = {
        "period": ParamRange(5, 30, integer=True),
        "oversold": ParamRange(15, 40, step=1),
        "overbought": ParamRange(60, 85, step=1),
        "adx_max": ParamRange(15, 40, step=1),
        "stop_loss_pips": ParamRange(10, 80, step=5),
        "take_profit_pips": ParamRange(10, 160, step=10),
    }
    params: RsiReversionParams

    def __init__(self, settings: StrategySettings) -> None:
        super().__init__(settings)
        self._rsi = {s: RSI(self.params.period) for s in self.instruments}
        self._adx = {s: ADX(14) for s in self.instruments}
        self._prev: dict[str, float | None] = dict.fromkeys(self.instruments)

    @property
    def warmup_bars(self) -> int:
        return max(self.params.period, 28) + 2

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[Intent]:
        p = self.params
        rsi = self._rsi[bar.symbol].update(float(bar.close))
        adx = self._adx[bar.symbol].update(float(bar.high), float(bar.low), float(bar.close))
        prev, self._prev[bar.symbol] = self._prev[bar.symbol], rsi
        if rsi is None or prev is None:
            return []

        intents: list[Intent] = []
        positions = ctx.positions(self.id, bar.symbol)
        if p.exit_at_mid:
            for side, crossed in (
                (Side.BUY, prev < 50 <= rsi),
                (Side.SELL, prev > 50 >= rsi),
            ):
                if crossed and any(pos.side is side for pos in positions):
                    intents.append(
                        CloseIntent(
                            strategy_id=self.id,
                            symbol=bar.symbol,
                            side=side,
                            ts=bar.close_time,
                            reason="RSI revenu à 50",
                        )
                    )

        trend_ok = p.adx_max == 0 or (adx is not None and adx <= p.adx_max)
        if not trend_ok or positions or not in_sessions(bar.close_time, p.sessions):
            return intents
        if prev < p.oversold <= rsi:
            intents.append(self.open(Side.BUY, bar, f"RSI sort de la survente ({rsi:.0f})"))
        elif prev > p.overbought >= rsi:
            intents.append(self.open(Side.SELL, bar, f"RSI sort du surachat ({rsi:.0f})"))
        return intents
