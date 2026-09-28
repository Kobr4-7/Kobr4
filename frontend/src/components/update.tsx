// Mise à jour du site depuis les réglages (administrateur) : la demande est traitée par
// deploy/auto-update.sh sur le serveur, qui reconstruit puis relance le site.

import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { dateTime } from "../format";
import { Notice, Panel } from "./ui";

type Status = {
  available: boolean;
  version?: string | null;
  pending?: { hash: string; subject: string }[];
  state?: "idle" | "running" | "done" | "failed";
  state_at?: string | null;
  requested_at?: string | null;
  request_stale?: boolean;
  log?: string[];
};

export function UpdatePanel() {
  const [st, setSt] = useState<Status | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [waiting, setWaiting] = useState(false);
  const [offline, setOffline] = useState(false);
  const since = useRef<number>(0);

  const load = async () => {
    try {
      const s = await api.get<Status>("/api/admin/update");
      setSt(s);
      setOffline(false);
      return s;
    } catch (e) {
      if ((e as ApiError).status === 0 || (e as ApiError).status >= 500) setOffline(true);
      return null;
    }
  };

  useEffect(() => {
    void load();
  }, []);

  // Pendant une mise à jour : suivi toutes les 4 s, puis rechargement de la page.
  useEffect(() => {
    if (!waiting) return;
    const t = window.setInterval(async () => {
      const s = await load();
      if (!s) return;
      const doneAt = s.state_at ? Date.parse(s.state_at) : 0;
      if (s.state === "done" && doneAt >= since.current) {
        window.clearInterval(t);
        window.setTimeout(() => window.location.reload(), 1500);
      }
      if (s.state === "failed" && doneAt >= since.current) {
        window.clearInterval(t);
        setWaiting(false);
        setError("La mise à jour a échoué : voir le journal ci-dessous.");
      }
    }, 4000);
    return () => window.clearInterval(t);
  }, [waiting]);

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      since.current = Date.now() - 5000;
      setSt(await api.post<Status>("/api/admin/update"));
      setWaiting(true);
    } catch (e) {
      setError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  if (!st) return null;
  const pending = st.pending ?? [];
  const running = st.state === "running" || offline;
  return (
    <Panel title="Mise à jour du site" actions={st.version ? <span className="muted num" style={{ fontSize: 12.5 }}>Version {st.version}</span> : undefined}>
      <div className="panel-b">
        {!st.available ? (
          <Notice tone="info">
            La mise à jour depuis le site n'est pas encore activée sur ce serveur. Sur le VPS, lancer une fois{" "}
            <span className="num">~/Kobr4/deploy/auto-update.sh</span> puis la commande cron indiquée dans le guide.
          </Notice>
        ) : (
          <>
            {error && <Notice tone="error">{error}</Notice>}
            {waiting && !error && (
              <Notice tone={running ? "warn" : "info"}>
                {running
                  ? "Mise à jour en cours : le site redémarre, la page se rechargera toute seule (1 à 3 minutes)."
                  : "Demande envoyée : le serveur la prend en charge dans la minute."}
              </Notice>
            )}
            {st.request_stale && (
              <Notice tone="warn">
                La demande attend depuis plus de 5 minutes : la tâche cron n'est sans doute pas réglée sur « chaque minute » (voir le guide).
              </Notice>
            )}
            {pending.length > 0 ? (
              <>
                <p>
                  <strong>{pending.length} nouveauté{pending.length > 1 ? "s" : ""} disponible{pending.length > 1 ? "s" : ""}</strong>
                </p>
                <ul className="changes">
                  {pending.slice(0, 8).map((p) => (
                    <li key={p.hash}>
                      <span className="num muted">{p.hash}</span> {p.subject}
                    </li>
                  ))}
                </ul>
              </>
            ) : (
              <p className="muted">Le site est à jour.{st.state_at && st.state === "done" ? ` Dernière mise à jour : ${dateTime(st.state_at)}.` : ""}</p>
            )}
            <div className="row">
              <button className="btn primary" disabled={busy || waiting || st.state === "running"} onClick={() => void start()}>
                {waiting ? "Mise à jour en cours…" : pending.length > 0 ? "Mettre à jour maintenant" : "Reconstruire et relancer le site"}
              </button>
              <span className="muted" style={{ fontSize: 12.5 }}>Les bots s'arrêtent proprement puis repartent tout seuls.</span>
            </div>
            {st.state === "failed" && (st.log ?? []).length > 0 && <pre className="update-log">{(st.log ?? []).join("\n")}</pre>}
          </>
        )}
      </div>
    </Panel>
  );
}
