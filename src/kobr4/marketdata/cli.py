"""Commandes `kobr4 data …` : téléchargement, import et contrôle de l'historique."""

import argparse
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl

from kobr4.core.instruments import INSTRUMENTS
from kobr4.marketdata.quality import check_m1
from kobr4.marketdata.sources import dukascopy, synthetic
from kobr4.marketdata.sources.histdata import read_histdata
from kobr4.marketdata.store import ParquetBarStore
from kobr4.paths import data_dir

log = logging.getLogger("kobr4.data")


def _symbols(value: str) -> list[str]:
    symbols = [s.strip().upper() for s in value.split(",") if s.strip()]
    unknown = [s for s in symbols if s not in INSTRUMENTS]
    if unknown:
        raise argparse.ArgumentTypeError(f"instruments inconnus : {', '.join(unknown)}")
    return symbols


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"date invalide : {value} (format AAAA-MM-JJ)") from None


def add_parser(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    data = sub.add_parser("data", help="historique des prix")
    data.add_argument("--store", type=Path, default=data_dir(), help="dossier de stockage")
    cmds = data.add_subparsers(dest="data_cmd", required=True)

    dl = cmds.add_parser("download", help="télécharger l'historique M1 depuis Dukascopy")
    dl.add_argument("--symbols", type=_symbols, required=True, help="ex. EUR/USD,GBP/USD")
    dl.add_argument("--start", type=_date, required=True)
    dl.add_argument("--end", type=_date, default=datetime.now(UTC).date() - timedelta(days=1))
    dl.add_argument("--workers", type=int, default=2)
    dl.add_argument("--force", action="store_true", help="retélécharger les jours déjà présents")

    imp = cmds.add_parser("import-histdata", help="importer des fichiers CSV M1 de HistData")
    imp.add_argument("files", type=Path, nargs="+")
    imp.add_argument("--symbol", type=_symbols, required=True)

    syn = cmds.add_parser(
        "synth", help="générer un historique synthétique (tests uniquement, pas le marché réel)"
    )
    syn.add_argument("--symbols", type=_symbols, required=True)
    syn.add_argument("--start", type=_date, required=True)
    syn.add_argument("--end", type=_date, required=True)
    syn.add_argument("--seed", type=int, default=0)

    info = cmds.add_parser("info", help="couverture et qualité de l'historique")
    info.add_argument("--symbols", type=_symbols, default=None)
    info.add_argument("--max-gap", type=int, default=30, help="trou signalé au-delà de N minutes")


def run(args: argparse.Namespace) -> int:
    store = ParquetBarStore(args.store)
    match args.data_cmd:
        case "download":
            return _download(store, args)
        case "import-histdata":
            return _import_histdata(store, args)
        case "synth":
            return _synth(store, args)
        case "info":
            return _info(store, args)
    raise AssertionError(args.data_cmd)


def _download(store: ParquetBarStore, args: argparse.Namespace) -> int:
    if args.end < args.start:
        log.error("--end (%s) est avant --start (%s)", args.end, args.start)
        return 2
    for symbol in args.symbols:
        skip = set() if args.force else store.days_present(symbol, args.start, args.end)
        batch: list[pl.DataFrame] = []
        rows = days = 0
        try:
            for day, df in dukascopy.download(
                symbol, args.start, args.end, skip=skip, workers=args.workers
            ):
                days += 1
                batch.append(df)
                if len(batch) >= 5:
                    rows += store.write_m1(symbol, pl.concat(batch))
                    batch.clear()
                    log.info("%s : %s atteint, %d bougies M1", symbol, day, rows)
        except (OSError, dukascopy.DataFormatError) as e:
            log.error(
                "%s : téléchargement interrompu (%s). Relancer reprend où il s'est arrêté.",
                symbol,
                e,
            )
            return 1
        finally:
            if batch:
                rows += store.write_m1(symbol, pl.concat(batch))
        log.info(
            "%s : %d jours téléchargés, %d bougies M1 (%d jours déjà présents)",
            symbol,
            days,
            rows,
            len(skip),
        )
    return 0


def _import_histdata(store: ParquetBarStore, args: argparse.Namespace) -> int:
    (symbol,) = args.symbol
    total = 0
    for f in args.files:
        df = read_histdata(f, symbol)
        total += store.write_m1(symbol, df)
        log.info("%s : %d bougies importées depuis %s", symbol, df.height, f.name)
    log.info("%s : %d bougies au total", symbol, total)
    return 0


def is_synthetic(store: ParquetBarStore) -> bool:
    return (store.root / synthetic.MARKER).exists()


def _synth(store: ParquetBarStore, args: argparse.Namespace) -> int:
    if store.symbols() and not is_synthetic(store):
        log.error(
            "%s contient des données réelles : choisir un autre dossier avec --store", store.root
        )
        return 2
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / synthetic.MARKER).write_text(
        "Données synthétiques générées par kobr4 data synth. Elles ne représentent pas le"
        " marché réel.\n",
        encoding="utf-8",
    )
    for symbol in args.symbols:
        df = synthetic.generate_m1(symbol, args.start, args.end, seed=args.seed)
        store.write_m1(symbol, df)
        log.info("%s : %d bougies M1 synthétiques", symbol, df.height)
    return 0


def _info(store: ParquetBarStore, args: argparse.Namespace) -> int:
    symbols = args.symbols or store.symbols()
    if not symbols:
        print(f"Aucun historique dans {args.store}/")
        return 0
    if is_synthetic(store):
        print(f"ATTENTION : {args.store}/ contient des données SYNTHÉTIQUES, pas le marché réel.")
    for symbol in symbols:
        cov = store.coverage(symbol)
        if cov is None:
            print(f"{symbol:8} aucune donnée")
            continue
        m1 = store.read_m1(symbol)
        report = check_m1(m1, max_gap=timedelta(minutes=args.max_gap))
        no_spread = m1.get_column("spread").null_count()
        print(
            f"{symbol:8} {cov.first:%Y-%m-%d %H:%M} → {cov.last:%Y-%m-%d %H:%M} UTC  "
            f"{cov.rows:>9,} bougies M1  "
            f"{len(report.gaps)} trous > {args.max_gap} min  "
            f"{report.invalid_bars} incohérentes  "
            f"{no_spread:,} sans spread".replace(",", " ")
        )
        for gap in sorted(report.gaps, key=lambda g: g.duration, reverse=True)[:5]:
            print(
                f"{'':9}trou : {gap.start:%Y-%m-%d %H:%M} → {gap.end:%Y-%m-%d %H:%M}"
                f" ({gap.duration})"
            )
    return 0
