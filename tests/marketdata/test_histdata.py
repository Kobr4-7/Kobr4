from datetime import UTC, datetime
from pathlib import Path

from kobr4.marketdata.sources.histdata import read_histdata


def test_read_histdata(tmp_path: Path) -> None:
    f = tmp_path / "DAT_ASCII_EURUSD_M1_2024.csv"
    f.write_text(
        "20240102 170000;1.104270;1.104290;1.104250;1.104290;0\n"
        "20240102 170100;1.104290;1.104310;1.104280;1.104300;0\n",
        encoding="utf-8",
    )
    df = read_histdata(f, "EUR/USD")
    row = df.row(0, named=True)
    assert row["open_time"] == datetime(2024, 1, 2, 22, 0, tzinfo=UTC)  # 17h EST = 22h UTC
    assert (row["open"], row["high"], row["low"], row["close"]) == (110427, 110429, 110425, 110429)
    assert row["spread"] is None
    assert df.height == 2


def test_read_histdata_jpy(tmp_path: Path) -> None:
    f = tmp_path / "usdjpy.csv"
    f.write_text("20240102 170000;141.021;141.030;141.015;141.028;0\n", encoding="utf-8")
    assert read_histdata(f, "USD/JPY").row(0, named=True)["close"] == 141028
