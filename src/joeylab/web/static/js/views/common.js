// Pieces shared by several views: the selection action bar, album and compare dialogs, and key
// handling for decisions.

import { api, img, P } from "../api.js";
import { loupeOpen, openLoupe } from "../loupe.js";
import { addToAlbum, removeFromAlbum } from "./albumpick.js";
import { bulkKeywordDialog, keywordDialog } from "./keywords.js";
import { canMove, fileActions } from "./moves.js";
import { acceptSuggestions, clearDecision, decide, emit, remember, row, setLabel, setPick, store, toggleFav, undo } from "../store.js";
import { agree, choiceSeg, clear, dialog, h, holdFocus, icon, LABEL_COLORS, n, plural, starsEl, toast } from "../ui.js";

const LABEL_KEYS = { 6: "Red", 7: "Yellow", 8: "Green", 9: "Blue" };

/** What a change to every matching photo did, in words: "Picked 337 photos". */
function bulkSaid(change, k) {
  const photos = plural(k, "photo");
  if (change.pick === 0) return `Cleared ${photos}`;
  if (change.pick === 1) return `Picked ${photos}`;
  if (change.pick === -1) return `Rejected ${photos}`;
  if (change.rating) return `Gave ${photos} ${"★".repeat(change.rating)}`;
  if (change.rating === 0) return `Took the stars off ${photos}`;
  if (change.label) return `${change.label} label on ${photos}`;
  return `Changed ${photos}`;
}

/** "Picked 24 photos · 6 were already", with Undo; or, when nothing changed, that it's so already. */
function told(change, k, of, batch) {
  const same = of - k;
  if (!k) {
    toast(change.pick === 1 ? (of > 1 ? `All ${n(of)} are picked already` : "Picked already")
      : change.pick === -1 ? (of > 1 ? `All ${n(of)} are rejected already` : "Rejected already")
        : change.pick === 0 ? "Nothing to clear" : "Nothing changed", { key: "decision" });
    return;
  }
  toast(bulkSaid(change, k) + (same > 0 && change.pick ? ` · ${n(same)} ${agree(same, "was", "were")} already` : ""),
    { key: "decision", ...(batch ? { action: () => undo(batch) } : {}) });
}

/** The decision a key makes. P and X set the pick or reject, so pressing again changes nothing;
 * 0 takes the pick, reject and stars off; a label key puts the label on, or takes it off again. */
function keyChange(k) {
  if (/^[1-5]$/.test(k)) return { change: { rating: +k }, what: `${k}★` };
  if (k === "0") return { change: { pick: 0, rating: 0 }, what: "clear" };
  if (LABEL_KEYS[k]) return { change: { label: LABEL_KEYS[k] }, what: `${LABEL_KEYS[k]} label`, toggle: true };
  if (k === "p" || k === "P") return { change: { pick: 1 }, what: "pick" };
  if (k === "x" || k === "X") return { change: { pick: -1 }, what: "reject" };
  return null;
}

/** Make a decision on these photos, and say what it did: picks, rejects and clearing always (the
 * grid has no big "Picked" like the viewer), stars only when they reached more photos than the
 * `shown` tiles (every frame of a stack). */
async function decideOn(ids, { change, what, toggle }, shown = ids.length) {
  if (toggle) return setLabel(ids, change.label);
  const res = await decide(ids.map((id) => ({ id, ...change })), what);
  if (res && ("pick" in change || ids.length > shown)) told(change, res.changed, ids.length, res.batch);
}

/** Keys that decide on a set of photos. Returns true if handled. */
export function decisionKey(e, ids) {
  if (e.metaKey || e.ctrlKey || e.altKey || !ids.length) return false;
  const k = e.key;
  const d = keyChange(k);
  if (d) { decideOn(ids, d); return true; }
  if (k === "f") { toggleFav(ids, !ids.every((id) => row(id)?.fav)); return true; }
  if (k === "t") { keywordDialog(ids); return true; }
  if (k === "b") { addToAlbum({ ids }); return true; }
  if (k === "a") { acceptSuggestions(ids); return true; }
  if (k === "c" && ids.length >= 2) { compareDialog(ids.slice(0, 6)); return true; }
  return false;
}

