"""Format tabulaire des bougies, partagé par les sources, le stockage et le backtest.

Les prix sont stockés en entiers : le prix multiplié par 10^précision de l'instrument
(1,08425 sur EUR/USD → 108425). C'est exact, compact et rapide à agréger.

Colonnes d'une table de bougies :

    open_time  Datetime(us, UTC)   début de la bougie
    open, high, low, close  Int64  prix bid, en unités de tick
    spread     Int64 (nullable)    écart ask - bid moyen, en unités de tick
    volume     Float64             volume fourni par la source
"""

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal

import polars as pl

from kobr4.core.instruments import Instrument
from kobr4.core.models import Bar
from kobr4.core.types import Timeframe

SCHEMA: dict[str, pl.DataType] = {
    "open_time": pl.Datetime("us", "UTC"),
    "open": pl.Int64(),
    "high": pl.Int64(),
    "low": pl.Int64(),
    "close": pl.Int64(),
    "spread": pl.Int64(),
    "volume": pl.Float64(),
}
COLUMNS = list(SCHEMA)

_EVERY = {
    Timeframe.M1: "1m",
    Timeframe.M5: "5m",
    Timeframe.M15: "15m",
    Timeframe.M30: "30m",
    Timeframe.H1: "1h",
    Timeframe.H4: "4h",
    Timeframe.D1: "1d",
}


def empty_frame() -> pl.DataFrame:
    return pl.DataFrame(schema=SCHEMA)


def conform(df: pl.DataFrame) -> pl.DataFrame:
    """Vérifie les colonnes, impose les types et l'ordre des colonnes."""
    missing = set(COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"colonnes manquantes : {', '.join(sorted(missing))}")
    return df.select([pl.col(c).cast(t) for c, t in SCHEMA.items()])


def resample(m1: pl.DataFrame, timeframe: Timeframe) -> pl.DataFrame:
    """Agrège des bougies M1 dans une unité de temps plus grande.

    Les périodes sont alignées sur l'heure UTC : H4 commence à 0h, 4h, 8h…, D1 à 0h UTC.
    Le spread d'une bougie est la moyenne des spreads M1, arrondie au tick supérieur
    (hypothèse prudente pour le backtest).
    """
    if timeframe is Timeframe.M1:
        return m1.sort("open_time")
    spread_n = pl.col("spread").count()
    spread_sum = pl.col("spread").sum()
    return (
        m1.sort("open_time")
        .group_by_dynamic(
            "open_time", every=_EVERY[timeframe], closed="left", label="left", start_by="window"
        )
        .agg(
            pl.col("open").first(),
            pl.col("high").max(),
            pl.col("low").min(),
            pl.col("close").last(),
            pl.when(spread_n > 0).then(-((-spread_sum) // spread_n)).alias("spread"),
            pl.col("volume").sum(),
        )
        .select(COLUMNS)
    )


def to_bars(df: pl.DataFrame, instrument: Instrument, timeframe: Timeframe) -> list[Bar]:
    exp = -instrument.price_precision

    def px(v: int) -> Decimal:
        return Decimal(v).scaleb(exp)

    return [
        Bar(
            symbol=instrument.symbol,
            timeframe=timeframe,
            open_time=row["open_time"],
            open=px(row["open"]),
            high=px(row["high"]),
            low=px(row["low"]),
            close=px(row["close"]),
            spread=None if row["spread"] is None else px(row["spread"]),
            volume=Decimal(str(row["volume"])),
        )
        for row in df.iter_rows(named=True)
    ]


def from_bars(bars: Iterable[Bar], instrument: Instrument) -> pl.DataFrame:
    scale = Decimal(10) ** instrument.price_precision

    def ticks(v: Decimal) -> int:
        return int((v * scale).to_integral_value())

    rows: list[tuple[datetime, int, int, int, int, int | None, float]] = [
        (
            b.open_time,
            ticks(b.open),
            ticks(b.high),
            ticks(b.low),
            ticks(b.close),
            None if b.spread is None else ticks(b.spread),
            float(b.volume),
        )
        for b in bars
    ]
    return pl.DataFrame(rows, schema=SCHEMA, orient="row")
