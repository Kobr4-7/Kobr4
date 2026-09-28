"""Propositions de réglages produites par le laboratoire, et leur cycle de validation."""

import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from kobr4.config.settings import StrategySettings
from kobr4.core.types import UtcDatetime


class ProposalStatus(StrEnum):
    PROPOSED = "proposed"
    """Validée par les critères automatiques, en attente d'une décision humaine."""
    FAILED = "failed"
    """Refusée par les critères automatiques : archivée avec le rapport."""
    APPROVED = "approved"
    """Acceptée par un humain : à faire tourner en démo."""
    REJECTED = "rejected"
    APPLIED = "applied"
    """Appliquée à une configuration."""


class PeriodResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    period: str
    params: dict[str, Any]
    trades: int
    total_return_pct: float
    max_drawdown_pct: float
    sharpe: float
    profit_factor: float
    win_rate_pct: float


class AcceptanceCriteria(BaseModel):
    """Seuils qu'une proposition doit tous franchir pour être soumise à un humain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_oos_sharpe: float = 0.5
    max_oos_drawdown_pct: float = 15.0
    min_oos_trades: int = 30
    min_stability: float = 0.6
    min_deflated_sharpe: float = 0.9
    holdout_must_be_positive: bool = True


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    created_at: UtcDatetime
    strategy_id: str
    kind: str
    base_version: int
    current_params: dict[str, Any]
    proposed_params: dict[str, Any]
    objective: str
    trials: int
    windows: list[PeriodResult]
    oos_total_return_pct: float
    oos_sharpe: float
    oos_max_drawdown_pct: float
    oos_trades: int
    holdout: PeriodResult | None
    holdout_baseline: PeriodResult | None
    """Réglages actuels sur la même période réservée, pour comparaison."""
    stability: float
    deflated_sharpe: float
    criteria: AcceptanceCriteria
    failures: list[str] = Field(default_factory=list)
    status: ProposalStatus
    decided_by: str | None = None
    decided_at: UtcDatetime | None = None
    note: str = ""

    def apply_to(self, settings: StrategySettings) -> StrategySettings:
        if settings.id != self.strategy_id:
            raise ValueError(f"la proposition concerne {self.strategy_id}, pas {settings.id}")
        if settings.version != self.base_version:
            raise ValueError(
                f"la stratégie est en version {settings.version}, la proposition a été calculée"
                f" sur la version {self.base_version} : relancer l'optimisation"
            )
        return settings.model_copy(
            update={
                "params": {**settings.params, **self.proposed_params},
                "version": settings.version + 1,
            }
        )


class ProposalStore:
    """Propositions enregistrées en JSON, une par fichier."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def save(self, proposal: Proposal) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{proposal.id}.json"
        path.write_text(proposal.model_dump_json(indent=2), encoding="utf-8")
        return path

    def get(self, proposal_id: str) -> Proposal:
        path = self.root / f"{proposal_id}.json"
        if not path.exists():
            raise KeyError(f"proposition inconnue : {proposal_id}")
        return Proposal.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def list(self) -> list[Proposal]:
        if not self.root.exists():
            return []
        items = [
            Proposal.model_validate(json.loads(p.read_text(encoding="utf-8")))
            for p in self.root.glob("*.json")
        ]
        return sorted(items, key=lambda p: p.created_at, reverse=True)

    def decide(
        self, proposal_id: str, approve: bool, by: str, at: datetime, note: str = ""
    ) -> Proposal:
        p = self.get(proposal_id)
        if p.status is not ProposalStatus.PROPOSED:
            raise ValueError(f"proposition {proposal_id} déjà traitée ({p.status})")
        p = p.model_copy(
            update={
                "status": ProposalStatus.APPROVED if approve else ProposalStatus.REJECTED,
                "decided_by": by,
                "decided_at": at,
                "note": note,
            }
        )
        self.save(p)
        return p

    def mark_applied(self, proposal_id: str) -> Proposal:
        p = self.get(proposal_id)
        if p.status is not ProposalStatus.APPROVED:
            raise ValueError("seule une proposition acceptée peut être appliquée")
        p = p.model_copy(update={"status": ProposalStatus.APPLIED})
        self.save(p)
        return p
