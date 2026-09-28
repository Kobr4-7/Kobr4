import { useId } from "react";

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
export const IconSun = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true"><circle {...P} cx="12" cy="12" r="4" /><path {...P} d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></svg>
);
export const IconMoon = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true"><path {...P} d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z" /></svg>
);
export const IconShield = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true"><path {...P} d="M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6z" /><path {...P} d="M9 12l2 2 4-4" /></svg>
);
export const IconPulse = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true"><path {...P} d="M3 12h4l3-8 4 16 3-8h4" /></svg>
);
export const IconTarget = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true"><circle {...P} cx="12" cy="12" r="9" /><circle {...P} cx="12" cy="12" r="5" /><circle {...P} cx="12" cy="12" r="1" /></svg>
);

/** Logo : courbe montante dorée dans un carré sombre. */
export const BrandMark = () => {
  const id = useId();
  return (
  <svg className="brand-mark" viewBox="0 0 64 64" aria-hidden="true">
    <defs>
      <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stopColor="#f0c76f" />
        <stop offset="1" stopColor="#c8963a" />
      </linearGradient>
    </defs>
    <rect width="64" height="64" rx="16" fill="#0b0f16" />
    <rect x="1" y="1" width="62" height="62" rx="15" fill="none" stroke="#e3b85c" strokeOpacity=".35" strokeWidth="2" />
    <path d="M15 44 L26 31 L34 38 L49 20" fill="none" stroke={`url(#${id})`} strokeWidth="5.5" strokeLinecap="round" strokeLinejoin="round" />
    <circle cx="49" cy="20" r="4.5" fill="#f0cd7f" />
  </svg>
  );
};