/** J and K between the groups on a page (duplicate groups and sets, event cards): the next or
 * previous one takes the keyboard focus and scrolls into view. Enter opens the one in focus (a link
 * or button does that itself). `items()` lists them; `focusOf(el)` is what takes the focus in one;
 * `open(el)` opens it. Returns a key handler. */
export function groupKeys(items, { focusOf = (el) => el, open = (el) => focusOf(el).click() } = {}) {
  let last = -1;                 // where J and K carry on from when the focus is elsewhere (a click, a redraw)
  return (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return false;
    const list = items();
    const at = list.findIndex((el) => el.contains(document.activeElement));
    if (e.key === "j" || e.key === "k") {
      if (!list.length) return false;
      const from = at >= 0 ? at : Math.min(last, list.length - 1);
      const i = from < 0 ? 0 : Math.max(0, Math.min(list.length - 1, from + (e.key === "j" ? 1 : -1)));
      last = i;
      focusOf(list[i]).focus({ preventScroll: true });
      list[i].scrollIntoView({ block: "nearest" });
      return true;
    }
    const el = at >= 0 ? focusOf(list[at]) : null;
    if (e.key === "Enter" && el === document.activeElement && !el.matches("a[href], button")) { open(list[at]); return true; }
    return false;
  };
}

/** The bar over a selection. Options, all functions:
 *   filter   the filter the grid shows (the Library's, or a search's: it may return a promise), so
 *            "Select all N matching" reaches past the loaded pages and the bar's actions then change
 *            every photo matching, on the server, as one step;
 *   stacked  the grid shows one frame per stack: keywords, albums and favourites then go on every
 *            matching frame of the stacks selected (stars, picks and labels on the frame shown);
 *   album    the hand-picked album being viewed, offering "Remove from album";
 *   dateable whether the selection holds photos without a date of their own (then "Date…" starts on
 *            giving them a date; otherwise on shifting their times);
 *   changed  called after a change that moves photos in or out of the grid (album, dates). */
