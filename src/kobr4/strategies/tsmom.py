"""Momentum temporel (TSMOM) : suit le sens des rendements passés sur plusieurs horizons.

Le rendement passé d'un actif prédit en partie son rendement futur (Moskowitz, Ooi &
Pedersen 2012 ; Hurst, Ooi & Pedersen 2017). C'est la stratégie de tendance qui a le
mieux tenu dans l'étude d'Aurum (dépôt zero-was-here/tradingbot, licence MIT) sur l'or
en H1 de 2013 à 2026, frais compris : proche de zéro sur 2016-2024, nettement positive
sur 2025-2026. Ce n'est pas une rente : elle gagne dans les marchés en tendance et
patiente (ou perd un peu) dans les marchés sans direction.

Calcul, à chaque clôture :
1. volatilité par bougie σ : moyenne exponentielle des carrés des rendements logarithmiques ;
2. pour chaque horizon h : z_h = ln(C_t / C_{t-h}) / (σ √h) ;
3. prévision = moyenne des z_h × 0,6267 × 1,253, bornée à [-1, 1] (l'échelle de Carver :
   une prévision moyenne vaut 0,5 en valeur absolue).

Entrée à l'achat quand la prévision dépasse `entry`, à la vente sous `-entry` ; sortie
quand elle change de signe. Le stop vaut `atr_stop` × ATR, sans objectif par défaut : on
laisse courir les tendances.
"""

import math
from collections import deque
from decimal import Decimal
from typing import ClassVar, Self

from pydantic import Field, model_validator

from kobr4.config.settings import StrategySettings
from kobr4.core.instruments import get_instrument
from kobr4.core.models import Bar, CloseIntent, OrderIntent
from kobr4.core.types import Side
from kobr4.strategies.base import BaseParams, Intent, ParamRange, Strategy, StrategyContext
from kobr4.strategies.indicators import ATR

FORECAST_SCALAR = 0.6267
"""0,5 / E|z| pour une loi normale : la prévision moyenne vaut 0,5 en valeur absolue."""
DIVERSIFICATION = 1.253
"""Multiplicateur de diversification des trois horizons par défaut (Aurum)."""


class TsmomParams(BaseParams):
    take_profit_pips: Decimal | None = Field(default=None, gt=0)
    short: int = Field(default=120, ge=5, le=5000)
    medium: int = Field(default=480, ge=10, le=5000)
    long: int = Field(default=1440, ge=20, le=5000)
    """Horizons en bougies (en H1 : environ une semaine, un mois, trois mois)."""
    vol_halflife: float = Field(default=240, ge=5, le=5000)
    entry: float = Field(default=0.3, ge=0.05, le=0.95)
    """Seuil de prévision pour ouvrir une position."""
    atr_period: int = Field(default=48, ge=5, le=500)
    atr_stop: float = Field(default=3.0, ge=0.5, le=10)
    """Stop loss en multiples de l'ATR."""

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.short < self.medium < self.long:
            raise ValueError("les horizons doivent être croissants : court < moyen < long")
        return self


class Tsmom(Strategy):
    kind = "tsmom"
    Params = TsmomParams
    search_space: ClassVar[dict[str, ParamRange]] = {
        "entry": ParamRange(0.1, 0.6, step=0.05),
        "atr_stop": ParamRange(1.5, 6, step=0.5),
        "short": ParamRange(24, 240, integer=True),
    }
    params: TsmomParams

    def __init__(self, settings: StrategySettings) -> None:
        super().__init__(settings)
        p = self.params
        self._closes = {s: deque[float](maxlen=p.long + 1) for s in self.instruments}
        self._var: dict[str, float | None] = dict.fromkeys(self.instruments)
        self._count = dict.fromkeys(self.instruments, 0)
        self._atr = {s: ATR(p.atr_period) for s in self.instruments}
        self._alpha = 1 - 0.5 ** (1 / p.vol_halflife)
        self.forecast: dict[str, float | None] = dict.fromkeys(self.instruments)

    @property
    def warmup_bars(self) -> int:
        return self.params.long + 1

    def _update(self, bar: Bar) -> float | None:
        closes = self._closes[bar.symbol]
        close = float(bar.close)
        if closes:
            r = math.log(close / closes[-1])
            var = self._var[bar.symbol]
            self._var[bar.symbol] = r * r if var is None else var + self._alpha * (r * r - var)
            self._count[bar.symbol] += 1
        closes.append(close)
        var = self._var[bar.symbol]
        if len(closes) <= self.params.long or not var or self._count[bar.symbol] < 20:
            return None
        sigma = math.sqrt(var)
        zs = [
            math.log(close / closes[-1 - h]) / (sigma * math.sqrt(h))
            for h in (self.params.short, self.params.medium, self.params.long)
        ]
        f = sum(zs) / len(zs) * FORECAST_SCALAR * DIVERSIFICATION
        return max(-1.0, min(1.0, f))

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[Intent]:
        atr = self._atr[bar.symbol].update(float(bar.high), float(bar.low), float(bar.close))
        f = self._update(bar)
        self.forecast[bar.symbol] = f
        if f is None or atr is None:
            return []
        positions = ctx.positions(self.id, bar.symbol)
        entry = self.params.entry
        if f >= entry:
            intents = self.reverse_to(Side.BUY, bar, ctx, f"momentum haussier ({f:+.2f})")
        elif f <= -entry:
            intents = self.reverse_to(Side.SELL, bar, ctx, f"momentum baissier ({f:+.2f})")
        else:
            # Entre les seuils : on sort seulement si le momentum a changé de signe.
            against = Side.SELL if f > 0 else Side.BUY
            if f != 0 and any(p.side is against for p in positions):
                return [
                    CloseIntent(
                        strategy_id=self.id,
                        symbol=bar.symbol,
                        side=against,
                        ts=bar.close_time,
                        reason=f"momentum retourné ({f:+.2f})",
                    )
                ]
            return []
        pip = float(get_instrument(bar.symbol).pip_size)
        sl = Decimal(str(round(max(atr * self.params.atr_stop / pip, 1.0), 1)))
        return [
            i.model_copy(update={"stop_loss_pips": sl}) if isinstance(i, OrderIntent) else i
            for i in intents
        ]
