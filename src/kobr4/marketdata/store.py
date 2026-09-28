"""Stockage de l'historique des bougies en fichiers Parquet.

Seules les bougies M1 sont stockées ; les autres unités de temps sont calculées à la
lecture. Organisation : `<racine>/<EURUSD>/M1/<année>.parquet`.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import polars as pl

from kobr4.core.instruments import get_instrument
from kobr4.core.models import Bar
from kobr4.core.types import Timeframe
from kobr4.marketdata import frames


@dataclass(frozen=True)
class Coverage:
    symbol: str
    first: datetime
    last: datetime
    rows: int


class ParquetBarStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _dir(self, symbol: str) -> Path:
        get_instrument(symbol)
        return self.root / symbol.replace("/", "") / "M1"

    def _files(self, symbol: str) -> list[Path]:
        d = self._dir(symbol)
        return sorted(d.glob("*.parquet")) if d.exists() else []

    def symbols(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(
            f"{p.name[:3]}/{p.name[3:]}"
            for p in self.root.iterdir()
            if (p / "M1").is_dir() and any((p / "M1").glob("*.parquet"))
        )

    def write_m1(self, symbol: str, df: pl.DataFrame) -> int:
        """Ajoute des bougies M1. Une bougie déjà présente est remplacée. Renvoie le nombre
        de lignes écrites."""
        df = frames.conform(df)
        if df.is_empty():
            return 0
        d = self._dir(symbol)
        d.mkdir(parents=True, exist_ok=True)
        for (year,), part in df.group_by(pl.col("open_time").dt.year()):
            path = d / f"{year}.parquet"
            if path.exists():
                part = pl.concat([pl.read_parquet(path), part])
            part = part.unique("open_time", keep="last").sort("open_time")
            tmp = path.with_suffix(".tmp")
            part.write_parquet(tmp, compression="zstd")
            tmp.replace(path)
        return df.height

    def read_m1(
        self, symbol: str, start: datetime | None = None, end: datetime | None = None
    ) -> pl.DataFrame:
        """Bougies M1 avec `start <= open_time < end`."""
        files = self._files(symbol)
        if not files:
            return frames.empty_frame()
        lf = pl.scan_parquet(files)
        if start is not None:
            lf = lf.filter(pl.col("open_time") >= start)
        if end is not None:
            lf = lf.filter(pl.col("open_time") < end)
        return frames.conform(lf.sort("open_time").collect())

    def read(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> pl.DataFrame:
        return frames.resample(self.read_m1(symbol, start, end), timeframe)

    def bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Bar]:
        return frames.to_bars(
            self.read(symbol, timeframe, start, end), get_instrument(symbol), timeframe
        )

    def coverage(self, symbol: str) -> Coverage | None:
        files = self._files(symbol)
        if not files:
            return None
        stats = (
            pl.scan_parquet(files)
            .select(
                pl.col("open_time").min().alias("first"),
                pl.col("open_time").max().alias("last"),
                pl.len().alias("rows"),
            )
            .collect()
            .row(0, named=True)
        )
        return Coverage(symbol, stats["first"], stats["last"], stats["rows"])

    def days_present(self, symbol: str, start: date, end: date) -> set[date]:
        """Jours (UTC) de l'intervalle [start, end] pour lesquels au moins une bougie existe."""
        df = self.read_m1(
            symbol,
            datetime.combine(start, time(), UTC),
            datetime.combine(end + timedelta(days=1), time(), UTC),
        )
        return set(df.get_column("open_time").dt.date().unique().to_list())
