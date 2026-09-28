"""Rapport de backtest : JSON, CSV des trades et de l'équité, page HTML autonome."""

import csv
import html
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from kobr4.backtest.engine import BacktestResult
from kobr4.backtest.metrics import Metrics, by_regime, compute_metrics

_CSS = """
:root{--bg:#EEF1F4;--surface:#fff;--line:#DCE2E8;--ink:#15202B;--muted:#5A6878;--accent:#0B6E8A;
--up:#17885A;--down:#C43D36;--warn:#B9800C;--warn-soft:#F7ECD3;--up-soft:#DDF2E8;--down-soft:#F8E1DF}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--bg:#0E1318;--surface:#151C23;--line:#26313C;
--ink:#E3E9EF;--muted:#9AA7B4;--accent:#3BB0CF;--up:#3CC48A;--down:#EF6B63;--warn:#E3AA3A;--warn-soft:#3A2E14;
--up-soft:#133126;--down-soft:#3A1C1B}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.5 "IBM Plex Sans",system-ui,sans-serif;padding:24px 16px 48px}
main{max-width:1100px;margin:0 auto;display:flex;flex-direction:column;gap:18px}
h1{font:700 26px "IBM Plex Sans Condensed","Arial Narrow",sans-serif;margin:0}
h2{font:600 16px "IBM Plex Sans Condensed","Arial Narrow",sans-serif;margin:0 0 10px}
.muted{color:var(--muted)}.num{font-family:"IBM Plex Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums}
.r{text-align:right}.up{color:var(--up)}.down{color:var(--down)}
.label{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);font-weight:600}
.warn{background:var(--warn-soft);color:var(--warn);padding:10px 14px;border-radius:6px;margin:0;font-weight:600}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;background:var(--line);
border:1px solid var(--line);border-radius:8px;overflow:hidden}
.kpi{background:var(--surface);padding:12px 14px;display:flex;flex-direction:column;gap:4px}
.kpi .v{font:500 20px "IBM Plex Mono",ui-monospace,monospace}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:14px 16px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px}@media(max-width:760px){.grid2{grid-template-columns:1fr}}
.chart{width:100%;height:auto;display:block}.grid{stroke:var(--line)}
.axis{fill:var(--muted);font:11px "IBM Plex Mono",ui-monospace,monospace}
.eq-line{fill:none;stroke:var(--accent);stroke-width:1.6}.eq-area{fill:var(--accent);opacity:.1}
.eq-dot{fill:var(--accent)}.dd-line{fill:none;stroke:var(--down);stroke-width:1.2}.dd-area{fill:var(--down);opacity:.15}
.tw{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);
padding:6px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:6px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
.tag{font-size:11px;font-weight:700;padding:2px 6px;border-radius:4px}
.tag.buy{background:var(--up-soft);color:var(--up)}.tag.sell{background:var(--down-soft);color:var(--down)}
pre{font:12px "IBM Plex Mono",ui-monospace,monospace;white-space:pre-wrap;margin:0}
"""


def summary(result: BacktestResult, metrics: Metrics, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        **meta,
        "start": result.start.isoformat() if result.start else None,
        "end": result.end.isoformat() if result.end else None,
        "initial_balance": str(result.initial_balance),
        "final_equity": str(result.final_equity),
        "bars_processed": result.bars_processed,
        "metrics": metrics.as_dict(),
        "risk_rejections": result.rejections,
        "by_regime": by_regime(result.trades, result.trade_context),
        "regime_skips": result.regime_skips,
    }


def write_report(
    result: BacktestResult, out_dir: Path, meta: dict[str, Any]
) -> tuple[Path, Metrics]:
    out_dir.mkdir(parents=True, exist_ok=True)
    metrics = compute_metrics(result.initial_balance, result.equity_curve, result.trades)
    data = summary(result, metrics, meta)
    (out_dir / "summary.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    with (out_dir / "trades.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "position_id",
                "strategy_id",
                "symbol",
                "side",
                "quantity",
                "entry_price",
                "exit_price",
                "opened_at",
                "closed_at",
                "reason",
                "pnl",
                "commission",
            ]
        )
        for t in result.trades:
            w.writerow(
                [
                    t.position_id,
                    t.strategy_id,
                    t.symbol,
                    t.side,
                    t.quantity,
                    t.entry_price,
                    t.exit_price,
                    t.opened_at.isoformat(),
                    t.closed_at.isoformat(),
                    t.reason,
                    f"{t.pnl:.2f}",
                    f"{t.commission:.2f}",
                ]
            )
    with (out_dir / "equity.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ts", "equity"])
        for ts, eq in result.equity_curve:
            w.writerow([ts.isoformat(), f"{eq:.2f}"])
    path = out_dir / "report.html"
    path.write_text(render_html(result, metrics, meta), encoding="utf-8")
    return path, metrics


