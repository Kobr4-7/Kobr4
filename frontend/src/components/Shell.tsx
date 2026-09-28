import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../context";
import { IconBacktest, IconBots, IconDashboard, IconLab, IconLogout, IconSettings } from "./Icons";

export function Shell() {
  const { setUser } = useAuth();
  const nav = useNavigate();
  const logout = async () => {
    try {
      await api.post("/api/auth/logout");
    } finally {
      setUser(null);
      nav("/connexion");
    }
  };
  return (
    <div className="shell">
      <nav className="nav" aria-label="Navigation principale">
        <div className="brand">
          Kobr4<span>FX</span>
        </div>
        <NavLink to="/" end>
          <IconDashboard /> Tableau de bord
        </NavLink>
        <NavLink to="/bots">
          <IconBots /> Bots
        </NavLink>
        <NavLink to="/backtests">
          <IconBacktest /> Backtests
        </NavLink>
        <NavLink to="/labo">
          <IconLab /> Labo
        </NavLink>
        <NavLink to="/reglages">
          <IconSettings /> Réglages
        </NavLink>
        <span className="grow" />
        <a href="#" className="only-desktop" onClick={(e) => (e.preventDefault(), void logout())}>
          <IconLogout /> Se déconnecter
        </a>
      </nav>
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
