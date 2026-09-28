"""Bougies des graphiques du site : l'historique stocké, complété par les bougies
construites en direct par le bot (sans aucun téléchargement pendant la requête)."""

from kobr4.core.models import Bar
from kobr4.core.types import Timeframe
from kobr4.marketdata.bar_builder import bucket_start


def aggregate(m1: list[Bar], timeframe: Timeframe) -> list[Bar]:
    """Regroupe des bougies M1 dans l'unité de temps demandée."""
    out: dict[object, Bar] = {}
    for b in m1:
        start = bucket_start(b.open_time, timeframe)
        prev = out.get(start)
        if prev is None:
            out[start] = b.model_copy(update={"open_time": start, "timeframe": timeframe})
        else:
            out[start] = prev.model_copy(
                update={
                    "high": max(prev.high, b.high),
                    "low": min(prev.low, b.low),
                    "close": b.close,
                    "volume": prev.volume + b.volume,
                }
            )
    return list(out.values())


def merge(stored: list[Bar], live: list[Bar], count: int) -> list[Bar]:
    """Fusionne l'historique et le direct ; une période présente des deux côtés garde
    l'ouverture stockée et prolonge plus haut, plus bas et clôture avec le direct."""
    by_time = {b.open_time: b for b in stored}
    for b in live:
        prev = by_time.get(b.open_time)
        if prev is None:
            by_time[b.open_time] = b
        else:
            by_time[b.open_time] = prev.model_copy(
                update={
                    "high": max(prev.high, b.high),
                    "low": min(prev.low, b.low),
                    "close": b.close,
                }
            )
    return [by_time[t] for t in sorted(by_time)][-count:]
