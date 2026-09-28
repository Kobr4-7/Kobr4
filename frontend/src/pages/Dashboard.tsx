import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, ApiError, type Bot, type LiveStatus, type Position, type Trade } from "../api";
import { PriceChart, type Candle } from "../components/charts";
import { Kpi, Meter, ModePill, Notice, Panel, StatusPill } from "../components/ui";
import { useLiveBots, useLoad, useToast } from "../context";
import { REASON_LABEL, REGIME_LABEL, cls, dateTime, money, num, pct, signed, time } from "../format";

export function Dashboard() {
  const { data: bots, reload } = useLoad<Bot[]>("/api/bots");
  const [params, setParams] = useSearchParams();
  const { bots: live, connected } = useLiveBots(true);
  if (!bots) return <p className="muted">Chargement…</p>;
  if (bots.length === 0)
    return (
      <Panel title="Aucun bot">
        <div className="panel-b">
          <p>Crée ton premier bot pour commencer, en démo.</p>
          <Link className="btn primary" to="/bots/nouveau">Créer un bot</Link>
        </div>
      </Panel>
    );
  const selected = bots.find((b) => b.id === params.get("bot")) ?? bots[0]!;
  return (
    <BotDashboard
      key={selected.id}
      bots={bots}
      bot={selected}
      live={live[selected.id] ?? null}
      wsConnected={connected}
      onSelect={(id) => setParams({ bot: id })}
      onChanged={reload}
    />
  );
}

