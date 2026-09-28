"""Historique synthétique pour tester la chaîne complète sans données réelles.

Marche aléatoire M1 avec alternance de phases de tendance et de range, week-ends fermés
(vendredi 22h → dimanche 22h UTC) et spread réaliste. **Ces données ne disent rien du
marché réel** : un résultat de backtest sur données synthétiques ne valide aucune
stratégie. Le dossier de stockage est marqué par un fichier `SYNTHETIC`.
"""

from datetime import date, datetime, time

import numpy as np
import polars as pl

from kobr4.core.instruments import get_instrument
from kobr4.marketdata import frames

MARKER = "SYNTHETIC"

_START_PRICES = {
    "EUR/USD": 1.0850,
    "GBP/USD": 1.2700,
    "USD/JPY": 149.30,
    "USD/CHF": 0.8840,
    "AUD/USD": 0.6570,
    "USD/CAD": 1.3600,
    "NZD/USD": 0.6000,
    "EUR/GBP": 0.8540,
    "EUR/JPY": 162.00,
    "GBP/JPY": 189.60,
}


def _market_open(ts: np.ndarray) -> np.ndarray:
    """Masque des minutes où le marché est ouvert."""
    dt = ts.astype("datetime64[m]")
    weekday = (dt.astype("datetime64[D]").astype(np.int64) + 3) % 7  # 0 = lundi
    hour = (dt - dt.astype("datetime64[D]")).astype(np.int64) // 60
    closed = (weekday == 5) | ((weekday == 4) & (hour >= 22)) | ((weekday == 6) & (hour < 22))
    return np.asarray(~closed)


def generate_m1(
    symbol: str, start: date, end: date, seed: int = 0, daily_vol: float = 0.005
) -> pl.DataFrame:
    """Bougies M1 synthétiques de `start` à `end` inclus."""
    instrument = get_instrument(symbol)
    scale = 10**instrument.price_precision
    rng = np.random.default_rng([seed, *symbol.encode()])
    t0 = np.datetime64(datetime.combine(start, time()), "m")
    n = int((end - start).days + 1) * 1440
    ts = t0 + np.arange(n)
    mask = _market_open(ts)
    ts = ts[mask]
    m = ts.size

    sigma = daily_vol / np.sqrt(1440)
    # Phases de 2 à 20 jours : tendance haussière, baissière ou range.
    drift = np.empty(m)
    i = 0
    while i < m:
        length = int(rng.integers(2, 21)) * 1320
        regime = rng.choice([-1.0, 0.0, 0.0, 1.0])
        drift[i : i + length] = regime * sigma * rng.uniform(0.002, 0.006)
        i += length
    # Volatilité plus forte pendant Londres / New York.
    hour = ((ts - ts.astype("datetime64[D]")).astype(np.int64) // 60) % 24
    vol = sigma * np.where((hour >= 7) & (hour < 21), 1.3, 0.6)
    rets = drift + vol * rng.standard_normal(m)
    close = _START_PRICES.get(symbol, 1.0) * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[close[0] / np.exp(rets[0])], close[:-1]])
    wick = np.abs(rng.standard_normal((2, m))) * vol * close * 0.6
    high = np.maximum(open_, close) + wick[0]
    low = np.minimum(open_, close) - wick[1]
    pip_ticks = int(instrument.pip_size * scale)
    spread = np.maximum(1, np.round(pip_ticks * (0.5 + 0.5 * rng.random(m)))).astype(np.int64)

    def ticks(a: np.ndarray) -> np.ndarray:
        out: np.ndarray = np.round(a * scale).astype(np.int64)
        return out

    o, h, lo, c = ticks(open_), ticks(high), ticks(low), ticks(close)
    h = np.maximum(h, np.maximum(o, c))
    lo = np.minimum(lo, np.minimum(o, c))
    df = pl.DataFrame(
        {
            "open_time": pl.Series(ts.astype("datetime64[us]")).dt.replace_time_zone("UTC"),
            "open": o,
            "high": h,
            "low": lo,
            "close": c,
            "spread": spread,
            "volume": rng.uniform(1, 50, m),
        }
    )
    return frames.conform(df)
