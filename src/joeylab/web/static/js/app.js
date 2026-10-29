// Shell: hash router, top bar (sections, one search box, history, job status, theme), the
// command palette, and keyboard dispatch.

import { api, P, qs } from "./api.js";
import { closeLoupe, forgetDetails, loupeKey, loupeOpen } from "./loupe.js";
import { emit, on, redo, settings, store, undo } from "./store.js";
import { applyTheme, setTheme, themeLabel, THEMES } from "./theme.js";
import { ago, clear, closePopover, controlKey, dismissToast, focusOn, forgetFocus, h, icon, isTyping, keepFocus, n, plural, popover, toast } from "./ui.js";
import { closeCompare, keysDialog } from "./views/common.js";
import * as albums from "./views/albums.js";
import * as cull from "./views/cull.js";
import * as events from "./views/events.js";
import * as historyView from "./views/history.js";
import * as home from "./views/home.js";
import * as library from "./views/library.js";
import * as people from "./views/people.js";
import * as places from "./views/places.js";
import * as projects from "./views/projects.js";
import * as search from "./views/search.js";
import * as settingsView from "./views/settings.js";
import * as tidy from "./views/tidy.js";
import { openPalette } from "./views/palette.js";

const VIEWS = {
  home: { mod: home, label: "Home", icon: "home" },
  library: { mod: library, label: "Library", icon: "grid" },
  cull: { mod: cull, label: "Cull", icon: "stack", count: (o) => o?.settings?.purpose === "archive" ? null : o?.counts?.undecided_stacks, counted: "stacks to cull" },
  people: { mod: people, label: "People", icon: "people" },
  places: { mod: places, label: "Places", icon: "pin" },
  events: { mod: events, label: "Events", icon: "calendar" },
  tidy: { mod: tidy, label: "Tidy", icon: "broom", count: (o) => o?.counts?.dup_sets || null, counted: "duplicate sets" },
  albums: { mod: albums, label: "Albums", icon: "album" },
  settings: { mod: settingsView, label: "Settings", icon: "gear", iconOnly: true },
  search: { mod: search, label: "Search", hidden: true },
  history: { mod: historyView, label: "History", icon: "history", hidden: true },
};
// Old links keep working.
const ALIASES = { overview: "home", dupes: "tidy", "folders-export": "settings" };

// ------------------------------------------------------------------ keeping your place
// A section's last filters, tab and order (per library, for this browser tab): the top bar's links
// go back to them. Not for one-off actions a link can carry (start the taste questions, look for
// empty folders, a deep health check).
const REMEMBER = new Set(["library", "tidy", "albums", "people", "events", "places"]);
const ONE_OFF = new Set(["learn", "empty", "deep"]);
// Within a section, settings that a change of tab mustn't drop (Tidy: the duplicates' tab, order and kinds).
const STICKY = { tidy: ["dtab", "sort", "kinds"] };
function rememberPlace(project, view, params) {
  if (!project || !REMEMBER.has(view)) return;
  const keep = new URLSearchParams([...params].filter(([k]) => !ONE_OFF.has(k)));
  try { sessionStorage.setItem(`joeylab.place.${project}.${view}`, keep.toString()); } catch { /* private mode */ }
}
function placeHref(project, view) {
  let q = "";
  try { q = REMEMBER.has(view) ? sessionStorage.getItem(`joeylab.place.${project}.${view}`) || "" : ""; } catch { q = ""; }
  return `#/p/${encodeURIComponent(project)}/${view}${q ? "?" + q : ""}`;
}
/** The filters the Library last showed in this browser tab (after a reload too). */
function libraryFilter(project) {
  let q = "";
  try { q = sessionStorage.getItem(`joeylab.place.${project}.library`) || ""; } catch { q = ""; }
  return Object.fromEntries([...new URLSearchParams(q)].filter(([k, v]) => v && library.FILTER_KEYS.includes(k)));
}
/** Another library, in the same section (a search keeps its words; a library's own filters don't carry over). */
export function switchTo(project) {
  const r = route;
  const view = r?.project && VIEWS[r.view] ? r.view : "home";
  return view === "search" && r.params.get("q") ? hashFor(project, "search", { q: r.params.get("q") }) : placeHref(project, view);
}

