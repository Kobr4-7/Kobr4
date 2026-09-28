"""Configuration du bot, chargée depuis un fichier YAML versionné.

Les secrets (clé API du courtier) ne sont jamais dans le fichier : il contient seulement
le nom de la variable d'environnement qui les porte.
"""

import os
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from kobr4.core.instruments import INSTRUMENTS
from kobr4.core.types import Timeframe


class ConfigError(Exception):
    pass


class Mode(StrEnum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BrokerSettings(_Strict):
    name: Literal["simulated", "oanda", "saxo", "ig"]
    environment: Literal["practice", "live"] = "practice"
    account_id: str | None = None
    api_key_env: str = "KOBR4_BROKER_API_KEY"

    def api_key(self) -> SecretStr:
        value = os.environ.get(self.api_key_env)
        if not value:
            raise ConfigError(
                f"clé API absente : définir la variable d'environnement {self.api_key_env}"
            )
        return SecretStr(value)


class AllocationSettings(_Strict):
    """Répartition du risque selon les résultats récents de chaque stratégie.

    Le risque par trade est multiplié par un coefficient borné, calculé sur les derniers
    trades de la stratégie : profit factor 1 → ×1, 2 → ×1,5, 0 → ×0,5.
    """

    enabled: bool = False
    lookback_trades: int = Field(default=30, ge=5, le=500)
    min_trades: int = Field(default=10, ge=1)
    min_multiplier: Decimal = Field(default=Decimal("0.5"), gt=0, le=1)
    max_multiplier: Decimal = Field(default=Decimal("1.5"), ge=1, le=3)


class RiskSettings(_Strict):
    """Limites du gestionnaire de risque (docs/ARCHITECTURE.md §4.4)."""

    risk_per_trade_pct: Decimal = Field(default=Decimal("1"), gt=0, le=5)
    max_open_positions: int = Field(default=5, ge=1, le=50)
    max_positions_per_symbol_strategy: int = Field(default=1, ge=1)
    max_currency_risk_pct: Decimal = Field(default=Decimal("3"), gt=0, le=20)
    max_daily_loss_pct: Decimal = Field(default=Decimal("3"), gt=0, le=20)
    max_drawdown_pct: Decimal = Field(default=Decimal("10"), gt=0, le=50)
    max_spread_multiplier: Decimal = Field(default=Decimal("2"), ge=1)
    news_blackout_minutes: int = Field(default=30, ge=0)
    leverage: Decimal = Field(default=Decimal(30), gt=0, le=500)
    allocation: AllocationSettings = AllocationSettings()
    min_units: int = Field(default=1_000, ge=1)
    """Taille minimale d'une position, et pas d'arrondi (1 000 unités = 0,01 lot)."""

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.risk_per_trade_pct > self.max_daily_loss_pct:
            raise ValueError("le risque par trade dépasse la perte journalière maximale")
        if self.max_daily_loss_pct > self.max_drawdown_pct:
            raise ValueError("la perte journalière maximale dépasse le drawdown maximal")
        return self


class BacktestSettings(_Strict):
    """Coûts et capital simulés."""

    initial_balance: Decimal = Field(default=Decimal(10_000), gt=0)
    slippage_pips: Decimal = Field(default=Decimal("0.2"), ge=0)
    commission_per_million: Decimal = Field(default=Decimal(0), ge=0)
    """Commission par million d'unités échangées, par sens (0 pour un compte sans
    commission, où le coût est dans le spread)."""


class StrategySettings(_Strict):
    id: str = Field(min_length=1, pattern=r"^[a-z0-9_-]+$")
    kind: str = Field(min_length=1)
    version: int = Field(default=1, ge=1)
    """Incrémentée à chaque changement de paramètres validé (laboratoire)."""
    enabled: bool = True
    instruments: list[str] = Field(min_length=1)
    timeframe: Timeframe
    params: dict[str, Any] = Field(default_factory=dict)
    ml_filter: str | None = None
    """Dossier d'un modèle de filtre entraîné (`kobr4 lab train-filter`), facultatif."""


class Settings(_Strict):
    mode: Mode
    base_currency: str = Field(default="USD", min_length=3, max_length=3)
    instruments: list[str] = Field(min_length=1)
    broker: BrokerSettings
    risk: RiskSettings = RiskSettings()
    strategies: list[StrategySettings] = Field(default_factory=list)
    backtest: BacktestSettings = BacktestSettings()
    news_calendar: Path | None = None
    """Fichier YAML des annonces économiques (voir risk/calendar.py)."""
    confirm_live: bool = False

    @field_validator("instruments")
    @classmethod
    def _known_instruments(cls, v: list[str]) -> list[str]:
        unknown = [s for s in v if s not in INSTRUMENTS]
        if unknown:
            raise ValueError(f"instruments inconnus : {', '.join(unknown)}")
        if len(set(v)) != len(v):
            raise ValueError("instruments en double")
        return v

    @model_validator(mode="after")
    def _check(self) -> Self:
        ids = [s.id for s in self.strategies]
        if len(set(ids)) != len(ids):
            raise ValueError("identifiants de stratégie en double")
        for s in self.strategies:
            missing = set(s.instruments) - set(self.instruments)
            if missing:
                raise ValueError(
                    f"stratégie {s.id} : instruments non déclarés {', '.join(sorted(missing))}"
                )

        match self.mode:
            case Mode.BACKTEST if self.broker.name != "simulated":
                raise ValueError("le mode backtest exige le courtier simulé")
            case Mode.PAPER if self.broker.environment != "practice":
                raise ValueError("le mode paper exige un environnement courtier « practice »")
            case Mode.LIVE:
                if self.broker.name == "simulated" or self.broker.environment != "live":
                    raise ValueError("le mode live exige un courtier réel en environnement live")
                if not self.confirm_live:
                    raise ValueError("le mode live exige confirm_live: true")
        return self


def load_settings(path: str | Path) -> Settings:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ConfigError(f"lecture impossible de {path} : {e}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML invalide dans {path} : {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} : un objet YAML est attendu à la racine")
    try:
        return Settings.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"configuration invalide dans {path} :\n{e}") from e
