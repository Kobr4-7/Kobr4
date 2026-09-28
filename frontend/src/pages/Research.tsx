import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, ApiError, type BacktestRun, type Bot, type Proposal } from "../api";
import { EquityChart } from "../components/charts";
import { Field, Kpi, Notice, Panel } from "../components/ui";
import { useLoad, useToast } from "../context";
import { REASON_LABEL, REGIME_LABEL, RULE_LABEL, cls, dateTime, pct, signed } from "../format";

const today = () => new Date().toISOString().slice(0, 10);
const yearsAgo = (n: number) => {
  const d = new Date();
  d.setFullYear(d.getFullYear() - n);
  return d.toISOString().slice(0, 10);
};

export function Backtests() {
  const [params] = useSearchParams();
  const toast = useToast();
  const { data: bots } = useLoad<Bot[]>("/api/bots");
  const { data: runs, reload } = useLoad<BacktestRun[]>("/api/backtests");
  const [form, setForm] = useState({ bot_id: params.get("bot") ?? "", start: yearsAgo(3), end: today(), initial_balance: 10000 });
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!form.bot_id && bots?.[0]) setForm((f) => ({ ...f, bot_id: bots[0]!.id }));
  }, [bots, form.bot_id]);
  useEffect(() => {
    if (!runs?.some((r) => r.status === "running")) return;
    const t = window.setInterval(reload, 2000);
    return () => window.clearInterval(t);
  }, [runs, reload]);
  return (
    <>
      <div className="page-head"><h1>Backtests</h1></div>
      <p className="muted">Rejoue l'historique avec les réglages d'un bot : mêmes stratégies, même gestion du risque, avec spread, glissement et commissions.</p>
      {error && <Notice tone="error">{error}</Notice>}
      <Panel title="Nouveau backtest">
        <form
          className="panel-b"
          onSubmit={async (e) => {
            e.preventDefault();
            setError(null);
            try {
              const r = await api.post<BacktestRun>("/api/backtests", form);
              toast("Backtest lancé");
              setSelected(r.id);
              reload();
            } catch (err) {
              setError((err as ApiError).message);
            }
          }}
        >
          <div className="row2">
            <Field label="Bot" htmlFor="bt-bot">
              <select id="bt-bot" value={form.bot_id} onChange={(e) => setForm({ ...form, bot_id: e.target.value })}>
                {(bots ?? []).map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}
              </select>
            </Field>
            <Field label="Capital de départ" htmlFor="bt-bal">
              <input id="bt-bal" className="num" type="number" min={100} value={form.initial_balance} onChange={(e) => setForm({ ...form, initial_balance: Number(e.target.value) })} />
            </Field>
            <Field label="Du" htmlFor="bt-start"><input id="bt-start" type="date" value={form.start} onChange={(e) => setForm({ ...form, start: e.target.value })} /></Field>
            <Field label="Au" htmlFor="bt-end"><input id="bt-end" type="date" value={form.end} onChange={(e) => setForm({ ...form, end: e.target.value })} /></Field>
          </div>
          <div><button className="btn primary" disabled={!form.bot_id}>Lancer</button></div>
        </form>
      </Panel>
      {selected && <BacktestDetail id={selected} />}
      <Panel title="Historique des backtests">
        {!runs || runs.length === 0 ? (
          <div className="empty">Aucun backtest.</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Lancé le</th><th>Période</th><th>État</th><th className="r">Trades</th><th className="r">Rendement</th><th className="r">Drawdown</th><th className="r">Sharpe</th><th /></tr></thead>
              <tbody>
                {runs.map((r) => {
                  const m = r.result?.metrics;
                  return (
                    <tr key={r.id}>
                      <td className="num">{dateTime(r.created_at)}</td>
                      <td className="num">{r.config.start ?? "début"} → {r.config.end ?? "fin"}</td>
                      <td>{r.status === "running" ? "En cours…" : r.status === "failed" ? <span className="down">Échec</span> : "Terminé"}</td>
                      <td className="r num">{m?.trades ?? "—"}</td>
                      <td className={`r num ${cls(m?.total_return_pct)}`}>{m ? pct(m.total_return_pct, true) : "—"}</td>
                      <td className="r num">{m ? pct(-m.max_drawdown_pct) : "—"}</td>
                      <td className="r num">{m ? m.sharpe.toFixed(2) : "—"}</td>
                      <td className="r"><button className="btn small" onClick={() => setSelected(r.id)}>Voir</button></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </>
  );
}

function BacktestDetail({ id }: { id: string }) {
  const { data: run, reload } = useLoad<BacktestRun>(`/api/backtests/${id}`);
  const [all, setAll] = useState(false);
  useEffect(() => {
    if (run?.status !== "running") return;
    const t = window.setInterval(reload, 1500);
    return () => window.clearInterval(t);
  }, [run, reload]);
  if (!run) return null;
  if (run.status === "running") return <Notice tone="info">Backtest en cours…</Notice>;
  if (run.status === "failed") return <Notice tone="error">Échec du backtest : {run.error}</Notice>;
  const r = run.result!;
  const m = r.metrics;
  return (
    <Panel title="Résultat">
      <div className="panel-b">
        {r.synthetic && <Notice tone="warn">Historique synthétique : ce résultat ne dit rien du marché réel.</Notice>}
        <section className="kpis">
          <Kpi label="Rendement" value={pct(m.total_return_pct, true)} tone={cls(m.total_return_pct)} sub={`${pct(m.cagr_pct, true)} par an`} />
          <Kpi label="Drawdown max" value={pct(-m.max_drawdown_pct)} tone={m.max_drawdown_pct > 0 ? "down" : ""} />
          <Kpi label="Sharpe" value={m.sharpe.toFixed(2)} sub={`Sortino ${m.sortino.toFixed(2)}`} />
          <Kpi label="Profit factor" value={m.profit_factor == null ? "—" : m.profit_factor.toFixed(2)} />
          <Kpi label="Trades" value={m.trades} sub={`${pct(m.win_rate_pct)} gagnants`} />
        </section>
        <EquityChart points={r.equity ?? []} height={260} />
        {r.by_regime && r.by_regime.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Régime à l'entrée</th><th className="r">Trades</th><th className="r">Réussite</th><th className="r">P&amp;L</th><th className="r">Profit factor</th></tr></thead>
              <tbody>
                {r.by_regime.map((g) => (
                  <tr key={g.regime}>
                    <td>{REGIME_LABEL[g.regime] ?? g.regime}</td>
                    <td className="r num">{g.trades}</td>
                    <td className="r num">{pct(g.win_rate_pct)}</td>
                    <td className={`r num ${cls(g.pnl)}`}>{signed(g.pnl)}</td>
                    <td className="r num">{g.profit_factor.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {r.rejections && Object.keys(r.rejections).length > 0 && (
          <p className="muted" style={{ fontSize: 13 }}>
            Refus du risque : {Object.entries(r.rejections).map(([k, v]) => `${RULE_LABEL[k] ?? k} (${v})`).join(", ")}
          </p>
        )}
        <div className="table-wrap">
          <table>
            <thead><tr><th>Fermé le</th><th>Paire</th><th>Sens</th><th className="r">Entrée</th><th className="r">Sortie</th><th>Motif</th><th className="r">P&amp;L</th></tr></thead>
            <tbody>
              {(r.trades ?? []).slice().reverse().slice(0, all ? 300 : 15).map((t, i) => (
                <tr key={i}>
                  <td className="num">{dateTime(t.closed_at)}</td>
                  <td>{t.symbol}</td>
                  <td><span className={`tag ${t.side}`}>{t.side === "buy" ? "ACHAT" : "VENTE"}</span></td>
                  <td className="r num">{t.entry_price}</td>
                  <td className="r num">{t.exit_price}</td>
                  <td>{REASON_LABEL[t.reason] ?? t.reason}</td>
                  <td className={`r num ${cls(t.pnl)}`}>{signed(t.pnl)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!all && (r.trades ?? []).length > 15 && (
          <div><button className="btn small" onClick={() => setAll(true)}>Afficher les {Math.min(300, r.trades!.length)} derniers trades</button></div>
        )}
      </div>
    </Panel>
  );
}

export function Lab() {
  const toast = useToast();
  const { data: bots } = useLoad<Bot[]>("/api/bots");
  const { data: proposals, reload } = useLoad<Proposal[]>("/api/proposals");
  const [form, setForm] = useState({ bot_id: "", strategy_id: "", start: yearsAgo(5), end: today(), trials: 60, train_months: 24, test_months: 6, holdout_months: 12 });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const bot = bots?.find((b) => b.id === form.bot_id);
  useEffect(() => {
    if (!form.bot_id && bots?.[0]) setForm((f) => ({ ...f, bot_id: bots[0]!.id, strategy_id: bots[0]!.config.strategies[0]?.id ?? "" }));
  }, [bots, form.bot_id]);
  const act = async (fn: () => Promise<unknown>, msg: string) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      toast(msg);
      reload();
    } catch (e) {
      setError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <div className="page-head"><h1>Laboratoire</h1><span className="spacer" /><button className="btn" onClick={reload}>Actualiser</button></div>
      <p className="muted" style={{ maxWidth: "72ch" }}>
        Le laboratoire cherche de meilleurs réglages hors ligne. Il les valide sur des périodes qu'il n'a pas vues, vérifie qu'ils ne
        tiennent pas du hasard, puis te les propose. Rien ne change sur un bot sans ton accord.
      </p>
      {error && <Notice tone="error">{error}</Notice>}
      <Panel title="Lancer une optimisation">
        <form className="panel-b" onSubmit={(e) => { e.preventDefault(); void act(() => api.post("/api/lab/optimize", form), "Optimisation lancée : la proposition apparaîtra ici une fois prête"); }}>
          <div className="row2">
            <Field label="Bot" htmlFor="lab-bot">
              <select id="lab-bot" value={form.bot_id} onChange={(e) => { const b = bots?.find((x) => x.id === e.target.value); setForm({ ...form, bot_id: e.target.value, strategy_id: b?.config.strategies[0]?.id ?? "" }); }}>
                {(bots ?? []).map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}
              </select>
            </Field>
            <Field label="Stratégie" htmlFor="lab-st">
              <select id="lab-st" value={form.strategy_id} onChange={(e) => setForm({ ...form, strategy_id: e.target.value })}>
                {(bot?.config.strategies ?? []).map((s) => <option key={s.id} value={s.id}>{s.id} (v{s.version})</option>)}
              </select>
            </Field>
            <Field label="Historique du" htmlFor="lab-start"><input id="lab-start" type="date" value={form.start} onChange={(e) => setForm({ ...form, start: e.target.value })} /></Field>
            <Field label="Au" htmlFor="lab-end"><input id="lab-end" type="date" value={form.end} onChange={(e) => setForm({ ...form, end: e.target.value })} /></Field>
            <Field label="Essais par fenêtre" htmlFor="lab-trials"><input id="lab-trials" className="num" type="number" min={4} max={500} value={form.trials} onChange={(e) => setForm({ ...form, trials: Number(e.target.value) })} /></Field>
            <Field label="Période réservée (mois)" hint="Jamais vue par l'optimisation, utilisée une seule fois à la fin." htmlFor="lab-hold"><input id="lab-hold" className="num" type="number" min={0} value={form.holdout_months} onChange={(e) => setForm({ ...form, holdout_months: Number(e.target.value) })} /></Field>
          </div>
          <div><button className="btn primary" disabled={busy || !form.strategy_id}>Lancer</button></div>
        </form>
      </Panel>
      {(proposals ?? []).length === 0 && <Panel><div className="empty">Aucune proposition pour l'instant.</div></Panel>}
      {(proposals ?? []).map((p) => (
        <Panel
          key={p.id}
          title={<div className="row"><h2>{p.strategy_id} v{p.base_version} → v{p.base_version + 1}</h2><span className={`pill ${p.status === "failed" || p.status === "rejected" ? "error" : p.status === "proposed" ? "starting" : "running"}`}>{{ proposed: "À décider", failed: "Critères non remplis", approved: "Acceptée", rejected: "Refusée", applied: "Appliquée" }[p.status]}</span></div>}
          actions={
            <>
              {p.status === "proposed" && (
                <>
                  <button className="btn small" disabled={busy} onClick={() => void act(() => api.post(`/api/proposals/${p.id}/decision`, { approve: false }), "Proposition refusée")}>Refuser</button>
                  <button className="btn small primary" disabled={busy} onClick={() => void act(() => api.post(`/api/proposals/${p.id}/decision`, { approve: true }), "Proposition acceptée")}>Accepter</button>
                </>
              )}
              {p.status === "approved" && <button className="btn small primary" disabled={busy} onClick={() => void act(() => api.post(`/api/proposals/${p.id}/apply`), "Réglages appliqués : fais tourner le bot en démo avant tout passage en réel")}>Appliquer au bot</button>}
            </>
          }
        >
          <div className="panel-b">
            {p.failures.length > 0 && <Notice tone="warn">{p.failures.join(" · ")}</Notice>}
            <section className="kpis">
              <Kpi label="Hors échantillon" value={pct(p.oos_total_return_pct, true)} tone={cls(p.oos_total_return_pct)} sub={`${p.oos_trades} trades`} />
              <Kpi label="Sharpe hors éch." value={p.oos_sharpe.toFixed(2)} />
              <Kpi label="Drawdown max" value={pct(-p.oos_max_drawdown_pct)} />
              <Kpi label="Stabilité" value={p.stability.toFixed(2)} sub="réglages voisins" />
              <Kpi label="Sharpe dégonflé" value={p.deflated_sharpe.toFixed(2)} sub={`${p.trials} essais`} />
            </section>
            <p className="num" style={{ fontSize: 12.5 }}>Actuel : {JSON.stringify(p.current_params)}<br />Proposé : {JSON.stringify(p.proposed_params)}</p>
            {p.holdout && (
              <p style={{ fontSize: 13 }}>
                Période réservée {p.holdout.period} : proposé <span className={cls(p.holdout.total_return_pct)}>{pct(p.holdout.total_return_pct, true)}</span>
                {p.holdout_baseline && <> · actuel <span className={cls(p.holdout_baseline.total_return_pct)}>{pct(p.holdout_baseline.total_return_pct, true)}</span></>}
              </p>
            )}
            <p className="muted" style={{ fontSize: 12 }}>{dateTime(p.created_at)}</p>
          </div>
        </Panel>
      ))}
    </>
  );
}
