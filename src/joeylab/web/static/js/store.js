// App-wide state: the current project, a cache of photo rows, decisions (undo lives on the
// server, so it survives a reload and works across devices), and preferences.

import { api, P } from "./api.js";
import { plural, toast } from "./ui.js";

const listeners = new Set();

export const store = {
  project: null,
  meta: null,
  overview: null,
  settings: null,
  rows: new Map(),     // id -> light row (shared by every view so tiles update in place)
  version: 0,          // bumps when a background job changed the library
  libraryParams: {},   // last Library filter, offered by Export and Albums
  show: null,          // (ids) => true when the page in view went to those photos itself (Cull: their stack)
  prefs: loadPrefs(),
};

export function on(fn) { listeners.add(fn); return () => listeners.delete(fn); }
export function emit(type, detail) { for (const fn of listeners) fn(type, detail); }

export function remember(rows) {
  for (const r of rows || []) store.rows.set(r.id, { ...(store.rows.get(r.id) || {}), ...r });
  return rows;
}
export const row = (id) => store.rows.get(id);

// ------------------------------------------------------------------ decisions
function snapshot(ids) {
  return ids.map((id) => {
    const r = store.rows.get(id) || {};
    return { id, rating: r.rating || 0, pick: r.pick || 0, label: r.label || "" };
  });
}

function applyLocal(items) {
  for (const it of items) {
    const r = store.rows.get(it.id);
    if (!r) continue;
    if (it.rating !== undefined && it.rating !== null) r.rating = it.rating;
    if (it.pick !== undefined && it.pick !== null) r.pick = it.pick;
    if (it.label !== undefined && it.label !== null) r.label = it.label || null;
  }
  emit("rows", items.map((i) => i.id));
}

/** items: [{id, rating?, pick?, label?}] — applied at once on screen, saved, and undoable as one step. */
export async function decide(items, label = "change") {
  if (!items.length) return;
  const before = snapshot(items.map((i) => i.id));
  applyLocal(items);
  try {
    const res = await api(P(store.project, "/decide"), { method: "POST", body: { items, label } });
    remember(res.rows);
    emit("rows", res.rows.map((r) => r.id));
    emit("history");
    if (items.some((i) => i.pick === -1)) explainReject();
    return res;
  } catch (e) {
    applyLocal(before);
    toast(`Couldn't save: ${e.message}`, { kind: "error" });
  }
}

/** The first reject on this browser says, once, what rejecting does and doesn't do. */
function explainReject() {
  if (store.prefs.toldReject) return;
  setPref("toldReject", true);
  const rejects = `#/p/${encodeURIComponent(store.project)}/library?status=rejects`;
  toast("Rejecting only marks a photo: nothing is deleted or moved. When you want rejects gone, Library › Rejects › "
    + "Move to Trash… moves them to the Mac's Trash after you confirm, and Finder's Put Back returns them. Undo (U) takes a reject back.",
  { persist: true, key: "reject-explainer", action: () => { location.hash = rejects; }, actionLabel: "See rejects" });
}

/** "1 photo you changed since was left as it is" / "3 photos … were left as they are". */
const keptSince = (k) => `${plural(k, "photo")} you changed since ${k === 1 ? "was left as it is" : "were left as they are"}`;

/** Go to these photos: the page in view can show them in place (Cull goes to their stack);
 * otherwise they open in the viewer. */
export function showPhotos(ids) {
  if (!ids?.length) return;
  if (store.show?.(ids)) return;
  import("./loupe.js").then((m) => m.openLoupe({ ids, index: 0 }));
}

/** Undo or Redo, said with what it did, and "Show" to go to the photos it touched. */
async function replay(which, body) {
  try {
    const res = await api(P(store.project, `/${which}`), { method: "POST", body });
    const what = which === "undo" ? res.undone : res.redone;
    if (!what) { toast(which === "undo" ? "Nothing to undo" : "Nothing to redo (a new change ends what can be redone)"); return null; }
    remember(res.rows);
    emit("rows", res.rows.map((r) => r.id));
    emit("history");
    emit("keywords");
    const ids = res.rows.map((r) => r.id);
    toast(`${which === "undo" ? "Undid" : "Redid"} ${what}` + (res.skipped ? ` · ${keptSince(res.skipped)}` : ""),
      ids.length ? { action: () => showPhotos(ids), actionLabel: "Show", key: "replay" } : { key: "replay" });
    return res;
  } catch (e) {
    toast(e.message, { kind: "error" });
    return null;
  }
}

