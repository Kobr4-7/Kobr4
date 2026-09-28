"""Commandes `kobr4 lab …` : optimiser, lister, accepter, refuser, appliquer."""

import argparse
import asyncio
import getpass
import html
import logging
import shutil
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import yaml

from kobr4.backtest.engine import load_bars, run_backtest
from kobr4.config import ConfigError, load_settings
from kobr4.intel.ml import samples_from_backtest, train_filter
from kobr4.lab.evaluate import strategy_only
from kobr4.lab.optimizer import LabSettings, Optimizer
from kobr4.lab.proposals import PeriodResult, Proposal, ProposalStatus, ProposalStore
from kobr4.marketdata.cli import is_synthetic
from kobr4.marketdata.store import ParquetBarStore
from kobr4.paths import data_dir, lab_dir

log = logging.getLogger("kobr4.lab")


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"date invalide : {value} (format AAAA-MM-JJ)") from None


def add_parser(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    lab = sub.add_parser("lab", help="laboratoire : optimisation et propositions de réglages")
    lab.add_argument("--dir", type=Path, default=lab_dir(), help="dossier des propositions")
    cmds = lab.add_subparsers(dest="lab_cmd", required=True)

    opt = cmds.add_parser("optimize", help="optimiser une stratégie (walk-forward)")
    opt.add_argument("--config", required=True)
    opt.add_argument("--strategy", required=True, help="identifiant de la stratégie")
    opt.add_argument("--store", type=Path, default=data_dir())
    opt.add_argument("--start", type=_date, required=True)
    opt.add_argument("--end", type=_date, required=True, help="dernier jour inclus")
    opt.add_argument("--train-months", type=int, default=24)
    opt.add_argument("--test-months", type=int, default=6)
    opt.add_argument("--holdout-months", type=int, default=12)
    opt.add_argument("--trials", type=int, default=60)
    opt.add_argument("--objective", choices=["sharpe", "calmar", "profit_factor"], default="sharpe")
    opt.add_argument("--workers", type=int, default=None)

    tf = cmds.add_parser(
        "train-filter", help="entraîner un filtre ML sur les trades d'une stratégie"
    )
    tf.add_argument("--config", required=True)
    tf.add_argument("--strategy", required=True)
    tf.add_argument("--store", type=Path, default=data_dir())
    tf.add_argument("--start", type=_date, required=True)
    tf.add_argument("--end", type=_date, required=True)
    tf.add_argument(
        "--out",
        type=Path,
        default=None,
        help="dossier du modèle (défaut : <dir>/models/<stratégie>)",
    )

    cmds.add_parser("list", help="lister les propositions")
    for name, help_ in (
        ("approve", "accepter une proposition"),
        ("reject", "refuser une proposition"),
    ):
        p = cmds.add_parser(name, help=help_)
        p.add_argument("id")
        p.add_argument("--note", default="")
    ap = cmds.add_parser(
        "apply", help="appliquer une proposition acceptée à un fichier de configuration"
    )
    ap.add_argument("id")
    ap.add_argument("--config", required=True, type=Path)


def run(args: argparse.Namespace) -> int:
    store = ProposalStore(args.dir)
    match args.lab_cmd:
        case "optimize":
            return _optimize(store, args)
        case "list":
            return _list(store)
        case "train-filter":
            return _train_filter(args)
        case "approve" | "reject":
            p = store.decide(
                args.id, args.lab_cmd == "approve", getpass.getuser(), datetime.now(UTC), args.note
            )
            print(f"{p.id} : {p.status}")
            return 0
        case "apply":
            return _apply(store, args)
    raise AssertionError(args.lab_cmd)


def _optimize(store: ProposalStore, args: argparse.Namespace) -> int:
    try:
        settings = load_settings(args.config)
    except ConfigError as e:
        log.error("%s", e)
        return 2
    if is_synthetic(ParquetBarStore(args.store)):
        log.warning("historique SYNTHÉTIQUE : la proposition ne dira rien du marché réel")
    kw: dict[str, object] = {
        "train_months": args.train_months,
        "test_months": args.test_months,
        "holdout_months": args.holdout_months,
        "trials": args.trials,
        "objective": args.objective,
    }
    if args.workers:
        kw["workers"] = args.workers
    lab = LabSettings.model_validate(kw)
    start = datetime.combine(args.start, time(), UTC)
    end = datetime.combine(args.end + timedelta(days=1), time(), UTC)
    opt = Optimizer(settings, args.strategy, str(args.store), lab, progress=print)
    proposal = opt.run(start, end)
    path = store.save(proposal)
    report = path.with_suffix(".html")
    report.write_text(render_proposal(proposal), encoding="utf-8")
    print()
    print(f"Proposition {proposal.id} : {proposal.status}")
    for f in proposal.failures:
        print(f"  ✗ {f}")
    print(f"Réglages proposés : {proposal.proposed_params}")
    print(f"Rapport : {report}")
    return 0


def _train_filter(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    s = strategy_only(settings, args.strategy, {})
    s = s.model_copy(
        update={"strategies": [s.strategies[0].model_copy(update={"ml_filter": None})]}
    )
    start = datetime.combine(args.start, time(), UTC)
    end = datetime.combine(args.end + timedelta(days=1), time(), UTC)
    bars = load_bars(ParquetBarStore(args.store), s, start, end)
    if not all(bars.values()):
        log.error("historique incomplet dans %s/", args.store)
        return 1
    result = asyncio.run(run_backtest(s, bars, use_ml_filters=False))
    samples = samples_from_backtest(result, args.strategy)
    try:
        model = train_filter(samples)
    except ValueError as e:
        log.error("%s", e)
        return 1
    r = model.report
    out = args.out or (args.dir / "models" / args.strategy)
    model.save(out)
    print(f"{r.samples} trades : {r.train_samples} pour apprendre, {r.test_samples} pour tester")
    print(f"AUC sur la période de test : {r.test_auc:.2f} · seuil {r.threshold:.2f}")
    print(
        f"Période de test : profit factor {r.test_pf_all:.2f} → {r.test_pf_kept:.2f} avec le filtre,"
        f" {r.test_kept_pct:.0f} % des trades gardés, P&L {r.test_pnl_all:+.0f} → {r.test_pnl_kept:+.0f}"
    )
    print(("Filtre UTILE : " if r.useful else "Filtre NON retenu : ") + r.reason)
    print(f"Modèle enregistré dans {out}")
    if r.useful:
        print(
            f"Pour l'utiliser : ajouter `ml_filter: {out}` à la stratégie {args.strategy}, puis la faire tourner en démo."
        )
    return 0


def _list(store: ProposalStore) -> int:
    items = store.list()
    if not items:
        print("Aucune proposition.")
    for p in items:
        print(
            f"{p.id}  {p.status:<9} OOS {p.oos_total_return_pct:+6.2f} %  Sharpe {p.oos_sharpe:5.2f}"
            f"  stabilité {p.stability:.2f}  DSR {p.deflated_sharpe:.2f}"
        )
    return 0


def _apply(store: ProposalStore, args: argparse.Namespace) -> int:
    p = store.get(args.id)
    if p.status is not ProposalStatus.APPROVED:
        log.error("la proposition %s n'est pas acceptée (%s)", p.id, p.status)
        return 2
    settings = load_settings(args.config)
    raw = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    for i, st in enumerate(settings.strategies):
        if st.id == p.strategy_id:
            updated = p.apply_to(st)
            raw["strategies"][i] = updated.model_dump(mode="json", exclude_defaults=False)
            break
    else:
        log.error("stratégie %s absente de %s", p.strategy_id, args.config)
        return 2
    backup = args.config.with_suffix(args.config.suffix + f".v{updated.version - 1}.bak")
    shutil.copy2(args.config, backup)
    args.config.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    load_settings(args.config)
    store.mark_applied(p.id)
    print(
        f"{p.strategy_id} passe en version {updated.version} dans {args.config} (sauvegarde : {backup})"
    )
    print("À faire tourner en compte démo avant tout passage en réel.")
    return 0


def _row(r: PeriodResult) -> str:
    cls = "up" if r.total_return_pct > 0 else "down"
    return (
        f"<tr><td class='num'>{html.escape(r.period)}</td><td class='num r {cls}'>{r.total_return_pct:+.2f} %</td>"
        f"<td class='num r'>{r.sharpe:.2f}</td><td class='num r'>{r.max_drawdown_pct:.1f} %</td>"
        f"<td class='num r'>{r.trades}</td><td class='num'>{html.escape(str(r.params))}</td></tr>"
    )


def render_proposal(p: Proposal) -> str:
    ok = p.status is not ProposalStatus.FAILED
    verdict = (
        "<p class='ok'>Critères automatiques remplis : en attente de ta décision.</p>"
        if ok
        else "<p class='ko'>Critères non remplis :</p><ul>"
        + "".join(f"<li>{html.escape(f)}</li>" for f in p.failures)
        + "</ul>"
    )
    hold = "".join(
        f"<tr><td>{label}</td>{_row(r)[4:]}"
        for label, r in (("Proposé", p.holdout), ("Actuel", p.holdout_baseline))
        if r
    )
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Proposition {html.escape(p.strategy_id)}</title>
<style>
:root{{--bg:#EEF1F4;--surface:#fff;--line:#DCE2E8;--ink:#15202B;--muted:#5A6878;--up:#17885A;--down:#C43D36}}
@media (prefers-color-scheme:dark){{:root{{color-scheme:dark;--bg:#0E1318;--surface:#151C23;--line:#26313C;
--ink:#E3E9EF;--muted:#9AA7B4;--up:#3CC48A;--down:#EF6B63}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,sans-serif;padding:24px 16px}}
main{{max-width:1000px;margin:0 auto;display:flex;flex-direction:column;gap:16px}}
h1{{margin:0;font-size:24px}}h2{{margin:0 0 8px;font-size:16px}}
.panel{{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:14px 16px;overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}}
.num{{font-family:ui-monospace,monospace}}.r{{text-align:right}}.up{{color:var(--up)}}.down{{color:var(--down)}}
.ok{{color:var(--up);font-weight:600}}.ko{{color:var(--down);font-weight:600}}.muted{{color:var(--muted)}}
</style></head><body><main>
<div><div class="muted">Laboratoire Kobr4 FX · {p.created_at:%d/%m/%Y %H:%M} UTC</div>
<h1>{html.escape(p.strategy_id)} v{p.base_version} → v{p.base_version + 1}</h1></div>
<section class="panel">{verdict}
<table><tr><td>Hors échantillon (cumul des fenêtres de test)</td><td class="num r">{p.oos_total_return_pct:+.2f} %</td></tr>
<tr><td>Sharpe hors échantillon</td><td class="num r">{p.oos_sharpe:.2f}</td></tr>
<tr><td>Drawdown max hors échantillon</td><td class="num r">{p.oos_max_drawdown_pct:.1f} %</td></tr>
<tr><td>Trades hors échantillon</td><td class="num r">{p.oos_trades}</td></tr>
<tr><td>Stabilité des réglages voisins</td><td class="num r">{p.stability:.2f}</td></tr>
<tr><td>Sharpe dégonflé ({p.trials} essais)</td><td class="num r">{p.deflated_sharpe:.2f}</td></tr></table></section>
<section class="panel"><h2>Réglages</h2><table><tr><th>Actuels</th><td class="num">{html.escape(str(p.current_params))}</td></tr>
<tr><th>Proposés</th><td class="num">{html.escape(str(p.proposed_params))}</td></tr></table></section>
<section class="panel"><h2>Fenêtres walk-forward (périodes de test)</h2><table>
<tr><th>Période</th><th class="r">Rendement</th><th class="r">Sharpe</th><th class="r">Drawdown</th><th class="r">Trades</th><th>Réglage optimisé avant</th></tr>
{"".join(_row(w) for w in p.windows)}</table></section>
<section class="panel"><h2>Période réservée (jamais vue par l'optimisation)</h2><table>
<tr><th></th><th>Période</th><th class="r">Rendement</th><th class="r">Sharpe</th><th class="r">Drawdown</th><th class="r">Trades</th><th>Réglage</th></tr>
{hold or "<tr><td colspan='7' class='muted'>Aucune période réservée</td></tr>"}</table></section>
</main></body></html>"""