export function selectionBar(grid, { filter, stacked = () => false, album = () => null, dateable = () => false, changed } = {}) {
  const all = () => grid.matchAll && filter;
  const target = () => grid.targetIds();
  const n = h("span", { class: "n" });
  const widen = h("button", { type: "button", class: "linkish", hidden: true, onclick: widenToAll });
  const ACCEPT_TIP = "Accept suggestions (A)";
  const ACCEPT_ALL_TIP = "Suggestions are accepted for the photos loaded so far: select those instead of everything matching";
  const acceptBtn = h("button", { class: "btn ghost small", title: ACCEPT_TIP, onclick: accept }, icon("sparkle"), "Accept");
  const albumBtn = h("button", { class: "btn ghost small", title: "Add to an album (B)", "aria-haspopup": "listbox", onclick: () => toAlbum() },
    icon("album"), "Add to album", h("span", { class: "caret", "aria-hidden": "true" }, "▸"));
  const outBtn = h("button", { class: "btn ghost small", hidden: true, title: "Take these out of this album (the photos stay in the library)",
    onclick: outOfAlbum }, "Remove from album");
  const dateBtn = h("button", { class: "btn ghost small", title: "Shift these photos' times (a camera whose clock was wrong), or date photos that have none",
    onclick: () => {
      const undated = dateable(all() ? null : target());
      return all() ? resolve().then((f) => dateDialog({ filter: f, count: grid.matchTotal, onDone: changed, undated }))
        : dateDialog({ ids: target(), onDone: changed, undated });
    } }, icon("calendar"), "Date…");
  const moveBtn = canMove() ? h("button", { class: "btn ghost small", title: "Move these photos to another folder, or rename their files, on disk",
    "aria-haspopup": "menu", onclick: () => onDisk() }, icon("folder"), "Move…") : null;
  const bar = h("div", { class: "selbar", hidden: true, role: "toolbar", "aria-label": "Selected photos" },
    n, widen,
    ...[1, 2, 3, 4, 5].map((s) => h("button", { class: "btn ghost small", title: `${s} stars (${s})`,
      onclick: () => decideSel(keyChange(String(s))) }, icon("star"), s)),
    h("span", { class: "sep" }),
    h("button", { class: "btn small", title: "Pick (P)", onclick: () => decideSel(keyChange("p")) }, icon("pick"), "Pick"),
    h("button", { class: "btn small", title: "Reject (X)", onclick: () => decideSel(keyChange("x")) }, icon("reject"), "Reject"),
    h("button", { class: "btn ghost small icon-only", title: "Favourite (F)", "aria-label": "Favourite", onclick: fav }, icon("heart")),
    h("span", { class: "label-pick" }, Object.entries(LABEL_COLORS).map(([name, c]) => h("button", {
      type: "button", class: "label-dot", style: { background: c }, title: `${name} label`, "aria-label": `${name} label`,
      onclick: () => decideSel({ change: { label: name }, what: `${name} label`, toggle: true }) }))),
    h("button", { class: "btn ghost small", title: "Remove stars, pick/reject and label",
      onclick: () => decideSel({ change: { rating: 0, pick: 0, label: "" }, what: "clear" }) }, "Clear"),
    acceptBtn,
    h("button", { class: "btn ghost small", title: "Add or remove keywords (T)", onclick: keyword }, "Keyword…"),
    h("span", { class: "sep" }),
    h("button", { class: "btn ghost small", title: "Compare side by side (C)", onclick: () => compareDialog(target().slice(0, 6)) }, icon("compare"), "Compare"),
    albumBtn, outBtn, dateBtn, moveBtn,
    h("button", { class: "btn ghost small", title: "Teach JoeyLab what these have in common", onclick: () => conceptDialog(target()) }, icon("wand"), "Teach…"),
    h("button", { class: "btn ghost small icon-only", title: "Deselect (Esc)", "aria-label": "Deselect", onclick: () => grid.clearSelection() }, icon("close")),
  );

  const resolve = async () => filter();
  // Stacks selected: how many photos they hold, and whether any holds more than its one frame.
  const stackPhotos = () => (stacked() ? [...grid.sel].reduce((k, id) => k + (grid.stackN?.get(id) || 1), 0) : grid.sel.size);

  function paint() {
    const g = grid;
    if (g.matchAll && g.sel.size < g.ids.length) g.matchAll = false;      // they deselected something
    bar.hidden = g.sel.size < 2;
    if (bar.hidden) return;
    // Accepting suggestions works on loaded photos only, so it's off (and says why) for "all matching".
    acceptBtn.disabled = !!all();
    acceptBtn.title = all() ? ACCEPT_ALL_TIP : ACCEPT_TIP;
    outBtn.hidden = !album();
    dateBtn.hidden = false;
    if (all()) {
      n.textContent = `All ${g.matchTotal.toLocaleString()} matching ${stacked() ? "photos " : ""}selected`;
      widen.hidden = true;
      return;
    }
    const photos = stackPhotos();
    n.textContent = photos > g.sel.size ? `${plural(g.sel.size, "stack")} (${plural(photos, "photo")}) selected`
      : `${g.sel.size.toLocaleString()} selected`;
    n.title = photos > g.sel.size ? "Stars, picks, labels, keywords, albums and favourites go on every photo in these stacks" : "";
    // Offer the rest when everything loaded is selected and there's more beyond it.
    const more = filter && g.sel.size === g.ids.length && !g.done;
    widen.hidden = !more;
    if (more) widen.textContent = stacked() ? "Select every photo matching" : `Select all ${g.total.toLocaleString()} matching`;
  }
  grid.onSelection(paint);

  async function widenToAll() {
    try {
      const r = await api(P(store.project, "/bulk"), { method: "POST", body: { filter: await resolve(), count_only: true } });
      grid.matchAll = true;
      grid.matchTotal = r.n;
      grid.selectAll();
    } catch (e) { toast(e.message, { kind: "error" }); }
  }
  if (filter) grid.selectAllMatching = widenToAll;           // ⌘A a second time

  async function bulk(change, what) {
    try {
      const r = await api(P(store.project, "/bulk"), { method: "POST", body: { filter: await resolve(), what, ...change } });
      afterBulk(r, change);
      told(change, r.n, Math.max(r.n, grid.matchTotal || 0), r.batch);
    } catch (e) { toast(e.message, { kind: "error" }); }
  }

  /** A decision from the bar or a key: on everything matching, or on the photos selected (or the one
   * in focus), and with stacks shown, on every frame of them. */
  async function decideSel(d) {
    if (all()) return bulk(d.change, d.what);
    try {
      const shown = target();
      if (shown.length) await decideOn(await frames(shown), d, shown.length);
    } catch (e) { toast(e.message, { kind: "error" }); }
  }

  /** Bring the loaded tiles up to date with what the server just changed. */
  function afterBulk(r, change = {}) {
    const shown = [];
    for (const id of r.ids || []) {
      const row_ = store.rows.get(id);
      if (!row_) continue;
      for (const k of ["rating", "pick", "label", "fav"]) if (k in change) row_[k] = k === "fav" ? (change.fav ? 1 : 0) : change[k];
      shown.push(id);
    }
    if (shown.length) emit("rows", shown);
    emit("history");
  }

  /** The photos a change to the selection reaches: with stacks shown, every matching frame in them. */
  async function frames(ids) {
    if (!stacked() || !ids.some((id) => (grid.stackN?.get(id) || 1) > 1)) return ids;
    const r = await api(P(store.project, "/frames"), { method: "POST", body: { filter: await resolve(), ids } });
    return r.ids;
  }

  async function keyword() {
    try {
      if (all()) return bulkKeywordDialog(await resolve(), grid.matchTotal, afterBulk);
      return keywordDialog(await frames(target()));
    } catch (e) { toast(e.message, { kind: "error" }); }
  }

  async function toAlbum(anchor = albumBtn) {
    try {
      if (all()) return addToAlbum({ filter: await resolve(), count: grid.matchTotal, anchor: bar.hidden ? null : anchor });
      return addToAlbum({ ids: await frames(target()), anchor: bar.hidden ? null : anchor });
    } catch (e) { toast(e.message, { kind: "error" }); }
  }

  async function onDisk() {
    try {
      const sel = all() ? { filter: await resolve(), count: grid.matchTotal } : { ids: await frames(target()) };
      fileActions(moveBtn, { ...sel, onDone: () => { grid.clearSel(); changed?.(); } });
    } catch (e) { toast(e.message, { kind: "error" }); }
  }

  async function outOfAlbum() {
    const id = album();
    if (!id) return;
    const done = () => { grid.clearSel(); changed?.(); };
    try {
      if (all()) return removeFromAlbum({ album: id, filter: await resolve(), onDone: done });
      return removeFromAlbum({ album: id, ids: await frames(target()), onDone: done });
    } catch (e) { toast(e.message, { kind: "error" }); }
  }

  async function fav() {
    const ids = target();
    const on = !ids.every((id) => row(id)?.fav);
    try {
      if (!all()) return toggleFav(await frames(ids), on);
      const r = await api(P(store.project, "/bulk"), { method: "POST", body: { filter: await resolve(), fav: on } });
      afterBulk(r, { fav: on });
      toast(`${on ? "Favourited" : "Unfavourited"} ${plural(r.n, "photo")}`);
    } catch (e) { toast(e.message, { kind: "error" }); }
  }

  function accept() {
    if (all()) { toast("Accepting suggestions works on the photos loaded so far. Select those (⌘A) instead of everything matching."); return; }
    acceptSuggestions(target());
  }

  // The bar's keys follow the same rules: everything matching, or every frame of the stacks selected.
  grid.bulkKey = (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return false;
    const k = e.key;
    if (k === "b" && (grid.sel.size || grid.focus >= 0)) { toAlbum(); return true; }
    const d = keyChange(k);
    if (d && (all() || target().length)) { decideSel(d); return true; }
    if (!all()) {
      if (k === "t" && grid.sel.size) { keyword(); return true; }
      if (k === "f" && grid.sel.size && stacked()) { fav(); return true; }
      return false;
    }
    if (k === "f") { fav(); return true; }
    if (k === "t") { keyword(); return true; }
    if (k === "a") { accept(); return true; }
    return false;
  };
  return bar;
}

