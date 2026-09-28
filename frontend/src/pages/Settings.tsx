import { useState } from "react";
import { api, ApiError, type BrokerConn, type User } from "../api";
import { DemoAccountForm } from "../components/demo";
import { UpdatePanel } from "../components/update";
import { Field, Notice, Panel } from "../components/ui";
import { useAuth, useLoad, useToast } from "../context";
import { dateTime } from "../format";

function useAction() {
  const toast = useToast();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const run = async (fn: () => Promise<unknown>, msg?: string) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      if (msg) toast(msg);
      return true;
    } catch (e) {
      setError((e as ApiError).message);
      return false;
    } finally {
      setBusy(false);
    }
  };
  return { error, busy, run };
}

export function Settings() {
  const { user } = useAuth();
  return (
    <>
      <div className="page-head"><h1>Réglages</h1></div>
      {user?.is_admin && <UpdatePanel />}
      <div className="grid2">
        <Profile />
        <Password />
        <TwoFactor />
        <Sessions />
      </div>
      <Brokers />
      <Notifications />
    </>
  );
}

function Profile() {
  const { user, setUser } = useAuth();
  const { error, busy, run } = useAction();
  const [name, setName] = useState(user?.display_name ?? "");
  const [tz, setTz] = useState(user?.timezone ?? "Europe/Paris");
  return (
    <Panel title="Profil">
      <form className="panel-b" onSubmit={(e) => { e.preventDefault(); void run(async () => setUser(await api.patch<User>("/api/profile", { display_name: name, timezone: tz })), "Profil enregistré"); }}>
        {error && <Notice tone="error">{error}</Notice>}
        <p className="muted" style={{ fontSize: 13 }}>{user?.email}</p>
        <Field label="Prénom ou pseudo" htmlFor="pf-name"><input id="pf-name" value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <Field label="Fuseau horaire" htmlFor="pf-tz"><input id="pf-tz" value={tz} onChange={(e) => setTz(e.target.value)} /></Field>
        <div><button className="btn primary" disabled={busy}>Enregistrer</button></div>
      </form>
    </Panel>
  );
}

function Password() {
  const { error, busy, run } = useAction();
  const [f, setF] = useState({ current_password: "", new_password: "" });
  return (
    <Panel title="Mot de passe">
      <form className="panel-b" onSubmit={(e) => { e.preventDefault(); void run(async () => { await api.post("/api/auth/password", f); setF({ current_password: "", new_password: "" }); }, "Mot de passe changé : tes autres sessions sont déconnectées"); }}>
        {error && <Notice tone="error">{error}</Notice>}
        <Field label="Mot de passe actuel" htmlFor="pw-cur"><input id="pw-cur" type="password" autoComplete="current-password" value={f.current_password} onChange={(e) => setF({ ...f, current_password: e.target.value })} /></Field>
        <Field label="Nouveau mot de passe" hint="12 caractères minimum." htmlFor="pw-new"><input id="pw-new" type="password" autoComplete="new-password" value={f.new_password} onChange={(e) => setF({ ...f, new_password: e.target.value })} /></Field>
        <div><button className="btn primary" disabled={busy}>Changer</button></div>
      </form>
    </Panel>
  );
}

function TwoFactor() {
  const { user, setUser } = useAuth();
  const { error, busy, run } = useAction();
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [codes, setCodes] = useState<string[] | null>(null);
  if (!user) return null;
  return (
    <Panel title="Double authentification">
      <div className="panel-b">
        {error && <Notice tone="error">{error}</Notice>}
        {user.totp_enabled ? (
          <>
            <Notice tone="ok">Activée · {user.recovery_codes_left} codes de secours restants</Notice>
            {codes && <div className="codes">{codes.map((c) => <span key={c}>{c}</span>)}</div>}
            <Field label="Code actuel de l'application" htmlFor="tf-code"><input id="tf-code" inputMode="numeric" value={code} onChange={(e) => setCode(e.target.value)} /></Field>
            <div className="row">
              <button className="btn" disabled={busy || !code} onClick={() => void run(async () => { const r = await api.post<{ recovery_codes: string[] }>("/api/auth/2fa/recovery-codes", { code }); setCodes(r.recovery_codes); setUser(await api.get<User>("/api/auth/me")); setCode(""); }, "Nouveaux codes de secours : note-les")}>
                Nouveaux codes de secours
              </button>
            </div>
            <Field label="Mot de passe (pour désactiver)" htmlFor="tf-pw"><input id="tf-pw" type="password" value={password} onChange={(e) => setPassword(e.target.value)} /></Field>
            <div><button className="btn danger" disabled={busy || !code || !password} onClick={() => void run(async () => setUser(await api.post<User>("/api/auth/2fa/disable", { password, code })), "Double authentification désactivée")}>Désactiver</button></div>
          </>
        ) : (
          <Notice tone="warn">Désactivée. Réactive-la depuis le parcours d'accueil pour connecter un courtier.</Notice>
        )}
      </div>
    </Panel>
  );
}

