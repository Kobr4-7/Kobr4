from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from kobr4.app import main
from kobr4.backtest.engine import load_bars, run_backtest
from kobr4.backtest.metrics import compute_metrics, max_drawdown_pct
from kobr4.config.settings import Settings
from kobr4.core.instruments import get_instrument
from kobr4.core.models import Bar, ClosedTrade, ExitReason
from kobr4.core.types import Side, Timeframe
from kobr4.marketdata import frames
from kobr4.marketdata.resampler import BarResampler
from kobr4.marketdata.sources.synthetic import generate_m1
from kobr4.marketdata.store import ParquetBarStore

ROOT = Path(__file__).resolve().parents[2]


def settings(**kw: object) -> Settings:
    base: dict[str, object] = {
        "mode": "backtest",
        "instruments": ["EUR/USD", "USD/JPY"],
        "broker": {"name": "simulated"},
        "risk": {"max_drawdown_pct": 50, "max_daily_loss_pct": 10},
        "strategies": [
            {
                "id": "ema",
                "kind": "ema_cross",
                "instruments": ["EUR/USD", "USD/JPY"],
                "timeframe": "H1",
                "params": {"fast": 10, "slow": 30},
            }
        ],
    }
    base.update(kw)
    return Settings.model_validate(base)


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> ParquetBarStore:
    st = ParquetBarStore(tmp_path_factory.mktemp("synth"))
    for sym in ("EUR/USD", "USD/JPY", "GBP/USD", "EUR/GBP"):
        st.write_m1(sym, generate_m1(sym, date(2024, 1, 1), date(2024, 4, 30), seed=3))
    return st


def test_resampler_matches_store_resample(store: ParquetBarStore) -> None:
    m15 = store.bars("EUR/USD", Timeframe.M15)
    rs = BarResampler("EUR/USD", Timeframe.H1)
    out = [b for bar in m15 for b in rs.on_bar(bar)] + rs.flush()
    direct = store.bars("EUR/USD", Timeframe.H1)
    assert [(b.open_time, b.open, b.high, b.low, b.close) for b in out] == [
        (b.open_time, b.open, b.high, b.low, b.close) for b in direct
    ]


def test_resampler_rejects_longer_source(store: ParquetBarStore) -> None:
    with pytest.raises(ValueError, match="plus longue"):
        BarResampler("EUR/USD", Timeframe.H1).on_bar(store.bars("EUR/USD", Timeframe.H4)[0])


async def test_backtest_is_deterministic_and_consistent(store: ParquetBarStore) -> None:
    s = settings()
    bars = load_bars(store, s, None, None)
    a = await run_backtest(s, bars)
    b = await run_backtest(s, load_bars(store, s, None, None))
    assert a.trades == b.trades
    assert a.equity_curve == b.equity_curve
    assert a.trades, "la stratégie doit trader sur 4 mois de données"
    # Le solde final est le capital plus la somme des résultats des trades.
    assert a.final_equity == a.initial_balance + sum((t.pnl for t in a.trades), Decimal(0))
    assert all(t.closed_at >= t.opened_at for t in a.trades)
    assert {t.reason for t in a.trades} <= set(ExitReason)
    # Taille calculée par le risque : un stop coûte ~1 % de l'équité au moment de l'entrée.
    equity_at = dict(a.equity_curve)
    stops = [t for t in a.trades if t.reason is ExitReason.STOP_LOSS]
    assert stops
    for t in stops:
        loss_pct = -t.pnl / equity_at[t.opened_at] * 100
        assert Decimal("0.85") < loss_pct < Decimal("1.15"), (t, loss_pct)


async def test_higher_timeframe_strategy_via_resampler(store: ParquetBarStore) -> None:
    s = settings(
        strategies=[
            {
                "id": "fast",
                "kind": "ema_cross",
                "instruments": ["EUR/USD"],
                "timeframe": "M15",
                "params": {"fast": 10, "slow": 30},
            },
            {"id": "slow", "kind": "breakout", "instruments": ["EUR/USD"], "timeframe": "H4"},
        ]
    )
    result = await run_backtest(s, load_bars(store, s, None, None))
    assert {t.strategy_id for t in result.trades} == {"fast", "slow"}


async def test_cross_pair_converts_pnl(store: ParquetBarStore) -> None:
    s = settings(
        instruments=["EUR/GBP"],
        strategies=[
            {"id": "x", "kind": "ema_cross", "instruments": ["EUR/GBP"], "timeframe": "H1"}
        ],
    )
    bars = load_bars(store, s, None, None)
    assert set(bars) == {"EUR/GBP", "GBP/USD", "EUR/USD"}
    result = await run_backtest(s, bars)
    assert result.trades


async def test_empty_inputs_rejected() -> None:
    with pytest.raises(ValueError, match="aucune bougie"):
        await run_backtest(settings(), {"EUR/USD": []})


def test_metrics_on_known_curve() -> None:
    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    curve = [(t0 + timedelta(days=i), Decimal(v)) for i, v in enumerate([100, 110, 99, 120])]
    assert max_drawdown_pct(curve) == pytest.approx(10.0)

    def trade(pnl: str) -> ClosedTrade:
        return ClosedTrade(
            position_id="p",
            strategy_id="s",
            symbol="EUR/USD",
            side=Side.BUY,
            quantity=1000,
            entry_price=Decimal(1),
            exit_price=Decimal(1),
            opened_at=t0,
            closed_at=t0 + timedelta(hours=2),
            reason=ExitReason.SIGNAL,
            pnl=Decimal(pnl),
        )

    m = compute_metrics(Decimal(100), curve, [trade("30"), trade("-10"), trade("-10")])
    assert m.total_return_pct == pytest.approx(20.0)
    assert m.profit_factor == pytest.approx(1.5)
    assert m.win_rate_pct == pytest.approx(100 / 3)
    assert m.expectancy == pytest.approx(10 / 3)
    assert m.avg_duration_hours == pytest.approx(2.0)


def test_cli_end_to_end(
    store: ParquetBarStore, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (store.root / "SYNTHETIC").write_text("x", encoding="utf-8")
    out = tmp_path / "reports"
    code = main(
        [
            "backtest",
            "--config",
            str(ROOT / "config" / "backtest.yaml"),
            "--store",
            str(store.root),
            "--out",
            str(out),
            "--name",
            "essai",
            "--start",
            "2024-01-01",
            "--end",
            "2024-03-31",
        ]
    )
    assert code == 0
    (report,) = out.iterdir()
    assert {p.name for p in report.iterdir()} == {
        "summary.json",
        "trades.csv",
        "equity.csv",
        "report.html",
    }
    html = (report / "report.html").read_text(encoding="utf-8")
    assert "Données synthétiques" in html
    assert "essai" in capsys.readouterr().out


def test_cli_without_data(tmp_path: Path) -> None:
    code = main(
        ["backtest", "--config", str(ROOT / "config" / "backtest.yaml"), "--store", str(tmp_path)]
    )
    assert code == 1


def test_bars_roundtrip_for_backtest(store: ParquetBarStore) -> None:
    bars: list[Bar] = store.bars("USD/JPY", Timeframe.H4)
    df = frames.from_bars(bars, get_instrument("USD/JPY"))
    assert frames.to_bars(df, get_instrument("USD/JPY"), Timeframe.H4) == bars
