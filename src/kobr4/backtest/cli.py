"""Commande `kobr4 backtest`."""

import argparse
import asyncio
import logging
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from kobr4.backtest.engine import load_bars, run_backtest
from kobr4.backtest.report import write_report
from kobr4.config import ConfigError, load_settings
from kobr4.marketdata.cli import is_synthetic
from kobr4.marketdata.store import ParquetBarStore
from kobr4.paths import data_dir, reports_dir
from kobr4.risk.calendar import EconomicCalendar

log = logging.getLogger("kobr4.backtest")


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"date invalide : {value} (format AAAA-MM-JJ)") from None


def add_parser(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    p = sub.add_parser("backtest", help="rejouer l'historique avec les stratégies configurées")
    p.add_argument("--config", required=True, help="fichier de configuration YAML")
    p.add_argument("--store", type=Path, default=data_dir(), help="dossier de l'historique")
    p.add_argument("--start", type=_date, default=None)
    p.add_argument("--end", type=_date, default=None, help="dernier jour inclus")
    p.add_argument("--out", type=Path, default=reports_dir(), help="dossier des rapports")
    p.add_argument("--name", default=None, help="nom du rapport")


def _bounds(start: date | None, end: date | None) -> tuple[datetime | None, datetime | None]:
    s = datetime.combine(start, time(), UTC) if start else None
    e = datetime.combine(end + timedelta(days=1), time(), UTC) if end else None
    return s, e


def run(args: argparse.Namespace) -> int:
    try:
        settings = load_settings(args.config)
    except ConfigError as e:
        log.error("%s", e)
        return 2
    store = ParquetBarStore(args.store)
    start, end = _bounds(args.start, args.end)
    bars = load_bars(store, settings, start, end)
    missing = [s for s, b in bars.items() if not b]
    if missing:
        log.error(
            "aucune donnée pour %s dans %s/ : lancer d'abord `kobr4 data download`",
            ", ".join(missing),
            args.store,
        )
        return 1
    calendar = EconomicCalendar.load(settings.news_calendar) if settings.news_calendar else None
    synthetic = is_synthetic(store)
    if synthetic:
        log.warning("historique SYNTHÉTIQUE : le résultat ne dit rien du marché réel")

    result = asyncio.run(run_backtest(settings, bars, calendar))
    name = args.name or "-".join(s.id for s in settings.strategies if s.enabled)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    meta = {
        "name": name,
        "config": str(args.config),
        "synthetic": synthetic,
        "strategies": [s.model_dump(mode="json") for s in settings.strategies if s.enabled],
        "risk": settings.risk.model_dump(mode="json"),
        "costs": settings.backtest.model_dump(mode="json"),
    }
    path, m = write_report(result, args.out / f"{stamp}-{name}", meta)
    print(
        f"{name} : {m.trades} trades, rendement {m.total_return_pct:+.2f} %,"
        f" drawdown max {m.max_drawdown_pct:.2f} %, Sharpe {m.sharpe:.2f},"
        f" profit factor {m.profit_factor:.2f}"
    )
    print(f"Rapport : {path}")
    return 0
