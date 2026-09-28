"""Point d'entrée : charge la configuration et assemble les modules.

Phase 0 : seuls le bus, l'horloge et la configuration existent. Les modules (données de
marché, stratégies, risque, exécution) seront branchés ici au fil des phases.
"""

import argparse
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from kobr4 import __version__
from kobr4.config import ConfigError, Mode, Settings, load_settings
from kobr4.core.bus import EventBus, InMemoryEventBus
from kobr4.core.clock import Clock, LiveClock, SimulatedClock
from kobr4.marketdata import cli as data_cli

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
    data_cli.add_parser(sub)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s : %(message)s"
    )
    if args.cmd == "data":
        return data_cli.run(args)
    return _run(args.config)


def _run(config: str) -> int:
    try:
        settings = load_settings(config)
    except ConfigError as e:
        log.error("%s", e)
        return 2

    if settings.mode is not Mode.BACKTEST:
        build_app(settings)
    enabled = [s.id for s in settings.strategies if s.enabled]
    log.info(
        "Kobr4 %s prêt : mode %s, courtier %s (%s), %d instruments, stratégies : %s",
        __version__,
        settings.mode,
        settings.broker.name,
        settings.broker.environment,
        len(settings.instruments),
        ", ".join(enabled) or "aucune",
    )
    if settings.mode is Mode.LIVE:
        log.warning("MODE RÉEL : les ordres engagent de l'argent réel.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
