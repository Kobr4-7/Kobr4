// Connexion d'un compte Saxo : clé de l'application, passage par la page de connexion
// de Saxo, puis choix du compte au retour (paramètre ?saxo=… dans l'adresse).

import { useEffect, useState } from "react";
import { api, ApiError, type BrokerConn } from "../api";
import { Field, Notice } from "./ui";

type SaxoAccount = { id: string; alias: string; currency: string; account_number: string };

function returnParams(): { state: string | null; error: string | null } {
  const q = new URLSearchParams(window.location.search);
  return { state: q.get("saxo"), error: q.get("saxo_error") };
}

function clearReturnParams() {
  const url = new URL(window.location.href);
  url.searchParams.delete("saxo");
  url.searchParams.delete("saxo_error");
  window.history.replaceState(null, "", url.pathname + url.search);
}

export function saxoReturnPending(): boolean {
  const { state, error } = returnParams();
  return Boolean(state || error);
}

export function SaxoConnect({ allowLive, onDone }: { allowLive: boolean; onDone: (c: BrokerConn) => void | Promise<void> }) {
  const [env, setEnv] = useState<"practice" | "live">("practice");
  const [appKey, setAppKey] = useState("");
  const [appSecret, setAppSecret] = useState("");
  const [redirectUri, setRedirectUri] = useState("");
  const [state, setState] = useState<string | null>(null);
  const [accounts, setAccounts] = useState<SaxoAccount[] | null>(null);
  const [account, setAccount] = useState("");
  const [label, setLabel] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    api.get<{ redirect_uri: string }>("/api/brokers/saxo/redirect-uri").then((r) => setRedirectUri(r.redirect_uri), () => undefined);
    const back = returnParams();
    if (back.error) setError(back.error);
    if (back.state) {
      const s = back.state;
      setState(s);
      void run(async () => {
        const r = await api.get<{ environment: "practice" | "live"; accounts: SaxoAccount[] }>(`/api/brokers/saxo/pending/${encodeURIComponent(s)}`);
        setEnv(r.environment);
        setAccounts(r.accounts);
        setAccount(r.accounts[0]?.id ?? "");
      });
    }
    if (back.error || back.state) clearReturnParams();
  }, []);

  if (accounts && state) {
    return (
      <>
        {error && <Notice tone="error">{error}</Notice>}
        <Notice tone="ok">Connexion Saxo réussie. Choisis le compte que les bots utiliseront.</Notice>
        {accounts.length === 0 ? (
          <Notice tone="warn">Aucun compte actif trouvé sur cette connexion Saxo.</Notice>
        ) : (
          <div className="field">
            <span className="flabel">Compte à utiliser</span>
            <div className="choice">
              {accounts.map((a) => (
                <button key={a.id} type="button" aria-pressed={account === a.id} onClick={() => setAccount(a.id)}>
                  <span className="t num">{a.account_number || a.id}</span>
                  <span className="d">
                    {a.alias || (env === "live" ? "Compte réel" : "Compte de simulation")} · {a.currency}
                  </span>
                </button>
              ))}
            </div>
          </div>
        )}
        <Field label="Nom affiché (facultatif)" htmlFor="sx-label">
          <input id="sx-label" value={label} onChange={(e) => setLabel(e.target.value)} />
        </Field>
        <div>
          <button
            className="btn primary"
            disabled={busy || !account}
            onClick={() =>
              void run(async () => {
                const c = await api.post<BrokerConn>("/api/brokers/saxo/finish", { state, account_id: account, label });
                setAccounts(null);
                setState(null);
                await onDone(c);
              })
            }
          >
            Connecter ce compte
          </button>
        </div>
      </>
    );
  }

  return (
    <>
      <ol className="muted" style={{ margin: 0, paddingLeft: 20, display: "flex", flexDirection: "column", gap: 4 }}>
        <li>
          Crée un compte développeur gratuit sur{" "}
          <a href="https://www.developer.saxo/" target="_blank" rel="noreferrer">developer.saxo</a>{" "}
          {env === "live" ? "et une application « Live » liée à ton compte réel." : "(environnement de simulation, argent fictif)."}
        </li>
        <li>
          Crée une application de type <strong>« Code »</strong> (Authorization Code Grant) avec cette adresse de retour :
          <br />
          <span className="num">{redirectUri || "…"}</span>
        </li>
        <li>Copie ici la clé (App Key) et le secret (App Secret) de l'application, puis connecte-toi chez Saxo.</li>
      </ol>
      {error && <Notice tone="error">{error}</Notice>}
      {allowLive && (
        <Field label="Type de compte" htmlFor="sx-env">
          <select id="sx-env" value={env} onChange={(e) => setEnv(e.target.value as "practice" | "live")}>
            <option value="practice">Simulation (argent fictif)</option>
            <option value="live">Réel</option>
          </select>
        </Field>
      )}
      {env === "live" && <Notice tone="warn">Un compte réel ne sert qu'aux bots passés en réel, après la période de démo obligatoire.</Notice>}
      <div className="row2">
        <Field label="App Key" htmlFor="sx-key">
          <input id="sx-key" autoComplete="off" value={appKey} onChange={(e) => setAppKey(e.target.value)} />
        </Field>
        <Field label="App Secret" htmlFor="sx-secret">
          <input id="sx-secret" type="password" autoComplete="off" value={appSecret} onChange={(e) => setAppSecret(e.target.value)} />
        </Field>
      </div>
      <div>
        <button
          className="btn primary"
          disabled={busy || appKey.trim().length < 8 || appSecret.trim().length < 8}
          onClick={() =>
            void run(async () => {
              const r = await api.post<{ authorize_url: string }>("/api/brokers/saxo/start", { environment: env, app_key: appKey, app_secret: appSecret });
              window.location.href = r.authorize_url;
            })
          }
        >
          Se connecter chez Saxo
        </button>
      </div>
    </>
  );
}