def _fmt(v: float, digits: int = 2, suffix: str = "") -> str:
    if v != v or v in (float("inf"), float("-inf")):
        return "∞" if v > 0 else "—"
    s = f"{v:,.{digits}f}".replace(",", " ").replace(".", ",")
    return s.replace("-", "−") + suffix


def _downsample(
    curve: list[tuple[datetime, Decimal]], n: int = 800
) -> list[tuple[datetime, float]]:
    if len(curve) <= n:
        return [(t, float(e)) for t, e in curve]
    step = len(curve) / n
    idx = sorted({int(i * step) for i in range(n)} | {len(curve) - 1})
    return [(curve[i][0], float(curve[i][1])) for i in idx]


def _equity_svg(curve: list[tuple[datetime, Decimal]]) -> str:
    pts = _downsample(curve)
    if len(pts) < 2:
        return "<p class='muted'>Pas assez de points pour tracer la courbe.</p>"
    w, h, pl, pr, pt, pb = 900, 300, 64, 16, 14, 28
    dd_h = 90
    t0, t1 = pts[0][0].timestamp(), pts[-1][0].timestamp()
    vals = [v for _, v in pts]
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.06 or 1
    lo, hi = lo - pad, hi + pad

    def x(ts: datetime) -> float:
        return pl + (ts.timestamp() - t0) / (t1 - t0 or 1) * (w - pl - pr)

    def y(v: float) -> float:
        return pt + (hi - v) / (hi - lo) * (h - pt - pb)

    line = " ".join(f"{x(t):.1f},{y(v):.1f}" for t, v in pts)
    area = f"{x(pts[0][0]):.1f},{h - pb} {line} {x(pts[-1][0]):.1f},{h - pb}"
    grid = []
    for i in range(5):
        v = lo + (hi - lo) * i / 4
        yy = y(v)
        grid.append(
            f"<line x1='{pl}' x2='{w - pr}' y1='{yy:.1f}' y2='{yy:.1f}' class='grid'/>"
            f"<text x='{pl - 8}' y='{yy + 4:.1f}' text-anchor='end' class='axis'>{_fmt(v, 0)}</text>"
        )
    labels = []
    for i in range(5):
        t = pts[0][0] + (pts[-1][0] - pts[0][0]) * i / 4
        anchor = "start" if i == 0 else "end" if i == 4 else "middle"
        labels.append(
            f"<text x='{x(t):.1f}' y='{h - 8}' text-anchor='{anchor}' class='axis'>{t:%d/%m/%Y}</text>"
        )
    peak, dds = -1e18, []
    for t, v in pts:
        peak = max(peak, v)
        dds.append((t, (v - peak) / peak * 100 if peak > 0 else 0.0))
    worst = min(d for _, d in dds) or -1

    def yd(d: float) -> float:
        return 6 + d / worst * (dd_h - 16)

    dd_line = " ".join(f"{x(t):.1f},{yd(d):.1f}" for t, d in dds)
    dd_area = f"{x(dds[0][0]):.1f},6 {dd_line} {x(dds[-1][0]):.1f},6"
    return f"""
<svg viewBox='0 0 {w} {h}' class='chart' role='img' aria-label="Courbe d'équité">
  {"".join(grid)}{"".join(labels)}
  <polygon points='{area}' class='eq-area'/>
  <polyline points='{line}' class='eq-line'/>
  <circle cx='{x(pts[-1][0]):.1f}' cy='{y(pts[-1][1]):.1f}' r='3.5' class='eq-dot'/>
</svg>
<div class='label'>Drawdown</div>
<svg viewBox='0 0 {w} {dd_h}' class='chart' role='img' aria-label='Drawdown'>
  <line x1='{pl}' x2='{w - pr}' y1='6' y2='6' class='grid'/>
  <text x='{pl - 8}' y='10' text-anchor='end' class='axis'>0 %</text>
  <text x='{pl - 8}' y='{dd_h - 8}' text-anchor='end' class='axis'>{_fmt(worst, 1)} %</text>
  <polygon points='{dd_area}' class='dd-area'/>
  <polyline points='{dd_line}' class='dd-line'/>
</svg>"""


