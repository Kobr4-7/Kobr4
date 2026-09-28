"""Filtre de signaux par machine learning (LightGBM).

Idée : à partir des trades passés d'une stratégie (backtest ou réel), apprendre dans
quelles conditions de marché ses signaux échouent le plus souvent, puis écarter ces
signaux. Le filtre ne crée jamais de trade : il ne peut qu'en refuser.

Garde-fous contre le sur-apprentissage :
- découpage chronologique : on apprend sur le passé (70 %), on mesure sur la suite (30 %)
  que le modèle n'a jamais vue ;
- le seuil de probabilité est choisi par validation croisée chronologique à l'intérieur
  de la période d'apprentissage, jamais sur la période de test ;
- le filtre n'est jugé utile que s'il améliore le profit factor sur la période de test,
  avec une AUC d'au moins 0,55, en gardant au moins 30 % des trades.
"""

import json
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import lightgbm as lgb
import numpy as np

from kobr4.core.models import OrderIntent
from kobr4.core.types import Timeframe
from kobr4.intel.features import FEATURES, FeatureTracker
from kobr4.risk.manager import Rejection, SignalFilter

if TYPE_CHECKING:
    from kobr4.backtest.engine import BacktestResult

MIN_SAMPLES = 60
PARAMS: dict[str, Any] = {
    "objective": "binary",
    "learning_rate": 0.05,
    "num_leaves": 15,
    "min_data_in_leaf": 20,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    "verbose": -1,
    "seed": 7,
    "deterministic": True,
    "num_threads": 1,
}
ROUNDS = 150


@dataclass(frozen=True)
class Sample:
    ts: datetime
    features: dict[str, float]
    pnl: float

    @property
    def win(self) -> bool:
        return self.pnl > 0


def samples_from_backtest(result: "BacktestResult", strategy_id: str) -> list[Sample]:
    out = []
    for t in result.trades:
        ctx = result.trade_context.get(t.position_id)
        if not ctx or ctx.get("strategy_id") != strategy_id or not ctx.get("features"):
            continue
        out.append(Sample(ts=t.opened_at, features=ctx["features"], pnl=float(t.pnl)))
    return sorted(out, key=lambda s: s.ts)


def _matrix(samples: Sequence[Sample]) -> np.ndarray:
    return np.array([[s.features[f] for f in FEATURES] for s in samples], dtype=float)


def auc(labels: Sequence[bool], scores: Sequence[float]) -> float:
    """Aire sous la courbe ROC (méthode des rangs)."""
    pos = [s for s, y in zip(scores, labels, strict=True) if y]
    neg = [s for s, y in zip(scores, labels, strict=True) if not y]
    if not pos or not neg:
        return 0.5
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def profit_factor(pnls: Sequence[float]) -> float:
    gains = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p <= 0)
    return gains / losses if losses > 0 else (10.0 if gains > 0 else 0.0)


def _fit(samples: Sequence[Sample]) -> lgb.Booster:
    data = lgb.Dataset(
        _matrix(samples), label=[int(s.win) for s in samples], feature_name=list(FEATURES)
    )
    return lgb.train(PARAMS, data, num_boost_round=ROUNDS)


def _best_threshold(samples: Sequence[Sample], probs: Sequence[float], min_keep: float) -> float:
    """Seuil qui maximise le P&L des trades gardés, en gardant au moins `min_keep`."""
    best_t, best_pnl = 0.0, sum(s.pnl for s in samples)
    for t in sorted(set(round(p, 3) for p in probs)):
        kept = [s.pnl for s, p in zip(samples, probs, strict=True) if p >= t]
        if len(kept) < min_keep * len(samples):
            break
        if sum(kept) > best_pnl:
            best_t, best_pnl = t, sum(kept)
    return best_t


@dataclass(frozen=True)
class FilterReport:
    samples: int
    train_samples: int
    test_samples: int
    test_auc: float
    threshold: float
    test_kept_pct: float
    test_pf_all: float
    test_pf_kept: float
    test_pnl_all: float
    test_pnl_kept: float
    useful: bool
    reason: str


