"""Construction des bougies en direct à partir des cotations."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal

from kobr4.core.instruments import Instrument
from kobr4.core.models import Bar, Tick
from kobr4.core.types import Timeframe

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def bucket_start(ts: datetime, timeframe: Timeframe) -> datetime:
    """Début de la période qui contient `ts`, aligné sur l'heure UTC."""
    step = timeframe.duration
    return _EPOCH + ((ts - _EPOCH) // step) * step


@dataclass
class _Pending:
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    spreads: list[Decimal] = field(default_factory=list)


class BarBuilder:
    """Agrège les cotations d'un instrument en bougies bid, pour une unité de temps.

    Une bougie est clôturée quand arrive une cotation de la période suivante, ou quand
    `flush` est appelé avec une heure postérieure à sa fin (marché calme, fin de session).
    Les périodes sans aucune cotation ne produisent pas de bougie.
    """

    def __init__(self, instrument: Instrument, timeframe: Timeframe) -> None:
        self.instrument = instrument
        self.timeframe = timeframe
        self._pending: _Pending | None = None
        self._last_ts: datetime | None = None

    def on_tick(self, tick: Tick) -> list[Bar]:
        if tick.symbol != self.instrument.symbol:
            raise ValueError(
                f"cotation {tick.symbol} reçue par le constructeur {self.instrument.symbol}"
            )
        if self._last_ts is not None and tick.ts < self._last_ts:
            return []  # cotation en retard : ignorée pour ne pas réécrire une bougie close
        self._last_ts = tick.ts
        start = bucket_start(tick.ts, self.timeframe)
        closed = []
        p = self._pending
        if p is not None and start != p.open_time:
            closed.append(self._close(p))
            p = None
        if p is None:
            self._pending = _Pending(start, tick.bid, tick.bid, tick.bid, tick.bid, [tick.spread])
        else:
            p.high = max(p.high, tick.bid)
            p.low = min(p.low, tick.bid)
            p.close = tick.bid
            p.spreads.append(tick.spread)
        return closed

    def flush(self, now: datetime) -> list[Bar]:
        p = self._pending
        if p is None or now < p.open_time + self.timeframe.duration:
            return []
        return [self._close(p)]

    def _close(self, p: _Pending) -> Bar:
        self._pending = None
        spread = (sum(p.spreads, Decimal(0)) / len(p.spreads)).quantize(
            self.instrument.tick_size, rounding=ROUND_CEILING
        )
        return Bar(
            symbol=self.instrument.symbol,
            timeframe=self.timeframe,
            open_time=p.open_time,
            open=p.open,
            high=p.high,
            low=p.low,
            close=p.close,
            spread=spread,
            volume=Decimal(len(p.spreads)),
        )
