// Création d'un compte démo interne : argent fictif, prix réels (clé Finnhub gratuite).

import { useState } from "react";
import { api, ApiError, type BrokerConn } from "../api";
import { Field, Notice } from "./ui";

export function DemoAccountForm({ onDone }: { onDone: (c: BrokerConn) => void | Promise<void> }) {
  const [key, setKey] = useState("");
  const [balance, setBalance] = useState(10000);
  const [currency, setCurrency] = useState<"USD" | "EUR">("USD");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  return (
    <>
      <ol className="muted" style={{ margin: 0, paddingLeft: 20, display: "flex", flexDirection: "column", gap: 4 }}>
        <li>
          Crée un compte gratuit sur{" "}
          <a href="https://finnhub.io/register" target="_blank" rel="noreferrer">finnhub.io</a> : il fournit les prix du marché en direct.
        </li>
        <li>Sur ton tableau de bord Finnhub, copie ta clé API (« API key »).</li>
        <li>Colle-la ici : elle est vérifiée puis enregistrée chiffrée.</li>
      </ol>
      <Field label="Clé API Finnhub" htmlFor="demo-key">
        <input id="demo-key" type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)} />
      </Field>
      <div className="row2">
        <Field label="Capital de départ (fictif)" htmlFor="demo-balance">
          <input id="demo-balance" type="number" min={1000} max={1000000} step={1000} value={balance} onChange={(e) => setBalance(Number(e.target.value))} />
        </Field>
        <Field label="Devise du compte" htmlFor="demo-ccy">
          <select id="demo-ccy" value={currency} onChange={(e) => setCurrency(e.target.value as "USD" | "EUR")}>
            <option value="USD">Dollar (USD)</option>
            <option value="EUR">Euro (EUR)</option>
          </select>
        </Field>
      </div>
      <div>
        <button
          className="btn primary"
          disabled={busy || key.trim().length < 10 || balance < 1000}
          onClick={async () => {
            setBusy(true);
            setError(null);
            try {
              const c = await api.post<BrokerConn>("/api/brokers/demo", { finnhub_key: key.trim(), initial_balance: balance, currency });
              setKey("");
              await onDone(c);
            } catch (e) {
              setError((e as ApiError).message);
            } finally {
              setBusy(false);
            }
          }}
        >
          {busy ? "Vérification de la clé…" : "Créer le compte démo"}
        </button>
      </div>
      {key.trim().length > 0 && key.trim().length < 10 && <Notice tone="warn">La clé semble incomplète : copie-la en entier depuis Finnhub.</Notice>}
      {error && <Notice tone="error">{error}</Notice>}
    </>
  );
}