// Back and Forward come back to where the page was scrolled to (and, in a grid, what was loaded).
if ("scrollRestoration" in history) history.scrollRestoration = "manual";
const scrollMemory = new Map();    // history entry -> {y, start, focus}
let entry = null;
function entryKey() {
  const st = history.state || {};
  if (st.psKey) return st.psKey;
  const key = Math.random().toString(36).slice(2, 10);
  history.replaceState({ ...st, psKey: key }, "");
  return key;
}

const topbar = document.getElementById("topbar");
const viewEl = document.getElementById("view");
let current = null;   // { name, api }
let route = null;

// ------------------------------------------------------------------ theme
applyTheme();
/** The theme button's menu: the three choices, the current one ticked. */
function themeMenu(anchor) {
  popover(anchor, h("div", {}, THEMES.map(([k, label, ic]) => h("button", { class: "menu-item", type: "button",
    "aria-current": store.prefs.theme === k ? "true" : null, onclick: () => { closePopover(); setTheme(k); } },
  icon(ic), label, store.prefs.theme === k ? h("span", { class: "meta" }, icon("check")) : null))));
}

// ------------------------------------------------------------------ routing
function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [path, query = ""] = raw.split("?");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  if (parts[0] === "p" && parts[1]) {
    const v = ALIASES[parts[2]] || parts[2];
    return { project: parts[1], view: VIEWS[v] ? v : "home", params: new URLSearchParams(query) };
  }
  return { project: null, view: "projects", params: new URLSearchParams(query) };
}

export function hashFor(project, view, params) {
  const q = qs(params);
  return `#/p/${encodeURIComponent(project)}/${view}${q ? "?" + q : ""}`;
}

async function navigate() {
  // On another page the keyboard starts on its heading. A control that showed another view of the
  // same page (Photos, Folders, Explore; a tab) keeps it.
  const fromPage = viewEl.contains(document.activeElement);
  closeLoupe();
  closePopover();
  closeCompare();
  document.querySelectorAll("dialog[open]").forEach((d) => d.close());   // a dialog belongs to the view it opened in
  const r = parseHash();
  const first = !route;
  const stay = fromPage && route && route.view === r.view && route.project === r.project;
  if (!stay) forgetFocus();
  const projectChanged = r.project !== store.project;
  route = r;
  if (projectChanged) {
    store.project = r.project;
    store.rows.clear();
    store.overview = null;
    store.settings = null;
    store.libraryParams = libraryFilter(r.project);   // the Library's filter, kept for this tab (Albums and Export offer it)
    store.peopleList = null;
    store.keywordTree = null;
    forgetDetails();
    lastVersion = null;
  }
  if (entry && current) scrollMemory.set(entry, current.api?.scrollState?.() || { y: Math.round(window.scrollY) });
  entry = entryKey();
  const back = scrollMemory.get(entry);       // been here before: Back or Forward
  rememberPlace(r.project, r.view, r.params);
  current?.api?.unmount?.();
  clear(viewEl);
  dismissToast("updated");             // the new page is drawn from the library as it is now
  window.scrollTo(0, 0);
  renderTopbar();
  const ctx = {
    project: r.project,
    params: r.params,
    view: r.view,
    setParams(obj, remount = false) {
      const sticky = Object.fromEntries((STICKY[r.view] || []).filter((k) => r.params.get(k)).map((k) => [k, r.params.get(k)]));
      obj = { ...sticky, ...obj };
      const url = hashFor(r.project, r.view, obj);
      if (remount) location.hash = url;
      else {
        history.replaceState(history.state, "", url);       // (keeps this entry's place for Back)
        r.params = new URLSearchParams(qs(obj));
        ctx.params = r.params;
        rememberPlace(r.project, r.view, r.params);
        renderTopbar();
      }
    },
    pollJob: () => poll(true),
    go: (view, params) => { location.hash = hashFor(r.project, view, params || {}); },
  };
  const mod = r.project ? VIEWS[r.view].mod : projects;
  if (r.project) await settings();     // views read the library's settings while drawing (a few ms)
  if (route !== r) return;             // another navigation started meanwhile
  current = { name: r.view, api: mod.mount(viewEl, ctx) || {} };
  document.title = r.project ? `${VIEWS[r.view].label} · ${r.project} · JoeyLab` : "JoeyLab";
  if (skipLink) skipLink.textContent = viewEl.querySelector("[data-skip]") ? "Skip to the photos" : "Skip to the page";
  if (!stay && !first) toHeading(r);
  if (back && (back.y || back.start)) {
    if (current.api.restore) current.api.restore(back);
    else setTimeout(() => window.scrollTo({ top: back.y, behavior: "instant" }), 120);   // once the page has drawn
  }
  // The top bar's counts: fetched again only after a change here, a library update, a minute, or when
  // the server has counted newer numbers (see poll).
  if (r.project && (!store.overview || store.overviewStale || Date.now() - (store.overviewAt || 0) > 60_000)) {
    store.overviewStale = false;
    fetchOverview(r.project).catch((e) => {
      if (/no library called/i.test(e.message)) { toast(e.message, { kind: "error" }); location.hash = "#/"; }
    });
  }
  poll(true);
}

