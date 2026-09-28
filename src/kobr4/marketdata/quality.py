"""Contrôle qualité de l'historique : trous, doublons, bougies incohérentes."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import polars as pl

from kobr4.marketdata import frames


@dataclass(frozen=True)
class Gap:
    start: datetime
    end: datetime

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


@dataclass(frozen=True)
class QualityReport:
    rows: int
    duplicates: int
    invalid_bars: int
    gaps: list[Gap] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.duplicates == 0 and self.invalid_bars == 0 and not self.gaps


def _spans_weekend(start: datetime, end: datetime) -> bool:
    """Le marché forex ferme du vendredi soir au dimanche soir (UTC) : un trou qui
    contient un samedi est normal."""
    d = start.date()
    while d <= end.date():
        if d.weekday() == 5:
            return True
        d += timedelta(days=1)
    return False


def check_m1(df: pl.DataFrame, max_gap: timedelta = timedelta(minutes=30)) -> QualityReport:
    """Analyse des bougies M1.

    Signale les trous plus longs que `max_gap` en dehors des week-ends. Des trous courts
    sont normaux (minutes sans cotation, fin de journée à 22h UTC).
    """
    df = frames.conform(df).sort("open_time")
    duplicates = df.height - df.get_column("open_time").n_unique()
    invalid = df.filter(
        (pl.col("high") < pl.max_horizontal("open", "close"))
        | (pl.col("low") > pl.min_horizontal("open", "close"))
        | (pl.col("low") <= 0)
        | (pl.col("spread") < 0)
    ).height
    step = pl.duration(minutes=1)
    holes = (
        df.select(
            pl.col("open_time").alias("prev"),
            pl.col("open_time").shift(-1).alias("next"),
        )
        .filter(pl.col("next") - pl.col("prev") > step + pl.lit(max_gap))
        .iter_rows()
    )
    gaps = [
        Gap(prev + timedelta(minutes=1), nxt)
        for prev, nxt in holes
        if not _spans_weekend(prev, nxt)
    ]
    return QualityReport(rows=df.height, duplicates=duplicates, invalid_bars=invalid, gaps=gaps)