/** Shift photos' times earlier or later (a camera whose clock was wrong: two bodies of one model,
 * say, that Tidy › Camera clocks can't tell apart), or give photos without a capture date one: ids, or
 * everything matching a filter. Both are kept in the catalog only and undone like a rating. */
export function dateDialog({ ids, filter, count, onDone, undated = false }) {
  const k = ids ? ids.length : count;
  const out = h("div", { class: "result", hidden: true });
  const fail = (e) => { out.hidden = false; out.className = "result err"; out.textContent = e.message; return false; };
  const done = (r, msg) => {
    remember(r.rows);
    emit("rows", r.rows.map((x) => x.id));
    emit("history");
    toast(msg, r.batch ? { action: () => undo(r.batch) } : {});
    onDone?.(r);
    return true;
  };
  // Shift
  const dir = h("select", { class: "input", id: "shift-dir", "aria-label": "Earlier or later" },
    h("option", { value: "1" }, "Later by"), h("option", { value: "-1" }, "Earlier by"));
  const hours = h("input", { class: "input", id: "shift-h", type: "number", min: 0, max: 17520, step: 1, value: "", placeholder: "0", inputmode: "numeric", "aria-label": "Hours" });
  const mins = h("input", { class: "input", id: "shift-m", type: "number", min: 0, max: 59, step: 1, value: "", placeholder: "0", inputmode: "numeric", "aria-label": "Minutes" });
  const seconds = () => (+dir.value) * ((+hours.value || 0) * 3600 + (+mins.value || 0) * 60);
  const first = ids ? ids.map(row).find((r) => r?.eff_ts != null && r.time_src !== "file") : null;
  const preview = h("p", { class: "muted small", "aria-live": "polite" });
  const paint = () => {
    if (!first) { preview.textContent = ""; return; }
    const t = first.eff_ts * 1000;            // the time shown everywhere: the camera's, corrected
    const s = seconds();
    const f = (ms) => new Date(ms).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "UTC" });
    preview.textContent = s ? `For example ${first.stem}: ${f(t)} → ${f(t + s * 1000)}` : "";
  };
  [dir, hours, mins].forEach((el) => el.addEventListener("input", paint));
  const shiftBox = h("div", { class: "stack-v" },
    h("p", { class: "note" }, "For a camera whose clock was wrong. When a whole camera was off, Tidy › Camera clocks corrects all its photos at once; this moves just the photos you chose (two bodies of one model, say). The files aren't changed, Undo takes it back, and updates keep it."),
    h("div", { class: "row shift-row" }, dir, h("label", { class: "field" }, h("span", {}, "Hours"), hours), h("label", { class: "field" }, h("span", {}, "Minutes"), mins)),
    preview);
  // Date
  const input = h("input", { class: "input", id: "date-when", placeholder: "1998, 1998-06 or 1998-06-14", autocomplete: "off" });
  const given = ids && ids.some((id) => row(id)?.time_src === "manual");
  const dateBox = h("div", { class: "stack-v" },
    h("p", { class: "note" }, "For photos with no capture date of their own, such as scans. A year on its own means 1 January, a month its first day. Photos whose files carry a date keep theirs."),
    h("label", { class: "field" }, h("span", {}, "Taken"), input));
  let mode = undated ? "date" : "shift";
  const seg = choiceSeg([["shift", "Shift times"], ["date", "Give a date"]], mode, (v) => { mode = v; show(); }, { label: "What to do" });
  const show = () => {
    seg.querySelectorAll("[role=radio]").forEach((b, i) => {
      const on = (i === 0) === (mode === "shift");
      b.setAttribute("aria-checked", on ? "true" : "false");
      b.tabIndex = on ? 0 : -1;
    });
    shiftBox.hidden = mode !== "shift";
    dateBox.hidden = mode !== "date";
    out.hidden = true;
    setTimeout(() => (mode === "shift" ? hours : input).focus(), 0);
  };
  const run = async () => {
    try {
      if (mode === "shift") {
        const s = seconds();
        if (!s) return fail(new Error("Say how many hours or minutes."));
        const r = await api(P(store.project, "/date/shift"), { method: "POST", body: { ...(ids ? { ids } : { filter }), seconds: s } });
        const h_ = Math.abs(s) / 3600;
        const by = h_ >= 1 ? `${+h_.toFixed(2)} h` : `${Math.abs(s) / 60} min`;
        const skipped = r.undated ? ` · ${plural(r.undated, "photo")} with no date left as ${r.undated === 1 ? "it is" : "they are"}` : "";
        return done(r, `Moved ${plural(r.n, "photo")} ${s > 0 ? "later" : "earlier"} by ${by}${skipped}`);
      }
      const date = input.value.trim();
      const r = await api(P(store.project, "/date"), { method: "POST", body: { ...(ids ? { ids } : { filter }), date } });
      const kept = r.kept ? ` · ${plural(r.kept, "photo")} with a date of ${r.kept === 1 ? "its" : "their"} own kept ${r.kept === 1 ? "it" : "theirs"}` : "";
      return done(r, `Dated ${plural(r.n, "photo")} ${date}${kept}`);
    } catch (e) { return fail(e); }
  };
  const takeOff = async () => {
    try {
      const r = await api(P(store.project, "/date"), { method: "POST", body: { ...(ids ? { ids } : { filter }), date: null } });
      return done(r, `Took the date off ${plural(r.n, "photo")}`);
    } catch (e) { return fail(e); }
  };
  dialog(`Date ${plural(k || 0, "photo")}`, h("div", { class: "stack-v" }, seg, shiftBox, dateBox, out), [
    { label: "Cancel" },
    ...(given ? [{ label: "Take the date off", run: takeOff }] : []),
    { label: "Apply", primary: true, submit: true, run },
  ]);
  show();
}

