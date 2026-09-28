import { useEffect, useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../context";
import {
  BrandMark,
  IconBacktest,
  IconBots,
  IconDashboard,
  IconLab,
  IconLogout,
  IconMoon,
  IconSettings,
  IconSun,
} from "./Icons";
import { currentTheme, setTheme, type Theme } from "./theme";

/** Le forex est ouvert du dimanche 22 h au vendredi 21 h (UTC), comme côté serveur. */
function marketOpen(now: Date): boolean {
  const day = now.getUTCDay();
  const h = now.getUTCHours();
  if (day === 6) return false;
  if (day === 5) return h < 21;
  if (day === 0) return h >= 22;
  return true;
}

function useNow(every = 1000): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = window.setInterval(() => setNow(new Date()), every);
    return () => window.clearInterval(t);
  }, [every]);
  return now;
}

function ThemeToggle() {
  const [theme, set] = useState<Theme>(currentTheme);
  const next: Theme = theme === "dark" ? "light" : "dark";
  return (
    <button
      type="button"
      className="icon-btn"
      aria-label={next === "light" ? "Passer au thème clair" : "Passer au thème sombre"}
      title={next === "light" ? "Thème clair" : "Thème sombre"}
      onClick={() => {
        setTheme(next);
        set(next);
      }}
    >
      {theme === "dark" ? <IconSun /> : <IconMoon />}
    </button>
  );
}

function TopBar() {
  const now = useNow();
  const open = marketOpen(now);
  return (
    <header className="topbar">
      <div className="mobile-brand">
        <BrandMark />
        <div>
          Kobr4<span>FX</span>
        </div>
      </div>
      <span className={`market ${open ? "open" : ""}`}>
        <span className="led" />
        {open ? (
          <>
            Marché <b>ouvert</b>
          </>
        ) : (
          "Marché fermé · réouverture dimanche soir"
        )}
      </span>
      <span className="spacer" />
      <span className="clock" title="Heure locale">
        {now.toLocaleTimeString("fr-FR")}
      </span>
      <ThemeToggle />
    </header>
  );
}

export function Shell() {
  const { user, setUser } = useAuth();
  const nav = useNavigate();
  const logout = async () => {
    try {
      await api.post("/api/auth/logout");
    } finally {
      setUser(null);
      nav("/connexion");
    }
  };
  const name = user?.display_name || user?.email?.split("@")[0] || "";
  const initials = name.slice(0, 2).toUpperCase() || "K";
  return (
    <div className="shell">
      <nav className="nav" aria-label="Navigation principale">
        <div className="brand">
          <BrandMark />
          <div>
            Kobr4<span>FX</span>
          </div>
        </div>
        <div className="nav-section">Trading</div>
        <NavLink to="/" end>
          <IconDashboard /> Tableau de bord
        </NavLink>
        <NavLink to="/bots">
          <IconBots /> Bots
        </NavLink>
        <div className="nav-section">Recherche</div>
        <NavLink to="/backtests">
          <IconBacktest /> Backtests
        </NavLink>
        <NavLink to="/labo">
          <IconLab /> Laboratoire
        </NavLink>
        <div className="nav-section">Compte</div>
        <NavLink to="/reglages">
          <IconSettings /> Réglages
        </NavLink>
        <span className="grow" />
        <div className="user-card">
          <span className="avatar" aria-hidden="true">{initials}</span>
          <span className="who">
            <b>{name}</b>
            <span>{user?.email}</span>
          </span>
          <button type="button" className="icon-btn" aria-label="Se déconnecter" title="Se déconnecter" onClick={() => void logout()}>
            <IconLogout />
          </button>
        </div>
      </nav>
      <div className="content">
        <TopBar />
        <main className="main">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
