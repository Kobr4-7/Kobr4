import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, ApiError, type User } from "../api";
import { Field, Notice } from "../components/ui";
import { useAuth } from "../context";

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
    <div className="center">
      <form className="card" onSubmit={submit}>
        <div>
          <div className="label">Kobr4 FX</div>
          <h1>{step === "password" ? "Connexion" : "Code de vérification"}</h1>
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
    </div>
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
    <div className="center">
      <form className="card" onSubmit={submit}>
        <div>
          <div className="label">Kobr4 FX</div>
          <h1>Créer un compte</h1>
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
    </div>
  );
}
