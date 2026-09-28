"""Coefficient de risque par stratégie, selon ses résultats récents."""

from collections.abc import Sequence
from decimal import Decimal

from kobr4.config.settings import AllocationSettings
from kobr4.core.models import ClosedTrade


def risk_multiplier(
    settings: AllocationSettings, strategy_id: str, trades: Sequence[ClosedTrade]
) -> Decimal:
    """×1 tant qu'il n'y a pas assez de trades ; sinon 0,5 + 0,5 × profit factor, borné."""
    if not settings.enabled:
        return Decimal(1)
    recent = [t for t in trades if t.strategy_id == strategy_id][-settings.lookback_trades :]
    if len(recent) < settings.min_trades:
        return Decimal(1)
    gains = sum((t.pnl for t in recent if t.pnl > 0), Decimal(0))
    losses = -sum((t.pnl for t in recent if t.pnl <= 0), Decimal(0))
    pf = gains / losses if losses > 0 else Decimal(3)
    mult = Decimal("0.5") + Decimal("0.5") * pf
    return max(settings.min_multiplier, min(settings.max_multiplier, mult))