/** Save photos as a JoeyLab album (in the catalog), or as a folder of links/copies on disk. */
export function albumDialog({ ids, filter, name }) {
  const base = store.meta?.home ? `${store.meta.home}/Pictures/JoeyLab` : "~/Pictures/JoeyLab";
  const albumName = h("input", { class: "input", id: "album-name", value: name || "", placeholder: "Album name", required: true });
  const where = h("div");
  let mode = "catalog";
  const folderBox = h("div", { class: "stack-v", hidden: true });
  const dest = h("input", { class: "input mono", id: "album-dest" });
  const files = h("select", { class: "input", id: "album-files" },
    h("option", { value: "preferred" }, "RAW when there is one, otherwise JPEG"),
    h("option", { value: "jpeg" }, "JPEG only"), h("option", { value: "raw" }, "RAW only"), h("option", { value: "all" }, "Every file"));
  const copy = h("input", { type: "checkbox", id: "album-copy" });
  folderBox.append(h("label", { class: "field" }, h("span", {}, "Folder"), dest),
    h("label", { class: "field" }, h("span", {}, "Files"), files),
    h("label", { class: "check" }, copy, h("span", {}, "Copy the files instead of linking them (for a USB stick or sharing; uses space)")));
  const paintSeg = () => {
    const had = where.contains(document.activeElement);
    clear(where).append(choiceSeg([["catalog", "In JoeyLab"], ["folder", "As a folder on disk"]], mode, (k) => { mode = k; paintSeg(); },
      { label: "Where the album goes" }));
    if (had) where.querySelector('[aria-checked="true"]')?.focus();
    folderBox.hidden = mode !== "folder";
  };
  paintSeg();
  albumName.addEventListener("input", () => { dest.value = `${base}/${albumName.value.replace(/[/:]/g, "-") || "Album"}`; });
  dest.value = `${base}/${(name || store.project + "-album").replace(/[/:]/g, "-")}`;
  const out = h("div", { class: "result", hidden: true });
  const count = ids ? plural(ids.length, "photo") : "the photos matching your filter";
  dialog("Save as album", h("div", { class: "stack-v" },
    h("p", { class: "note" }, `${count[0].toUpperCase()}${count.slice(1)}. Your originals stay where they are.`),
    h("label", { class: "field" }, h("span", {}, "Name"), albumName),
    where, folderBox, out,
  ), [
    { label: "Cancel" },
    { label: "Save album", primary: true, submit: true, run: async () => {
      try {
        if (mode === "catalog") {
          const r = await api(P(store.project, "/albums"), { method: "POST", body: { name: albumName.value.trim(), ids, filter } });
          toast(`Album “${albumName.value.trim()}” with ${plural(r.added, "photo")}`, {
            action: () => { location.hash = `#/p/${encodeURIComponent(store.project)}/library?album=${r.id}`; }, actionLabel: "Open" });
        } else {
          const r = await api(P(store.project, "/export/album"), { method: "POST",
            body: { ids, filter, dest: dest.value.trim(), files: files.value, copy: copy.checked } });
          toast(`${copy.checked ? "Copied" : "Linked"} ${plural(r.created, "photo")} into ${r.dest}`);
        }
        return true;
      } catch (e) {
        out.hidden = false; out.className = "result err"; out.textContent = e.message;
        return false;
      }
    } },
  ]);
}

