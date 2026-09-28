// Thème clair ou sombre : suit le système, sauf choix enregistré.

export type Theme = "light" | "dark";
const KEY = "kobr4-theme";

export function currentTheme(): Theme {
  const set = document.documentElement.dataset.theme;
  if (set === "light" || set === "dark") return set;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function setTheme(t: Theme): void {
  document.documentElement.dataset.theme = t;
  try {
    localStorage.setItem(KEY, t);
  } catch {
    // stockage indisponible : le choix vaut pour cette visite
  }
  window.dispatchEvent(new CustomEvent("kobr4:theme", { detail: t }));
}
