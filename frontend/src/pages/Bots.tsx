import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, ApiError, type Bot, type BrokerConn, type Catalog, type StrategyConfig } from "../api";
import { EquityChart } from "../components/charts";
import { RiskEditor, StrategyEditor, botPayload, defaultRisk, defaultStrategy, type RiskValues } from "../components/editors";
import { Field, ModePill, Notice, Panel, StatusPill } from "../components/ui";
import { useLoad, useToast } from "../context";
import { dateTime } from "../format";

export function BotList() {
  const { data: bots, error } = useLoad<Bot[]>("/api/bots");
  return (
    <>
      <div className="page-head">
        <h1>Bots</h1>
        <span className="spacer" />
        <Link className="btn primary" to="/bots/nouveau">Nouveau bot</Link>
      </div>
      {error && <Notice tone="error">{error}</Notice>}
      <Panel>
        {!bots ? (
          <div className="empty">Chargement…</div>
        ) : bots.length === 0 ? (
          <div className="empty">Aucun bot. Crée le premier : il démarre en démo.</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>Nom</th><th>État</th><th>Mode</th><th>Paires</th><th>Stratégies</th><th>Créé le</th></tr>
              </thead>
              <tbody>
                {bots.map((b) => (
                  <tr key={b.id}>
                    <td><Link to={`/bots/${b.id}`}><strong>{b.name}</strong></Link></td>
                    <td><StatusPill status={b.status} /></td>
                    <td><ModePill mode={b.mode} /></td>
                    <td>{b.config.instruments.join(", ")}</td>
                    <td>{b.config.strategies.map((s) => s.id).join(", ")}</td>
                    <td className="num">{dateTime(b.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </>
  );
}

function ConfigForm({
  catalog, initial, onSubmit, submitLabel, busy,
}: {
  catalog: Catalog;
  initial: { strategies: StrategyConfig[]; risk: RiskValues };
  onSubmit: (strategies: StrategyConfig[], risk: RiskValues) => void;
  submitLabel: string;
  busy: boolean;
}) {
  const [strategies, setStrategies] = useState<StrategyConfig[]>(initial.strategies);
  const [risk, setRisk] = useState<RiskValues>(initial.risk);
  const update = (i: number, s: StrategyConfig) => setStrategies(strategies.map((x, j) => (j === i ? s : x)));
  return (
    <>
      {strategies.map((s, i) => (
        <Panel
          key={i}
          title={
            <div className="row">
              <h2>Stratégie</h2>
              <input
                aria-label="Identifiant de la stratégie"
                className="num"
                style={{ padding: "4px 8px", border: "1px solid var(--line)", borderRadius: 6, background: "var(--surface-2)" }}
                value={s.id}
                onChange={(e) => update(i, { ...s, id: e.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, "-") })}
              />
            </div>
          }
          actions={strategies.length > 1 && <button className="btn small" onClick={() => setStrategies(strategies.filter((_, j) => j !== i))}>Retirer</button>}
        >
          <div className="panel-b">
            <StrategyEditor catalog={catalog} value={s} onChange={(v) => update(i, v)} instruments={catalog.instruments} />
          </div>
        </Panel>
      ))}
      <div>
        <button className="btn" onClick={() => setStrategies([...strategies, defaultStrategy(catalog, `strategie-${strategies.length + 1}`)])}>
          Ajouter une stratégie
        </button>
      </div>
      <Panel title="Risque">
        <div className="panel-b">
          <RiskEditor catalog={catalog} value={risk} onChange={setRisk} />
        </div>
      </Panel>
      <div>
        <button className="btn primary" disabled={busy || strategies.some((s) => s.instruments.length === 0)} onClick={() => onSubmit(strategies, risk)}>
          {submitLabel}
        </button>
      </div>
    </>
  );
}

export function BotNew() {
  const nav = useNavigate();
  const toast = useToast();
  const { data: catalog } = useLoad<Catalog>("/api/catalog");
  const { data: brokers } = useLoad<BrokerConn[]>("/api/brokers");
  const [name, setName] = useState("");
  const [conn, setConn] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const practice = (brokers ?? []).filter((b) => b.environment === "practice");
  useEffect(() => {
    if (!conn && practice[0]) setConn(practice[0].id);
  }, [practice, conn]);
  if (!catalog || !brokers) return <p className="muted">Chargement…</p>;
  return (
    <>
      <div className="page-head"><h1>Nouveau bot</h1></div>
      {error && <Notice tone="error">{error}</Notice>}
      {practice.length === 0 && <Notice tone="warn">Connecte d'abord un compte démo dans les <Link to="/reglages">réglages</Link>.</Notice>}
      <Panel title="Général">
        <div className="panel-b">
          <div className="row2">
            <Field label="Nom" htmlFor="name"><input id="name" value={name} onChange={(e) => setName(e.target.value)} /></Field>
            <Field label="Compte démo" htmlFor="conn">
              <select id="conn" value={conn} onChange={(e) => setConn(e.target.value)}>
                {practice.map((b) => <option key={b.id} value={b.id}>{b.label} ({b.account_id})</option>)}
              </select>
            </Field>
          </div>
        </div>
      </Panel>
      <ConfigForm
        catalog={catalog}
        initial={{ strategies: [defaultStrategy(catalog, "strategie-1")], risk: defaultRisk(catalog) }}
        submitLabel="Créer le bot"
        busy={busy || !name || !conn}
        onSubmit={async (strategies, risk) => {
          setBusy(true);
          setError(null);
          try {
            const b = await api.post<Bot>("/api/bots", { name, broker_connection_id: conn, ...botPayload(strategies, risk) });
            toast("Bot créé");
            nav(`/bots/${b.id}`);
          } catch (e) {
            setError((e as ApiError).message);
          } finally {
            setBusy(false);
          }
        }}
      />
    </>
  );
}

type Readiness = { ready: boolean; checks: { label: string; ok: boolean; value?: number }[]; confirmation: string };

export function BotDetail() {
  const { id } = useParams();
  const nav = useNavigate();
  const toast = useToast();
  const { data: bot, reload, error: loadError } = useLoad<Bot>(`/api/bots/${id}`);
  const { data: catalog } = useLoad<Catalog>("/api/catalog");
  const { data: brokers } = useLoad<BrokerConn[]>("/api/brokers");
  const { data: equity } = useLoad<{ ts: string; equity: string }[]>(`/api/bots/${id}/equity?days=90`);
  const { data: readiness } = useLoad<Readiness>(bot?.mode === "paper" ? `/api/bots/${id}/live-readiness` : null);
  const [edit, setEdit] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [live, setLive] = useState({ broker_connection_id: "", password: "", code: "", confirmation: "" });
  const [armDelete, setArmDelete] = useState(false);

  const run = async (fn: () => Promise<unknown>, msg: string) => {
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

  if (loadError) return <Notice tone="error">{loadError}</Notice>;
  if (!bot || !catalog) return <p className="muted">Chargement…</p>;
  const running = bot.status === "running" || bot.status === "starting";
  const liveConns = (brokers ?? []).filter((b) => b.environment === "live");
  const practiceConns = (brokers ?? []).filter((b) => b.environment === "practice");

  return (
    <>
      <div className="page-head">
        <h1>{bot.name}</h1>
        <StatusPill status={bot.status} />
        <ModePill mode={bot.mode} />
        <span className="spacer" />
        <Link className="btn" to={`/?bot=${bot.id}`}>Tableau de bord</Link>
        <Link className="btn" to={`/backtests?bot=${bot.id}`}>Backtest</Link>
      </div>
      {error && <Notice tone="error">{error}</Notice>}
      {bot.last_error && <Notice tone="error">Dernière erreur : {bot.last_error}</Notice>}

      <Panel title="Équité (90 jours)">
        <div className="panel-b">
          <EquityChart points={(equity ?? []).map((p) => ({ ts: p.ts, equity: Number(p.equity) }))} />
        </div>
      </Panel>

      {!edit ? (
        <Panel title="Réglages" actions={<button className="btn small" disabled={running} title={running ? "Arrête le bot pour modifier ses réglages" : undefined} onClick={() => setEdit(true)}>Modifier</button>}>
          <div className="panel-b">
            {running && <p className="muted" style={{ fontSize: 13 }}>Arrête le bot pour modifier ses réglages.</p>}
            <div className="table-wrap">
              <table>
                <thead><tr><th>Stratégie</th><th>Modèle</th><th>Unité</th><th>Paires</th><th>Paramètres</th></tr></thead>
                <tbody>
                  {bot.config.strategies.map((s) => (
                    <tr key={s.id}>
                      <td><strong>{s.id}</strong> <span className="muted num">v{s.version}</span></td>
                      <td>{catalog.strategies.find((c) => c.kind === s.kind)?.name ?? s.kind}</td>
                      <td>{s.timeframe}</td>
                      <td>{s.instruments.join(", ")}</td>
                      <td className="num" style={{ whiteSpace: "normal" }}>
                        {Object.entries(s.params).filter(([, v]) => v != null && !(Array.isArray(v) && v.length === 0)).map(([k, v]) => `${k}=${Array.isArray(v) ? v.join("+") : String(v)}`).join(", ")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="muted" style={{ fontSize: 13 }}>
              Risque : {bot.config.risk.risk_per_trade_pct} % par trade · {bot.config.risk.max_open_positions} positions max · perte du jour max {bot.config.risk.max_daily_loss_pct} % · drawdown max {bot.config.risk.max_drawdown_pct} %
            </p>
          </div>
        </Panel>
      ) : (
        <ConfigForm
          catalog={catalog}
          initial={{
            strategies: bot.config.strategies,
            risk: Object.fromEntries(Object.entries(bot.config.risk).map(([k, v]) => [k, Number(v)])),
          }}
          submitLabel="Enregistrer"
          busy={busy}
          onSubmit={(strategies, risk) =>
            void run(async () => {
              await api.patch(`/api/bots/${bot.id}`, botPayload(strategies, risk));
              setEdit(false);
            }, "Réglages enregistrés")
          }
        />
      )}

      {bot.mode === "paper" && readiness && (
        <Panel title="Passer en argent réel">
          <div className="panel-b">
            <p className="muted">Le passage en réel n'est possible qu'après une période de démo suffisante, avec ton mot de passe et ton code de double authentification.</p>
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {readiness.checks.map((c) => (
                <li key={c.label} className={c.ok ? "up" : "down"}>
                  {c.ok ? "✓" : "✗"} {c.label}{c.value !== undefined ? ` (actuellement : ${c.value})` : ""}
                </li>
              ))}
            </ul>
            {readiness.ready && (
              <form
                className="panel-b"
                style={{ padding: 0 }}
                onSubmit={(e) => {
                  e.preventDefault();
                  void run(() => api.post(`/api/bots/${bot.id}/live`, live), "Bot configuré en argent réel : démarre-le quand tu es prêt");
                }}
              >
                <Notice tone="warn">En réel, chaque perte est une vraie perte. Commence avec un petit capital.</Notice>
                {liveConns.length === 0 ? (
                  <p>Connecte d'abord un compte réel OANDA dans les <Link to="/reglages">réglages</Link>.</p>
                ) : (
                  <>
                    <Field label="Compte réel" htmlFor="live-conn">
                      <select id="live-conn" value={live.broker_connection_id} onChange={(e) => setLive({ ...live, broker_connection_id: e.target.value })}>
                        <option value="">Choisir…</option>
                        {liveConns.map((b) => <option key={b.id} value={b.id}>{b.label} ({b.account_id})</option>)}
                      </select>
                    </Field>
                    <div className="row2">
                      <Field label="Mot de passe" htmlFor="live-pw"><input id="live-pw" type="password" autoComplete="current-password" value={live.password} onChange={(e) => setLive({ ...live, password: e.target.value })} /></Field>
                      <Field label="Code de double authentification" htmlFor="live-code"><input id="live-code" inputMode="numeric" value={live.code} onChange={(e) => setLive({ ...live, code: e.target.value })} /></Field>
                    </div>
                    <Field label={`Recopie : ${readiness.confirmation}`} htmlFor="live-confirm">
                      <input id="live-confirm" value={live.confirmation} onChange={(e) => setLive({ ...live, confirmation: e.target.value })} />
                    </Field>
                    <div><button className="btn danger solid" disabled={busy}>Passer ce bot en argent réel</button></div>
                  </>
                )}
              </form>
            )}
          </div>
        </Panel>
      )}

      {bot.mode === "live" && (
        <Panel title="Revenir en démo">
          <div className="panel-b">
            <p className="muted">Toujours possible. Les positions réelles déjà ouvertes restent chez le courtier avec leur stop.</p>
            <div className="row">
              {practiceConns.map((b) => (
                <button key={b.id} className="btn" disabled={busy} onClick={() => void run(() => api.post(`/api/bots/${bot.id}/paper`, { broker_connection_id: b.id }), "Bot repassé en démo")}>
                  Revenir sur {b.label}
                </button>
              ))}
            </div>
          </div>
        </Panel>
      )}

      <Panel title="Zone sensible">
        <div className="panel-b">
          <div>
            <button
              className={`btn danger ${armDelete ? "solid" : ""}`}
              disabled={busy || running}
              onClick={() => {
                if (!armDelete) return setArmDelete(true);
                void run(async () => {
                  await api.del(`/api/bots/${bot.id}`);
                  nav("/bots");
                }, "Bot supprimé");
              }}
              onBlur={() => setArmDelete(false)}
            >
              {armDelete ? "Confirmer la suppression (historique compris)" : "Supprimer ce bot"}
            </button>
          </div>
          {running && <p className="muted" style={{ fontSize: 13 }}>Arrête le bot avant de le supprimer.</p>}
        </div>
      </Panel>
    </>
  );
}
