import type { Catalog, ParamSchema, StrategyConfig } from "../api";

export type RiskValues = Record<string, number>;

const RISK_FIELDS: { key: string; label: string; hint: string; step: number }[] = [
  { key: "risk_per_trade_pct", label: "Risque par trade (%)", hint: "Perte maximale si le stop est touché", step: 0.25 },
  { key: "max_open_positions", label: "Positions ouvertes max", hint: "Toutes paires confondues", step: 1 },
  { key: "max_currency_risk_pct", label: "Risque max par devise (%)", hint: "Cumul des positions exposées à une même devise", step: 0.5 },
  { key: "max_daily_loss_pct", label: "Perte journalière max (%)", hint: "Au-delà, plus d'entrée jusqu'au lendemain", step: 0.5 },
  { key: "max_drawdown_pct", label: "Drawdown max (%)", hint: "Au-delà, arrêt d'urgence et intervention manuelle", step: 1 },
];

export function RiskEditor({ catalog, value, onChange }: { catalog: Catalog; value: RiskValues; onChange: (v: RiskValues) => void }) {
  const presetKey = Object.entries(catalog.risk_presets).find(([, p]) =>
    RISK_FIELDS.every((f) => Number(p[f.key]) === value[f.key]),
  )?.[0];
  return (
    <div className="panel-b" style={{ padding: 0 }}>
      <div className="choice" role="group" aria-label="Profil de risque">
        {Object.entries(catalog.risk_presets).map(([key, p]) => (
          <button
            key={key}
            type="button"
            aria-pressed={presetKey === key}
            onClick={() => onChange(Object.fromEntries(RISK_FIELDS.map((f) => [f.key, Number(p[f.key])])))}
          >
            <span className="t">{p.label}</span>
            <span className="d">
              {p.risk_per_trade_pct} % par trade · arrêt à −{p.max_drawdown_pct} %
            </span>
          </button>
        ))}
      </div>
      <div className="row2">
        {RISK_FIELDS.map((f) => (
          <div className="field" key={f.key}>
            <label htmlFor={`risk-${f.key}`}>{f.label}</label>
            <input
              id={`risk-${f.key}`}
              className="num"
              type="number"
              step={f.step}
              min={0}
              value={value[f.key] ?? ""}
              onChange={(e) => onChange({ ...value, [f.key]: Number(e.target.value) })}
            />
            <span className="hint">{f.hint}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

const PARAM_LABELS: Record<string, string> = {
  fast: "Moyenne rapide (bougies)",
  slow: "Moyenne lente (bougies)",
  stop_loss_pips: "Stop loss (pips)",
  take_profit_pips: "Take profit (pips)",
  period: "Période du RSI",
  oversold: "Seuil de survente",
  overbought: "Seuil de surachat",
  adx_max: "ADX max (0 : désactivé)",
  lookback: "Bougies du canal",
  atr_stop: "Stop en ATR (0 : en pips)",
};

function isNumeric(s: ParamSchema): boolean {
  const types = [s.type, ...(s.anyOf ?? []).map((a) => a.type)];
  return types.includes("number") || types.includes("integer") || types.includes("string");
}

export function StrategyEditor({
  catalog,
  value,
  onChange,
  instruments,
}: {
  catalog: Catalog;
  value: StrategyConfig;
  onChange: (v: StrategyConfig) => void;
  instruments: string[];
}) {
  const def = catalog.strategies.find((s) => s.kind === value.kind);
  const params = value.params;
  const setParam = (k: string, v: unknown) => onChange({ ...value, params: { ...params, [k]: v } });
  const sessions = (params.sessions as string[] | undefined) ?? [];
  return (
    <div className="panel-b" style={{ padding: 0 }}>
      <div className="choice" role="group" aria-label="Modèle de stratégie">
        {catalog.strategies.map((s) => (
          <button
            key={s.kind}
            type="button"
            aria-pressed={s.kind === value.kind}
            onClick={() => onChange({ ...value, kind: s.kind, params: { ...s.defaults, sessions, regimes: params.regimes ?? [] } })}
          >
            <span className="label">{s.family}</span>
            <span className="t">{s.name}</span>
            <span className="d">{s.summary}</span>
          </button>
        ))}
      </div>
      <div className="field">
        <span className="flabel">Paires tradées</span>
        <div className="chips">
          {instruments.map((sym) => (
            <label className="chip" key={sym}>
              <input
                type="checkbox"
                checked={value.instruments.includes(sym)}
                onChange={(e) =>
                  onChange({
                    ...value,
                    instruments: e.target.checked ? [...value.instruments, sym] : value.instruments.filter((x) => x !== sym),
                  })
                }
              />
              {sym}
            </label>
          ))}
        </div>
      </div>
      <div className="field">
        <span className="flabel">Unité de temps</span>
        <div className="seg" role="group" aria-label="Unité de temps">
          {catalog.timeframes.map((tf) => (
            <button key={tf} type="button" aria-pressed={value.timeframe === tf} onClick={() => onChange({ ...value, timeframe: tf })}>
              {tf}
            </button>
          ))}
        </div>
      </div>
      {def && (
        <div className="row2">
          {Object.entries(def.params)
            .filter(([k, s]) => k !== "sessions" && k !== "regimes" && isNumeric(s))
            .map(([k, s]) => (
              <div className="field" key={k}>
                <label htmlFor={`p-${value.id}-${k}`}>{PARAM_LABELS[k] ?? s.title ?? k}</label>
                <input
                  id={`p-${value.id}-${k}`}
                  className="num"
                  type="number"
                  step="any"
                  value={params[k] == null ? "" : String(params[k])}
                  onChange={(e) => setParam(k, e.target.value === "" ? null : Number(e.target.value))}
                />
              </div>
            ))}
        </div>
      )}
      <div className="field">
        <span className="flabel">Régimes de marché où le bot peut ouvrir des positions (aucun : tous)</span>
        <div className="chips">
          {[
            ["trend", "Tendance (ADX ≥ 25)"],
            ["range", "Range"],
            ["volatile", "Forte volatilité"],
          ].map(([id, label]) => {
            const regimes = (params.regimes as string[] | undefined) ?? [];
            return (
              <label className="chip" key={id}>
                <input
                  type="checkbox"
                  checked={regimes.includes(id!)}
                  onChange={(e) => setParam("regimes", e.target.checked ? [...regimes, id] : regimes.filter((x) => x !== id))}
                />
                {label}
              </label>
            );
          })}
        </div>
      </div>
      <div className="field">
        <span className="flabel">Sessions où le bot peut ouvrir des positions (aucune : toujours)</span>
        <div className="chips">
          {catalog.sessions.map((s) => (
            <label className="chip" key={s.id}>
              <input
                type="checkbox"
                checked={sessions.includes(s.id)}
                onChange={(e) => setParam("sessions", e.target.checked ? [...sessions, s.id] : sessions.filter((x) => x !== s.id))}
              />
              {s.label}
            </label>
          ))}
        </div>
      </div>
    </div>
  );
}

export function defaultStrategy(catalog: Catalog, id = "s1"): StrategyConfig {
  const first = catalog.strategies[0]!;
  return { id, kind: first.kind, version: 1, enabled: true, instruments: ["EUR/USD"], timeframe: "H1", params: { ...first.defaults } };
}

export function defaultRisk(catalog: Catalog): RiskValues {
  const p = catalog.risk_presets["equilibre"] ?? Object.values(catalog.risk_presets)[0]!;
  return Object.fromEntries(RISK_FIELDS.map((f) => [f.key, Number(p[f.key])]));
}

export function botPayload(strategies: StrategyConfig[], risk: RiskValues) {
  const instruments = [...new Set(strategies.flatMap((s) => s.instruments))];
  return {
    instruments,
    risk,
    strategies: strategies.map((s) => ({ ...s, id: s.id || `${s.kind}-${s.timeframe}`.toLowerCase() })),
  };
}
