"""Import des fichiers M1 de HistData.com (format « Generic ASCII »).

Une ligne par minute : `20240102 170000;1.104270;1.104290;1.104250;1.104290;0`.
Les heures sont en EST fixe (UTC-5, sans heure d'été). Les prix sont des bid ; HistData
ne fournit pas de spread.

Les fichiers se téléchargent à la main sur https://www.histdata.com (formulaire).
"""

from pathlib import Path

import polars as pl

from kobr4.core.instruments import get_instrument
from kobr4.marketdata import frames


def read_histdata(path: str | Path, symbol: str) -> pl.DataFrame:
    scale = 10 ** get_instrument(symbol).price_precision
    raw = pl.read_csv(
        path,
        separator=";",
        has_header=False,
        new_columns=["ts", "open", "high", "low", "close", "volume"],
        schema_overrides={"ts": pl.String},
    )

    def px(c: str) -> pl.Expr:
        expr: pl.Expr = (pl.col(c).cast(pl.Float64) * scale).round(0).cast(pl.Int64)
        return expr

    df = raw.select(
        pl.col("ts")
        .str.strptime(pl.Datetime("us"), "%Y%m%d %H%M%S")
        .dt.replace_time_zone("Etc/GMT+5")
        .dt.convert_time_zone("UTC")
        .alias("open_time"),
        px("open").alias("open"),
        px("high").alias("high"),
        px("low").alias("low"),
        px("close").alias("close"),
        pl.lit(None, pl.Int64).alias("spread"),
        pl.col("volume").cast(pl.Float64),
    )
    return frames.conform(df)
