"""Point d'entrée : charge la configuration et assemble les modules.

Phase 0 : seuls le bus, l'horloge et la configuration existent. Les modules (données de
marché, stratégies, risque, exécution) seront branchés ici au fil des phases.
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from kobr4 import __version__
from kobr4.backtest import cli as backtest_cli
from kobr4.config import ConfigError, Mode, Settings, load_settings
from kobr4.core.bus import EventBus, InMemoryEventBus
from kobr4.core.clock import Clock, LiveClock, SimulatedClock
from kobr4.lab import cli as lab_cli
from kobr4.live.standalone import run_standalone
from kobr4.marketdata import cli as data_cli
from kobr4.web import cli as server_cli

log = logging.getLogger("kobr4")


@dataclass(frozen=True)
class App:
    settings: Settings
    bus: EventBus
    clock: Clock


def build_app(settings: Settings, start: datetime | None = None) -> App:
    clock: Clock
    if settings.mode is Mode.BACKTEST:
        if start is None:
            raise ValueError("le mode backtest exige une date de départ")
        clock = SimulatedClock(start)
    else:
        clock = LiveClock()
    return App(settings=settings, bus=InMemoryEventBus(), clock=clock)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kobr4", description="Bot de trading forex Kobr4 FX")
    parser.add_argument("--version", action="version", version=f"kobr4 {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="démarrer le bot")
    run_p.add_argument("--config", required=True, help="fichier de configuration YAML")
    run_p.add_argument(
        "--check", action="store_true", help="vérifier la configuration sans démarrer"
    )
    data_cli.add_parser(sub)
    backtest_cli.add_parser(sub)
    lab_cli.add_parser(sub)
    server_cli.add_parser(sub)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s : %(message)s"
    )
    # httpx journalise chaque URL appelée, clés d'API comprises (Finnhub, Telegram…).
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if args.cmd == "data":
        return data_cli.run(args)
    if args.cmd == "backtest":
        return backtest_cli.run(args)
    if args.cmd == "lab":
        return lab_cli.run(args)
    if args.cmd == "server":
        return server_cli.run(args)
    return _run(args.config, args.check)


def _run(config: str, check_only: bool) -> int:
    try:
        settings = load_settings(config)
    except ConfigError as e:
        log.error("%s", e)
        return 2
    enabled = [s.id for s in settings.strategies if s.enabled]
    log.info(
        "Kobr4 %s : mode %s, courtier %s (%s), %d instruments, stratégies : %s",
        __version__,
        settings.mode,
        settings.broker.name,
        settings.broker.environment,
        len(settings.instruments),
        ", ".join(enabled) or "aucune",
    )
    if check_only:
        log.info("configuration valide")
        return 0
    if settings.mode is Mode.BACKTEST:
        log.error("configuration de backtest : utiliser `kobr4 backtest --config %s`", config)
        return 2
    try:
        return asyncio.run(run_standalone(settings))
    except (ValueError, ConfigError) as e:
        log.error("%s", e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
