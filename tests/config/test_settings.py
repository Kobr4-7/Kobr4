from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from kobr4.app import main
from kobr4.config import ConfigError, Mode, Settings, load_settings

ROOT = Path(__file__).resolve().parents[2]


def base(**kw: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "mode": "paper",
        "instruments": ["EUR/USD", "USD/JPY"],
        "broker": {"name": "oanda", "environment": "practice"},
        "strategies": [
            {"id": "ema", "kind": "ema_cross", "instruments": ["EUR/USD"], "timeframe": "H1"}
        ],
    }
    cfg.update(kw)
    return cfg


@pytest.mark.parametrize("name", ["paper.yaml", "backtest.yaml", "backtest-or.yaml"])
def test_shipped_configs_are_valid(name: str) -> None:
    settings = load_settings(ROOT / "config" / name)
    assert settings.strategies


def test_unknown_instrument() -> None:
    with pytest.raises(ValidationError, match="XAG/USD"):
        Settings.model_validate(base(instruments=["EUR/USD", "XAG/USD"]))


def test_strategy_instrument_must_be_declared() -> None:
    strategies = [{"id": "s", "kind": "k", "instruments": ["GBP/USD"], "timeframe": "H1"}]
    with pytest.raises(ValidationError, match="non déclarés"):
        Settings.model_validate(base(strategies=strategies))


def test_duplicate_strategy_ids() -> None:
    s = {"id": "s", "kind": "k", "instruments": ["EUR/USD"], "timeframe": "H1"}
    with pytest.raises(ValidationError, match="double"):
        Settings.model_validate(base(strategies=[s, s]))


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(base(risk={"risk_per_trade": 1}))


def test_risk_limits_consistency() -> None:
    with pytest.raises(ValidationError, match="perte journalière"):
        Settings.model_validate(base(risk={"risk_per_trade_pct": 4, "max_daily_loss_pct": 3}))


def test_backtest_requires_simulated_broker() -> None:
    with pytest.raises(ValidationError, match="simulé"):
        Settings.model_validate(base(mode="backtest"))


def test_paper_refuses_live_environment() -> None:
    with pytest.raises(ValidationError, match="practice"):
        Settings.model_validate(base(broker={"name": "oanda", "environment": "live"}))


def test_live_requires_explicit_confirmation() -> None:
    live = base(mode="live", broker={"name": "oanda", "environment": "live"})
    with pytest.raises(ValidationError, match="confirm_live"):
        Settings.model_validate(live)
    assert Settings.model_validate({**live, "confirm_live": True}).mode is Mode.LIVE


def test_api_key_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings.model_validate(base())
    monkeypatch.delenv("KOBR4_BROKER_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="KOBR4_BROKER_API_KEY"):
        settings.broker.api_key()
    monkeypatch.setenv("KOBR4_BROKER_API_KEY", "secret-123")
    key = settings.broker.api_key()
    assert key.get_secret_value() == "secret-123"
    assert "secret-123" not in repr(key)


def test_load_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="lecture impossible"):
        load_settings(tmp_path / "absent.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("- une\n- liste\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="racine"):
        load_settings(bad)


def test_cli(tmp_path: Path) -> None:
    assert main(["run", "--config", str(ROOT / "config" / "paper.yaml"), "--check"]) == 0
    bad = tmp_path / "bad.yaml"
    bad.write_text("mode: paper\n", encoding="utf-8")
    assert main(["run", "--config", str(bad)]) == 2