class SignalModel:
    def __init__(self, booster: lgb.Booster, threshold: float, report: FilterReport) -> None:
        self.booster = booster
        self.threshold = threshold
        self.report = report

    def probability(self, features: dict[str, float]) -> float:
        x = np.array([[features[f] for f in FEATURES]], dtype=float)
        return float(self.booster.predict(x)[0])

    def save(self, directory: str | Path) -> Path:
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        self.booster.save_model(str(d / "model.txt"))
        meta = {
            "features": list(FEATURES),
            "threshold": self.threshold,
            "report": asdict(self.report),
        }
        (d / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return d

    @classmethod
    def load(cls, directory: str | Path) -> "SignalModel":
        d = Path(directory)
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        if meta["features"] != list(FEATURES):
            raise ValueError("modèle entraîné avec d'autres caractéristiques : le réentraîner")
        return cls(
            lgb.Booster(model_file=str(d / "model.txt")),
            meta["threshold"],
            FilterReport(**meta["report"]),
        )


def train_filter(
    samples: Sequence[Sample], train_frac: float = 0.7, min_keep: float = 0.3
) -> SignalModel:
    if len(samples) < MIN_SAMPLES:
        raise ValueError(
            f"{len(samples)} trades : au moins {MIN_SAMPLES} sont nécessaires pour apprendre"
        )
    cut = int(len(samples) * train_frac)
    train, test = list(samples[:cut]), list(samples[cut:])

    # Seuil choisi sur des prédictions « hors échantillon » à l'intérieur de l'apprentissage.
    folds = 3
    size = len(train) // (folds + 1)
    oof_samples: list[Sample] = []
    oof_probs: list[float] = []
    for k in range(1, folds + 1):
        fit_part, val_part = train[: size * k], train[size * k : size * (k + 1)]
        if len(fit_part) < 20 or not val_part:
            continue
        booster = _fit(fit_part)
        oof_samples += val_part
        oof_probs += list(booster.predict(_matrix(val_part)))
    threshold = _best_threshold(oof_samples, oof_probs, min_keep) if oof_samples else 0.0

    booster = _fit(train)
    probs = list(booster.predict(_matrix(test)))
    kept = [s.pnl for s, p in zip(test, probs, strict=True) if p >= threshold]
    all_pnl = [s.pnl for s in test]
    test_auc = auc([s.win for s in test], probs)
    pf_all, pf_kept = profit_factor(all_pnl), profit_factor(kept)
    kept_pct = len(kept) / len(test) * 100 if test else 0.0
    reasons = []
    if test_auc < 0.55:
        reasons.append(
            f"AUC {test_auc:.2f} < 0,55 : le modèle ne distingue pas mieux que le hasard"
        )
    if pf_kept <= pf_all:
        reasons.append(f"profit factor filtré {pf_kept:.2f} ≤ non filtré {pf_all:.2f}")
    if kept_pct < min_keep * 100:
        reasons.append(f"seulement {kept_pct:.0f} % des trades gardés")
    if threshold == 0.0:
        reasons.append("aucun seuil utile trouvé sur la période d'apprentissage")
    report = FilterReport(
        samples=len(samples),
        train_samples=len(train),
        test_samples=len(test),
        test_auc=test_auc,
        threshold=threshold,
        test_kept_pct=kept_pct,
        test_pf_all=pf_all,
        test_pf_kept=pf_kept,
        test_pnl_all=sum(all_pnl),
        test_pnl_kept=sum(kept),
        useful=not reasons,
        reason="; ".join(reasons) or "améliore les résultats sur une période jamais vue",
    )
    return SignalModel(booster, threshold, report)


def ml_filter(
    model: SignalModel, strategy_id: str, timeframe: Timeframe, features: FeatureTracker
) -> SignalFilter:
    """Règle de risque : refuse les signaux de `strategy_id` jugés trop peu probables."""

    def check(intent: OrderIntent) -> Rejection | None:
        if intent.strategy_id != strategy_id:
            return None
        f = features.features(intent.symbol, timeframe)
        if f is None:
            return None  # pas assez d'historique : on ne filtre pas
        p = model.probability(f)
        if math.isfinite(p) and p < model.threshold:
            return Rejection(
                "ml_filter", f"probabilité de réussite {p:.2f} < seuil {model.threshold:.2f}"
            )
        return None

    return check


def build_filters(
    strategies: Sequence[Any], settings: Sequence[Any], features: FeatureTracker
) -> list[SignalFilter]:
    """Filtres ML déclarés dans la configuration (`ml_filter` d'une stratégie)."""
    tf = {s.id: s.timeframe for s in strategies}
    out = []
    for st in settings:
        if st.enabled and st.ml_filter:
            out.append(ml_filter(SignalModel.load(st.ml_filter), st.id, tf[st.id], features))
    return out
