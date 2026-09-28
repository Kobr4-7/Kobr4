import {
  CandlestickSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  LineStyle,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef, useState } from "react";
import type { Position, Trade } from "../api";

function css(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** Change de valeur à chaque changement de thème (clair ou sombre). */
function useThemeKey(): number {
  const [k, setK] = useState(0);
  useEffect(() => {
    const bump = () => setK((x) => x + 1);
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    window.addEventListener("kobr4:theme", bump);
    mq.addEventListener("change", bump);
    return () => {
      window.removeEventListener("kobr4:theme", bump);
      mq.removeEventListener("change", bump);
    };
  }, []);
  return k;
}

export type Candle = { time: number; open: number; high: number; low: number; close: number };

const toSec = (iso: string) => Math.floor(Date.parse(iso) / 1000);

/** Graphique en chandeliers avec les trades : flèches aux entrées, ronds aux sorties
 *  (vert si gagnant, rouge sinon), lignes d'entrée, de stop et d'objectif des positions
 *  ouvertes. Le graphique est créé une fois : le zoom est conservé aux rafraîchissements. */
export function PriceChart({
  candles,
  precision,
  step,
  trades = [],
  positions = [],
}: {
  candles: Candle[];
  precision: number;
  step: number;
  trades?: Trade[];
  positions?: Position[];
}) {
  const box = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const markers = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const lines = useRef<IPriceLine[]>([]);
  const fitted = useRef(false);
  const themeKey = useThemeKey();

  useEffect(() => {
    if (!box.current) return;
    const c = createChart(box.current, {
      autoSize: true,
      localization: { locale: "fr-FR" },
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: css("--muted"),
        fontFamily: "JetBrains Mono, ui-monospace, monospace",
        fontSize: 11,
        attributionLogo: false,
      },
      grid: { vertLines: { color: css("--line") }, horzLines: { color: css("--line") } },
      rightPriceScale: { borderColor: css("--line") },
      timeScale: { borderColor: css("--line"), timeVisible: true, rightOffset: 6 },
      crosshair: {
        vertLine: { color: css("--faint"), labelBackgroundColor: css("--surface-3") },
        horzLine: { color: css("--faint"), labelBackgroundColor: css("--surface-3") },
      },
    });
    const s = c.addSeries(CandlestickSeries, {
      upColor: css("--up"),
      downColor: css("--down"),
      borderVisible: false,
      wickUpColor: css("--up"),
      wickDownColor: css("--down"),
      priceFormat: { type: "price", precision, minMove: 1 / 10 ** precision },
    });
    chart.current = c;
    series.current = s;
    markers.current = createSeriesMarkers(s, []);
    lines.current = [];
    fitted.current = false;
    return () => {
      c.remove();
      chart.current = null;
      series.current = null;
      markers.current = null;
    };
  }, [precision, themeKey]);

  useEffect(() => {
    const s = series.current;
    if (!s || !chart.current) return;
    s.setData(candles.map((k) => ({ ...k, time: k.time as UTCTimestamp })));
    if (!fitted.current && candles.length) {
      chart.current.timeScale().fitContent();
      fitted.current = true;
    }
    // Trades : alignés sur le début de la bougie qui les contient.
    const first = candles[0]?.time ?? 0;
    const snap = (t: number) => (Math.floor(t / step) * step) as UTCTimestamp;
    const up = css("--up"), down = css("--down"), accent = css("--accent");
    const m: SeriesMarker<Time>[] = [];
    for (const t of trades) {
      const o = toSec(t.opened_at), c = toSec(t.closed_at);
      const buy = t.side === "buy";
      if (o >= first)
        m.push({ time: snap(o), position: buy ? "belowBar" : "aboveBar", shape: buy ? "arrowUp" : "arrowDown", color: buy ? up : down });
      if (c >= first) {
        const win = Number(t.pnl) >= 0;
        m.push({ time: snap(c), position: buy ? "aboveBar" : "belowBar", shape: "circle", color: win ? up : down });
      }
    }
    for (const p of positions) {
      const o = toSec(p.opened_at);
      const buy = p.side === "buy";
      if (o >= first)
        m.push({ time: snap(o), position: buy ? "belowBar" : "aboveBar", shape: buy ? "arrowUp" : "arrowDown", color: accent });
    }
    m.sort((a, b) => (a.time as number) - (b.time as number));
    markers.current?.setMarkers(m);
    // Positions ouvertes : entrée, stop et objectif.
    for (const l of lines.current) s.removePriceLine(l);
    lines.current = [];
    for (const p of positions) {
      const buy = p.side === "buy";
      lines.current.push(
        s.createPriceLine({ price: Number(p.entry_price), color: accent, lineWidth: 1, lineStyle: LineStyle.Solid, axisLabelVisible: true, title: buy ? "Achat" : "Vente" }),
        s.createPriceLine({ price: Number(p.stop_loss), color: down, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: "SL" }),
      );
      if (p.take_profit)
        lines.current.push(
          s.createPriceLine({ price: Number(p.take_profit), color: up, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: "TP" }),
        );
    }
  }, [candles, trades, positions, step, themeKey]);

  return <div ref={box} className="chart-box" />;
}