/** The library's counts for the top bar (and Home, which listens for "overview"). The server answers at
 * once from what it last counted, so this never waits for a count; one fetch at a time (`after`: a
 * change was just made, so a fetch already on its way may be too early, and another follows it). */
let overviewFetch = null;
function fetchOverview(p = store.project, after = false) {
  if (overviewFetch?.p === p) {
    return after ? overviewFetch.done.catch(() => null).then(() => fetchOverview(p)) : overviewFetch.done;
  }
  const done = api(P(p, "/overview")).then((o) => {
    if (store.project !== p) return null;
    store.overview = o;
    store.overviewAt = Date.now();
    renderTopbar();
    emit("overview", o);
    return o;
  }).finally(() => { if (overviewFetch?.done === done) overviewFetch = null; });
  overviewFetch = { p, done };
  return done;
}

// ------------------------------------------------------------------ the keyboard's way in
// "Skip to the photos" (or the page) comes first on every page; after moving to another page, the
// keyboard starts on its heading (which a screen reader reads out). A page may draw it a moment later.
const skipLink = document.getElementById("skip");
skipLink?.addEventListener("click", (e) => {
  e.preventDefault();
  focusOn(viewEl.querySelector("[data-skip]") || viewEl.querySelector("h1") || viewEl);
});
function toHeading(r) {
  const go = () => {
    if (route !== r) return true;
    const h1 = viewEl.querySelector("h1");
    if (!h1) return false;
    const active = document.activeElement;
    if (!active || active === document.body || topbar.contains(active)) { h1.tabIndex = -1; h1.focus({ preventScroll: true }); }
    return true;
  };
  if (go()) return;
  const watch = new MutationObserver(() => { if (go()) watch.disconnect(); });
  watch.observe(viewEl, { childList: true, subtree: true });
  setTimeout(() => watch.disconnect(), 4000);
}

// ------------------------------------------------------------------ top bar
const jobPill = h("button", { class: "job-pill", hidden: true, type: "button", "aria-haspopup": "dialog", onclick: (e) => showJob(e.currentTarget) });
let lastJob = null;

let topSearch = null;
let topSearchFor = "";

