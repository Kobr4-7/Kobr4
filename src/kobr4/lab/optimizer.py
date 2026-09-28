"""Optimisation walk-forward d'une stratégie, jusqu'à une proposition de réglage.

Déroulé :
1. l'historique est coupé en période d'étude et période réservée (jamais vue par
   l'optimisation) ;
2. sur chaque fenêtre walk-forward de la période d'étude, on cherche le meilleur
   réglage (Optuna) sur la partie « entraînement », puis on le mesure sur la partie
   « test » qui suit : ce sont les résultats hors échantillon ;
3. le réglage proposé est optimisé sur la fenêtre la plus récente, puis on mesure sa
   stabilité (réglages voisins) et son Sharpe dégonflé (nombre d'essais) ;
4. il est évalué une seule fois sur la période réservée, à côté des réglages actuels ;
5. les critères d'acceptation décident s'il est soumis à un humain ou archivé.
"""

import logging
import math
import os
import uuid
from collections.abc import Callable
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import optuna
from pydantic import BaseModel, ConfigDict, Field

from kobr4.config.settings import Settings
from kobr4.lab.evaluate import EvalRequest, EvalResult, evaluate
from kobr4.lab.proposals import AcceptanceCriteria, PeriodResult, Proposal, ProposalStatus
from kobr4.lab.stats import deflated_sharpe, stability
from kobr4.lab.windows import Period, Window, add_months, split_holdout, walk_forward
from kobr4.strategies import STRATEGIES
from kobr4.strategies.base import ParamRange

log = logging.getLogger(__name__)
optuna.logging.set_verbosity(optuna.logging.WARNING)


class LabSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    train_months: int = Field(default=24, ge=1)
    test_months: int = Field(default=6, ge=1)
    holdout_months: int = Field(default=12, ge=0)
    trials: int = Field(default=60, ge=2)
    objective: str = Field(default="sharpe", pattern="^(sharpe|calmar|profit_factor)$")
    min_trades: int = Field(default=10, ge=0)
    """En dessous, un essai est pénalisé (trop peu de trades pour conclure)."""
    workers: int = Field(default=max(1, (os.cpu_count() or 2) - 1), ge=1)
    seed: int = 42
    criteria: AcceptanceCriteria = AcceptanceCriteria()


Progress = Callable[[str], None]


@dataclass
class _Search:
    best_params: dict[str, Any]
    best: EvalResult
    trial_sharpes: list[float]


def _suggest(trial: optuna.trial.Trial, name: str, r: ParamRange) -> float | int:
    if r.integer:
        return trial.suggest_int(name, int(r.low), int(r.high), step=int(r.step or 1))
    return trial.suggest_float(name, r.low, r.high, step=r.step)


def _neighbours(params: dict[str, Any], space: dict[str, ParamRange]) -> list[dict[str, Any]]:
    """Réglages voisins : chaque paramètre décalé d'un cran (ou de 10 %) dans les deux sens."""
    out = []
    for name, r in space.items():
        if name not in params:
            continue
        v = params[name]
        delta: float = r.step or (1 if r.integer else abs(v) * 0.1 or 0.1)
        if r.integer:
            delta = max(1, round(abs(v) * 0.1), int(delta))
        for sign in (-1, 1):
            nv = v + sign * delta
            if r.low <= nv <= r.high:
                out.append({**params, name: int(nv) if r.integer else round(nv, 6)})
    return out


