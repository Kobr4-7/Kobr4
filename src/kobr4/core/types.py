"""Types partagés : dates UTC et énumérations du domaine."""

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("la date doit porter un fuseau horaire (UTC attendu)")
    return value.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(_ensure_utc)]
"""Date obligatoirement munie d'un fuseau, normalisée en UTC."""


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        """+1 pour un achat, -1 pour une vente."""
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class Timeframe(StrEnum):
    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    M30 = "M30"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"

    @property
    def duration(self) -> timedelta:
        return _TIMEFRAME_DURATIONS[self]


_TIMEFRAME_DURATIONS = {
    Timeframe.M1: timedelta(minutes=1),
    Timeframe.M5: timedelta(minutes=5),
    Timeframe.M15: timedelta(minutes=15),
    Timeframe.M30: timedelta(minutes=30),
    Timeframe.H1: timedelta(hours=1),
    Timeframe.H4: timedelta(hours=4),
    Timeframe.D1: timedelta(days=1),
}


class Regime(StrEnum):
    """Régime de marché (voir intel/features.py)."""

    TREND = "trend"
    RANGE = "range"
    VOLATILE = "volatile"
    UNKNOWN = "unknown"
