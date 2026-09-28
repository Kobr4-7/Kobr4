from datetime import timedelta

import polars as pl

from kobr4.marketdata.quality import check_m1
from tests.marketdata.helpers import T0, m1_frame


def test_clean_data() -> None:
    report = check_m1(m1_frame(T0, [108400] * 60))
    assert report.ok
    assert report.rows == 60


def test_gap_detected_on_weekday() -> None:
    df = pl.concat([m1_frame(T0, [108400] * 5), m1_frame(T0 + timedelta(hours=2), [108400] * 5)])
    (gap,) = check_m1(df).gaps
    assert gap.start == T0 + timedelta(minutes=5)
    assert gap.end == T0 + timedelta(hours=2)
    assert check_m1(df, max_gap=timedelta(hours=3)).ok


def test_weekend_gap_ignored() -> None:
    friday = T0 - timedelta(days=3) + timedelta(hours=21)
    sunday = T0 - timedelta(days=1) + timedelta(hours=22)
    df = pl.concat([m1_frame(friday, [108400] * 5), m1_frame(sunday, [108400] * 5)])
    assert check_m1(df).ok


def test_invalid_and_duplicate_bars() -> None:
    df = m1_frame(T0, [108400] * 3)
    bad = df.head(1).with_columns(pl.lit(108000, pl.Int64).alias("high"))
    report = check_m1(pl.concat([df, bad]))
    assert report.duplicates == 1
    assert report.invalid_bars == 1
