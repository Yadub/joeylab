// Light, dark, or the device's own setting. The stylesheet gives every colour as light-dark(), so
// the theme is only a color-scheme: data-theme on <html> fixes it, and without it the device decides.

import { emit, setPref, store } from "./store.js";

export const THEMES = [["system", "Follow the system", "auto"], ["light", "Light", "sun"], ["dark", "Dark", "moon"]];
const GROUND = { light: "#F4F3EF", dark: "#1B1B19" };     // --ground, for the browser's own bar

export function applyTheme() {
  const t = store.prefs.theme;
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
  // index.html has one theme-color per system scheme; a chosen theme sets both.
  document.querySelectorAll('meta[name="theme-color"]').forEach((m) => {
    m.content = GROUND[t === "light" || t === "dark" ? t : /dark/.test(m.media) ? "dark" : "light"];
  });
}

export function setTheme(t) {
  setPref("theme", t);
  applyTheme();
  emit("theme", t);
}

export const themeLabel = (t) => (THEMES.find(([k]) => k === t) || THEMES[0])[1];
