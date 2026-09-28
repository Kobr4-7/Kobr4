import { useEffect, useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { api, ApiError, type BrokerAccount, type BrokerConn, type Catalog, type StrategyConfig, type User } from "../api";
import { RiskEditor, StrategyEditor, botPayload, defaultRisk, defaultStrategy, type RiskValues } from "../components/editors";
import { Field, Notice } from "../components/ui";
import { useAuth, useLoad, useToast } from "../context";

const ORDER = ["profile", "security", "broker", "notifications", "setup"] as const;
const TITLES: Record<string, string> = {
  profile: "Ton profil",
  security: "Sécuriser ton compte",
  broker: "Connecter ton courtier",
  notifications: "Recevoir les alertes",
  setup: "Ton premier bot",
};

export function Onboarding() {
  const { user } = useAuth();
  if (!user) return <Navigate to="/connexion" replace />;
  if (user.onboarding_step === "done") return <Navigate to="/" replace />;
  const idx = ORDER.indexOf(user.onboarding_step as (typeof ORDER)[number]);
  return (
    <div className="center">
      <div className="card wide">
        <div className="steps" aria-label={`Étape ${idx + 1} sur ${ORDER.length}`}>
          {ORDER.map((s, i) => (
            <span key={s} className={i <= idx ? "done" : ""} />
          ))}
        </div>
        <div>
          <div className="label">
            Étape {idx + 1} sur {ORDER.length}
          </div>
          <h1>{TITLES[user.onboarding_step]}</h1>
        </div>
        {user.onboarding_step === "profile" && <ProfileStep />}
        {user.onboarding_step === "security" && <SecurityStep />}
        {user.onboarding_step === "broker" && <BrokerStep />}
        {user.onboarding_step === "notifications" && <NotificationsStep />}
        {user.onboarding_step === "setup" && <SetupStep />}
      </div>
    </div>
  );
}

function useStep() {
  const { setUser } = useAuth();
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
  return { error, busy, run, setUser };
}

function ProfileStep() {
  const { user } = useAuth();
  const { error, busy, run, setUser } = useStep();
  const [name, setName] = useState(user?.display_name ?? "");
  const [tz, setTz] = useState(Intl.DateTimeFormat().resolvedOptions().timeZone || "Europe/Paris");
  return (
    <form
      className="panel-b"
      style={{ padding: 0 }}
      onSubmit={(e) => {
        e.preventDefault();
        void run(async () => setUser(await api.patch<User>("/api/profile", { display_name: name, timezone: tz })));
      }}
    >
      <p className="muted">Quelques réglages, puis on sécurise le compte avant de le relier à ton argent.</p>
      {error && <Notice tone="error">{error}</Notice>}
      <Field label="Prénom ou pseudo" htmlFor="name">
        <input id="name" value={name} onChange={(e) => setName(e.target.value)} />
      </Field>
      <Field label="Fuseau horaire" hint="Pour afficher les heures chez toi. Le bot, lui, travaille en UTC." htmlFor="tz">
        <input id="tz" value={tz} onChange={(e) => setTz(e.target.value)} />
      </Field>
      <button className="btn primary" disabled={busy}>Continuer</button>
    </form>
  );
}

function SecurityStep() {
  const { error, busy, run, setUser } = useStep();
  const [setup, setSetup] = useState<{ secret: string; qr_svg: string } | null>(null);
  const [code, setCode] = useState("");
  const [codes, setCodes] = useState<{ codes: string[]; user: User } | null>(null);

  if (codes) {
    return (
      <div className="panel-b" style={{ padding: 0 }}>
        <Notice tone="ok">Double authentification activée.</Notice>
        <p>
          Voici tes <strong>codes de secours</strong>. Chacun remplace une fois le code de ton téléphone si tu le perds. Note-les
          dans un endroit sûr : ils ne seront plus affichés.
        </p>
        <div className="codes">{codes.codes.map((c) => <span key={c}>{c}</span>)}</div>
        <button className="btn primary" onClick={() => setUser(codes.user)}>J'ai noté mes codes, continuer</button>
      </div>
    );
  }
  return (
    <div className="panel-b" style={{ padding: 0 }}>
      <p>
        Ton compte donnera accès à ton courtier. On ajoute donc un code à usage unique, généré par une application sur ton
        téléphone (Google Authenticator, 1Password, Authy…). C'est obligatoire avant de connecter un courtier.
      </p>
      {error && <Notice tone="error">{error}</Notice>}
      {!setup ? (
        <button className="btn primary" disabled={busy} onClick={() => void run(async () => setSetup(await api.post("/api/auth/2fa/setup")))}>
          Configurer la double authentification
        </button>
      ) : (
        <form
          className="panel-b"
          style={{ padding: 0 }}
          onSubmit={(e) => {
            e.preventDefault();
            void run(async () => {
              const r = await api.post<{ recovery_codes: string[]; user: User }>("/api/auth/2fa/enable", { code });
              setCodes({ codes: r.recovery_codes, user: r.user });
            });
          }}
        >
          <p>1. Scanne ce QR code avec ton application.</p>
          <div className="qr" dangerouslySetInnerHTML={{ __html: setup.qr_svg }} />
          <p className="muted" style={{ fontSize: 12.5 }}>
            Ou saisis la clé à la main : <span className="num">{setup.secret}</span>
          </p>
          <Field label="2. Saisis le code à 6 chiffres affiché par l'application" htmlFor="code">
            <input id="code" inputMode="numeric" autoComplete="one-time-code" value={code} onChange={(e) => setCode(e.target.value)} />
          </Field>
          <button className="btn primary" disabled={busy}>Activer</button>
        </form>
      )}
    </div>
  );
}

function BrokerStep() {
  const { error, busy, run, setUser } = useStep();
  const [token, setToken] = useState("");
  const [accounts, setAccounts] = useState<BrokerAccount[] | null>(null);
  const [account, setAccount] = useState("");
  return (
    <div className="panel-b" style={{ padding: 0 }}>
      <p>
        Le bot passe ses ordres chez <strong>OANDA</strong>. On commence toujours sur un <strong>compte démo</strong> (argent fictif).
      </p>
      <ol className="muted" style={{ margin: 0, paddingLeft: 20, display: "flex", flexDirection: "column", gap: 4 }}>
        <li>
          Ouvre un compte démo gratuit sur{" "}
          <a href="https://www.oanda.com/" target="_blank" rel="noreferrer">oanda.com</a>.
        </li>
        <li>Dans ton espace OANDA, ouvre « Gérer l'accès à l'API » et génère un jeton.</li>
        <li>Colle le jeton ici : il est vérifié auprès d'OANDA puis enregistré chiffré.</li>
      </ol>
      {error && <Notice tone="error">{error}</Notice>}
      <Field label="Jeton API OANDA (compte démo)" htmlFor="token">
        <input id="token" type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} />
      </Field>
      {!accounts ? (
        <button
          className="btn primary"
          disabled={busy || token.length < 10}
          onClick={() =>
            void run(async () => {
              const list = await api.post<BrokerAccount[]>("/api/brokers/accounts", { token, environment: "practice" });
              setAccounts(list);
              setAccount(list[0]?.id ?? "");
            })
          }
        >
          Chercher mes comptes
        </button>
      ) : (
        <>
          <div className="field">
            <span className="flabel">Compte à utiliser</span>
            <div className="choice">
              {accounts.map((a) => (
                <button key={a.id} type="button" aria-pressed={account === a.id} onClick={() => setAccount(a.id)}>
                  <span className="t num">{a.id}</span>
                  <span className="d">
                    {a.alias || "Compte démo"} · {Number(a.balance).toLocaleString("fr-FR")} {a.currency}
                  </span>
                </button>
              ))}
            </div>
          </div>
          <button
            className="btn primary"
            disabled={busy || !account}
            onClick={() =>
              void run(async () => {
                await api.post<BrokerConn>("/api/brokers", { token, account_id: account, environment: "practice" });
                setUser(await api.get<User>("/api/auth/me"));
              })
            }
          >
            Connecter ce compte
          </button>
        </>
      )}
    </div>
  );
}