/** Teach a personal concept ("my blue bike") from a few examples. */
export function conceptDialog(ids) {
  const nameIn = h("input", { class: "input", placeholder: "What do these have in common? e.g. Grandma's house", required: true });
  const out = h("div", { class: "result", hidden: true });
  dialog("Teach JoeyLab something new", h("div", { class: "stack-v" },
    h("p", { class: "note" }, `From these ${ids.length} examples JoeyLab learns to find more photos like them, on this Mac. Use at least 3; more varied examples work better.`),
    h("label", { class: "field" }, h("span", {}, "Name"), nameIn), out), [
    { label: "Cancel" },
    { label: "Learn it", primary: true, submit: true, run: async () => {
      try {
        const r = await api(P(store.project, "/concepts"), { method: "POST", body: { name: nameIn.value.trim(), ids } });
        location.hash = `#/p/${encodeURIComponent(store.project)}/search?concept=${r.id}`;
        return true;
      } catch (e) { out.hidden = false; out.className = "result err"; out.textContent = e.message; return false; }
    } },
  ]);
}

/** Close the Compare overlay, if one is open (on navigation). */
export function closeCompare() { closeCompare.current?.(); }

/** Survey: 2-6 photos side by side to choose the strongest. */
export async function compareDialog(ids) {
  if (ids.length < 2) { toast("Select two to six photos to compare."); return; }
  const missing = ids.filter((id) => !row(id));
  if (missing.length) {
    const { remember } = await import("../store.js");
    remember((await api(P(store.project, "/rows"), { params: { ids: missing.join(",") } })).rows);
  }
  const host = h("div", { class: "survey stage-ui", role: "dialog", "aria-modal": "true", "aria-label": "Compare photos" });
  const cells = h("div", { class: "survey-grid", style: { "--cols": ids.length === 4 ? 2 : Math.min(ids.length, 3) } });
  let focus = 0;
  function paint() {
    clear(cells).append(ids.map((id, i) => {
      const r = row(id) || {};
      return h("div", { class: `survey-cell ${i === focus ? "cur" : ""} ${r.pick === -1 ? "rejected" : ""}`, "aria-current": i === focus ? "true" : null,
        onclick: () => { focus = i; paint(); } },
        h("img", { src: img.preview(store.project, r), alt: r.stem, ondblclick: () => openLoupe({ ids, index: i }) }),
        h("div", { class: "survey-bar" },
          h("kbd", {}, String(i + 1)), h("b", {}, r.stem),
          r.pick === 1 ? h("span", { class: "pill pick" }, "Picked") : r.pick === -1 ? h("span", { class: "pill reject" }, "Rejected") : null,
          h("span", { class: "spacer" }),
          starsEl(r.rating || 0, (v) => decide([{ id, rating: v }], `${v}★`).then(paint))));
    }));
  }
  const keepBtn = h("button", { class: "btn primary", onclick: async () => {
    const keep = ids[focus];
    await decide(ids.map((id) => (id === keep ? { id, pick: 1 } : { id, pick: -1 })), "keep the best of a comparison");
    close();
  } }, icon("keep"), "Keep the highlighted one, reject the others");
  let release = () => {};
  const close = () => {
    document.removeEventListener("keydown", onKey, true);
    release();
    host.remove();
    document.body.style.overflow = "";
    if (closeCompare.current === close) closeCompare.current = null;
  };
  closeCompare.current?.();
  closeCompare.current = close;
  const onKey = (e) => {
    if (loupeOpen()) return;              // a photo opened from here has the keys until it closes
    const k = e.key;
    if (k === "Escape") { close(); }
    else if (/^[1-6]$/.test(k) && +k <= ids.length) { focus = +k - 1; paint(); }
    else if (k === "ArrowRight") { focus = Math.min(ids.length - 1, focus + 1); paint(); }
    else if (k === "ArrowLeft") { focus = Math.max(0, focus - 1); paint(); }
    else if (k === "p") { setPick([ids[focus]], 1).then(paint); }
    else if (k === "x") { setPick([ids[focus]], -1).then(paint); }
    else if (k === "0") { clearDecision([ids[focus]]).then(paint); }
    else if (k === "Enter") { keepBtn.click(); }
    else if (k === " ") { openLoupe({ ids, index: focus }); }
    else return;
    e.preventDefault(); e.stopPropagation();
  };
  host.append(h("div", { class: "loupe-top" }, h("div", { class: "loupe-title" }, h("b", {}, "Compare"), h("span", {}, `${ids.length} photos · 1–${ids.length} to highlight, Enter keeps it, Space opens it`)),
    h("div", { class: "right" }, keepBtn, h("button", { class: "btn ghost icon-only", "aria-label": "Close", onclick: close }, icon("close")))), cells);
  document.body.append(host);
  document.body.style.overflow = "hidden";
  document.addEventListener("keydown", onKey, true);
  release = holdFocus(host);
  keepBtn.focus({ preventScroll: true });
  paint();
}

