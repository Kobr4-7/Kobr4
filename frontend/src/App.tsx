import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { Shell } from "./components/Shell";
import { useAuth } from "./context";
import { Login, Signup } from "./pages/Auth";
import { BotDetail, BotList, BotNew } from "./pages/Bots";
import { Dashboard } from "./pages/Dashboard";
import { Onboarding } from "./pages/Onboarding";
import { Backtests, Lab } from "./pages/Research";
import { Settings } from "./pages/Settings";

function Protected() {
  const { user, loading } = useAuth();
  const loc = useLocation();
  if (loading) return <div className="center muted">Chargement…</div>;
  if (!user) return <Navigate to="/connexion" replace state={{ from: loc.pathname }} />;
  if (user.onboarding_step !== "done") return <Navigate to="/bienvenue" replace />;
  return <Shell />;
}

export function App() {
  const { user, loading } = useAuth();
  if (loading) return <div className="center muted">Chargement…</div>;
  return (
    <Routes>
      <Route path="/connexion" element={user ? <Navigate to="/" replace /> : <Login />} />
      <Route path="/inscription" element={user ? <Navigate to="/" replace /> : <Signup />} />
      <Route path="/bienvenue" element={<Onboarding />} />
      <Route element={<Protected />}>
        <Route index element={<Dashboard />} />
        <Route path="bots" element={<BotList />} />
        <Route path="bots/nouveau" element={<BotNew />} />
        <Route path="bots/:id" element={<BotDetail />} />
        <Route path="backtests" element={<Backtests />} />
        <Route path="labo" element={<Lab />} />
        <Route path="reglages" element={<Settings />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
