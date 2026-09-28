"""Évaluation d'un réglage sur une période : un backtest, réduit à quelques chiffres.

Conçu pour tourner dans des processus séparés (optimisation en parallèle) : les données
sont relues depuis le stockage et gardées en cache dans chaque processus.
"""

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from itertools import pairwise
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from kobr4.backtest.engine import execution_timeframe, required_symbols, run_backtest
from kobr4.backtest.metrics import compute_metrics
from kobr4.config.settings import Settings
from kobr4.core.models import Bar
from kobr4.core.types import Timeframe
from kobr4.marketdata.store import ParquetBarStore
from kobr4.strategies import create_strategy


@dataclass(frozen=True)
class EvalRequest:
    settings_json: str
    strategy_id: str
    params: tuple[tuple[str, Any], ...]
    store: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class EvalResult:
    valid: bool
    trades: int = 0
    total_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe: float = 0.0
    profit_factor: float = 0.0
    win_rate_pct: float = 0.0
    daily_returns: tuple[float, ...] = ()
    error: str = ""

    @property
    def period_sharpe(self) -> float:
        """Sharpe par jour (non annualisé), utilisé par le Sharpe dégonflé."""
        return self.sharpe / math.sqrt(252)

    def score(self, objective: str, min_trades: int) -> float:
        if not self.valid:
            return -math.inf
        if self.trades < min_trades:
            return -10.0 + self.trades / max(min_trades, 1)
        match objective:
            case "sharpe":
                return self.sharpe
            case "calmar":
                return self.total_return_pct / max(self.max_drawdown_pct, 1.0)
            case "profit_factor":
                return min(self.profit_factor, 10.0)
        raise ValueError(f"objectif inconnu : {objective}")


def strategy_only(settings: Settings, strategy_id: str, params: dict[str, Any]) -> Settings:
    """Configuration réduite à une stratégie, avec des paramètres remplacés."""
    matches = [s for s in settings.strategies if s.id == strategy_id]
    if not matches:
        raise ValueError(f"stratégie inconnue : {strategy_id}")
    st = matches[0]
    st = st.model_copy(update={"enabled": True, "params": {**st.params, **params}})
    return settings.model_copy(update={"strategies": [st]})


@lru_cache(maxsize=64)
def _bars(
    store: str, symbol: str, tf: Timeframe, start: datetime, end: datetime
) -> tuple[Bar, ...]:
    return tuple(ParquetBarStore(Path(store)).bars(symbol, tf, start, end))


def evaluate(req: EvalRequest) -> EvalResult:
    # Des centaines de backtests : les alertes de chacun (arrêt d'urgence…) sont attendues
    # et déjà résumées par les statistiques.
    for name in ("kobr4.risk", "kobr4.execution"):
        logging.getLogger(name).setLevel(logging.ERROR)
    settings = Settings.model_validate_json(req.settings_json)
    try:
        s = strategy_only(settings, req.strategy_id, dict(req.params))
        strategies = [create_strategy(st) for st in s.strategies]
    except (ValidationError, ValueError) as e:
        return EvalResult(valid=False, error=str(e).splitlines()[0])
    tf = execution_timeframe(strategies)
    bars = {sym: list(_bars(req.store, sym, tf, req.start, req.end)) for sym in required_symbols(s)}
    if not all(bars.values()):
        return EvalResult(valid=False, error="données manquantes sur la période")
    result = asyncio.run(run_backtest(s, bars, strategies=strategies))
    m = compute_metrics(result.initial_balance, result.equity_curve, result.trades)
    by_day: dict[object, float] = {}
    for ts, eq in result.equity_curve:
        by_day[ts.date()] = float(eq)
    values = list(by_day.values())
    rets = tuple(b / a - 1 for a, b in pairwise(values) if a > 0)
    return EvalResult(
        valid=True,
        trades=m.trades,
        total_return_pct=m.total_return_pct,
        max_drawdown_pct=m.max_drawdown_pct,
        sharpe=m.sharpe,
        profit_factor=m.profit_factor if math.isfinite(m.profit_factor) else 10.0,
        win_rate_pct=m.win_rate_pct,
        daily_returns=rets,
    )
