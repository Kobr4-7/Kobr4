"""Statistiques anti-chance : Sharpe « dégonflé » et stabilité des paramètres."""

import math
from statistics import NormalDist

_EULER = 0.5772156649015329


def moments(returns: list[float]) -> tuple[float, float, float, float]:
    """Moyenne, écart type, asymétrie, aplatissement (non centré : 3 pour une loi normale)."""
    n = len(returns)
    if n < 2:
        return 0.0, 0.0, 0.0, 3.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / n
    sd = math.sqrt(var)
    if sd == 0:
        return mean, 0.0, 0.0, 3.0
    skew = sum((r - mean) ** 3 for r in returns) / n / sd**3
    kurt = sum((r - mean) ** 4 for r in returns) / n / sd**4
    return mean, sd, skew, kurt


def expected_max_sharpe(trial_sharpes: list[float]) -> float:
    """Sharpe maximal attendu par pur hasard après N essais (Bailey & López de Prado)."""
    n = len(trial_sharpes)
    if n < 2:
        return 0.0
    mean = sum(trial_sharpes) / n
    var = sum((s - mean) ** 2 for s in trial_sharpes) / (n - 1)
    z = NormalDist()
    return math.sqrt(var) * (
        (1 - _EULER) * z.inv_cdf(1 - 1 / n) + _EULER * z.inv_cdf(1 - 1 / (n * math.e))
    )


def deflated_sharpe(returns: list[float], trial_sharpes: list[float]) -> float:
    """Probabilité que le vrai Sharpe (par période) dépasse celui qu'on obtiendrait par
    chance après autant d'essais. Proche de 1 : résultat crédible ; < 0,95 : suspect.

    `returns` : rendements par période (jour) de la configuration retenue.
    `trial_sharpes` : Sharpe par période de chaque essai de l'optimisation.
    """
    t = len(returns)
    mean, sd, skew, kurt = moments(returns)
    if t < 3 or sd == 0:
        return 0.0
    sr = mean / sd
    sr0 = expected_max_sharpe(trial_sharpes)
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr**2
    if denom <= 0:
        return 0.0
    return NormalDist().cdf((sr - sr0) * math.sqrt(t - 1) / math.sqrt(denom))


def stability(best_score: float, neighbour_scores: list[float]) -> float:
    """Part des réglages voisins qui gardent au moins la moitié du score retenu (0 à 1).

    Un bon réglage est entouré de réglages presque aussi bons ; un pic isolé est un
    signe de sur-optimisation.
    """
    if not neighbour_scores or best_score <= 0:
        return 0.0
    return sum(s >= best_score / 2 for s in neighbour_scores) / len(neighbour_scores)
