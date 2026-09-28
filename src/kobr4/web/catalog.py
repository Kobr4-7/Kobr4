"""Catalogue présenté à l'utilisateur : stratégies, profils de risque, instruments."""

from typing import Any

from kobr4.core.instruments import INSTRUMENTS
from kobr4.strategies import STRATEGIES

DESCRIPTIONS: dict[str, dict[str, str]] = {
    "ema_cross": {
        "name": "Croisement de moyennes mobiles",
        "family": "Tendance",
        "summary": "Achète quand la moyenne rapide passe au-dessus de la lente, vend dans le cas"
        " inverse. Suit les mouvements de fond ; perd dans les marchés sans direction.",
    },
    "rsi_reversion": {
        "name": "Retour à la moyenne (RSI)",
        "family": "Contre-tendance",
        "summary": "Achète quand le RSI sort de la survente, vend quand il sort du surachat,"
        " seulement si la tendance n'est pas trop forte (ADX). Adapté aux marchés en range.",
    },
    "tsmom": {
        "name": "Momentum temporel (TSMOM)",
        "family": "Tendance",
        "summary": "Suit le sens des rendements passés sur trois horizons (semaine, mois,"
        " trimestre en H1), avec un stop en multiples de l'ATR et sans objectif. La mieux"
        " documentée des stratégies de tendance ; gagne dans les tendances, patiente sinon.",
    },
    "breakout": {
        "name": "Cassure de range",
        "family": "Volatilité",
        "summary": "Entre quand le prix sort du plus haut ou du plus bas des N dernières"
        " bougies. Capte les débuts de mouvement ; beaucoup de fausses cassures.",
    },
}

RISK_PRESETS: dict[str, dict[str, Any]] = {
    "prudent": {
        "label": "Prudent",
        "risk_per_trade_pct": 0.5,
        "max_open_positions": 3,
        "max_currency_risk_pct": 1.5,
        "max_daily_loss_pct": 2,
        "max_weekly_loss_pct": 4,
        "max_drawdown_pct": 8,
    },
    "equilibre": {
        "label": "Équilibré",
        "risk_per_trade_pct": 1,
        "max_open_positions": 5,
        "max_currency_risk_pct": 3,
        "max_daily_loss_pct": 3,
        "max_weekly_loss_pct": 6,
        "max_drawdown_pct": 10,
    },
    "dynamique": {
        "label": "Dynamique",
        "risk_per_trade_pct": 2,
        "max_open_positions": 6,
        "max_currency_risk_pct": 5,
        "max_daily_loss_pct": 5,
        "max_weekly_loss_pct": 10,
        "max_drawdown_pct": 15,
    },
}


def catalog() -> dict[str, Any]:
    strategies = []
    for kind, cls in STRATEGIES.items():
        if kind not in DESCRIPTIONS:
            continue
        schema = cls.Params.model_json_schema()
        strategies.append(
            {
                "kind": kind,
                **DESCRIPTIONS[kind],
                "params": schema.get("properties", {}),
                "defaults": cls.Params().model_dump(mode="json"),
                "optimizable": sorted(cls.search_space),
            }
        )
    return {
        "strategies": strategies,
        "risk_presets": RISK_PRESETS,
        "instruments": sorted(INSTRUMENTS),
        "timeframes": ["M15", "M30", "H1", "H4", "D1"],
        "sessions": [
            {"id": "tokyo", "label": "Tokyo (0h–9h UTC)"},
            {"id": "london", "label": "Londres (7h–16h UTC)"},
            {"id": "new_york", "label": "New York (12h–21h UTC)"},
        ],
    }
