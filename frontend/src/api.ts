// Client de l'API. Chaque requête qui modifie quelque chose porte l'en-tête X-Kobr4
// (protection CSRF côté serveur).

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { "X-Kobr4": "1" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res: Response;
  try {
    res = await fetch(url, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
    });
  } catch {
    throw new ApiError(0, "Serveur injoignable. Vérifie ta connexion.");
  }
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    let message = `Erreur ${res.status}`;
    if (data && typeof data.detail === "string") message = data.detail;
    else if (data && Array.isArray(data.detail)) message = data.detail.map((d: { msg: string }) => d.msg).join(", ");
    message = message.charAt(0).toUpperCase() + message.slice(1);
    if (res.status === 401) window.dispatchEvent(new CustomEvent("kobr4:unauthorized", { detail: message }));
    throw new ApiError(res.status, message);
  }
  return data as T;
}

export const api = {
  get: <T>(url: string) => request<T>("GET", url),
  post: <T>(url: string, body?: unknown) => request<T>("POST", url, body ?? {}),
  put: <T>(url: string, body?: unknown) => request<T>("PUT", url, body ?? {}),
  patch: <T>(url: string, body?: unknown) => request<T>("PATCH", url, body ?? {}),
  del: <T>(url: string) => request<T>("DELETE", url),
};

// Types renvoyés par l'API

export type User = {
  id: string;
  email: string;
  display_name: string;
  totp_enabled: boolean;
  is_admin: boolean;
  onboarding_step: "profile" | "security" | "broker" | "notifications" | "setup" | "done";
  base_currency: string;
  timezone: string;
  recovery_codes_left: number;
};

export type BrokerConn = {
  id: string;
  broker: string;
  environment: "practice" | "live";
  account_id: string;
  label: string;
  account_currency: string | null;
  verified_at: string | null;
};

export type BrokerAccount = { id: string; alias: string; currency: string; balance: string };

export type StrategyConfig = {
  id: string;
  kind: string;
  version: number;
  enabled: boolean;
  instruments: string[];
  timeframe: string;
  params: Record<string, unknown>;
};

export type BotConfig = {
  base_currency: string;
  instruments: string[];
  risk: Record<string, string | number>;
  strategies: StrategyConfig[];
};

export type Position = {
  id: string;
  strategy_id: string;
  symbol: string;
  side: "buy" | "sell";
  quantity: number;
  entry_price: string;
  stop_loss: string;
  take_profit: string | null;
  opened_at: string;
  unrealized: string | null;
};

export type LiveStatus = {
  id: string;
  name: string;
  state: "stopped" | "starting" | "running" | "error";
  error: string | null;
  mode: string;
  started_at: string | null;
  connected: boolean;
  kill_switch: string | null;
  balance: string;
  equity: string;
  drawdown_pct: string;
  daily_loss_pct: string;
  weekly_pnl_pct?: string;
  weekly_loss_pct?: string;
  stale: string[];
  strategies: { id: string; kind: string; enabled: boolean; regime: Record<string, string> }[];
  positions: Position[];
  quotes: Record<string, { bid: string; ask: string; ts: string }>;
  recent: { ts: string; type: string; message: string }[];
};

export type Bot = {
  id: string;
  name: string;
  mode: "paper" | "live";
  status: "stopped" | "starting" | "running" | "error";
  desired_state: string;
  broker_connection_id: string | null;
  config: BotConfig;
  last_error: string | null;
  live_confirmed_at: string | null;
  auto_optimize: boolean;
  last_auto_optimize_at: string | null;
  created_at: string;
  live?: LiveStatus | null;
};

export type Trade = {
  position_id?: string;
  strategy_id?: string;
  symbol: string;
  side: "buy" | "sell";
  quantity: number;
  entry_price: string;
  exit_price: string;
  opened_at: string;
  closed_at: string;
  reason: string;
  pnl: string | number;
};

export type ParamSchema = {
  type?: string;
  anyOf?: { type: string }[];
  default?: unknown;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
  description?: string;
  title?: string;
};

export type Catalog = {
  strategies: {
    kind: string;
    name: string;
    family: string;
    summary: string;
    params: Record<string, ParamSchema>;
    defaults: Record<string, unknown>;
    optimizable: string[];
  }[];
  risk_presets: Record<string, Record<string, number | string> & { label: string }>;
  instruments: string[];
  timeframes: string[];
  sessions: { id: string; label: string }[];
};

export type Metrics = {
  trades: number;
  total_return_pct: number;
  cagr_pct: number;
  max_drawdown_pct: number;
  sharpe: number;
  sortino: number;
  profit_factor: number | null;
  win_rate_pct: number;
  expectancy: number;
  avg_win: number;
  avg_loss: number;
  avg_duration_hours: number;
  days: number;
};

export type BacktestRun = {
  id: string;
  created_at: string;
  status: "running" | "done" | "failed";
  config: BotConfig & { start: string | null; end: string | null; bot_id: string | null };
  result: {
    metrics: Metrics;
    equity?: { ts: string; equity: number }[];
    trades?: Trade[];
    rejections?: Record<string, number>;
    by_regime?: { regime: string; trades: number; win_rate_pct: number; pnl: number; profit_factor: number }[];
    synthetic?: boolean;
    initial_balance?: number;
    final_equity?: number;
    start?: string;
    end?: string;
  } | null;
  error: string | null;
};

export type PeriodResult = {
  period: string;
  params: Record<string, unknown>;
  trades: number;
  total_return_pct: number;
  max_drawdown_pct: number;
  sharpe: number;
};

export type Proposal = {
  id: string;
  created_at: string;
  strategy_id: string;
  kind: string;
  base_version: number;
  current_params: Record<string, unknown>;
  proposed_params: Record<string, unknown>;
  windows: PeriodResult[];
  oos_total_return_pct: number;
  oos_sharpe: number;
  oos_max_drawdown_pct: number;
  oos_trades: number;
  holdout: PeriodResult | null;
  holdout_baseline: PeriodResult | null;
  stability: number;
  deflated_sharpe: number;
  failures: string[];
  status: "proposed" | "failed" | "approved" | "rejected" | "applied";
  bot_id: string | null;
  trials: number;
};
