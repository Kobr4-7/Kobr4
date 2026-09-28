"""Spread habituel de chaque instrument, pour refuser les entrées quand il s'élargit."""

import statistics
from collections import deque
from decimal import Decimal


class SpreadMonitor:
    """Médiane glissante des spreads observés."""

    def __init__(self, window: int = 500, min_samples: int = 20) -> None:
        self.window = window
        self.min_samples = min_samples
        self._samples: dict[str, deque[Decimal]] = {}

    def observe(self, symbol: str, spread: Decimal) -> None:
        self._samples.setdefault(symbol, deque(maxlen=self.window)).append(spread)

    def typical(self, symbol: str) -> Decimal | None:
        """Spread médian, ou `None` tant qu'il n'y a pas assez d'observations."""
        s = self._samples.get(symbol)
        if s is None or len(s) < self.min_samples:
            return None
        return statistics.median(s)