function NotificationsStep() {
  const { error, busy, run, setUser } = useStep();
  const toast = useToast();
  const [token, setToken] = useState("");
  const [chat, setChat] = useState("");
  const [trades, setTrades] = useState(true);
  const [saved, setSaved] = useState(false);
  return (
    <div className="panel-b" style={{ padding: 0 }}>
      <p>
        Facultatif, mais utile : le bot t'écrit sur <strong>Telegram</strong> en cas d'arrêt d'urgence, de limite atteinte, de coupure
        des cotations, et (si tu veux) à chaque trade.
      </p>
      <ol className="muted" style={{ margin: 0, paddingLeft: 20, display: "flex", flexDirection: "column", gap: 4 }}>
        <li>Dans Telegram, écris à <span className="num">@BotFather</span>, envoie <span className="num">/newbot</span> et copie le jeton donné.</li>
        <li>Écris un message à ton nouveau bot, puis à <span className="num">@userinfobot</span> pour obtenir ton identifiant (un nombre).</li>
      </ol>
      {error && <Notice tone="error">{error}</Notice>}
      <div className="row2">
        <Field label="Jeton du bot Telegram" htmlFor="tg-token">
          <input id="tg-token" type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} />
        </Field>
        <Field label="Ton identifiant Telegram" htmlFor="tg-chat">
          <input id="tg-chat" inputMode="numeric" value={chat} onChange={(e) => setChat(e.target.value)} />
        </Field>
      </div>
      <label className="row" style={{ fontSize: 13.5 }}>
        <input type="checkbox" checked={trades} onChange={(e) => setTrades(e.target.checked)} /> Me prévenir aussi à chaque ouverture et
        fermeture de position
      </label>
      <div className="row">
        <button
          className="btn"
          disabled={busy || !token || !chat}
          onClick={() =>
            void run(async () => {
              await api.put("/api/notifications", { telegram_token: token, telegram_chat_id: chat, notify_trades: trades });
              setSaved(true);
              await api.post("/api/notifications/test");
              toast("Message de test envoyé : vérifie Telegram.");
            })
          }
        >
          Enregistrer et tester
        </button>
        <button
          className="btn primary"
          disabled={busy || (!saved && !!token)}
          onClick={() =>
            void run(async () => {
              if (!saved && !token) setUser(await api.post<User>("/api/onboarding/skip-notifications"));
              else setUser(await api.get<User>("/api/auth/me"));
            })
          }
        >
          {token ? "Continuer" : "Passer cette étape"}
        </button>
      </div>
    </div>
  );
}

