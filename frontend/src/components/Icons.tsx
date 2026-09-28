// Icônes simples en trait (24×24), dessinées pour la navigation.
const P = { fill: "none", stroke: "currentColor", strokeWidth: 1.8, strokeLinecap: "round", strokeLinejoin: "round" } as const;

export const IconDashboard = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true"><path {...P} d="M4 19V11M10 19V5M16 19v-6M22 19H2" /></svg>
);
export const IconBots = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true"><rect {...P} x="4" y="8" width="16" height="11" rx="2" /><path {...P} d="M12 4v4M9 13h.01M15 13h.01" /></svg>
);
export const IconBacktest = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true"><path {...P} d="M3 3v18h18" /><path {...P} d="M7 15l4-4 3 3 5-6" /></svg>
);
export const IconLab = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true"><path {...P} d="M9 3h6M10 3v6l-5 9a2 2 0 0 0 1.7 3h10.6a2 2 0 0 0 1.7-3l-5-9V3" /></svg>
);
export const IconSettings = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true"><circle {...P} cx="12" cy="12" r="3" /><path {...P} d="M19 12a7 7 0 0 0-.1-1.2l2-1.6-2-3.4-2.4 1a7 7 0 0 0-2-1.2L14 3h-4l-.5 2.6a7 7 0 0 0-2 1.2l-2.4-1-2 3.4 2 1.6a7 7 0 0 0 0 2.4l-2 1.6 2 3.4 2.4-1a7 7 0 0 0 2 1.2L10 21h4l.5-2.6a7 7 0 0 0 2-1.2l2.4 1 2-3.4-2-1.6c.1-.4.1-.8.1-1.2z" /></svg>
);
export const IconLogout = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true"><path {...P} d="M15 4h4v16h-4M10 8l-4 4 4 4M6 12h10" /></svg>
);
