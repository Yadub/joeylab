// Command palette (⌘K): every page and action in one searchable list, with its shortcut.

import { api, P } from "../api.js";
import { goTo } from "../loupe.js";
import { redo, store, undo } from "../store.js";
import { setTheme } from "../theme.js";
import { clear, h, holdFocus, icon, plural, toast } from "../ui.js";
import { keysDialog } from "./common.js";
import { trashDialog } from "./trash.js";

export function openPalette({ project, views, hashFor, current, switchTo }) {
  if (document.querySelector(".palette")) return;
  const go = (view, params) => () => { goTo(hashFor(project, view, params || {})); };
  const owner = store.meta?.can_trash;
  const cmds = [];
  if (project) {
    for (const [k, v] of Object.entries(views)) if (!v.hidden) cmds.push({ label: `Go to ${v.label}`, icon: v.icon, run: go(k) });
    cmds.push(
      { label: "Search photos", hint: "/", icon: "search", run: go("search") },
      { label: "Cull: next undecided stack", icon: "stack", run: go("cull") },
      { label: "Library: picks", icon: "pick", run: go("library", { status: "picks" }) },
      { label: "Library: suggested rejects to review", icon: "reject", run: go("library", { status: "undecided", suggested: "reject" }) },
      { label: "Library: worth checking (soft focus, blinks, exposure)", icon: "info", run: go("library", { status: "undecided", suggested: "check" }) },
      { label: "Cull: go through the photos worth checking", icon: "stack", run: go("cull", { suggested: "check" }) },
      { label: "Library: your favourites", icon: "heart", run: go("library", { favs: "me" }) },
      { label: "Library: videos", icon: "play", run: go("library", { media: "video" }) },
      { label: "Library: fixable in editing (RAW with exposure problems)", icon: "wand", run: go("library", { flag: "rescuable" }) },
      { label: "Library: edited photos", icon: "wand", run: go("library", { edited: 1 }) },
      { label: "Library: undated photos", icon: "clock", run: go("library", { undated: 1 }) },
      { label: "Library: explore everything, by what's in it, colour, light, time, quality or camera", icon: "explore", run: go("library", { view: "explore" }) },
      { label: "Library: folders", icon: "folder", run: go("library", { view: "folders" }) },
      { label: "Undo the last change", hint: "U", icon: "undo", run: () => undo() },
      { label: "Redo what you undid", hint: "⇧U", icon: "refresh", run: () => redo() },
      { label: "History: every change, to undo or redo, and moves to the Trash", icon: "history", run: go("history") },
      { label: "Library: photos in no album", icon: "album", run: go("library", { no_album: 1 }) },
      { label: "Library: RAW only", icon: "layers", run: go("library", { filetype: "raw" }) },
      { label: "Library: recently added", icon: "clock", run: go("library", { sort: "added" }) },
      { label: "People: name faces quickly", icon: "people", run: go("people", { tab: "stream" }) },
      { label: "Events and trips", icon: "calendar", run: go("events") },
      { label: "Places and map", icon: "pin", run: go("places") },
      { label: "Tidy: duplicates", icon: "broom", run: go("tidy", { tab: "dupes" }) },
      { label: "Library: rejected photos", icon: "reject", run: go("library", { status: "rejects" }) },
      ...(owner ? [
        { label: "Move rejected photos to the Trash…", icon: "close", run: () => trashDialog({ what: "rejects", filter: { status: "rejects" }, onDone: () => current?.()?.api?.refresh?.() }) },
        { label: "Move empty folders to the Trash…", icon: "folder", run: () => trashDialog({ what: "folders", onDone: () => current?.()?.api?.refresh?.() }) },
      ] : []),
      { label: "Tidy: empty folders", icon: "folder", run: go("tidy", { tab: "health", empty: 1 }) },
      { label: "Tidy: library health", icon: "shield", run: go("tidy", { tab: "health" }) },
      { label: "Tidy: photos with only one copy (backup check)", icon: "shield", run: go("tidy", { tab: "backups" }) },
      { label: "Tidy: sidecars whose photo is gone", icon: "shield", run: go("tidy", { tab: "health", deep: 1 }) },
      { label: "Tidy: camera clocks", icon: "clock", run: go("tidy", { tab: "clock" }) },
      { label: "Tidy: folder names", icon: "folder", run: go("tidy", { tab: "names" }) },
      { label: "Albums", icon: "album", run: go("albums") },
      { label: "Best of a year", icon: "star", run: go("albums", { tab: "bestof" }) },
      { label: "Balanced set for delivery", icon: "keep", run: go("albums", { tab: "balance" }) },
      ...(store.settings?.purpose === "shoot" ? [{ label: "Shot list", icon: "check2", run: go("home", { tab: "shotlist" }) }] : []),
      { label: "Help JoeyLab learn your taste (10 quick choices)", icon: "sparkle", run: go("home", { learn: 1 }) },
      { label: "Settings: folders", icon: "folder", run: go("settings", { s: "folders" }) },
      { label: "Settings: export ratings to Lightroom (XMP)", icon: "download", run: go("settings", { s: "export" }) },
      { label: "Settings: use with Claude Code or Codex (MCP)", icon: "sparkle", run: go("settings", { s: "assistants" }) },
      { label: "Settings: backups and restore", icon: "shield", run: go("settings", { s: "backups" }) },
      { label: "Download all decisions (portable JSON)", icon: "download", run: () => { location.href = P(project, "/export/decisions.json"); } },
      { label: "Update the library now", icon: "refresh", run: async () => {
        try { await api(P(project, "/jobs"), { method: "POST", body: {} }); toast("Updating: looking for new and changed photos…"); }
        catch (e) { toast(e.message, { kind: "error" }); }
      } },
    );
  }
  cmds.push(
    { label: "Keyboard shortcuts", hint: "?", icon: "info", run: keysDialog },
    { label: "Theme: light", icon: "sun", run: () => setTheme("light") },
    { label: "Theme: dark", icon: "moon", run: () => setTheme("dark") },
    { label: "Theme: follow the system", icon: "auto", run: () => setTheme("system") },
    { label: "All libraries", icon: "home", run: () => goTo("#/") },
  );

  // Things to jump to by name (people, keywords, albums, events, folders, years), fetched once a
  // minute at most and matched only once something is typed.
  let places = [];
  const fresh = store.jumpIndex?.project === project && Date.now() - store.jumpIndex.at < 60_000;
  if (project) {
    (fresh ? Promise.resolve(store.jumpIndex.d) : api(P(project, "/jump")).then((d) => { store.jumpIndex = { project, at: Date.now(), d }; return d; }))
      .then((d) => { places = jumpTargets(d, go); if (input.value.trim()) filter(); })
      .catch(() => {});
  }
  // The other libraries (opening in the same section), found by their names too.
  api("/api/projects").then((list) => {
    cmds.push(...list.filter((x) => x.name !== project).map((x) => ({
      label: `Switch to library ${x.name}`, icon: "home", kind: plural(x.photos, "photo"),
      run: () => goTo(switchTo ? switchTo(x.name) : `#/p/${encodeURIComponent(x.name)}`) })));
    if (box.isConnected) filter();
  }).catch(() => {});

  // A combobox: the keyboard stays in the box, ↑ and ↓ move the highlight through the list (a
  // screen reader reads the one highlighted), Enter runs it.
  const input = h("input", { class: "input", placeholder: "Type a command, a name or a search…", "aria-label": "Command",
    role: "combobox", "aria-expanded": "true", "aria-controls": "palette-list", "aria-autocomplete": "list", autocomplete: "off" });
  const list = h("div", { class: "palette-list", id: "palette-list", role: "listbox", "aria-label": "Commands" });
  const count = h("div", { class: "sr-only", role: "status" });
  const box = h("div", { class: "palette", role: "dialog", "aria-modal": "true", "aria-label": "Command palette" }, input, list, count);
  const shade = h("div", { class: "palette-shade", onclick: () => close() });
  let sel = 0, shown = [];
  function filter() {
    const q = input.value.trim().toLowerCase();
    const words = q.split(/\s+/).filter(Boolean);
    shown = cmds.filter((c) => words.every((w) => c.label.toLowerCase().includes(w)));
    if (q.length >= 2) {
      const hits = places.filter((c) => words.every((w) => c.key.includes(w)));
      // Names that start with what was typed come first, then the biggest.
      hits.sort((a, b) => (b.key.startsWith(q) - a.key.startsWith(q)) || (b.n || 0) - (a.n || 0));
      shown.push(...hits.slice(0, 20));
    }
    if (q && project) shown.push({ label: `Search photos for “${input.value.trim()}”`, icon: "search",
      run: () => { location.hash = hashFor(project, "search", { q: input.value.trim() }); } });
    sel = Math.min(sel, Math.max(0, Math.min(shown.length, 40) - 1));
    clear(list).append(shown.slice(0, 40).map((c, i) => h("button", {
      type: "button", class: `menu-item ${i === sel ? "on" : ""}`, role: "option", id: `palette-opt-${i}`, tabindex: "-1",
      "aria-selected": i === sel ? "true" : "false",
      onmouseenter: () => { sel = i; paint(); }, onclick: () => run(c),
    }, icon(c.icon || "right"), h("span", { class: "grow" }, c.label),
    c.hint ? [h("span", { class: "sr-only" }, ", shortcut"), h("kbd", {}, c.hint)] : c.kind ? h("span", { class: "faint" }, h("span", { class: "sr-only" }, ", "), c.kind) : null)));
    paint();
    const k = Math.min(shown.length, 40);
    count.textContent = input.value.trim() ? (k ? `${k} ${k === 1 ? "result" : "results"}` : "Nothing matches") : "";
  }
  function paint() {
    [...list.children].forEach((el, i) => { el.classList.toggle("on", i === sel); el.setAttribute("aria-selected", i === sel ? "true" : "false"); });
    list.children[sel]?.scrollIntoView({ block: "nearest" });
    if (list.children[sel]) input.setAttribute("aria-activedescendant", list.children[sel].id);
    else input.removeAttribute("aria-activedescendant");
  }
  function run(c) { close(); c.run(); }
  let release = () => {};
  function close() { box.remove(); shade.remove(); document.removeEventListener("keydown", onKey, true); release(); }
  function onKey(e) {
    if (e.key === "Escape") { close(); }
    else if (e.key === "ArrowDown") { sel = Math.min(Math.min(shown.length, 40) - 1, sel + 1); paint(); }
    else if (e.key === "ArrowUp") { sel = Math.max(0, sel - 1); paint(); }
    else if (e.key === "Tab") { input.focus(); }           // the list is read from the box; Tab stays here
    else if (e.key === "Enter") { if (shown[sel]) run(shown[sel]); }
    else return;
    e.preventDefault(); e.stopPropagation();
  }
  input.addEventListener("input", () => { sel = 0; filter(); });
  document.addEventListener("keydown", onKey, true);
  document.body.append(shade, box);
  release = holdFocus(box);           // Tab stays in the palette; Esc goes back where you were
  filter();
  input.focus();
}

