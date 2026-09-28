"""Sessions de marché, en heures UTC (approximation hors changement d'heure)."""

from datetime import datetime
from enum import StrEnum


class Session(StrEnum):
    TOKYO = "tokyo"
    LONDON = "london"
    NEW_YORK = "new_york"


HOURS: dict[Session, tuple[int, int]] = {
    Session.TOKYO: (0, 9),
    Session.LONDON: (7, 16),
    Session.NEW_YORK: (12, 21),
}


def in_sessions(ts: datetime, sessions: list[Session]) -> bool:
    """Vrai si `ts` tombe dans une des sessions ; toujours vrai si la liste est vide."""
    if not sessions:
        return True
    return any(start <= ts.hour < end for start, end in (HOURS[s] for s in sessions))
