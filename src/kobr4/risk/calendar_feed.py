"""Récupération du calendrier économique de la semaine (flux public de Forex Factory).

Format : liste d'objets `{"title", "country", "date", "impact", ...}`, `country` étant
la devise (USD, EUR…) et `impact` High / Medium / Low / Holiday.
"""

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from kobr4.risk.calendar import EconomicCalendar, Impact, NewsEvent

log = logging.getLogger(__name__)

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_IMPACTS = {"high": Impact.HIGH, "medium": Impact.MEDIUM, "low": Impact.LOW}


def parse_feed(items: list[dict[str, Any]]) -> list[NewsEvent]:
    events = []
    for item in items:
        impact = _IMPACTS.get(str(item.get("impact", "")).lower())
        currency = str(item.get("country", "")).upper()
        if impact is None or len(currency) != 3:
            continue
        try:
            ts = datetime.fromisoformat(str(item["date"])).astimezone(UTC)
        except (KeyError, ValueError):
            continue
        events.append(
            NewsEvent(ts=ts, currency=currency, impact=impact, title=str(item.get("title", "")))
        )
    return events


async def fetch_calendar(client: httpx.AsyncClient, url: str = FEED_URL) -> EconomicCalendar:
    resp = await client.get(url, timeout=15)
    resp.raise_for_status()
    return EconomicCalendar(parse_feed(resp.json()))
