"""Statistiques de performance d'un backtest."""

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise

from kobr4.core.models import ClosedTrade


@dataclass(frozen=True)
class Metrics:
    trades: int
    total_return_pct: float
    cagr_pct: float
    max_drawdown_pct: float
    sharpe: float
    sortino: float
    profit_factor: float
    win_rate_pct: float
    expectancy: float
    """Gain moyen par trade, en devise du compte."""
    avg_win: float
    avg_loss: float
    avg_duration_hours: float
    total_commission: float
    days: float

    def as_dict(self) -> dict[str, float | int]:
        return asdict(self)


def _daily_returns(curve: Sequence[tuple[datetime, Decimal]]) -> list[float]:
    """Rendements journaliers, à partir de la dernière équité de chaque jour UTC."""
    by_day: dict[object, float] = {}
    for ts, eq in curve:
        by_day[ts.date()] = float(eq)
    values = list(by_day.values())
    return [b / a - 1 for a, b in pairwise(values) if a > 0]


def max_drawdown_pct(curve: Sequence[tuple[datetime, Decimal]]) -> float:
    peak = -math.inf
    worst = 0.0
    for _, eq in curve:
        e = float(eq)
        peak = max(peak, e)
        if peak > 0:
            worst = max(worst, (peak - e) / peak * 100)
    return worst


def compute_metrics(
    initial: Decimal,
    curve: Sequence[tuple[datetime, Decimal]],
    trades: Sequence[ClosedTrade],
) -> Metrics:
    final = float(curve[-1][1]) if curve else float(initial)
    start = float(initial)
    days = (curve[-1][0] - curve[0][0]) / timedelta(days=1) if len(curve) > 1 else 0.0
    total = (final / start - 1) * 100 if start else 0.0
    years = days / 365.25
    cagr = ((final / start) ** (1 / years) - 1) * 100 if years > 0 and final > 0 else 0.0

    rets = _daily_returns(curve)
    if len(rets) > 1:
        mean = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
        down = [min(r, 0.0) for r in rets]
        dd = math.sqrt(sum(d * d for d in down) / len(down))
        sharpe = mean / sd * math.sqrt(252) if sd > 0 else 0.0
        sortino = mean / dd * math.sqrt(252) if dd > 0 else 0.0
    else:
        sharpe = sortino = 0.0

    pnls = [float(t.pnl) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_loss = -sum(losses)
    profit_factor = sum(wins) / gross_loss if gross_loss > 0 else (math.inf if wins else 0.0)
    durations = [(t.closed_at - t.opened_at) / timedelta(hours=1) for t in trades]

    return Metrics(
        trades=len(trades),
        total_return_pct=total,
        cagr_pct=cagr,
        max_drawdown_pct=max_drawdown_pct(curve),
        sharpe=sharpe,
        sortino=sortino,
        profit_factor=profit_factor,
        win_rate_pct=len(wins) / len(pnls) * 100 if pnls else 0.0,
        expectancy=sum(pnls) / len(pnls) if pnls else 0.0,
        avg_win=sum(wins) / len(wins) if wins else 0.0,
        avg_loss=sum(losses) / len(losses) if losses else 0.0,
        avg_duration_hours=sum(durations) / len(durations) if durations else 0.0,
        total_commission=float(sum((t.commission for t in trades), Decimal(0))),
        days=days,
    )
