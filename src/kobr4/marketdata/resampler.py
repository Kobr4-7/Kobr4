"""Agrégation en continu de bougies courtes en bougies plus longues."""

from decimal import ROUND_CEILING, Decimal

from kobr4.core.instruments import get_instrument
from kobr4.core.models import Bar
from kobr4.core.types import Timeframe
from kobr4.marketdata.bar_builder import bucket_start


class BarResampler:
    """Reçoit les bougies d'un instrument dans une unité courte (M1, M15…) et produit les
    bougies d'une unité plus longue dès que la période est complète."""

    def __init__(self, symbol: str, timeframe: Timeframe) -> None:
        self.symbol = symbol
        self.timeframe = timeframe
        self._tick = get_instrument(symbol).tick_size
        self._bars: list[Bar] = []

    def on_bar(self, bar: Bar) -> list[Bar]:
        if bar.timeframe.duration >= self.timeframe.duration:
            raise ValueError("unité source plus longue que l'unité cible")
        out = []
        start = bucket_start(bar.open_time, self.timeframe)
        if self._bars and bucket_start(self._bars[0].open_time, self.timeframe) != start:
            out.append(self._emit())
        self._bars.append(bar)
        if bar.close_time >= start + self.timeframe.duration:
            out.append(self._emit())
        return out

    def flush(self) -> list[Bar]:
        return [self._emit()] if self._bars else []

    def _emit(self) -> Bar:
        bars, self._bars = self._bars, []
        spreads = [b.spread for b in bars if b.spread is not None]
        spread = (
            (sum(spreads, Decimal(0)) / len(spreads)).quantize(self._tick, rounding=ROUND_CEILING)
            if spreads
            else None
        )
        return Bar(
            symbol=self.symbol,
            timeframe=self.timeframe,
            open_time=bucket_start(bars[0].open_time, self.timeframe),
            open=bars[0].open,
            high=max(b.high for b in bars),
            low=min(b.low for b in bars),
            close=bars[-1].close,
            spread=spread,
            volume=sum((b.volume for b in bars), Decimal(0)),
        )