function renderTopbar() {
  const r = route;
  if (topSearch && document.activeElement === topSearch && topbar.contains(topSearch)) return;   // typing: leave it be
  const themeIcon = { system: "auto", light: "sun", dark: "moon" }[store.prefs.theme] || "auto";
  const themeBtn = h("button", { class: "btn ghost icon-only", type: "button", title: `Theme: ${themeLabel(store.prefs.theme)}`,
    "aria-label": `Theme: ${themeLabel(store.prefs.theme)}`, onclick: (e) => themeMenu(e.currentTarget) }, icon(themeIcon));
  const items = [
    h("a", { class: "brand", href: "#/", title: "All libraries" }, h("img", { class: "mark", src: "/static/icon.svg", alt: "" }),
      h("span", { class: "word" }, "JoeyLab")),
  ];
  let nav = null;
  if (r?.project) {
    items.push(h("button", { class: "proj-btn", type: "button", title: "Libraries", "aria-haspopup": "menu", "aria-label": `Library: ${r.project}`,
      onclick: (e) => projectMenu(e.currentTarget) },
      h("span", { class: "proj-name" }, r.project), icon("down")));
    nav = h("nav", { class: "nav", "aria-label": "Sections", onscroll: (e) => navEdges(e.currentTarget) },
      Object.entries(VIEWS).filter(([, v]) => !v.hidden).map(([k, v]) => {
        const c = v.count?.(store.overview);
        return h("a", { href: placeHref(r.project, k), "aria-current": r.view === k ? "page" : null, title: v.label,
          "aria-label": v.iconOnly ? v.label : null, class: v.iconOnly ? "icon-link" : "" },
        v.iconOnly ? icon(v.icon) : v.label,
        c ? h("span", { class: "count" }, h("span", { "aria-hidden": "true" }, n(c)), h("span", { class: "sr-only" }, [n(c), v.counted].filter(Boolean).join(" "))) : null);
      }));
    items.push(nav);
    // One search box for the page's life: rebuilding the bar (counts, the job pill) must never
    // swap it out from under your typing.
    if (!topSearch) topSearch = h("input", { class: "input", id: "top-search", type: "search", placeholder: "Search: Alex at the beach, is:pick…  /", autocomplete: "off" });
    const key = `${r.project}|${r.view}|${r.view === "search" ? r.params.get("q") || "" : ""}`;
    if (topSearchFor !== key) { topSearch.value = r.view === "search" ? r.params.get("q") || "" : ""; topSearchFor = key; }
    const q = topSearch;
    items.push(h("div", { class: "top-right" },
      // The Search page has its own big box; one is enough.
      h("form", { class: "search-box", role: "search", hidden: r.view === "search", onsubmit: (e) => {
        e.preventDefault();
        if (q.value.trim()) location.hash = hashFor(r.project, "search", { q: q.value.trim() });
      } }, h("label", { class: "sr-only", for: "top-search" }, "Search photos"), icon("search"), q),
      h("a", { class: "btn ghost icon-only search-btn", href: hashFor(r.project, "search", {}), title: "Search", "aria-label": "Search" }, icon("search")),
      h("button", { class: "btn ghost icon-only", type: "button", title: "History and undo", "aria-label": "History", "aria-haspopup": "dialog",
        onclick: (e) => historyMenu(e.currentTarget) }, icon("history")),
      jobPill, themeBtn));
  } else {
    items.push(h("div", { class: "top-right" }, themeBtn));
  }
  const scrolled = topbar.querySelector(".nav")?.scrollLeft || 0;
  clear(topbar).append(...items);
  if (nav) {
    // On a narrow screen the sections scroll sideways: keep the current one in view, and fade the
    // edge that has more.
    nav.scrollLeft = scrolled;
    const a = nav.querySelector('[aria-current="page"]')?.getBoundingClientRect(), b = nav.getBoundingClientRect();
    if (a && (a.left < b.left || a.right > b.right)) nav.scrollLeft += a.left - b.left - (b.width - a.width) / 2;
    navEdges(nav);
  }
}