function Sessions() {
  const { data, reload } = useLoad<{ id: string; current: boolean; last_seen_at: string; user_agent: string; ip: string }[]>("/api/auth/sessions");
  const { run } = useAction();
  return (
    <Panel title="Appareils connectés">
      <div className="table-wrap">
        <table>
          <tbody>
            {(data ?? []).map((s) => (
              <tr key={s.id}>
                <td style={{ whiteSpace: "normal" }}>
                  <div style={{ fontSize: 12.5 }}>{s.user_agent.slice(0, 60) || "Navigateur"}</div>
                  <div className="muted num" style={{ fontSize: 12 }}>{s.ip} · {dateTime(s.last_seen_at)}</div>
                </td>
                <td className="r">{s.current ? <span className="pill running">Cet appareil</span> : <button className="btn small" onClick={() => void run(async () => { await api.del(`/api/auth/sessions/${s.id}`); reload(); }, "Appareil déconnecté")}>Déconnecter</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

function Brokers() {
  const { data, reload } = useLoad<BrokerConn[]>("/api/brokers");
  const { error, run } = useAction();
  const toast = useToast();
  return (
    <Panel title="Comptes démo">
      <div className="panel-b">
        {error && <Notice tone="error">{error}</Notice>}
        <div className="table-wrap">
          <table>
            <thead><tr><th>Compte</th><th>Devise</th><th>Créé le</th><th /></tr></thead>
            <tbody>
              {(data ?? []).map((b) => (
                <tr key={b.id}>
                  <td><strong>{b.label}</strong> <span className="pill paper">Démo</span></td>
                  <td>{b.account_currency}</td>
                  <td className="num">{dateTime(b.verified_at)}</td>
                  <td className="r"><button className="btn small" onClick={() => void run(async () => { await api.del(`/api/brokers/${b.id}`); reload(); }, "Compte supprimé")}>Supprimer</button></td>
                </tr>
              ))}
              {(data ?? []).length === 0 && <tr><td colSpan={4} className="empty">Aucun compte démo.</td></tr>}
            </tbody>
          </table>
        </div>
        <h3>Ajouter un compte démo</h3>
        <p className="muted">Un compte démo par bot en marche : chaque bot a son propre capital fictif.</p>
        <DemoAccountForm onDone={() => { reload(); toast("Compte démo créé"); }} />
      </div>
    </Panel>
  );
}

function Notifications() {
  const { data, reload } = useLoad<{ telegram: boolean; telegram_chat_id: string | null; notify_trades: boolean; notify_risk: boolean; notify_daily_summary: boolean }>("/api/notifications");
  const { error, busy, run } = useAction();
  const [token, setToken] = useState("");
  const [chat, setChat] = useState("");
  if (!data) return null;
  const save = (extra: Record<string, unknown>) =>
    run(async () => {
      await api.put("/api/notifications", { notify_trades: data.notify_trades, notify_risk: data.notify_risk, notify_daily_summary: data.notify_daily_summary, ...extra });
      reload();
    }, "Enregistré");
  return (
    <Panel title="Alertes Telegram">
      <div className="panel-b">
        {error && <Notice tone="error">{error}</Notice>}
        {data.telegram ? <Notice tone="ok">Telegram connecté (chat {data.telegram_chat_id}).</Notice> : <Notice tone="info">Telegram non configuré.</Notice>}
        <div className="row2">
          <Field label="Jeton du bot Telegram" htmlFor="nt-token"><input id="nt-token" type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} /></Field>
          <Field label="Identifiant du chat" htmlFor="nt-chat"><input id="nt-chat" value={chat} onChange={(e) => setChat(e.target.value)} /></Field>
        </div>
        <label className="row" style={{ fontSize: 13.5 }}><input type="checkbox" checked={data.notify_trades} onChange={(e) => void save({ notify_trades: e.target.checked })} /> Prévenir à chaque trade (les bots en marche le prennent en compte au prochain démarrage)</label>
        <div className="row">
          <button className="btn primary" disabled={busy || (!token && !chat)} onClick={() => void save({ telegram_token: token || null, telegram_chat_id: chat || null }).then(() => { setToken(""); setChat(""); })}>Enregistrer</button>
          {data.telegram && <button className="btn" disabled={busy} onClick={() => void run(() => api.post("/api/notifications/test"), "Message de test envoyé")}>Envoyer un test</button>}
          {data.telegram && <button className="btn danger" disabled={busy} onClick={() => void save({ clear_telegram: true })}>Déconnecter Telegram</button>}
        </div>
      </div>
    </Panel>
  );
}