function SetupStep() {
  const { error, busy, run, setUser } = useStep();
  const nav = useNavigate();
  const { data: catalog } = useLoad<Catalog>("/api/catalog");
  const { data: brokers } = useLoad<BrokerConn[]>("/api/brokers");
  const [name, setName] = useState("Mon premier bot");
  const [strategy, setStrategy] = useState<StrategyConfig | null>(null);
  const [risk, setRisk] = useState<RiskValues | null>(null);
  useEffect(() => {
    if (catalog && !strategy) {
      setStrategy(defaultStrategy(catalog, "tendance-h1"));
      setRisk(defaultRisk(catalog));
    }
  }, [catalog, strategy]);
  const practice = brokers?.find((b) => b.environment === "practice");
  if (!catalog || !strategy || !risk) return <p className="muted">Chargement…</p>;
  return (
    <div className="panel-b" style={{ padding: 0 }}>
      <p>
        Choisis une stratégie et un niveau de risque. Le bot démarre en <strong>démo</strong> : tu pourras tout ajuster, lancer des
        backtests, puis passer en réel après au moins 4 semaines de démo.
      </p>
      {error && <Notice tone="error">{error}</Notice>}
      <Field label="Nom du bot" htmlFor="bot-name">
        <input id="bot-name" value={name} onChange={(e) => setName(e.target.value)} />
      </Field>
      <h2>Stratégie</h2>
      <StrategyEditor catalog={catalog} value={strategy} onChange={setStrategy} instruments={catalog.instruments} />
      <h2>Risque</h2>
      <RiskEditor catalog={catalog} value={risk} onChange={setRisk} />
      <button
        className="btn primary"
        disabled={busy || !practice || strategy.instruments.length === 0}
        onClick={() =>
          void run(async () => {
            const r = await api.post<{ bot_id: string; start_error: string | null; user: User }>("/api/onboarding/finish", {
              name,
              broker_connection_id: practice!.id,
              ...botPayload([strategy], risk),
              start: true,
            });
            setUser(r.user);
            nav(`/?bot=${r.bot_id}`);
          })
        }
      >
        Créer le bot et le démarrer en démo
      </button>
    </div>
  );
}
