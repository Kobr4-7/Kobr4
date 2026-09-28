from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl

from kobr4.core.types import Timeframe
from kobr4.marketdata.store import ParquetBarStore
from tests.marketdata.helpers import T0, m1_frame


def test_write_read_roundtrip(tmp_path: Path) -> None:
    store = ParquetBarStore(tmp_path)
    m1 = m1_frame(T0, list(range(108400, 108520)))
    assert store.write_m1("EUR/USD", m1) == 120
    assert store.read_m1("EUR/USD").equals(m1)
    assert store.read("EUR/USD", Timeframe.H1).height == 2
    bars = store.bars("EUR/USD", Timeframe.H1)
    assert bars[0].symbol == "EUR/USD"
    assert bars[0].timeframe is Timeframe.H1


def test_rewrite_replaces_existing_bars(tmp_path: Path) -> None:
    store = ParquetBarStore(tmp_path)
    store.write_m1("EUR/USD", m1_frame(T0, [108400, 108401, 108402]))
    store.write_m1("EUR/USD", m1_frame(T0 + timedelta(minutes=1), [108500, 108501]))
    closes = store.read_m1("EUR/USD").get_column("close").to_list()
    assert closes == [108400, 108500, 108501]


def test_partitions_by_year_and_filters_range(tmp_path: Path) -> None:
    store = ParquetBarStore(tmp_path)
    nye = datetime(2023, 12, 31, 23, 58, tzinfo=UTC)
    store.write_m1("USD/JPY", m1_frame(nye, [149300, 149310, 149320, 149330]))
    files = sorted(p.name for p in (tmp_path / "USDJPY" / "M1").iterdir())
    assert files == ["2023.parquet", "2024.parquet"]
    part = store.read_m1("USD/JPY", start=datetime(2024, 1, 1, tzinfo=UTC))
    assert part.get_column("close").to_list() == [149320, 149330]
    part = store.read_m1("USD/JPY", end=datetime(2024, 1, 1, tzinfo=UTC))
    assert part.height == 2


def test_coverage_days_and_symbols(tmp_path: Path) -> None:
    store = ParquetBarStore(tmp_path)
    assert store.coverage("EUR/USD") is None
    assert store.symbols() == []
    assert store.read_m1("EUR/USD").is_empty()
    store.write_m1(
        "EUR/USD",
        pl.concat([m1_frame(T0, [108400] * 3), m1_frame(T0 + timedelta(days=2), [108400] * 2)]),
    )
    cov = store.coverage("EUR/USD")
    assert cov is not None
    assert (cov.first, cov.rows) == (T0, 5)
    assert store.days_present("EUR/USD", date(2024, 3, 1), date(2024, 3, 31)) == {
        date(2024, 3, 4),
        date(2024, 3, 6),
    }
    assert store.symbols() == ["EUR/USD"]
