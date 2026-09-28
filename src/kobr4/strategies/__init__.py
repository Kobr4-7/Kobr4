"""Stratégies de trading et registre des types disponibles."""

from kobr4.config.settings import StrategySettings
from kobr4.strategies.base import Strategy
from kobr4.strategies.breakout import Breakout
from kobr4.strategies.ema_cross import EmaCross
from kobr4.strategies.rsi_reversion import RsiReversion

STRATEGIES: dict[str, type[Strategy]] = {
    cls.kind: cls for cls in (EmaCross, RsiReversion, Breakout)
}


def create_strategy(settings: StrategySettings) -> Strategy:
    try:
        cls = STRATEGIES[settings.kind]
    except KeyError:
        known = ", ".join(sorted(STRATEGIES))
        raise ValueError(
            f"stratégie {settings.id} : type inconnu {settings.kind!r} ({known})"
        ) from None
    return cls(settings)


__all__ = ["STRATEGIES", "Strategy", "create_strategy"]