function navEdges(nav) {
  nav.classList.toggle("more-left", nav.scrollLeft > 2);
  nav.classList.toggle("more-right", nav.scrollLeft + nav.clientWidth < nav.scrollWidth - 2);
}
window.addEventListener("resize", () => { const nav = topbar.querySelector(".nav"); if (nav) navEdges(nav); });

async function projectMenu(anchor) {
  let list = [];
  try { list = await api("/api/projects"); } catch { /* shown as empty */ }
  popover(anchor, h("div", {},
    list.map((p) => h("a", { class: "menu-item", role: "menuitem", href: switchTo(p.name), "aria-current": p.name === store.project ? "true" : null,
      onclick: () => closePopover({ refocus: false }) }, p.name, h("span", { class: "meta" }, plural(p.photos, "photo")))),
    h("div", { class: "menu-sep", role: "separator" }),
    h("a", { class: "menu-item", role: "menuitem", href: "#/", onclick: () => closePopover({ refocus: false }) }, "All libraries and new library…")),
  { label: "Libraries" });
}

async function historyMenu(anchor) {
  const body = h("div", { class: "history" }, h("div", { class: "muted", style: { padding: "8px" } }, "Loading…"));
  popover(anchor, body, { label: "History" });
  let d;
  try { d = await api(P(store.project, "/history"), { params: { limit: 12 } }); }
  catch (e) { clear(body).append(h("div", { class: "result err" }, e.message)); return; }
  const t = d.today;
  clear(body).append(
    h("div", { class: "history-head" }, h("b", {}, "Today"), h("span", { class: "muted" },
      t.photos ? [t.picked && `${n(t.picked)} picked`, t.rejected && `${n(t.rejected)} rejected`, t.rated && `${n(t.rated)} rated`].filter(Boolean).join(" · ") || "labels only"
        : "nothing decided yet")),
    (d.can_redo ?? d.steps.some((s) => s.source === "undo" && !s.undone)) ? h("button", { class: "menu-item", type: "button",
      onclick: async () => { await redo(); closePopover(); } }, icon("refresh"), "Redo what you undid", h("span", { class: "meta" }, "⇧U")) : null,
    h("div", { class: "menu-sep" }),
    d.steps.length ? d.steps.map((s) => h("div", { class: `menu-item history-step ${s.undone ? "undone" : ""}` },
      h("span", { class: "grow" }, h("b", {}, s.label), h("br"), h("span", { class: "muted" },
        `${plural(s.n, "photo")} · ${ago(s.ts)}${s.source !== "web" ? " · " + s.source : ""}${s.viewer ? " · " + s.viewer : ""}${s.undone ? " · undone" : ""}`)),
      !s.undone && s.source !== "undo" ? h("button", { class: "btn small", "aria-label": `Undo ${s.label}`, onclick: async () => { await undo(s.batch); closePopover(); } }, "Undo") : null))
      : h("div", { class: "muted", style: { padding: "8px" } }, "No changes yet. Everything you decide shows up here, and can be undone, even after a reload."),
    h("div", { class: "menu-sep" }),
    h("a", { class: "menu-item", href: hashFor(store.project, "history", {}), onclick: () => closePopover({ refocus: false }) }, icon("history"),
      "All of the history", h("span", { class: "meta" }, store.meta?.can_trash ? "and the Trash" : "")));
}

/** "Up to date · 593 files untouched": the check at the end of every update, said where you look. */
function untouchedText(u) {
  if (!u) return "Up to date";
  if (u.ok) return `Up to date · ${plural(u.files ?? u.checked, "file")} untouched`;
  const k = (u.changed || 0) + (u.gone || 0);
  return `Up to date · ${plural(k, "file")} changed by another app`;
}

