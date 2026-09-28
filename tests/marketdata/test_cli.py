from pathlib import Path

import pytest

from kobr4.app import main
from kobr4.marketdata.sources import dukascopy
from tests.marketdata.helpers import bi5


def test_download_then_info(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []

    def fetch(url: str, retries: int = 4, timeout: float = 30) -> bytes:
        calls.append(url)
        side_offset = 2 if "ASK" in url else 0
        return bi5(
            [
                (
                    m * 60,
                    108400 + side_offset,
                    108401 + side_offset,
                    108399 + side_offset,
                    108402 + side_offset,
                    1.0,
                )
                for m in range(3)
            ]
        )

    monkeypatch.setattr(dukascopy, "http_fetch", fetch)
    args = [
        "data",
        "--store",
        str(tmp_path),
        "download",
        "--symbols",
        "EUR/USD",
        "--start",
        "2024-03-04",
        "--end",
        "2024-03-05",
    ]
    assert main(args) == 0
    assert len(calls) == 4
    # Deuxième lancement : les jours déjà présents ne sont pas retéléchargés.
    assert main(args) == 0
    assert len(calls) == 4

    assert main(["data", "--store", str(tmp_path), "info"]) == 0
    out = capsys.readouterr().out
    assert "EUR/USD" in out
    assert "6 bougies M1" in out


def test_download_rejects_inverted_dates(tmp_path: Path) -> None:
    assert (
        main(
            [
                "data",
                "--store",
                str(tmp_path),
                "download",
                "--symbols",
                "EUR/USD",
                "--start",
                "2024-03-05",
                "--end",
                "2024-03-04",
            ]
        )
        == 2
    )


def test_unknown_symbol_is_usage_error(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(
            [
                "data",
                "--store",
                str(tmp_path),
                "download",
                "--symbols",
                "XAG/USD",
                "--start",
                "2024-03-04",
            ]
        )


def test_import_histdata(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = tmp_path / "eurusd.csv"
    f.write_text("20240102 170000;1.104270;1.104290;1.104250;1.104290;0\n", encoding="utf-8")
    store = tmp_path / "store"
    assert (
        main(["data", "--store", str(store), "import-histdata", str(f), "--symbol", "EUR/USD"]) == 0
    )
    assert main(["data", "--store", str(store), "info"]) == 0
    assert "1 sans spread" in capsys.readouterr().out
