import lzma
import struct
from datetime import UTC, datetime, timedelta

import polars as pl

from kobr4.marketdata import frames


def bi5(records: list[tuple[int, int, int, int, int, float]]) -> bytes:
    """Fichier de bougies Dukascopy : (secondes, open, close, low, high, volume)."""
    data = b"".join(struct.pack(">5if", *r) for r in records)
    return lzma.compress(data, format=lzma.FORMAT_ALONE)


def m1_frame(start: datetime, closes: list[int], spread: int | None = 2) -> pl.DataFrame:
    """Bougies M1 consécutives ; open = close précédent, high/low à ±3 ticks."""
    rows = []
    prev = closes[0]
    for i, c in enumerate(closes):
        rows.append(
            (start + timedelta(minutes=i), prev, max(prev, c) + 3, min(prev, c) - 3, c, spread, 1.0)
        )
        prev = c
    return pl.DataFrame(rows, schema=frames.SCHEMA, orient="row")


T0 = datetime(2024, 3, 4, 0, 0, tzinfo=UTC)  # lundi
