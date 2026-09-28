"""Indicateurs techniques incrémentaux : une valeur à la fois, sans recalcul.

Calculés en flottants : ils servent à décider, pas à compter de l'argent.
Chaque indicateur renvoie `None` tant qu'il n'a pas assez d'historique.
"""

from collections import deque


class EMA:
    """Moyenne mobile exponentielle, initialisée par une moyenne simple."""

    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("période >= 1 attendue")
        self.period = period
        self._k = 2 / (period + 1)
        self._seed: list[float] = []
        self.value: float | None = None

    def update(self, x: float) -> float | None:
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) == self.period:
                self.value = sum(self._seed) / self.period
            return self.value
        self.value = x * self._k + self.value * (1 - self._k)
        return self.value


class _Wilder:
    """Lissage de Wilder (RMA)."""

    def __init__(self, period: int) -> None:
        self.period = period
        self._seed: list[float] = []
        self.value: float | None = None

    def update(self, x: float) -> float | None:
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) == self.period:
                self.value = sum(self._seed) / self.period
            return self.value
        self.value = (self.value * (self.period - 1) + x) / self.period
        return self.value


class RSI:
    def __init__(self, period: int = 14) -> None:
        self._gain = _Wilder(period)
        self._loss = _Wilder(period)
        self._prev: float | None = None
        self.value: float | None = None

    def update(self, close: float) -> float | None:
        if self._prev is None:
            self._prev = close
            return None
        change = close - self._prev
        self._prev = close
        g = self._gain.update(max(change, 0.0))
        loss = self._loss.update(max(-change, 0.0))
        if g is None or loss is None:
            return None
        self.value = 100.0 if loss == 0 else 100 - 100 / (1 + g / loss)
        return self.value


class ATR:
    def __init__(self, period: int = 14) -> None:
        self._rma = _Wilder(period)
        self._prev_close: float | None = None
        self.value: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        tr = high - low
        if self._prev_close is not None:
            tr = max(tr, abs(high - self._prev_close), abs(low - self._prev_close))
        self._prev_close = close
        self.value = self._rma.update(tr)
        return self.value


class ADX:
    """Force de la tendance (0-100), indépendamment de son sens."""

    def __init__(self, period: int = 14) -> None:
        self._tr = _Wilder(period)
        self._pdm = _Wilder(period)
        self._ndm = _Wilder(period)
        self._dx = _Wilder(period)
        self._prev: tuple[float, float, float] | None = None
        self.value: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        if self._prev is None:
            self._prev = (high, low, close)
            return None
        ph, pl, pc = self._prev
        self._prev = (high, low, close)
        up, down = high - ph, pl - low
        pdm = up if up > down and up > 0 else 0.0
        ndm = down if down > up and down > 0 else 0.0
        tr = max(high - low, abs(high - pc), abs(low - pc))
        atr, p, n = self._tr.update(tr), self._pdm.update(pdm), self._ndm.update(ndm)
        if atr is None or p is None or n is None or atr == 0:
            return None
        pdi, ndi = 100 * p / atr, 100 * n / atr
        dx = 0.0 if pdi + ndi == 0 else 100 * abs(pdi - ndi) / (pdi + ndi)
        self.value = self._dx.update(dx)
        return self.value


class Donchian:
    """Plus haut et plus bas des `period` bougies précédentes (bougie courante exclue)."""

    def __init__(self, period: int = 20) -> None:
        self.period = period
        self._highs: deque[float] = deque(maxlen=period)
        self._lows: deque[float] = deque(maxlen=period)
        self.upper: float | None = None
        self.lower: float | None = None

    def update(self, high: float, low: float) -> tuple[float, float] | None:
        """Renvoie le canal *avant* d'intégrer la bougie courante."""
        channel = None
        if len(self._highs) == self.period:
            channel = (max(self._highs), min(self._lows))
        self._highs.append(high)
        self._lows.append(low)
        self.upper, self.lower = channel if channel else (None, None)
        return channel
