"""Découpage de l'historique : fenêtres walk-forward et période réservée."""

from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime


def add_months(ts: datetime, months: int) -> datetime:
    m = ts.month - 1 + months
    year, month = ts.year + m // 12, m % 12 + 1
    return ts.replace(year=year, month=month, day=min(ts.day, monthrange(year, month)[1]))


@dataclass(frozen=True)
class Period:
    start: datetime
    end: datetime
    """Exclu."""

    def label(self) -> str:
        return f"{self.start:%Y-%m-%d} → {self.end:%Y-%m-%d}"


@dataclass(frozen=True)
class Window:
    train: Period
    test: Period


def walk_forward(
    start: datetime, end: datetime, train_months: int, test_months: int
) -> list[Window]:
    """Fenêtres successives : optimisation sur `train_months`, test sur les
    `test_months` suivants, puis décalage de `test_months`."""
    if train_months < 1 or test_months < 1:
        raise ValueError("durées en mois >= 1 attendues")
    windows = []
    train_start = start
    while True:
        train_end = add_months(train_start, train_months)
        test_end = add_months(train_end, test_months)
        if test_end > end:
            break
        windows.append(Window(Period(train_start, train_end), Period(train_end, test_end)))
        train_start = add_months(train_start, test_months)
    return windows


def split_holdout(
    start: datetime, end: datetime, holdout_months: int
) -> tuple[Period, Period | None]:
    """Sépare la période d'étude et la période réservée à la validation finale."""
    if holdout_months <= 0:
        return Period(start, end), None
    cut = add_months(end, -holdout_months)
    if cut <= start:
        raise ValueError("historique trop court pour la période réservée demandée")
    return Period(start, cut), Period(cut, end)
