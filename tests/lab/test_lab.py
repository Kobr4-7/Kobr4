import json
import random
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml

from kobr4.app import main
from kobr4.config.settings import Settings, StrategySettings, load_settings
from kobr4.lab.optimizer import LabSettings, Optimizer, _neighbours
from kobr4.lab.proposals import AcceptanceCriteria, Proposal, ProposalStatus, ProposalStore
from kobr4.lab.stats import deflated_sharpe, expected_max_sharpe, stability
from kobr4.lab.windows import Period, add_months, split_holdout, walk_forward
from kobr4.marketdata.sources.synthetic import generate_m1
from kobr4.marketdata.store import ParquetBarStore
from kobr4.core.types import Timeframe
from kobr4.strategies.base import ParamRange


def d(y: int, m: int, day: int = 1) -> datetime:
    return datetime(y, m, day, tzinfo=UTC)


def test_add_months_clamps_day() -> None:
    assert add_months(d(2024, 1, 31), 1) == d(2024, 2, 29)
    assert add_months(d(2024, 3, 15), -3) == d(2023, 12, 15)


def test_walk_forward_windows() -> None:
    ws = walk_forward(d(2020, 1), d(2023, 1), train_months=24, test_months=6)
    assert [(w.train.start, w.test.start, w.test.end) for w in ws] == [
        (d(2020, 1), d(2022, 1), d(2022, 7)),
        (d(2020, 7), d(2022, 7), d(2023, 1)),
    ]
    assert walk_forward(d(2020, 1), d(2021, 1), 24, 6) == []
    with pytest.raises(ValueError, match="mois"):
        walk_forward(d(2020, 1), d(2021, 1), 0, 6)


def test_split_holdout() -> None:
    study, hold = split_holdout(d(2020, 1), d(2024, 1), 12)
    assert study == Period(d(2020, 1), d(2023, 1))
    assert hold == Period(d(2023, 1), d(2024, 1))
    assert split_holdout(d(2020, 1), d(2024, 1), 0) == (Period(d(2020, 1), d(2024, 1)), None)
    with pytest.raises(ValueError, match="trop court"):
        split_holdout(d(2023, 6), d(2024, 1), 12)


def test_deflated_sharpe_penalizes_many_trials() -> None:
    rng = random.Random(1)
    strong = [0.002 + rng.gauss(0, 0.005) for _ in range(500)]
    few = [0.01, 0.02, 0.015]
    many = [rng.gauss(0, 0.04) for _ in range(500)]
    assert deflated_sharpe(strong, few) > 0.95
    assert expected_max_sharpe(many) > expected_max_sharpe(few)
    noise = [rng.gauss(0, 0.005) for _ in range(500)]
    assert deflated_sharpe(noise, many) < 0.5
    assert deflated_sharpe([], few) == 0.0


def test_stability() -> None:
    assert stability(1.0, [0.9, 0.6, 0.4, 0.1]) == 0.5
    assert stability(0.0, [1.0]) == 0.0
    assert stability(1.0, []) == 0.0


def test_neighbours_stay_in_range() -> None:
    space = {"fast": ParamRange(5, 50, integer=True), "sl": ParamRange(10, 80, step=5)}
    out = _neighbours({"fast": 5, "sl": 80.0}, space)
    assert {"fast": 6, "sl": 80.0} in out
    assert {"fast": 5, "sl": 75.0} in out
    assert all(5 <= n["fast"] <= 50 and 10 <= n["sl"] <= 80 for n in out)
    assert len(out) == 2


