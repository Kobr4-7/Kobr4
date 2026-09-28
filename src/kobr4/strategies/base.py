"""Interface commune des stratégies.

Une stratégie reçoit des bougies clôturées et renvoie des intentions. Elle ne connaît
ni le courtier, ni le mode (backtest, démo, réel), ni la taille des positions.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field

from kobr4.config.settings import StrategySettings
from kobr4.core.models import Bar, CloseIntent, OrderIntent, Position
from kobr4.core.types import Side
from kobr4.strategies.sessions import Session, in_sessions

Intent = OrderIntent | CloseIntent


class StrategyContext(Protocol):
    def now(self) -> datetime: ...

    def positions(self, strategy_id: str, symbol: str) -> Sequence[Position]: ...


class BaseParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stop_loss_pips: Decimal = Field(default=Decimal(25), gt=0)
    take_profit_pips: Decimal | None = Field(default=Decimal(50), gt=0)
    sessions: list[Session] = Field(default_factory=list)


@dataclass(frozen=True)
class ParamRange:
    """Plage explorée par le laboratoire pour un paramètre."""

    low: float
    high: float
    step: float | None = None
    integer: bool = False


class Strategy(ABC):
    kind: ClassVar[str]
    Params: ClassVar[type[BaseParams]] = BaseParams
    search_space: ClassVar[dict[str, ParamRange]] = {}

    def __init__(self, settings: StrategySettings) -> None:
        self.id = settings.id
        self.instruments = list(settings.instruments)
        self.timeframe = settings.timeframe
        self.params = self.Params.model_validate(settings.params)

    @property
    @abstractmethod
    def warmup_bars(self) -> int:
        """Nombre de bougies nécessaires avant le premier signal."""

    @abstractmethod
    def on_bar(self, bar: Bar, ctx: StrategyContext) -> list[Intent]:
        """Appelée à la clôture de chaque bougie d'un instrument suivi."""

    # Outils communs

    def reverse_to(self, side: Side, bar: Bar, ctx: StrategyContext, reason: str) -> list[Intent]:
        """Ferme les positions de sens opposé et ouvre `side` si aucune position de ce sens
        n'est déjà ouverte et que la session est autorisée."""
        intents: list[Intent] = []
        positions = ctx.positions(self.id, bar.symbol)
        ts = bar.close_time
        if any(p.side is side.opposite for p in positions):
            intents.append(
                CloseIntent(
                    strategy_id=self.id, symbol=bar.symbol, side=side.opposite, ts=ts, reason=reason
                )
            )
        if not any(p.side is side for p in positions) and in_sessions(ts, self.params.sessions):
            intents.append(self.open(side, bar, reason))
        return intents

    def open(
        self, side: Side, bar: Bar, reason: str, stop_loss_pips: Decimal | None = None
    ) -> OrderIntent:
        return OrderIntent(
            strategy_id=self.id,
            symbol=bar.symbol,
            side=side,
            stop_loss_pips=stop_loss_pips or self.params.stop_loss_pips,
            take_profit_pips=self.params.take_profit_pips,
            ts=bar.close_time,
            reason=reason,
        )


def with_params(settings: StrategySettings, params: dict[str, Any]) -> StrategySettings:
    return settings.model_copy(update={"params": {**settings.params, **params}})