function BotDashboard({
  bots, bot, live, wsConnected, onSelect, onChanged,
}: {
  bots: Bot[];
  bot: Bot;
  live: LiveStatus | null;
  wsConnected: boolean;
  onSelect: (id: string) => void;
  onChanged: () => void;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [armed, setArmed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const symbols = bot.config.instruments;
  const [symbol, setSymbol] = useState(symbols[0] ?? "EUR/USD");
  const [tab, setTab] = useState<"positions" | "history" | "journal">("positions");
  const status = live?.state ?? bot.status;
  const running = status === "running" || status === "starting";

  const act = async (fn: () => Promise<unknown>, msg: string) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      toast(msg);
      onChanged();
    } catch (e) {
      setError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    if (!armed) return;
    const t = window.setTimeout(() => setArmed(false), 4000);
    return () => window.clearTimeout(t);
  }, [armed]);

  const floating = useMemo(() => (live?.positions ?? []).reduce((a, p) => a + num(p.unrealized ?? 0), 0), [live]);
  const risk = bot.config.risk;
  const ccy = bot.config.base_currency === "EUR" ? "€" : bot.config.base_currency === "USD" ? "$" : bot.config.base_currency;

  return (
    <>
      <div className="page-head">
        {bots.length > 1 ? (
          <select aria-label="Bot affiché" value={bot.id} onChange={(e) => onSelect(e.target.value)} className="btn" style={{ fontWeight: 600 }}>
            {bots.map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}
          </select>
        ) : (
          <h1>{bot.name}</h1>
        )}
        <StatusPill status={status} kill={live?.kill_switch} />
        <ModePill mode={bot.mode} />
        {running && !wsConnected && <span className="pill stopped">Reconnexion…</span>}
        <span className="spacer" />
        {!running && (
          <button className="btn primary" disabled={busy} onClick={() => void act(() => api.post(`/api/bots/${bot.id}/start`), "Bot démarré")}>
            Démarrer
          </button>
        )}
        {running && (
          <button className="btn" disabled={busy} onClick={() => void act(() => api.post(`/api/bots/${bot.id}/stop`), "Bot arrêté (les positions gardent leurs stops)")}>
            Arrêter
          </button>
        )}
        {live?.kill_switch ? (
          <button className="btn" disabled={busy} onClick={() => void act(() => api.post(`/api/bots/${bot.id}/release`), "Arrêt d'urgence levé")}>
            Lever l'arrêt d'urgence
          </button>
        ) : (
          running && (
            <button
              className={`btn danger ${armed ? "solid" : ""}`}
              disabled={busy}
              onClick={() => {
                if (!armed) return setArmed(true);
                setArmed(false);
                void act(() => api.post(`/api/bots/${bot.id}/kill`, { close_positions: true, reason: "arrêt manuel depuis le site" }), "Positions clôturées, entrées bloquées");
              }}
            >
              {armed ? "Confirmer : tout clôturer" : "Tout clôturer"}
            </button>
          )
        )}
      </div>

      {error && <Notice tone="error">{error}</Notice>}
      {(live?.error || (!live && bot.last_error)) && <Notice tone="error">Dernière erreur : {live?.error ?? bot.last_error}</Notice>}
      {live?.kill_switch && <Notice tone="warn">Arrêt d'urgence actif : {live.kill_switch}. Aucune nouvelle position ne sera ouverte.</Notice>}
      {live && live.stale.length > 0 && <Notice tone="warn">Cotations interrompues sur {live.stale.join(", ")} : entrées suspendues sur ces paires.</Notice>}
      {!live && <Notice tone="info">Le bot est arrêté. Les chiffres en direct apparaîtront au démarrage.</Notice>}

      <section className="kpis" aria-label="Résumé du compte">
        <Kpi label="Équité" value={money(live?.equity, ccy)} sub={`Solde ${money(live?.balance, ccy)}`} />
        <Kpi label="P&L flottant" value={signed(live ? floating : null, ccy)} tone={cls(floating)} sub={`${live?.positions.length ?? 0} positions ouvertes`} />
        <Kpi
          label="Cette semaine"
          value={pct(live?.weekly_pnl_pct != null ? num(live.weekly_pnl_pct) : null)}
          tone={cls(live?.weekly_pnl_pct ?? 0)}
          sub={risk.max_weekly_loss_pct ? `Pause si −${risk.max_weekly_loss_pct} %` : `Jour : −${pct(live?.daily_loss_pct ?? 0)} / ${risk.max_daily_loss_pct} %`}
        />
        <Kpi label="Drawdown" value={pct(live ? -num(live.drawdown_pct) : null)} tone={num(live?.drawdown_pct) > 0 ? "down" : ""} sub={`Arrêt à ${risk.max_drawdown_pct} %`} />
      </section>

      <div className="grid3">
        <Panel
          title={
            <div className="row">
              <h2>{symbol}</h2>
              {live?.quotes[symbol] && <span className="num" style={{ fontSize: 18 }}>{live.quotes[symbol].bid}</span>}
            </div>
          }
          actions={
            symbols.length > 1 && <div className="seg" role="group" aria-label="Paire affichée">
              {symbols.map((s) => (
                <button key={s} aria-pressed={s === symbol} onClick={() => setSymbol(s)}>{s}</button>
              ))}
            </div>
          }
        >
          <CandlePanel botId={bot.id} symbol={symbol} timeframe={bot.config.strategies[0]?.timeframe ?? "H1"} running={running} positions={live?.positions ?? []} />
          {live?.quotes[symbol] && (
            <div className="panel-b" style={{ paddingTop: 0 }}>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <span>Vendre <span className="num down">{live.quotes[symbol].bid}</span></span>
                <span className="muted num" style={{ fontSize: 12 }}>{time(live.quotes[symbol].ts)}</span>
                <span>Acheter <span className="num up">{live.quotes[symbol].ask}</span></span>
              </div>
            </div>
          )}
        </Panel>

        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <Panel title="Risque">
            <div className="panel-b">
              <Meter label="Perte du jour" value={num(live?.daily_loss_pct ?? 0)} max={num(risk.max_daily_loss_pct)} text={`${pct(live?.daily_loss_pct ?? 0)} / ${risk.max_daily_loss_pct} %`} />
              {risk.max_weekly_loss_pct ? (
                <Meter label="Perte de la semaine" value={num(live?.weekly_loss_pct ?? 0)} max={num(risk.max_weekly_loss_pct)} text={`${pct(live?.weekly_loss_pct ?? 0)} / ${risk.max_weekly_loss_pct} %`} />
              ) : null}
              <Meter label="Drawdown" value={num(live?.drawdown_pct ?? 0)} max={num(risk.max_drawdown_pct)} text={`${pct(live?.drawdown_pct ?? 0)} / ${risk.max_drawdown_pct} %`} />
              <Meter label="Positions ouvertes" value={live?.positions.length ?? 0} max={num(risk.max_open_positions)} text={`${live?.positions.length ?? 0} / ${risk.max_open_positions}`} />
              <p className="muted" style={{ fontSize: 12.5 }}>Risque par trade : {risk.risk_per_trade_pct} % de l'équité.</p>
            </div>
          </Panel>
          <WeeklyResults botId={bot.id} ccy={ccy} />
          <Panel title="Stratégies">
            <div className="panel-b">
              {bot.config.strategies.map((s) => {
                const ls = live?.strategies.find((x) => x.id === s.id);
                return (
                  <div key={s.id} className="row" style={{ justifyContent: "space-between" }}>
                    <div>
                      <div style={{ fontWeight: 600 }}>{s.id} <span className="muted num" style={{ fontSize: 12 }}>v{s.version}</span></div>
                      <div className="muted" style={{ fontSize: 12.5 }}>{s.kind} · {s.timeframe} · {s.instruments.join(", ")}</div>
                      {ls?.regime && (
                        <div className="muted" style={{ fontSize: 12 }}>
                          Régime : {Object.entries(ls.regime).map(([sym, r]) => `${sym} ${(REGIME_LABEL[r] ?? r).toLowerCase()}`).join(" · ")}
                        </div>
                      )}
                    </div>
                    {ls && (
                      <button
                        className="btn small"
                        disabled={busy}
                        onClick={() => void act(() => api.post(`/api/bots/${bot.id}/strategies/${s.id}/${ls.enabled ? "pause" : "resume"}`), ls.enabled ? "Stratégie en pause (les positions restent gérées)" : "Stratégie relancée")}
                      >
                        {ls.enabled ? "Pause" : "Reprendre"}
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          </Panel>
        </div>
      </div>

      <Panel
        title={
          <div className="tabs" role="tablist">
            {(["positions", "history", "journal"] as const).map((t) => (
              <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)}>
                {t === "positions" ? `Positions (${live?.positions.length ?? 0})` : t === "history" ? "Historique" : "Journal"}
              </button>
            ))}
          </div>
        }
      >
        {tab === "positions" && <Positions live={live} ccy={ccy} onClose={(id) => void act(() => api.post(`/api/bots/${bot.id}/positions/${id}/close`), "Position clôturée")} busy={busy} />}
        {tab === "history" && <History botId={bot.id} ccy={ccy} />}
        {tab === "journal" && <Journal live={live} botId={bot.id} />}
      </Panel>
    </>
  );
}

const TF_SECONDS: Record<string, number> = { M15: 900, H1: 3600, H4: 14400, D1: 86400 };

function CandlePanel({ botId, symbol, timeframe, running, positions }: { botId: string; symbol: string; timeframe: string; running: boolean; positions: Position[] }) {
  const [tf, setTf] = useState(timeframe);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    if (!running) return;
    const t = window.setInterval(() => setRefresh((x) => x + 1), 20000);
    return () => window.clearInterval(t);
  }, [running]);
  const { data, error } = useLoad<Candle[]>(`/api/bots/${botId}/candles?symbol=${encodeURIComponent(symbol)}&timeframe=${tf}&count=300`, [running, refresh]);
  const { data: trades } = useLoad<Trade[]>(`/api/bots/${botId}/trades?limit=200`, [refresh]);
  const precision = symbol.endsWith("JPY") || symbol === "XAU/USD" ? 3 : 5;
  const mine = useMemo(() => (trades ?? []).filter((t) => t.symbol === symbol), [trades, symbol]);
  const open = useMemo(() => positions.filter((p) => p.symbol === symbol), [positions, symbol]);
  return (
    <div>
      <div className="panel-b" style={{ paddingBottom: 0 }}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div className="seg" role="group" aria-label="Unité de temps">
            {["M15", "H1", "H4", "D1"].map((t) => (
              <button key={t} aria-pressed={t === tf} onClick={() => setTf(t)}>{t}</button>
            ))}
          </div>
          <div className="chart-legend">
            <span><i className="lg-buy" />Achat</span>
            <span><i className="lg-sell" />Vente</span>
            <span><i className="lg-win" />Sortie gagnante</span>
            <span><i className="lg-loss" />Sortie perdante</span>
            <span><i className="lg-sl" />Stop</span>
            <span><i className="lg-tp" />Objectif</span>
          </div>
        </div>
      </div>
      {error ? (
        <div className="empty">{error}</div>
      ) : data && data.length === 0 ? (
        <div className="chart-box chart-empty">
          <p>Pas encore de bougies pour {symbol}.</p>
          <p className="muted">Le bot les construit en direct à partir des prix : les premières apparaissent dans quelques minutes.</p>
        </div>
      ) : data ? (
        <PriceChart candles={data} precision={precision} step={TF_SECONDS[tf] ?? 3600} trades={mine} positions={open} />
      ) : (
        <div className="chart-box chart-empty"><p className="muted">Chargement du graphique…</p></div>
      )}
    </div>
  );
}

/** Résultat de chaque semaine (lundi-vendredi), d'après la courbe d'équité. */
function WeeklyResults({ botId, ccy }: { botId: string; ccy: string }) {
  const { data } = useLoad<{ ts: string; equity: string }[]>(`/api/bots/${botId}/equity?days=120`);
  const weeks = useMemo(() => {
    if (!data || data.length < 2) return [];
    const monday = (iso: string) => {
      const d = new Date(iso);
      const day = (d.getUTCDay() + 6) % 7;
      return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() - day);
    };
    const last = new Map<number, number>();
    const first = new Map<number, number>();
    for (const p of data) {
      const k = monday(p.ts);
      if (!first.has(k)) first.set(k, num(p.equity));
      last.set(k, num(p.equity));
    }
    const keys = [...last.keys()].sort((a, b) => a - b);
    return keys
      .map((k, i) => {
        const start = i > 0 ? last.get(keys[i - 1]!)! : first.get(k)!;
        const end = last.get(k)!;
        return { week: k, pnl: end - start, pct: start > 0 ? ((end - start) / start) * 100 : 0 };
      })
      .slice(-12);
  }, [data]);
  const won = weeks.filter((w) => w.pnl > 0).length;
  const total = weeks.reduce((a, w) => a + w.pnl, 0);
  const max = Math.max(0.01, ...weeks.map((w) => Math.abs(w.pct)));
  return (
    <Panel title="Semaine par semaine" actions={weeks.length ? <span className={`num ${cls(total)}`} style={{ fontSize: 13 }}>{signed(total, ccy)}</span> : undefined}>
      <div className="panel-b">
        {weeks.length === 0 ? (
          <p className="muted">Les résultats hebdomadaires apparaîtront après quelques jours de fonctionnement.</p>
        ) : (
          <>
            <div className="weeks" role="img" aria-label="Résultat de chaque semaine">
              {weeks.map((w) => (
                <div key={w.week} className="wk" title={`Semaine du ${new Date(w.week).toLocaleDateString("fr-FR")} : ${signed(w.pnl, ccy)} (${w.pct.toFixed(2)} %)`}>
                  <div className="wk-bar">
                    <i className={w.pnl >= 0 ? "pos" : "neg"} style={{ height: `${(Math.abs(w.pct) / max) * 100}%` }} />
                  </div>
                  <span>{new Date(w.week).toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit" })}</span>
                </div>
              ))}
            </div>
            <p className="muted" style={{ fontSize: 12.5 }}>
              {won} semaine{won > 1 ? "s" : ""} gagnante{won > 1 ? "s" : ""} sur {weeks.length}. En suivi de tendance, quelques très bonnes semaines paient les petites semaines perdantes : c'est le total qui compte.
            </p>
          </>
        )}
      </div>
    </Panel>
  );
}

function Positions({ live, ccy, onClose, busy }: { live: LiveStatus | null; ccy: string; onClose: (id: string) => void; busy: boolean }) {
  if (!live || live.positions.length === 0) return <div className="empty">Aucune position ouverte.</div>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr><th>Paire</th><th>Sens</th><th className="r">Unités</th><th className="r">Entrée</th><th className="r">Stop</th><th className="r">Objectif</th><th className="r">P&amp;L</th><th>Stratégie</th><th /></tr>
        </thead>
        <tbody>
          {live.positions.map((p) => (
            <tr key={p.id}>
              <td><strong>{p.symbol}</strong></td>
              <td><span className={`tag ${p.side}`}>{p.side === "buy" ? "ACHAT" : "VENTE"}</span></td>
              <td className="r num">{p.quantity.toLocaleString("fr-FR")}</td>
              <td className="r num">{p.entry_price}</td>
              <td className="r num">{p.stop_loss}</td>
              <td className="r num">{p.take_profit ?? "—"}</td>
              <td className={`r num ${cls(p.unrealized)}`}>{signed(p.unrealized, ccy)}</td>
              <td className="muted">{p.strategy_id}</td>
              <td className="r"><button className="btn small" disabled={busy} onClick={() => onClose(p.id)}>Clôturer</button></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function History({ botId, ccy }: { botId: string; ccy: string }) {
  const { data } = useLoad<Trade[]>(`/api/bots/${botId}/trades?limit=200`);
  if (!data) return <div className="empty">Chargement…</div>;
  if (data.length === 0) return <div className="empty">Aucun trade fermé pour l'instant.</div>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr><th>Fermé le</th><th>Paire</th><th>Sens</th><th className="r">Unités</th><th className="r">Entrée</th><th className="r">Sortie</th><th>Motif</th><th className="r">P&amp;L</th></tr>
        </thead>
        <tbody>
          {data.map((t, i) => (
            <tr key={t.position_id ?? i}>
              <td className="num">{dateTime(t.closed_at)}</td>
              <td><strong>{t.symbol}</strong></td>
              <td><span className={`tag ${t.side}`}>{t.side === "buy" ? "ACHAT" : "VENTE"}</span></td>
              <td className="r num">{t.quantity.toLocaleString("fr-FR")}</td>
              <td className="r num">{t.entry_price}</td>
              <td className="r num">{t.exit_price}</td>
              <td>{REASON_LABEL[t.reason] ?? t.reason}</td>
              <td className={`r num ${cls(t.pnl)}`}>{signed(t.pnl, ccy)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Journal({ live, botId }: { live: LiveStatus | null; botId: string }) {
  const { data } = useLoad<{ ts: string; type: string; payload: Record<string, unknown> }[]>(`/api/bots/${botId}/events?limit=100`);
  const items = [
    ...(live?.recent ?? []).map((r) => ({ ts: r.ts, text: r.message })),
    ...(data ?? []).map((e) => ({ ts: e.ts, text: describe(e.type, e.payload) })),
  ]
    .sort((a, b) => b.ts.localeCompare(a.ts))
    .filter((x, i, arr) => i === 0 || x.ts !== arr[i - 1]!.ts || x.text !== arr[i - 1]!.text)
    .slice(0, 150);
  if (items.length === 0) return <div className="empty">Rien pour l'instant.</div>;
  return (
    <ul className="log">
      {items.map((x, i) => (
        <li key={i}><span className="muted">{time(x.ts)}</span><span>{x.text}</span></li>
      ))}
    </ul>
  );
}

function describe(type: string, p: Record<string, unknown>): string {
  const intent = p.intent as { symbol?: string; side?: string; reason?: string } | undefined;
  switch (type) {
    case "SignalEmitted":
      return `Signal ${intent?.side === "buy" ? "achat" : "vente"} ${intent?.symbol} : ${intent?.reason ?? ""}`;
    case "RiskRejected":
      return `Refusé par le risque (${String(p.rule)}) : ${String(p.reason)}`;
    case "RiskApproved":
      return `Ordre validé : ${String((p.order as { quantity?: number })?.quantity)} ${intent?.symbol}`;
    case "PositionOpened": {
      const pos = p.position as { symbol: string; side: string; entry_price: string };
      return `Position ouverte : ${pos.side === "buy" ? "achat" : "vente"} ${pos.symbol} à ${pos.entry_price}`;
    }
    case "PositionClosed": {
      const t = p.trade as { symbol: string; reason: string; pnl: string };
      return `Position fermée ${t.symbol} (${REASON_LABEL[t.reason] ?? t.reason}) : ${signed(t.pnl)}`;
    }
    case "KillSwitchActivated":
      return `Arrêt d'urgence : ${String(p.reason)}`;
    case "RiskLimitReached":
      return `Limite atteinte : ${String(p.detail)}`;
    case "MarketDataStale":
      return `Cotations interrompues sur ${String(p.symbol)}`;
    case "ReconciliationMismatch":
      return `Écart avec le courtier : ${String(p.detail)}`;
    default:
      return type;
  }
}
