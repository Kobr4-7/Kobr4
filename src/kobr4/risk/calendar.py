"""Calendrier des annonces économiques, pour suspendre les entrées autour des annonces
à fort impact.

Format du fichier YAML :

    - ts: 2026-10-02T12:30:00Z
      currency: USD
      impact: high
      title: Emplois non agricoles (NFP)
"""

from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, TypeAdapter

from kobr4.core.types import UtcDatetime


class Impact(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class NewsEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: UtcDatetime
    currency: str
    impact: Impact
    title: str


class EconomicCalendar:
    def __init__(self, events: list[NewsEvent] | None = None) -> None:
        self.events = sorted(events or [], key=lambda e: e.ts)

    @classmethod
    def load(cls, path: str | Path) -> "EconomicCalendar":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
        return cls(TypeAdapter(list[NewsEvent]).validate_python(raw))

    def blackout(self, symbol: str, ts: datetime, minutes: int) -> NewsEvent | None:
        """Annonce à fort impact sur une devise de la paire à moins de `minutes` de `ts`."""
        if minutes <= 0:
            return None
        window = timedelta(minutes=minutes)
        currencies = {symbol[:3], symbol[4:]}
        for e in self.events:
            if e.ts > ts + window:
                break
            if e.impact is Impact.HIGH and e.currency in currencies and abs(e.ts - ts) <= window:
                return e
        return None
