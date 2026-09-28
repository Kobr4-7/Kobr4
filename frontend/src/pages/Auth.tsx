import { useState, type FormEvent, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, ApiError, type User } from "../api";
import { BrandMark, IconPulse, IconShield, IconTarget } from "../components/Icons";
import { Field, Notice } from "../components/ui";
import { useAuth } from "../context";

/** Mise en page des écrans d'accès : la marque à gauche, le formulaire à droite. */
function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <div className="auth">
      <aside className="auth-side">
        <div className="brand">
          <BrandMark />
          <div>
            Kobr4<span>FX</span>
          </div>
        </div>
        <div>
          <h2>
            Le trading automatisé, <em>avec la rigueur d'un desk pro</em>.
          </h2>
          <p className="lead">
            Des stratégies testées sur des années d'historique, un risque encadré à chaque ordre, et un suivi en direct depuis
            n'importe quel écran.
          </p>
        </div>
        <svg className="auth-spark" viewBox="0 0 520 140" aria-hidden="true">
          <defs>
            <linearGradient id="auth-area" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" stopColor="#e3b85c" stopOpacity=".35" />
              <stop offset="1" stopColor="#e3b85c" stopOpacity="0" />
            </linearGradient>
          </defs>
          <path
            d="M0 118 L40 110 L80 114 L120 96 L160 101 L200 84 L240 88 L280 66 L320 72 L360 52 L400 58 L440 36 L480 40 L520 18 L520 140 L0 140 Z"
            fill="url(#auth-area)"
          />
          <path
            d="M0 118 L40 110 L80 114 L120 96 L160 101 L200 84 L240 88 L280 66 L320 72 L360 52 L400 58 L440 36 L480 40 L520 18"
            fill="none"
            stroke="#e3b85c"
            strokeWidth="2.5"
            strokeLinejoin="round"
          />
          <circle cx="520" cy="18" r="5" fill="#f0cd7f" />
        </svg>
        <ul className="auth-feats">
          <li>
            <span className="ic"><IconTarget /></span>
            <span><b>Stratégies éprouvées</b>Tendance, cassure, momentum : chacune testée frais compris avant la démo.</span>
          </li>
          <li>
            <span className="ic"><IconShield /></span>
            <span><b>Risque sous contrôle</b>Stop sur chaque position, limites de perte journalière et arrêt d'urgence.</span>
          </li>
          <li>
            <span className="ic"><IconPulse /></span>
            <span><b>Suivi en temps réel</b>Positions, résultats et alertes Telegram, sur ordinateur comme sur téléphone.</span>
          </li>
        </ul>
        <p className="auth-foot">© {new Date().getFullYear()} Kobr4 FX · Le trading comporte un risque de perte en capital.</p>
      </aside>
      <main className="auth-main">{children}</main>
    </div>
  );
}

function Kicker() {
  return (
    <div className="kicker">
      <BrandMark />
      <div>
        Kobr4<span>FX</span>
      </div>
    </div>
  );
}

export function Login() {
  const { setUser } = useAuth();
  const nav = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [step, setStep] = useState<"password" | "mfa">("password");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (step === "password") {
        const r = await api.post<{ user: User; mfa_required: boolean }>("/api/auth/login", { email, password });
        if (r.mfa_required) setStep("mfa");
        else {
          setUser(r.user);
          nav("/");
        }
      } else {
        setUser(await api.post<User>("/api/auth/mfa", { code }));
        nav("/");
      }
    } catch (err) {
      setError((err as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthLayout>
      <form className="card" onSubmit={submit}>
        <Kicker />
        <div>
          <h1>{step === "password" ? "Bon retour parmi nous" : "Code de vérification"}</h1>
          <p className="muted" style={{ marginTop: 6 }}>
            {step === "password" ? "Connecte-toi pour retrouver tes bots." : "Ouvre ton application d'authentification."}
          </p>
        </div>
        {error && <Notice tone="error">{error}</Notice>}
        {step === "password" ? (
          <>
            <Field label="Adresse email" htmlFor="email">
              <input id="email" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
            </Field>
            <Field label="Mot de passe" htmlFor="password">
              <input id="password" type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
            </Field>
          </>
        ) : (
          <Field label="Code à 6 chiffres de ton application, ou un code de secours" htmlFor="code">
            <input id="code" inputMode="numeric" autoComplete="one-time-code" autoFocus required value={code} onChange={(e) => setCode(e.target.value)} />
          </Field>
        )}
        <button className="btn primary" disabled={busy}>
          {step === "password" ? "Se connecter" : "Valider"}
        </button>
        <p className="muted" style={{ fontSize: 13 }}>
          Pas encore de compte ? <Link to="/inscription">Créer un compte</Link>
        </p>
      </form>
    </AuthLayout>
  );
}

export function Signup() {
  const { setUser } = useAuth();
  const nav = useNavigate();
  const [form, setForm] = useState({ email: "", password: "", confirm: "", display_name: "", invite_code: "" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (k: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value });

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (form.password !== form.confirm) {
      setError("Les deux mots de passe ne correspondent pas.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const u = await api.post<User>("/api/auth/signup", {
        email: form.email,
        password: form.password,
        display_name: form.display_name,
        invite_code: form.invite_code || null,
      });
      setUser(u);
      nav("/bienvenue");
    } catch (err) {
      setError((err as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthLayout>
      <form className="card" onSubmit={submit}>
        <Kicker />
        <div>
          <h1>Créer un compte</h1>
          <p className="muted" style={{ marginTop: 6 }}>Quelques secondes, puis on configure ton premier bot ensemble.</p>
        </div>
        {error && <Notice tone="error">{error}</Notice>}
        <Field label="Prénom ou pseudo" htmlFor="name">
          <input id="name" value={form.display_name} onChange={set("display_name")} autoComplete="nickname" />
        </Field>
        <Field label="Adresse email" htmlFor="email">
          <input id="email" type="email" required value={form.email} onChange={set("email")} autoComplete="username" />
        </Field>
        <Field label="Mot de passe" hint="12 caractères minimum. Une phrase de plusieurs mots est idéale." htmlFor="password">
          <input id="password" type="password" required minLength={12} value={form.password} onChange={set("password")} autoComplete="new-password" />
        </Field>
        <Field label="Confirmer le mot de passe" htmlFor="confirm">
          <input id="confirm" type="password" required value={form.confirm} onChange={set("confirm")} autoComplete="new-password" />
        </Field>
        <Field label="Code d'invitation" hint="Inutile pour le tout premier compte du site." htmlFor="invite">
          <input id="invite" value={form.invite_code} onChange={set("invite_code")} />
        </Field>
        <button className="btn primary" disabled={busy}>Créer mon compte</button>
        <p className="muted" style={{ fontSize: 13 }}>
          Déjà inscrit ? <Link to="/connexion">Se connecter</Link>
        </p>
      </form>
    </AuthLayout>
  );
}
