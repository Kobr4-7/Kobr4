from datetime import date, timedelta

import pytest

from kobr4.marketdata.sources import dukascopy
from kobr4.marketdata.sources.dukascopy import DataFormatError, candles_url, parse_candles
from tests.marketdata.helpers import T0, bi5

DAY = T0.date()


def test_url_month_is_zero_based() -> None:
    assert candles_url("EUR/USD", date(2024, 1, 2), "BID") == (
        "https://datafeed.dukascopy.com/datafeed/EURUSD/2024/00/02/BID_candles_min_1.bi5"
    )
    assert candles_url("USD/JPY", date(2024, 12, 31), "ASK").endswith(
        "USDJPY/2024/11/31/ASK_candles_min_1.bi5"
    )


def test_parse_candles() -> None:
    raw = bi5(
        [(0, 108400, 108420, 108390, 108430, 12.5), (60, 108420, 108410, 108405, 108425, 3.0)]
    )
    df = parse_candles(raw, DAY)
    assert df.height == 2
    row = df.row(1, named=True)
    assert row["open_time"] == T0 + timedelta(minutes=1)
    assert (row["open"], row["high"], row["low"], row["close"]) == (108420, 108425, 108405, 108410)
    assert row["volume"] == 3.0


def test_parse_empty_file() -> None:
    assert parse_candles(b"", DAY).is_empty()


def test_parse_rejects_unexpected_format() -> None:
    # high < close : signe d'un ordre de champs différent de celui attendu
    with pytest.raises(DataFormatError, match="incohérente"):
        parse_candles(bi5([(0, 108400, 108420, 108390, 108410, 1.0)]), DAY)
    with pytest.raises(DataFormatError, match="LZMA"):
        parse_candles(b"pas du lzma", DAY)


def fake_fetch(files: dict[str, bytes]) -> dukascopy.Fetcher:
    def fetch(url: str) -> bytes:
        for key, raw in files.items():
            if url.endswith(key):
                return raw
        return b""

    return fetch


def test_fetch_day_merges_bid_ask_and_drops_flat_minutes() -> None:
    files = {
        "/00/BID_candles_min_1.bi5": b"",
        "04/BID_candles_min_1.bi5": bi5(
            [
                (0, 108400, 108420, 108390, 108430, 10.0),
                (60, 108420, 108420, 108420, 108420, 0.0),  # minute sans cotation
                (120, 108420, 108410, 108405, 108425, 4.0),
            ]
        ),
        "04/ASK_candles_min_1.bi5": bi5(
            [
                (0, 108402, 108422, 108392, 108432, 10.0),
                (60, 108422, 108422, 108422, 108422, 0.0),
                (120, 108422, 108413, 108407, 108427, 4.0),
            ]
        ),
    }
    df = dukascopy.fetch_day("EUR/USD", DAY, fake_fetch(files))
    assert df.get_column("open_time").to_list() == [T0, T0 + timedelta(minutes=2)]
    assert df.get_column("spread").to_list() == [2, 3]


def test_fetch_day_without_data() -> None:
    assert dukascopy.fetch_day("EUR/USD", DAY, fake_fetch({})).is_empty()


def test_download_skips_saturdays_and_known_days() -> None:
    seen: list[str] = []

    def fetch(url: str) -> bytes:
        seen.append(url)
        return b""

    start, end = date(2024, 3, 1), date(2024, 3, 4)  # vendredi → lundi
    days = [
        d
        for d, _ in dukascopy.download("EUR/USD", start, end, skip={date(2024, 3, 1)}, fetch=fetch)
    ]
    assert days == [date(2024, 3, 3), date(2024, 3, 4)]
    assert len(seen) == 4  # bid + ask pour chaque jour