def render_html(result: BacktestResult, m: Metrics, meta: dict[str, Any]) -> str:
    esc = html.escape
    title = esc(str(meta.get("name", "Backtest")))
    warn = (
        "<p class='warn'>Données synthétiques : ce résultat ne dit rien du marché réel.</p>"
        if meta.get("synthetic")
        else ""
    )
    kpis = [
        ("Rendement", _fmt(m.total_return_pct, 2, " %"), m.total_return_pct),
        ("Rendement annualisé", _fmt(m.cagr_pct, 2, " %"), m.cagr_pct),
        ("Drawdown max", _fmt(-m.max_drawdown_pct, 2, " %"), -1.0 if m.max_drawdown_pct else 0.0),
        ("Sharpe", _fmt(m.sharpe), m.sharpe),
        ("Profit factor", _fmt(m.profit_factor), m.profit_factor - 1),
        ("Trades", str(m.trades), 0.0),
    ]
    kpi_html = "".join(
        f"<div class='kpi'><span class='label'>{k}</span>"
        f"<span class='v {'up' if s > 0 else 'down' if s < 0 else ''}'>{v}</span></div>"
        for k, v, s in kpis
    )
    details = [
        ("Capital initial", _fmt(float(result.initial_balance))),
        ("Équité finale", _fmt(float(result.final_equity))),
        ("Taux de réussite", _fmt(m.win_rate_pct, 1, " %")),
        ("Gain moyen par trade", _fmt(m.expectancy)),
        ("Gain moyen / perte moyenne", f"{_fmt(m.avg_win)} / {_fmt(m.avg_loss)}"),
        ("Durée moyenne", _fmt(m.avg_duration_hours, 1, " h")),
        ("Sortino", _fmt(m.sortino)),
        ("Commissions", _fmt(m.total_commission)),
        ("Bougies rejouées", f"{result.bars_processed:,}".replace(",", " ")),
    ]
    detail_html = "".join(f"<tr><td>{k}</td><td class='num r'>{v}</td></tr>" for k, v in details)
    rej = result.rejections
    rej_html = (
        "".join(
            f"<tr><td>{esc(k)}</td><td class='num r'>{v}</td></tr>" for k, v in sorted(rej.items())
        )
        or "<tr><td colspan='2' class='muted'>Aucun refus</td></tr>"
    )
    labels = {
        "trend": "Tendance",
        "range": "Range",
        "volatile": "Forte volatilité",
        "unknown": "Indéterminé",
    }
    regime_html = (
        "".join(
            f"<tr><td>{labels[str(r['regime'])]}</td><td class='num r'>{r['trades']}</td>"
            f"<td class='num r'>{_fmt(float(r['win_rate_pct']), 1, ' %')}</td>"
            f"<td class='num r {'up' if float(r['pnl']) > 0 else 'down'}'>{_fmt(float(r['pnl']))}</td>"
            f"<td class='num r'>{_fmt(float(r['profit_factor']))}</td></tr>"
            for r in by_regime(result.trades, result.trade_context)
        )
        or "<tr><td colspan='5' class='muted'>Aucun trade</td></tr>"
    )
    rows = []
    for t in result.trades[-300:][::-1]:
        cls = "up" if t.pnl > 0 else "down"
        rows.append(
            f"<tr><td class='num'>{t.closed_at:%Y-%m-%d %H:%M}</td><td>{esc(t.symbol)}</td>"
            f"<td><span class='tag {t.side}'>{'ACHAT' if t.side == 'buy' else 'VENTE'}</span></td>"
            f"<td class='num r'>{t.quantity:,}</td><td class='num r'>{t.entry_price}</td>"
            f"<td class='num r'>{t.exit_price}</td><td>{esc(str(t.reason))}</td>"
            f"<td class='num r {cls}'>{_fmt(float(t.pnl))}</td></tr>".replace(",", " ")
        )
    params = esc(json.dumps(meta.get("strategies", []), ensure_ascii=False, indent=1))
    period = (
        f"{result.start:%d/%m/%Y} → {result.end:%d/%m/%Y}" if result.start and result.end else ""
    )
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>{_CSS}</style></head><body><main>
<div><div class="label">Backtest Kobr4 FX</div><h1>{title}</h1>
<div class="muted">{period} · généré le {datetime.now(UTC):%d/%m/%Y %H:%M} UTC</div></div>
{warn}
<section class="kpis">{kpi_html}</section>
<section class="panel"><h2>Équité</h2>{_equity_svg(result.equity_curve)}</section>
<div class="grid2">
<section class="panel"><h2>Détails</h2><table>{detail_html}</table></section>
<section class="panel"><h2>Refus du gestionnaire de risque</h2><table>{rej_html}</table></section>
</div>
<section class="panel"><h2>Résultats par régime de marché (à l'entrée)</h2><table>
<thead><tr><th>Régime</th><th class="r">Trades</th><th class="r">Réussite</th><th class="r">P&amp;L</th><th class="r">Profit factor</th></tr></thead>
<tbody>{regime_html}</tbody></table></section>
<section class="panel"><h2>Derniers trades</h2><div class="tw"><table>
<thead><tr><th>Fermé le</th><th>Paire</th><th>Sens</th><th class="r">Unités</th><th class="r">Entrée</th>
<th class="r">Sortie</th><th>Motif</th><th class="r">P&amp;L</th></tr></thead>
<tbody>{"".join(rows) or "<tr><td colspan='8' class='muted'>Aucun trade</td></tr>"}</tbody></table></div></section>
<section class="panel"><h2>Paramètres</h2><pre>{params}</pre></section>
</main></body></html>"""