@pytest.fixture(scope="module")
def synth(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("synth")
    st = ParquetBarStore(root)
    st.write_m1("EUR/USD", generate_m1("EUR/USD", date(2023, 1, 1), date(2024, 6, 30), seed=11))
    return root


def lab_settings() -> Settings:
    return Settings.model_validate(
        {
            "mode": "backtest",
            "instruments": ["EUR/USD"],
            "broker": {"name": "simulated"},
            "risk": {"max_drawdown_pct": 40, "max_daily_loss_pct": 10},
            "strategies": [
                {"id": "ema", "kind": "ema_cross", "instruments": ["EUR/USD"], "timeframe": "H1",
                 "params": {"fast": 20, "slow": 50}},
            ],
        }
    )


def test_optimizer_end_to_end(synth: Path) -> None:
    lab = LabSettings(train_months=6, test_months=3, holdout_months=3, trials=4, workers=1, min_trades=1)
    messages: list[str] = []
    with ThreadPoolExecutor(1) as ex:
        opt = Optimizer(lab_settings(), "ema", str(synth), lab, progress=messages.append, executor=ex)
        p = opt.run(d(2023, 1), d(2024, 7))
    assert len(p.windows) == 3  # 15 mois d'étude : 6 + 3 mois, décalées de 3 mois
    assert p.holdout is not None
    assert p.holdout_baseline is not None
    assert p.holdout.period == "2024-04-01 → 2024-07-01"
    assert set(p.proposed_params) == {"fast", "slow", "stop_loss_pips", "take_profit_pips"}
    assert 0 <= p.stability <= 1
    assert 0 <= p.deflated_sharpe <= 1
    assert p.status in (ProposalStatus.PROPOSED, ProposalStatus.FAILED)
    assert (p.status is ProposalStatus.FAILED) == bool(p.failures)
    assert any("réglage final" in m for m in messages)
    # Deux exécutions avec la même graine donnent la même proposition.
    with ThreadPoolExecutor(1) as ex:
        again = Optimizer(lab_settings(), "ema", str(synth), lab, executor=ex).run(d(2023, 1), d(2024, 7))
    assert again.proposed_params == p.proposed_params


def test_optimizer_rejects_unknown_strategy(synth: Path) -> None:
    with pytest.raises(ValueError, match="inconnue"):
        Optimizer(lab_settings(), "nope", str(synth), LabSettings())


def proposal(status: ProposalStatus = ProposalStatus.PROPOSED, version: int = 1) -> Proposal:
    return Proposal(
        id="p1", created_at=d(2026, 1), strategy_id="ema-cross-h1", kind="ema_cross",
        base_version=version, current_params={"fast": 20}, proposed_params={"fast": 12, "slow": 40},
        objective="sharpe", trials=10, windows=[], oos_total_return_pct=5.0, oos_sharpe=0.8,
        oos_max_drawdown_pct=6.0, oos_trades=40, holdout=None, holdout_baseline=None,
        stability=0.8, deflated_sharpe=0.97, criteria=AcceptanceCriteria(), status=status,
    )


def test_proposal_lifecycle(tmp_path: Path) -> None:
    store = ProposalStore(tmp_path)
    store.save(proposal())
    with pytest.raises(ValueError, match="acceptée"):
        store.mark_applied("p1")
    p = store.decide("p1", True, "moi", d(2026, 1, 2), "ok")
    assert p.status is ProposalStatus.APPROVED
    with pytest.raises(ValueError, match="déjà traitée"):
        store.decide("p1", False, "moi", d(2026, 1, 2))
    assert store.mark_applied("p1").status is ProposalStatus.APPLIED
    assert [x.id for x in store.list()] == ["p1"]
    with pytest.raises(KeyError):
        store.get("absent")


def test_apply_to_checks_version() -> None:
    st = StrategySettings(id="ema-cross-h1", kind="ema_cross", instruments=["EUR/USD"], timeframe=Timeframe.H1,
                          params={"fast": 20, "slow": 50, "stop_loss_pips": 25})
    new = proposal().apply_to(st)
    assert new.version == 2
    assert new.params == {"fast": 12, "slow": 40, "stop_loss_pips": 25}
    with pytest.raises(ValueError, match="version"):
        proposal(version=3).apply_to(st)


def test_cli_approve_and_apply(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = Path(__file__).resolve().parents[2]
    cfg = tmp_path / "paper.yaml"
    cfg.write_text((root / "config" / "paper.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    lab_dir = tmp_path / "lab"
    ProposalStore(lab_dir).save(proposal())
    assert main(["lab", "--dir", str(lab_dir), "apply", "p1", "--config", str(cfg)]) == 2
    assert main(["lab", "--dir", str(lab_dir), "approve", "p1", "--note", "vu"]) == 0
    assert main(["lab", "--dir", str(lab_dir), "apply", "p1", "--config", str(cfg)]) == 0
    s = load_settings(cfg)
    (ema,) = [x for x in s.strategies if x.id == "ema-cross-h1"]
    assert ema.version == 2
    assert ema.params["fast"] == 12
    assert (tmp_path / "paper.yaml.v1.bak").exists()
    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["mode"] == "paper"
    assert main(["lab", "--dir", str(lab_dir), "list"]) == 0
    assert "applied" in capsys.readouterr().out
    assert json.loads((lab_dir / "p1.json").read_text(encoding="utf-8"))["decided_by"]
