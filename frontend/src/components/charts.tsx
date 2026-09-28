import { CandlestickSeries, ColorType, createChart, type IChartApi, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef } from "react";

function css(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export type Candle = { time: number; open: number; high: number; low: number; close: number };

/** Graphique en chandeliers (bibliothèque lightweight-charts), aux couleurs du thème. */
export function PriceChart({ candles, precision }: { candles: Candle[]; precision: number }) {
  const box = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  useEffect(() => {
    if (!box.current) return;
    const c = createChart(box.current, {
      autoSize: true,
      localization: { locale: "fr-FR" },
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: css("--muted"), fontFamily: "IBM Plex Mono, monospace", attributionLogo: false },
      grid: { vertLines: { color: css("--line") }, horzLines: { color: css("--line") } },
      rightPriceScale: { borderColor: css("--line") },
      timeScale: { borderColor: css("--line"), timeVisible: true },
    });
    const s = c.addSeries(CandlestickSeries, {
      upColor: css("--up"), downColor: css("--down"), borderVisible: false,
      wickUpColor: css("--up"), wickDownColor: css("--down"),
      priceFormat: { type: "price", precision, minMove: 1 / 10 ** precision },
    });
    s.setData(candles.map((k) => ({ ...k, time: k.time as UTCTimestamp })));
    c.timeScale().fitContent();
    chart.current = c;
    return () => c.remove();
  }, [candles, precision]);
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