export function redo() { return replay("redo", {}); }
export function undo(batch) { return replay("undo", batch ? { batch } : {}); }

// Decision helpers used by keyboard shortcuts in several views.
export function togglePick(ids, value) {
  // If every photo already has this state, clear it; otherwise set it.
  const all = ids.every((id) => (store.rows.get(id)?.pick || 0) === value);
  const pick = all ? 0 : value;
  return decide(ids.map((id) => ({ id, pick })), pick === 1 ? "pick" : pick === -1 ? "reject" : value === 1 ? "clear pick" : "clear reject");
}
/** Pick (1) or reject (-1): sets it, never clears it, so a second press or swipe changes nothing. */
export function setPick(ids, value) {
  return decide(ids.map((id) => ({ id, pick: value })), value === 1 ? "pick" : "reject");
}
/** Take the pick or reject and the stars off. */
export function clearDecision(ids) {
  return decide(ids.map((id) => ({ id, pick: 0, rating: 0 })), "clear");
}
export function setLabel(ids, label) {
  const all = ids.every((id) => (store.rows.get(id)?.label || "") === label);
  const v = all ? "" : label;
  return decide(ids.map((id) => ({ id, label: v })), v ? `${v} label` : "clear label");
}
export function acceptSuggestions(ids) {
  if (store.settings && store.settings.show_suggestions === false) {
    toast("Suggestions are hidden in this library, so there's nothing to accept. Turn them on under Settings › Browsing.");
    return Promise.resolve(0);
  }
  const items = [];
  for (const id of ids) {
    const r = store.rows.get(id);
    if (!r || r.pick || r.rating) continue;
    if (r.suggestion === "pick") items.push({ id, pick: 1 });
    if (r.suggestion === "reject") items.push({ id, pick: -1 });
  }
  if (!items.length) { toast("No undecided suggestions here"); return Promise.resolve(0); }
  return decide(items, "accept suggestions").then((res) => {
    if (res) toast(`Accepted ${plural(items.length, "suggestion")}`, { action: () => undo(res.batch) });
    return items.length;
  });
}
export async function toggleFav(ids, fav) {
  try {
    await api(P(store.project, "/fav"), { method: "POST", body: { ids, fav } });
    for (const id of ids) { const r = store.rows.get(id); if (r) r.fav = fav; }
    emit("rows", ids);
  } catch (e) { toast(e.message, { kind: "error" }); }
}

// ------------------------------------------------------------------ prefs (per browser)
function loadPrefs() {
  const defaults = { theme: "system", thumb: 200, stack: false, sort: "time", autoAdvance: true, info: true,
    headers: true, overlay: "", cullStart: "best" };
  try { return { ...defaults, ...JSON.parse(localStorage.getItem("joeylab.prefs") || "{}") }; }
  catch { return defaults; }
}
export function setPref(k, v) {
  store.prefs[k] = v;
  try { localStorage.setItem("joeylab.prefs", JSON.stringify(store.prefs)); } catch { /* private mode */ }
}
export function getLocal(k, d = null) {
  try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; }
}
export function setLocal(k, v) {
  try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ }
}

/** Settings of the current library (purpose, browsing...), cached. */
export async function settings(force = false) {
  if (!store.project) return {};
  if (!force && store.settings && store.settings._p === store.project) return store.settings;
  try { store.settings = { ...(await api(P(store.project, "/settings"))), _p: store.project }; }
  catch { store.settings = { _p: store.project }; }
  return store.settings;
}
export async function saveSettings(patch) {
  const s = await api(P(store.project, "/settings"), { method: "POST", body: patch });
  store.settings = { ...s, _p: store.project };
  emit("settings", store.settings);
  return store.settings;
}
