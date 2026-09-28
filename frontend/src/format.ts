const nf2 = new Intl.NumberFormat("fr-FR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const nf0 = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 0 });

export const num = (v: string | number | null | undefined): number => (v == null ? NaN : Number(v));

export function money(v: string | number | null | undefined, ccy = "$"): string {
  const n = num(v);
  if (!Number.isFinite(n)) return "—";
  return `${n < 0 ? "−" : ""}${nf2.format(Math.abs(n))} ${ccy}`;
}

export function signed(v: string | number | null | undefined, ccy = "$"): string {
  const n = num(v);
  if (!Number.isFinite(n)) return "—";
  return `${n > 0 ? "+" : n < 0 ? "−" : ""}${nf2.format(Math.abs(n))} ${ccy}`;
}

export function pct(v: string | number | null | undefined, sign = false): string {
  const n = num(v);
  if (!Number.isFinite(n)) return "—";
  const s = sign && n > 0 ? "+" : n < 0 ? "−" : "";
  return `${s}${nf2.format(Math.abs(n))} %`;
}

export const int = (v: number): string => nf0.format(v);
export const cls = (v: string | number | null | undefined): string => {
  const n = num(v);
  return n > 0 ? "up" : n < 0 ? "down" : "";
};

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" });
}

export function time(iso: string): string {
  return new Date(iso).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export const STATUS_LABEL: Record<string, string> = {
  running: "En marche",
  stopped: "Arrêté",
  starting: "Démarrage",
  error: "En erreur",
};

export const REASON_LABEL: Record<string, string> = {
  stop_loss: "Stop loss",
  take_profit: "Take profit",
  signal: "Signal",
  manual: "Manuel",
  kill_switch: "Arrêt d'urgence",
  end_of_test: "Fin du test",
};

export const RULE_LABEL: Record<string, string> = {
  kill_switch: "Arrêt d'urgence actif",
  max_daily_loss: "Perte journalière max",
  stale_data: "Cotations interrompues",
  news_blackout: "Annonce économique",
  no_price: "Prix indisponible",
  spread: "Spread trop large",
  max_open_positions: "Trop de positions",
  max_positions_per_symbol_strategy: "Déjà une position",
  size_too_small: "Taille trop petite",
  currency_exposure: "Exposition par devise",
  margin: "Marge insuffisante",
};