/** Courbe d'équité en SVG, avec aire et point final mis en valeur. */
export function EquityChart({ points, height = 220 }: { points: { ts: string; equity: number }[]; height?: number }) {
  if (points.length < 2) return <div className="empty">Pas encore assez de données pour tracer la courbe.</div>;
  const w = 800, h = height, pl = 64, pr = 14, pt = 12, pb = 26;
  const t0 = Date.parse(points[0]!.ts), t1 = Date.parse(points[points.length - 1]!.ts);
  const vals = points.map((p) => p.equity);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = (hi - lo) * 0.08 || 1;
  lo -= pad;
  hi += pad;
  const x = (ts: string) => pl + ((Date.parse(ts) - t0) / (t1 - t0 || 1)) * (w - pl - pr);
  const y = (v: number) => pt + ((hi - v) / (hi - lo)) * (h - pt - pb);
  const line = points.map((p) => `${x(p.ts).toFixed(1)},${y(p.equity).toFixed(1)}`).join(" ");
  const last = points[points.length - 1]!;
  const ticks = [0, 1, 2, 3].map((i) => lo + ((hi - lo) * i) / 3);
  const fmt = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 0 });
  const dates = [0, 0.5, 1].map((f) => new Date(t0 + (t1 - t0) * f));
  return (
    <svg viewBox={`0 0 ${w} ${h}`} style={{ width: "100%", height: "auto", display: "block" }} role="img" aria-label="Courbe d'équité">
      {ticks.map((v) => (
        <g key={v}>
          <line x1={pl} x2={w - pr} y1={y(v)} y2={y(v)} stroke="var(--line)" />
          <text x={pl - 8} y={y(v) + 4} textAnchor="end" fill="var(--muted)" fontSize="11" fontFamily="var(--mono)">{fmt.format(v)}</text>
        </g>
      ))}
      {dates.map((d, i) => (
        <text key={i} x={pl + (w - pl - pr) * [0, 0.5, 1][i]!} y={h - 8} textAnchor={i === 0 ? "start" : i === 2 ? "end" : "middle"} fill="var(--muted)" fontSize="11" fontFamily="var(--mono)">
          {d.toLocaleDateString("fr-FR")}
        </text>
      ))}
      <polygon points={`${x(points[0]!.ts)},${h - pb} ${line} ${x(last.ts)},${h - pb}`} fill="var(--accent)" opacity="0.1" />
      <polyline points={line} fill="none" stroke="var(--accent)" strokeWidth="1.8" />
      <circle cx={x(last.ts)} cy={y(last.equity)} r="3.5" fill="var(--accent)" />
    </svg>
  );
}