// The shortcuts dialog lives in keys.js (by page, the current one first; gestures on a phone).
export { keysDialog } from "./keys.js";

/** How strictly frames are stacked: a 1-5 slider that shows, while you drag, what each level would give,
 * and restacks when you let go. `onApplied` runs once the new stacks are in. */
export function groupingControl(project, onApplied) {
  const range = h("input", { type: "range", min: 1, max: 5, step: 1, class: "grouping-range", "aria-label": "How strictly frames are stacked" });
  const name = h("b", {}, "…");
  const detail = h("span", { class: "muted" });
  const box = h("div", { class: "grouping-ctl" },
    h("div", { class: "row" }, h("span", { class: "muted" }, "Stricter"), range, h("span", { class: "muted" }, "Looser")),
    h("div", { class: "grouping-read" }, name, detail));
  let levels = {};
  const show = (lv) => {
    const x = levels[lv];
    if (!x) return;
    name.textContent = x.name;
    detail.textContent = ` · ${plural(x.stacks, "stack")}, ${x.multi.toLocaleString()} with more than one frame`
      + (x.largest > 1 ? `, the biggest ${x.largest}` : "");
  };
  range.addEventListener("input", () => show(+range.value));
  range.addEventListener("change", async () => {
    const lv = +range.value;
    range.disabled = true;
    try {
      await api(P(project, "/settings"), { method: "POST", body: { grouping: lv } });
      let state = {};
      for (let i = 0; i < 120; i++) {           // restacking takes seconds; an update may hold it up longer
        await new Promise((r) => setTimeout(r, 1000));
        state = await api(P(project, "/grouping"));
        if (!state.busy) break;
      }
      if (state.error) throw new Error(`Restacking failed: ${state.error}`);
      toast(`Restacked: ${levels[lv]?.name || lv}`);
      onApplied?.();
    } catch (e) { toast(e.message, { kind: "error" }); }
    range.disabled = false;
  });
  api(P(project, "/grouping")).then((d) => { levels = d.levels; range.value = d.level; show(d.level); })
    .catch((e) => { detail.textContent = e.message; });
  return box;
}

export { undo };
