"""Historique gratuit de Dukascopy : bougies M1 bid et ask, par jour.

URL d'un fichier : `https://datafeed.dukascopy.com/datafeed/EURUSD/2024/00/02/BID_candles_min_1.bi5`
(le mois commence à 00). Fichier compressé en LZMA, une bougie par enregistrement de
24 octets big-endian : secondes depuis minuit UTC, open, close, low, high (entiers, prix
multiplié par 10^précision), volume (float32). Un fichier vide signifie « pas de données ».

Dukascopy remplit les minutes sans cotation avec des bougies plates de volume nul :
elles sont retirées.
"""

import logging
import lzma
import struct
import threading
import time as systime
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

import polars as pl

from kobr4.core.instruments import get_instrument
from kobr4.marketdata import frames

log = logging.getLogger(__name__)

BASE_URL = "https://datafeed.dukascopy.com/datafeed"
_RECORD = struct.Struct(">5if")

Side = Literal["BID", "ASK"]
Fetcher = Callable[[str], bytes]


class DataFormatError(ValueError):
    pass


def candles_url(symbol: str, day: date, side: Side) -> str:
    code = symbol.replace("/", "")
    return (
        f"{BASE_URL}/{code}/{day.year}/{day.month - 1:02d}/{day.day:02d}/{side}_candles_min_1.bi5"
    )


def parse_candles(raw: bytes, day: date) -> pl.DataFrame:
    """Décode un fichier de bougies M1. Colonnes : open_time, open, high, low, close, volume."""
    schema = {k: frames.SCHEMA[k] for k in ("open_time", "open", "high", "low", "close", "volume")}
    if not raw:
        return pl.DataFrame(schema=schema)
    try:
        data = lzma.decompress(raw)
    except lzma.LZMAError as e:
        raise DataFormatError(f"{day} : fichier LZMA illisible ({e})") from e
    if len(data) % _RECORD.size:
        raise DataFormatError(f"{day} : taille {len(data)} non multiple de {_RECORD.size}")
    midnight = datetime.combine(day, time(), UTC)
    rows = []
    for secs, o, c, lo, hi, vol in _RECORD.iter_unpack(data):
        if not (0 <= secs < 86_400 and 0 < lo <= min(o, c) and hi >= max(o, c)):
            raise DataFormatError(
                f"{day} : bougie incohérente (t={secs}, o={o}, h={hi}, l={lo}, c={c})"
            )
        rows.append((midnight + timedelta(seconds=secs), o, hi, lo, c, float(vol)))
    return pl.DataFrame(rows, schema=schema, orient="row")


class _Throttle:
    """Espace les requêtes vers Dukascopy, tous fils d'exécution confondus."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = systime.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.interval
        if delay > 0:
            systime.sleep(delay)

    def slow_down(self) -> None:
        with self._lock:
            self.interval = min(self.interval * 2, 5.0)


_throttle = _Throttle(0.25)


def _retry_after(e: urllib.error.HTTPError, attempt: int) -> float:
    default = min(10.0 * float(2**attempt), 120.0)
    value = str(e.headers.get("Retry-After") or "") if e.headers else ""
    try:
        return max(float(value), 1.0) if value else default
    except ValueError:
        return default


def http_fetch(url: str, retries: int = 8, timeout: float = 30) -> bytes:
    """Télécharge une URL. Un 404 vaut « pas de données ».

    Dukascopy limite le débit (HTTP 429) : les requêtes sont espacées, et après un 429
    on patiente (10 s, 20 s, 40 s… jusqu'à 2 min) en ralentissant le rythme.
    """
    for attempt in range(retries):
        _throttle.wait()
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                body: bytes = resp.read()
                return body
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return b""
            if attempt == retries - 1:
                raise
            if e.code == 429:
                _throttle.slow_down()
                wait = _retry_after(e, attempt)
                log.info("Dukascopy demande de ralentir, pause de %.0f s", wait)
                systime.sleep(wait)
                continue
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == retries - 1:
                raise
        systime.sleep(2**attempt)
    raise AssertionError("inaccessible")


def fetch_day(symbol: str, day: date, fetch: Fetcher | None = None) -> pl.DataFrame:
    """Bougies M1 d'une journée UTC, au format `frames.SCHEMA`, avec le spread."""
    get_instrument(symbol)
    fetch = fetch or http_fetch
    bid = parse_candles(fetch(candles_url(symbol, day, "BID")), day)
    ask = parse_candles(fetch(candles_url(symbol, day, "ASK")), day)
    bid = bid.filter(pl.col("volume") > 0)
    if bid.is_empty():
        return frames.empty_frame()
    df = bid.join(
        ask.select("open_time", pl.col("close").alias("ask_close")), on="open_time", how="left"
    ).with_columns(
        pl.when(pl.col("ask_close") >= pl.col("close"))
        .then(pl.col("ask_close") - pl.col("close"))
        .alias("spread")
    )
    return frames.conform(df)


def _days(start: date, end: date) -> Iterator[date]:
    d = start
    while d <= end:
        if d.weekday() != 5:  # samedi : marché fermé
            yield d
        d += timedelta(days=1)


def download(
    symbol: str,
    start: date,
    end: date,
    skip: set[date] | None = None,
    workers: int = 2,
    fetch: Fetcher | None = None,
) -> Iterator[tuple[date, pl.DataFrame]]:
    """Télécharge les jours de [start, end] absents de `skip`, en parallèle.

    Les résultats arrivent dans l'ordre des jours.
    """
    days = [d for d in _days(start, end) if d not in (skip or set())]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        yield from zip(days, pool.map(lambda d: fetch_day(symbol, d, fetch), days), strict=True)