function jumpTargets(d, go) {
  const out = [];
  const add = (label, key, iconName, n, run, sub) => out.push({ label, key: key.toLowerCase(), icon: iconName, n, run, kind: sub });
  for (const x of d.people) {
    add(x.name, x.name, "people", 1e6, go("library", { person: x.id }), x.kind === "pet" ? "pet" : "person");
    for (const a of x.aliases || []) add(`${a} → ${x.name}`, a, "people", 1e6, go("library", { person: x.id }), x.kind === "pet" ? "pet" : "person");
  }
  if (d.me) add("Photos of you", "me you myself photos of me", "people", 2e6, go("library", { person: d.me }), "you");
  for (const k of d.keywords) add(k.path, k.path, "tag", k.n, go("library", { keyword: k.id }), "keyword");
  for (const a of d.albums) add(a.name, a.name, "album", 5e5, go("library", { album: a.id }), "album");
  // An event is found by its name, what its photos show ("Dance performance") or its folder's name.
  for (const e of d.events) {
    const also = e.alt_name && !String(e.name).toLowerCase().includes(e.alt_name.toLowerCase()) ? ` · ${e.alt_name}` : "";
    add((e.year && !String(e.name).includes(String(e.year)) ? `${e.name} · ${e.year}` : e.name) + also,
      `${e.name} ${e.alt_name || ""} ${e.folder || ""} ${e.year || ""}`, e.kind === "trip" ? "pin" : "calendar", e.n,
      go("library", e.kind === "trip" ? { trip: e.id } : { event: e.id }), e.kind || "event");
  }
  for (const f of d.folders) {
    const base = f.path.split("/").filter(Boolean).pop() || f.path;
    add(f.label ? `${f.label} (${base})` : base, `${f.label || ""} ${base} ${f.path}`, "folder", f.n, go("library", { folder: f.path }), "folder");
  }
  for (const y of d.years) add(y.year, y.year, "calendar", y.n, go("library", { from: `${y.year}-01-01`, to: `${y.year}-12-31` }), "year");
  return out;
}
