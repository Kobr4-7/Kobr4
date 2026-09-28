"""Horloges. Aucun module n'appelle `datetime.now()` directement : il reçoit une `Clock`.

En réel, `LiveClock` donne l'heure du système. En backtest, `SimulatedClock` est avancée
par le moteur de rejeu au rythme des données historiques.
"""

from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta


class Clock(ABC):
    @abstractmethod
    def now(self) -> datetime:
        """Heure courante, en UTC."""


class LiveClock(Clock):
    def now(self) -> datetime:
        return datetime.now(UTC)


class SimulatedClock(Clock):
    def __init__(self, start: datetime) -> None:
        self._now = _require_utc(start)

    def now(self) -> datetime:
        return self._now

    def set(self, ts: datetime) -> None:
        ts = _require_utc(ts)
        if ts < self._now:
            raise ValueError(f"l'horloge ne peut pas reculer ({self._now} → {ts})")
        self._now = ts

    def advance(self, delta: timedelta) -> None:
        self.set(self._now + delta)


def _require_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("la date doit porter un fuseau horaire (UTC attendu)")
    return ts.astimezone(UTC)