function showJob(anchor) {
  const j = lastJob;
  popover(anchor, h("div", {},
    h("div", { style: { padding: "6px 8px" } }, h("b", {}, j ? (j.state === "running" ? j.stage : j.state === "error" ? "Update failed" : untouchedText(j.untouched)) : "Idle"),
      j?.total ? h("div", { class: "muted small" }, `${n(j.done)} of ${n(j.total)}`) : null),
    j?.log?.length ? h("div", { class: "log" }, j.log.slice(-14).map((l) => h("div", { class: l.level }, l.msg))) : null,
    h("div", { class: "menu-sep" }),
    h("button", { class: "menu-item", type: "button", onclick: async () => {
      closePopover();
      try { await api(P(store.project, "/jobs"), { method: "POST", body: {} }); poll(true); } catch (e) { toast(e.message, { kind: "error" }); }
    } }, icon("refresh"), "Update now"),
    h("div", { class: "muted", style: { padding: "2px 8px 6px", fontSize: "12px", maxWidth: "34ch" } },
      "An update finds new and changed photos and reads them. Your files are only read, never changed.")), { label: "Library update" });
}

// ------------------------------------------------------------------ job polling
let pollTimer = null;
let lastVersion = null;
let unreachable = false;
async function poll() {
  clearTimeout(pollTimer);
  if (!store.project) return;
  let res;
  try { res = await api(P(store.project, "/jobs/current")); } catch { unreachable = true; pollTimer = setTimeout(poll, 8000); return; }
  if (unreachable) { unreachable = false; checkBuild(); }      // back after a restart: perhaps a new version
  const j = res.job;
  lastJob = j;
  store.job = j;
  emit("job", j);
  const running = j?.state === "running";
  const recent = j && j.finished && Date.now() / 1000 - j.finished < 20;
  jobPill.hidden = !(running || j?.state === "error" || recent);
  jobPill.classList.toggle("error", j?.state === "error");
  if (running) {
    const pct = j.total ? Math.round((100 * j.done) / j.total) : null;
    clear(jobPill).append(h("span", { class: "spinner" }), h("span", { class: "txt" }, j.stage),
      pct !== null ? h("span", { class: "bar" }, h("i", { style: { width: `${pct}%` } })) : null);
  } else if (j?.state === "error") {
    clear(jobPill).append(icon("info"), h("span", { class: "txt" }, "Update failed"));
  } else if (j) {
    clear(jobPill).append(icon("check"), h("span", { class: "txt" }, untouchedText(j.untouched)));
    jobPill.classList.toggle("warn", j.untouched?.ok === false);
  }
  if (lastVersion !== null && res.version !== lastVersion) libraryChanged(j);
  // Newer counts are ready (the server counts again in the background after a change): fetch them.
  else if (res.overview != null && store.overview && res.overview !== store.overview.seq) fetchOverview().catch(() => {});
  lastVersion = res.version;
  pollTimer = setTimeout(poll, running ? 1000 : 5000);
}

function libraryChanged(j) {
  forgetDetails();
  fetchOverview().catch(() => {});
  // A page still waiting for the first scan simply fills in. Any other page stays as it is (a list
  // you were working through, a dialog's result) until you ask for the new one.
  if (current?.api?.waiting?.()) { current.api.refresh?.(); return; }
  if (!current?.api?.refresh) return;
  // Part way through an update: its new photos are stacked and scored; faces, search and places follow.
  const early = j?.state === "running" && (j.ready || []).includes("group");
  if (early && viewEl.querySelector(".wait-note:not([hidden]) button")) return;     // the page's own note offers it
  // Said only when files were added, changed or gone (an update that read faces or search alone
  // changes nothing you'd reload for).
  const f = j?.files || {};
  const found = [f.added && plural(f.added, "new file"), f.changed && `${n(f.changed)} changed`, f.gone && `${n(f.gone)} gone`].filter(Boolean);
  if (!early && !found.length) return;
  toast(early ? "New photos are stacked and scored; faces, search and places follow" : `Library updated: ${found.join(", ")}`,
    { action: () => current?.api?.refresh?.(), actionLabel: "Show", key: "updated", persist: true });
}

