import type { ReactNode } from "react";
import { STATUS_LABEL } from "../format";

export function Panel({ title, actions, children, className }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`panel ${className ?? ""}`}>
      {(title || actions) && (
        <div className="panel-h">
          {typeof title === "string" ? <h2>{title}</h2> : title}
          {actions && <div className="row">{actions}</div>}
        </div>
      )}
      {children}
    </section>
  );
}

export function StatusPill({ status, kill }: { status: string; kill?: string | null }) {
  if (kill) return <span className="pill kill"><span className="dot" />Arrêt d'urgence</span>;
  return (
    <span className={`pill ${status}`}>
      <span className="dot" />
      {STATUS_LABEL[status] ?? status}
    </span>
  );
}

export function ModePill({ mode }: { mode: string }) {
  return <span className={`pill ${mode}`}>{mode === "live" ? "Argent réel" : "Compte démo"}</span>;
}

export function Kpi({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: string }) {
  return (
    <div className="kpi">
      <span className="label">{label}</span>
      <span className={`v ${tone ?? ""}`}>{value}</span>
      {sub !== undefined && <span className="s">{sub}</span>}
    </div>
  );
}

export function Field({ label, hint, children, htmlFor }: { label: string; hint?: ReactNode; children: ReactNode; htmlFor?: string }) {
  return (
    <div className="field">
      <label htmlFor={htmlFor}>{label}</label>
      {children}
      {hint && <span className="hint">{hint}</span>}
    </div>
  );
}

export function Notice({ tone, children }: { tone: "warn" | "error" | "ok" | "info"; children: ReactNode }) {
  return <div className={`notice ${tone}`} role={tone === "error" ? "alert" : undefined}>{children}</div>;
}

export function Meter({ label, value, max, text }: { label: string; value: number; max: number; text: string }) {
  const p = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return (
    <div className="field">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <span style={{ fontSize: 13 }}>{label}</span>
        <span className="num" style={{ fontSize: 12.5 }}>{text}</span>
      </div>
      <div className="bar">
        <i style={{ width: `${p}%` }} className={p >= 80 ? "crit" : p >= 50 ? "warn" : ""} />
      </div>
    </div>
  );
}