class Optimizer:
    def __init__(
        self,
        settings: Settings,
        strategy_id: str,
        store: str,
        lab: LabSettings,
        progress: Progress | None = None,
        executor: Executor | None = None,
    ) -> None:
        matches = [s for s in settings.strategies if s.id == strategy_id]
        if not matches:
            raise ValueError(f"stratégie inconnue : {strategy_id}")
        self.strategy = matches[0]
        self.space = STRATEGIES[self.strategy.kind].search_space
        if not self.space:
            raise ValueError(f"aucun paramètre optimisable pour {self.strategy.kind}")
        self.settings = settings
        self.settings_json = settings.model_dump_json()
        self.store = store
        self.lab = lab
        self.progress = progress or (lambda msg: log.info("%s", msg))
        self._executor = executor

    # Évaluations

    def _request(self, params: dict[str, Any], period: Period) -> EvalRequest:
        return EvalRequest(
            settings_json=self.settings_json,
            strategy_id=self.strategy.id,
            params=tuple(sorted(params.items())),
            store=self.store,
            start=period.start,
            end=period.end,
        )

    def _run(
        self, executor: Executor, jobs: list[tuple[dict[str, Any], Period]]
    ) -> list[EvalResult]:
        return list(executor.map(evaluate, [self._request(p, per) for p, per in jobs]))

    def _search(self, executor: Executor, period: Period) -> _Search:
        study = optuna.create_study(
            direction="maximize", sampler=optuna.samplers.TPESampler(seed=self.lab.seed)
        )
        study.enqueue_trial({k: v for k, v in self.strategy.params.items() if k in self.space})
        results: dict[int, tuple[dict[str, Any], EvalResult]] = {}
        done = 0
        while done < self.lab.trials:
            batch = [study.ask() for _ in range(min(self.lab.workers, self.lab.trials - done))]
            params = [{n: _suggest(t, n, r) for n, r in self.space.items()} for t in batch]
            for trial, p, res in zip(
                batch, params, self._run(executor, [(p, period) for p in params]), strict=True
            ):
                score = res.score(self.lab.objective, self.lab.min_trades)
                if math.isfinite(score):
                    study.tell(trial, score)
                    results[trial.number] = (p, res)
                else:
                    study.tell(trial, state=optuna.trial.TrialState.PRUNED)
            done += len(batch)
        if not results:
            raise RuntimeError("aucun réglage valide trouvé")
        best_number = max(
            results, key=lambda n: results[n][1].score(self.lab.objective, self.lab.min_trades)
        )
        best_params, best = results[best_number]
        return _Search(best_params, best, [r.period_sharpe for _, r in results.values()])

    # Déroulé complet

    def run(self, start: datetime, end: datetime) -> Proposal:
        lab = self.lab
        study_period, holdout = split_holdout(start, end, lab.holdout_months)
        windows = walk_forward(
            study_period.start, study_period.end, lab.train_months, lab.test_months
        )
        if not windows:
            raise ValueError(
                f"période d'étude {study_period.label()} trop courte pour"
                f" {lab.train_months} + {lab.test_months} mois"
            )
        own = self._executor is None
        executor: Executor = self._executor or (
            ProcessPoolExecutor(lab.workers) if lab.workers > 1 else ThreadPoolExecutor(1)
        )
        try:
            return self._run_all(executor, study_period, holdout, windows)
        finally:
            if own:
                executor.shutdown()

    def _run_all(
        self,
        executor: Executor,
        study_period: Period,
        holdout: Period | None,
        windows: list[Window],
    ) -> Proposal:
        lab = self.lab
        oos: list[PeriodResult] = []
        oos_returns: list[float] = []
        oos_growth = 1.0
        for i, w in enumerate(windows, 1):
            self.progress(f"fenêtre {i}/{len(windows)} : optimisation sur {w.train.label()}")
            search = self._search(executor, w.train)
            (test,) = self._run(executor, [(search.best_params, w.test)])
            oos.append(_period(w.test, search.best_params, test))
            oos_returns.extend(test.daily_returns)
            oos_growth *= 1 + test.total_return_pct / 100
            self.progress(
                f"  test {w.test.label()} : {test.total_return_pct:+.2f} %,"
                f" Sharpe {test.sharpe:.2f}, {test.trades} trades"
            )

        final_train = Period(
            max(study_period.start, add_months(study_period.end, -lab.train_months)),
            study_period.end,
        )
        self.progress(f"réglage final : optimisation sur {final_train.label()}")
        final = self._search(executor, final_train)
        neighbours = _neighbours(final.best_params, self.space)
        n_scores = [
            r.score(lab.objective, lab.min_trades)
            for r in self._run(executor, [(p, final_train) for p in neighbours])
        ]
        stab = stability(final.best.score(lab.objective, lab.min_trades), n_scores)
        dsr = deflated_sharpe(list(final.best.daily_returns), final.trial_sharpes)

        holdout_res = baseline = None
        if holdout is not None:
            self.progress(f"validation sur la période réservée {holdout.label()}")
            current = {k: v for k, v in self.strategy.params.items() if k in self.space}
            h, b = self._run(executor, [(final.best_params, holdout), (current, holdout)])
            holdout_res = _period(holdout, final.best_params, h)
            baseline = _period(holdout, current, b)

        oos_sharpe = _sharpe(oos_returns)
        oos_dd = max((w.max_drawdown_pct for w in oos), default=0.0)
        oos_trades = sum(w.trades for w in oos)
        c = lab.criteria
        failures = []
        if oos_sharpe < c.min_oos_sharpe:
            failures.append(f"Sharpe hors échantillon {oos_sharpe:.2f} < {c.min_oos_sharpe}")
        if oos_dd > c.max_oos_drawdown_pct:
            failures.append(
                f"drawdown hors échantillon {oos_dd:.1f} % > {c.max_oos_drawdown_pct} %"
            )
        if oos_trades < c.min_oos_trades:
            failures.append(f"{oos_trades} trades hors échantillon < {c.min_oos_trades}")
        if stab < c.min_stability:
            failures.append(f"stabilité {stab:.2f} < {c.min_stability}")
        if dsr < c.min_deflated_sharpe:
            failures.append(f"Sharpe dégonflé {dsr:.2f} < {c.min_deflated_sharpe}")
        if (
            c.holdout_must_be_positive
            and holdout_res is not None
            and holdout_res.total_return_pct <= 0
        ):
            failures.append(f"période réservée négative ({holdout_res.total_return_pct:+.2f} %)")

        now = datetime.now(UTC)
        return Proposal(
            id=f"{now:%Y%m%d-%H%M%S}-{self.strategy.id}-{uuid.uuid4().hex[:6]}",
            created_at=now,
            strategy_id=self.strategy.id,
            kind=self.strategy.kind,
            base_version=self.strategy.version,
            current_params=dict(self.strategy.params),
            proposed_params=final.best_params,
            objective=lab.objective,
            trials=lab.trials,
            windows=oos,
            oos_total_return_pct=(oos_growth - 1) * 100,
            oos_sharpe=oos_sharpe,
            oos_max_drawdown_pct=oos_dd,
            oos_trades=oos_trades,
            holdout=holdout_res,
            holdout_baseline=baseline,
            stability=stab,
            deflated_sharpe=dsr,
            criteria=c,
            failures=failures,
            status=ProposalStatus.FAILED if failures else ProposalStatus.PROPOSED,
        )


def _sharpe(daily: list[float]) -> float:
    if len(daily) < 2:
        return 0.0
    mean = sum(daily) / len(daily)
    sd = math.sqrt(sum((r - mean) ** 2 for r in daily) / (len(daily) - 1))
    return mean / sd * math.sqrt(252) if sd > 0 else 0.0


def _period(period: Period, params: dict[str, Any], r: EvalResult) -> PeriodResult:
    return PeriodResult(
        period=period.label(),
        params=params,
        trades=r.trades,
        total_return_pct=r.total_return_pct,
        max_drawdown_pct=r.max_drawdown_pct,
        sharpe=r.sharpe,
        profit_factor=r.profit_factor,
        win_rate_pct=r.win_rate_pct,
    )