// ------------------------------------------------------------------ JoeyLab itself updated
// An open page keeps running the code it loaded. When the server comes back with another build
// (after an upgrade or a restart with changed files), say so and offer to reload.
let toldBuild = null;
async function checkBuild() {
  if (!store.meta?.build) return;
  let m;
  try { m = await api("/api/meta"); } catch { return; }
  if (m.build && m.build !== store.meta.build && m.build !== toldBuild) {
    toldBuild = m.build;                 // once per new build; it stays until Reload or ×
    toast("JoeyLab was updated", { action: () => location.reload(), actionLabel: "Reload", key: "build", persist: true });
  }
}
setInterval(checkBuild, 4 * 60_000);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") checkBuild(); });

// ------------------------------------------------------------------ keyboard
document.addEventListener("keydown", (e) => {
  if (document.querySelector("dialog[open]") || (document.querySelector(".survey") && !loupeOpen())) return;
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    openPalette({ project: store.project, views: VIEWS, hashFor, current: () => current, switchTo });
    return;
  }
  if (isTyping(e)) {
    if (e.key === "Escape") e.target.blur();
    return;
  }
  if (loupeOpen()) {
    if (loupeKey(e)) { e.preventDefault(); return; }
  }
  const k = e.key;
  if ((k === "U" && !e.metaKey && !e.ctrlKey) || ((e.metaKey || e.ctrlKey) && k.toLowerCase() === "z" && e.shiftKey)) {
    if (!store.project) return;
    e.preventDefault(); redo(); return;       // ⇧U or ⌘⇧Z: put back what you just undid
  }
  if ((k === "u" && !e.metaKey && !e.ctrlKey) || ((e.metaKey || e.ctrlKey) && k.toLowerCase() === "z" && !e.shiftKey)) {
    if (!store.project) return;
    e.preventDefault(); undo(); return;
  }
  if (k === "?") { e.preventDefault(); keysDialog(loupeOpen() ? null : current?.api?.keys); return; }
  if (loupeOpen()) return;
  if (controlKey(e)) return;                  // Enter on a button presses it; the arrows move a slider
  if (k === "/" && store.project) {
    e.preventDefault();
    const q = document.getElementById("search-q") || document.getElementById("top-search");
    if (q && q.offsetParent) q.focus();
    else location.hash = hashFor(store.project, "search", {});
    return;
  }
  if (current?.api?.onKey?.(e)) e.preventDefault();
});

on((type) => { if (type === "settings" || type === "theme") renderTopbar(); });

// ------------------------------------------------------------------ boot
if ("serviceWorker" in navigator && location.protocol !== "file:") {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}
// Anything decided, tagged or named here changes the top bar's counts. Cull's and Tidy's counts follow
// each decision, Undo, move to the Trash or move a moment after it: the server counts what those change
// again as soon as they change, so these numbers are exact.
let countTimer = null;
on((type) => {
  if (["history", "keywords", "rows"].includes(type)) store.overviewStale = true;
  if (!store.project || !["history", "gone", "raws", "moved"].includes(type)) return;
  clearTimeout(countTimer);
  countTimer = setTimeout(() => fetchOverview(store.project, true).catch(() => {}), 700);
});
window.addEventListener("hashchange", navigate);
// Anything that fails without its own message (a lost connection, a refused change) still says so.
window.addEventListener("unhandledrejection", (e) => {
  const m = e.reason?.message || (typeof e.reason === "string" ? e.reason : "");
  if (m) toast(m, { kind: "error" });
});
keepFocus();
api("/api/meta").then((m) => { store.meta = m; }).catch(() => {}).finally(async () => {
  if (!location.hash || location.hash === "#" || location.hash === "#/") {
    try {
      const list = await api("/api/projects");
      if (list.length === 1 && !sessionStorage.getItem("joeylab.home")) {
        sessionStorage.setItem("joeylab.home", "1");
        location.replace(`#/p/${encodeURIComponent(list[0].name)}`);
      }
    } catch { /* home view shows the error */ }
  }
  navigate();
});
