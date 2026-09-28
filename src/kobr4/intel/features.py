"""Caractéristiques de marché calculées en continu, identiques en backtest et en réel.

Pour chaque instrument et unité de temps : tendance (ADX), volatilité (ATR et son rang
sur les 200 dernières bougies), RSI, pente de la moyenne mobile, heure et jour. Elles
servent à classer le marché en régimes et à nourrir le filtre ML.
"""

from bisect import bisect_left, insort
from collections import deque

from kobr4.core.models import Bar
from kobr4.core.types import Regime, Timeframe
from kobr4.strategies.indicators import ADX, ATR, EMA, RSI

REGIME_LABELS = {
    Regime.TREND: "Tendance",
    Regime.RANGE: "Range",
    Regime.VOLATILE: "Forte volatilité",
    Regime.UNKNOWN: "Indéterminé",
}

TREND_ADX = 25.0
VOLATILE_RANK = 0.8

FEATURES = (
    "adx",
    "atr_pips",
    "atr_rank",
    "rsi",
    "ema_slope",
    "ret_5",
    "ret_20",
    "hour",
    "weekday",
    "spread_atr",
)


class _Rank:
    """Rang d'une valeur parmi les `size` dernières (0 à 1)."""

    def __init__(self, size: int = 200) -> None:
        self.window: deque[float] = deque(maxlen=size)
        self.sorted: list[float] = []

    def update(self, x: float) -> float | None:
        if len(self.window) == self.window.maxlen:
            old = self.window[0]
            del self.sorted[bisect_left(self.sorted, old)]
        self.window.append(x)
        insort(self.sorted, x)
        if len(self.sorted) < 50:
            return None
        return bisect_left(self.sorted, x) / (len(self.sorted) - 1)


class _SymbolFeatures:
    def __init__(self, pip: float) -> None:
        self.pip = pip
        self.adx = ADX(14)
        self.atr = ATR(14)
        self.rsi = RSI(14)
        self.ema = EMA(50)
        self.rank = _Rank(200)
        self.closes: deque[float] = deque(maxlen=21)
        self.prev_ema: float | None = None
        self.values: dict[str, float] | None = None

    def update(self, bar: Bar) -> None:
        h, lo, c = float(bar.high), float(bar.low), float(bar.close)
        adx = self.adx.update(h, lo, c)
        atr = self.atr.update(h, lo, c)
        rsi = self.rsi.update(c)
        ema = self.ema.update(c)
        rank = self.rank.update(atr) if atr is not None else None
        self.closes.append(c)
        slope = None
        if ema is not None and self.prev_ema is not None and atr:
            slope = (ema - self.prev_ema) / atr
        self.prev_ema = ema
        if adx is None or atr is None or rsi is None or rank is None or slope is None:
            self.values = None
            return
        if len(self.closes) < 21:
            self.values = None
            return
        spread = float(bar.spread) if bar.spread is not None else 0.0
        t = bar.close_time
        self.values = {
            "adx": adx,
            "atr_pips": atr / self.pip,
            "atr_rank": rank,
            "rsi": rsi,
            "ema_slope": slope,
            "ret_5": (c / self.closes[-6] - 1) * 1e4,
            "ret_20": (c / self.closes[0] - 1) * 1e4,
            "hour": float(t.hour),
            "weekday": float(t.weekday()),
            "spread_atr": spread / atr if atr else 0.0,
        }


class FeatureTracker:
    def __init__(self) -> None:
        self._by: dict[tuple[str, Timeframe], _SymbolFeatures] = {}

    def update(self, bar: Bar) -> None:
        key = (bar.symbol, bar.timeframe)
        st = self._by.get(key)
        if st is None:
            pip = 0.01 if bar.symbol.endswith("JPY") else 0.0001
            st = self._by[key] = _SymbolFeatures(pip)
        st.update(bar)

    def features(self, symbol: str, timeframe: Timeframe) -> dict[str, float] | None:
        st = self._by.get((symbol, timeframe))
        return dict(st.values) if st and st.values else None

    def regime(self, symbol: str, timeframe: Timeframe) -> Regime:
        f = self.features(symbol, timeframe)
        return classify(f) if f else Regime.UNKNOWN


def classify(f: dict[str, float]) -> Regime:
    if f["adx"] >= TREND_ADX:
        return Regime.TREND
    if f["atr_rank"] >= VOLATILE_RANK:
        return Regime.VOLATILE
    return Regime.RANGE
