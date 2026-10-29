// Library: filter rail + three lenses on the same photos.
//   Photos  — the justified grid, with month headers and a scrubber when sorted by time
//   Folders — the drive's folders as a tree, with your own names for them
//   Explore — every photo on one surface, arranged by what's in it, colour, light, time, quality or camera (explore.js)
// Filters live in the URL.

import { api, P } from "../api.js";
import { createGrid } from "../grid.js";
import { openLoupe } from "../loupe.js";
import { on, row, saveSettings, setPref, store } from "../store.js";
import { keywordPath, keywordTree, manageKeywords } from "./keywords.js";
import { agree, ask, choiceSeg, clear, flagLabel, focusOn, h, icon, LABEL_COLORS, monthLabel, n, plural, roving, starsEl, sugLabel, tabBar, toast } from "../ui.js";
import { albumName, isManualAlbum } from "./albumpick.js";
import { albumDialog, decisionKey, keysDialog, selectionBar } from "./common.js";
import { eventHeader, eventName } from "./events.js";
import { exploreView } from "./explore.js";
import { canMove, folderBar, folderMenu } from "./moves.js";
import { notePlaces, placeName } from "./places.js";
import { moveToLibrary } from "./splitlib.js";
import { trashDialog } from "./trash.js";
import { rawActions, rawFilterLabel, rawTrashDialog, RAW_SEG } from "./raws.js";
import { waitNote } from "./waiting.js";

export const FILTER_KEYS = ["status", "min_rating", "suggested", "flag", "no_flag", "min_q", "max_q", "camera", "lens", "min_iso", "max_iso",
  "min_focal", "max_focal", "max_f", "folder", "folder_like", "from", "to", "month", "person", "people", "cluster", "album", "no_album",
  "event", "trip", "place", "country", "gps", "media", "filetype", "label", "edited", "undated", "favs", "c2pa", "keyword", "not_keyword",
  "untagged", "step", "raw", "backup", "ids", "q", "all"];
const list = (v) => (v ? String(v).split(",").filter(Boolean) : []);
// The rail lists this many keywords before asking you to find the rest by name.
const RAIL_KEYWORDS = 300;

export function mount(root, ctx) {
  const params = Object.fromEntries([...ctx.params].filter(([k]) => FILTER_KEYS.includes(k) || ["sort", "view", "by"].includes(k)));
  // "view" is Photos / Folders / Explore; "lens" is the camera-lens filter. (Old links used lens=folders,
  // and Explore was called Map.)
  if (["folders", "map"].includes(params.lens)) { params.view = params.view || params.lens; delete params.lens; }
  if (params.view === "map") params.view = "explore";
  const lens = params.view || "photos";
  const rail = h("aside", { class: "rail", id: "lib-filters", "aria-label": "Filters" });
  const count = h("span", { class: "count" }, "…");
  // Browsing hides thumbnail-sized photos (and utility shots if asked): say how many, and offer them.
  const hiddenChip = h("button", { type: "button", class: "chip hidden-chip", hidden: true,
    title: "Hidden while browsing: thumbnail-sized photos, extra copies and, if you chose, screenshots (Settings › Browsing), and files that couldn't be read. Show them too",
    onclick: () => set("all", "1") });
  const main = h("div", { class: "lib-main" });
  // On a phone the filters slide over the photos: a tap outside, Done or Esc closes them.
  const scrim = h("div", { class: "rail-scrim", onclick: () => openRail(false) });
  const wrap = h("div", { class: "lib" }, h("h1", { class: "sr-only" }, "Library"), rail, scrim, main);
  root.append(wrap);
  // On a phone the rail is off the screen: the button says how many filters are on, and a row of
  // chips above the photos shows them (each one removes its filter).
  const filterLbl = h("span", {}, "Filters");
  const filterBtn = h("button", { class: "btn filter-toggle", type: "button", "aria-controls": "lib-filters", "aria-expanded": "false",
    onclick: () => openRail(!wrap.classList.contains("rail-open")) }, icon("filter"), filterLbl);
  const chipRow = h("div", { class: "chips lib-chips", role: "group", "aria-label": "Filters in use", hidden: true });
  function openRail(open) {
    wrap.classList.toggle("rail-open", open);
    filterBtn.setAttribute("aria-expanded", open ? "true" : "false");
    railReach();
    if (open) rail.querySelector("button, input, select, a")?.focus({ preventScroll: true });
  }
  // On a phone the closed panel is off the screen: the keyboard and a screen reader pass it by too.
  const narrow = window.matchMedia("(max-width: 900px)");
  const railReach = () => { rail.inert = narrow.matches && !wrap.classList.contains("rail-open"); };
  narrow.addEventListener("change", railReach);
  railReach();
  const railOpen = () => wrap.classList.contains("rail-open");
  const st = store.settings || {};

  const filterParams = () => Object.fromEntries(Object.entries(params).filter(([k, v]) => FILTER_KEYS.includes(k) && v));
  store.libraryParams = filterParams();
  const sortNow = () => params.sort || store.prefs.sort;
  const trashSlot = h("span", { class: "trash-slot" });
  // The summary of what's shown (when, who, where...), open by default when arriving with a search.
  const summaryEl = h("section", { class: "lib-summary", "aria-label": "Summary of these photos", hidden: true });
  const summaryOpen = () => store.prefs.summary ?? Boolean(params.q);
  const summaryBtn = h("button", { class: "btn ghost summary-btn", type: "button", "aria-pressed": String(summaryOpen()), "aria-label": "Summary",
    title: "Summary: when, who, where and what these photos are", onclick: () => { setPref("summary", !summaryOpen()); paintSummary(); } },
  icon("chart"), h("span", { class: "lbl" }, "Summary"));
  let summarySeq = 0;
  async function paintSummary() {
    summaryBtn.setAttribute("aria-pressed", String(summaryOpen()));
    summaryEl.hidden = !summaryOpen() || lens !== "photos";
    if (summaryEl.hidden) return;
    const seq = ++summarySeq;
    let s;
    try { s = await api(P(ctx.project, "/summary"), { params: filterParams() }); } catch (e) { clear(summaryEl).append(h("div", { class: "result err" }, e.message)); return; }
    if (seq !== summarySeq) return;
    renderSummary(s);
  }
  function setRange(key, unit) {
    const [y, m] = key.split("-").map(Number);
    const last = unit === "month" ? new Date(y, m, 0).getDate() : 31;
    params.from = unit === "month" ? `${key}-01` : `${key}-01-01`;
    set("to", unit === "month" ? `${key}-${String(last).padStart(2, "0")}` : `${key}-12-31`);
  }
  function renderSummary(s) {
    notePlaces(s.places);
    const range = s.first && s.last ? (s.first.slice(0, 4) === s.last.slice(0, 4) ? s.first.slice(0, 4) : `${s.first.slice(0, 4)}–${s.last.slice(0, 4)}`) : "";
    const facts = [plural(s.n, "photo"), range, s.videos ? plural(s.videos, "video") : "", s.picks ? plural(s.picks, "pick") : "",
      s.rejects ? plural(s.rejects, "reject") : "", s.undated ? `${n(s.undated)} undated` : ""].filter(Boolean);
    const card = (title, ...body) => h("div", { class: "sum-card" }, h("h3", {}, title), ...body);
    const chips = (items, label, onPick) => h("div", { class: "chips" }, items.map((it) => h("button", { type: "button", class: "chip",
      onclick: () => onPick(it), title: `Show only ${label(it)}` }, label(it), h("span", { class: "meta" }, n(it.n)))));
    const max = Math.max(1, ...s.when.map((r) => r.n));
    const cards = [
      s.when.length ? card(s.unit === "year" ? "By year" : "By month", h("div", { class: "sum-bars" }, s.when.map((r) =>
        h("button", { type: "button", class: "sum-bar", onclick: () => setRange(r.key, s.unit), title: `Show only ${s.unit === "year" ? r.key : monthLabel(r.key)}` },
          h("span", { class: "k" }, s.unit === "year" ? r.key : monthLabel(r.key)),
          h("span", { class: "track" }, h("i", { style: { width: `${Math.max(2, Math.round((r.n / max) * 100))}%` } })),
          h("span", { class: "n" }, n(r.n)))))) : null,
      s.people.length ? card("People", chips(s.people, (x) => x.name, (x) => set("person", x.id))) : null,
      s.places.length ? card("Places", chips(s.places, (x) => x.name, (x) => set("place", x.id))) : null,
      s.events.length ? card("Events", chips(s.events, (x) => x.name || "An event", (x) => set("event", x.id))) : null,
      s.cameras.length ? card("Cameras", chips(s.cameras, (x) => x.name, (x) => set("camera", x.name))) : null,
      s.folders.length ? card("Folders", chips(s.folders, (x) => x.path.split("/").filter(Boolean).slice(-2).join(" › "), (x) => set("folder", x.path))) : null,
    ].filter(Boolean);
    clear(summaryEl).append(h("p", { class: "sum-head" }, facts.join(" · ")), h("div", { class: "sum-cards" }, cards));
  }
  const timeSorted = () => ["time", "time_desc"].includes(sortNow());

  let grid = null;
  let explore = null;
  function set(k, v) {
    if (v === undefined || v === null || v === "" || v === false) delete params[k];
    else params[k] = String(v);
    store.libraryParams = filterParams();
    ctx.setParams(params);
    renderRail();
    paintTrash();
    if (lens === "photos") { grid?.reload(); paintSummary(); }
    else if (lens === "explore") explore?.setFilter(filterParams());
  }

  // A date box reports every keystroke as a change ("0002-05-01" after the first digit of the year).
  // Applying that redrew the rail and sent the rest of the typing to the grid as rating keys, so a
  // date is applied only once it's whole (a year from 1900) or cleared.
  function setDate(k, v) {
    if (!v) return set(k, "");
    const y = +v.slice(0, 4);
    if (/^\d{4}-\d{2}-\d{2}$/.test(v) && y >= 1900 && y <= 2100) set(k, v);
  }

  // ------------------------------------------------------------ toolbar
  const body = h("div");
  const lensSeg = tabBar([["photos", [icon("grid"), "Photos"]], ["folders", [icon("folder"), "Folders"]], ["explore", [icon("explore"), "Explore"]]], lens,
    (k) => ctx.setParams({ ...params, view: k === "photos" ? "" : k }, true), { label: "View", id: "lib-view", panel: body });
  const sort = h("select", { class: "input", id: "lib-sort", style: { width: "auto" }, "aria-label": "Sort",
    onchange: (e) => { setPref("sort", e.target.value); set("sort", e.target.value); } },
  [["time", "Oldest first"], ["time_desc", "Newest first"], ["added", "Recently added"], ["quality", "Best quality"], ["taste", "Your taste"],
    ["aesthetic", "Best looking"], ["sharpness", "Sharpest"], ["rating", "Highest rated"], ["size", "Largest files"], ["camera", "Camera"],
    ["path", "Folder and name"]]
    .map(([v, t]) => h("option", { value: v, selected: sortNow() === v }, t)));
  const stack = h("input", { type: "checkbox", id: "lib-stack", checked: store.prefs.stack,
    onchange: (e) => { setPref("stack", e.target.checked); grid?.reload(); } });
  const headersBox = h("input", { type: "checkbox", id: "lib-headers", checked: store.prefs.headers,
    onchange: (e) => { setPref("headers", e.target.checked); grid?.reload(); } });
  // (Its title says how many it saves once the grid has counted them.)
  const albumBtn = h("button", { class: "btn ghost", onclick: () => albumDialog({ filter: filterParams(), name: "" }), title: "Save everything matching as an album" },
    icon("album"), "Album…");
  const size = h("input", { type: "range", class: "size", id: "lib-size", min: 90, max: 380, step: 10, value: store.prefs.thumb,
    "aria-label": "Thumbnail size", oninput: (e) => { setPref("thumb", +e.target.value); grid?.relayout(); } });
  main.append(
    h("div", { class: "toolbar" },
      filterBtn,
      count, hiddenChip, lensSeg, lens === "photos" ? summaryBtn : null,
      h("span", { class: "spacer" }),
      lens === "photos" ? [
        h("label", { class: "check hide-sm", title: "Month headings and a year scrubber" }, headersBox, h("span", {}, "Months")),
        h("label", { class: "check", title: "One photo per stack: frames shot together sit behind the best one" }, stack, h("span", {}, "Stacks")),
        sort, size,
        h("button", { class: "btn ghost hide-sm", onclick: () => grid.selectAll(), title: "Select all loaded (⌘A)" }, "Select all"),
        albumBtn,
        h("button", { class: "btn ghost hide-sm", title: "A smart album keeps following this filter", onclick: smartAlbum }, icon("sparkle"), "Smart album"),
        trashSlot,
      ] : null),
    chipRow,
    lens === "photos" ? h("div", { class: "keyhint" },
      // An archive is tagged and starred more than it's culled, so its hints lead with that.
      st.purpose === "archive" ? [
        h("span", {}, h("kbd", {}, "T"), " keyword"), h("span", {}, h("kbd", {}, "1"), "–", h("kbd", {}, "5"), " stars"),
        h("span", {}, h("kbd", {}, "F"), " favourite"), h("span", {}, h("kbd", {}, "⌘A"), " select all"),
      ] : [
        h("span", {}, h("kbd", {}, "P"), " pick"), h("span", {}, h("kbd", {}, "X"), " reject"), h("span", {}, h("kbd", {}, "0"), " clear"),
        h("span", {}, h("kbd", {}, "1"), "–", h("kbd", {}, "5"), " stars"), h("span", {}, h("kbd", {}, "6"), "–", h("kbd", {}, "9"), " label"),
        h("span", {}, h("kbd", {}, "F"), " favourite"),
      ],
      h("span", {}, h("kbd", {}, "U"), " undo"),
      h("span", {}, h("kbd", {}, "Enter"), " open"), h("span", {}, h("kbd", {}, "C"), " compare selected"),
      h("span", {}, "⇧/⌘ click to select several"), h("button", { class: "linkish", onclick: keysDialog }, "All shortcuts")) : null);

  main.append(waitNote({ step: "group", progressOf: "analyse", text: "Stacks and suggestions will appear when analysis finishes",
    when: () => !store.overview?.counts?.groups, ready: "Stacks and suggestions are ready", onReady: () => grid?.reload() }), body);
  paintTrash();

  // Rejected photos can be moved to the Trash from here, by the Mac's owner only (the server checks).
  function paintTrash() {
    clear(trashSlot);
    if (params.status === "rejects" && store.meta?.can_trash) {
      trashSlot.append(h("button", { class: "btn danger", title: "Check what stays behind, then move these rejected photos to the Trash",
        onclick: () => trashDialog({ what: "rejects", filter: filterParams(), onDone: () => grid?.reload() }) }, icon("close"), "Move to Trash…"));
    } else if (["removable", "notneeded", "unkept"].includes(params.raw) && store.meta?.can_trash) {
      trashSlot.append(h("button", { class: "btn danger", title: "Check each RAW and the JPEG that stays, then move these RAWs to the Trash",
        onclick: () => rawTrashDialog({ filter: filterParams(), onDone: () => grid?.reload() }) }, icon("close"), "Move RAWs to Trash…"));
    }
  }

  async function smartAlbum() {
    const name = await ask("Save as a smart album", { label: "Name", note: "A smart album keeps following this filter: new photos that match appear in it by themselves." });
    if (!name) return;
    try {
      await api(P(ctx.project, "/albums"), { method: "POST", body: { name, smart: { ...filterParams(), sort: sortNow() } } });
      toast(`Smart album “${name}” saved`, { action: () => ctx.go("albums"), actionLabel: "Albums" });
    } catch (e) { toast(e.message, { kind: "error" }); }
  }

  // ------------------------------------------------------------ lenses
  let offGrid = () => {};
  const openKw = new Set();          // keyword branches opened in the rail
  if (lens === "photos") {
    // One event or trip: its name and actions head the grid. Duplicate extras hidden: say how many.
    const evId = params.event || params.trip;
    if (evId) body.append(eventHeader(ctx.project, evId, { onChange: (what) => { renderRail(); if (what !== "loaded") grid?.reload(); } }));
    const dupNote = h("div", { class: "dup-note", hidden: true });
    if (params.folder && !params.folder.includes("\n")) body.append(folderBar(params.folder, { isRoot: (store.overview?.roots || []).includes(params.folder),
      onDone: (r) => (r.folder ? set("folder", r.folder) : grid.reload()),
      toLibrary: () => moveToLibrary({ folders: [params.folder], onDone: () => { store.overview = null; set("folder", null); } }) }));
    body.append(summaryEl, dupNote);
    paintSummary();
    const paintDupNote = (d) => {
      dupNote.hidden = !d?.n;
      if (!d?.n) return;
      const flip = async () => {
        if (store.meta?.writer === false) { set("all", "1"); return; }
        try { await saveSettings({ hide_duplicates: !d.hidden }); } catch (e) { toast(e.message, { kind: "error" }); return; }
        store.overview = null;
        grid.reload();
      };
      clear(dupNote).append(icon("layers"),
        h("span", {}, d.hidden ? `${plural(d.n, "extra copy", "extra copies")} of photos kept in another folder ${agree(d.n, "is", "are")} hidden.`
          : `Includes ${plural(d.n, "extra copy", "extra copies")} of photos kept in another folder.`),
        h("button", { type: "button", class: "linkish", onclick: flip }, d.hidden ? "Show them" : "Hide them"));
    };
    grid = createGrid({
      load: (offset, limit, { first } = {}) => api(P(ctx.project, "/shots"), {
        params: { ...filterParams(), sort: sortNow(), stack: store.prefs.stack ? 1 : "", offset, limit, total: first && offset ? 1 : "" },
      }).then((r) => { if (!offset) paintDupNote(r.duplicates); return r; }),
      openAt: (i, g) => openLoupe({ ids: g.ids, index: i, onClose: (k) => g.setFocus(k, { keep: true }), onIndex: (k) => { g.focus = k; }, loadMore: () => g.more(), total: () => g.total }),
      headers: () => timeSorted() && store.prefs.headers && !store.prefs.stack,
      timeline: () => api(P(ctx.project, "/timeline"), { params: filterParams() }),
      offset: async (month) => (await api(P(ctx.project, "/offset"), { params: { ...filterParams(), sort: sortNow(), month } })).offset,
      showSuggestions: () => st.show_suggestions !== false,
      emptyText: () => (Object.keys(filterParams()).length ? "No photos match these filters."
        : store.job?.state === "running" ? "JoeyLab is still reading this library. Photos appear here as it goes."
          : "No photos in this library yet. Copy photos into its folders and update, or add another folder under Settings › Folders."),
    });
    // Viewing a hand-picked album: photos can be taken out of it from the selection bar.
    let manualAlbum = null;
    if (params.album) isManualAlbum(params.album).then((yes) => { manualAlbum = yes ? params.album : null; });
    const undated = (ids) => params.undated === "1" ||
      !!ids?.some((id) => { const r = row(id); return r && (!r.capture_str || r.time_src === "file" || r.time_src === "manual"); });
    const bar = selectionBar(grid, { filter: filterParams, stacked: () => !!store.prefs.stack, album: () => manualAlbum,
      dateable: undated, changed: () => grid.reload() });
    rawActions(bar, grid, { filter: filterParams });
    body.append(grid.el, bar);
    offGrid = on((type, g) => {
      if (type === "grid" && g === grid) {
        count.textContent = plural(grid.total, store.prefs.stack ? "stack" : "photo");
        albumBtn.title = !Object.keys(filterParams()).length ? "Save every photo in the library as an album"
          : store.prefs.stack ? "Save every photo in these stacks as an album" : `Save all ${plural(grid.total, "photo")} as an album`;
        const hidden = grid.response?.hidden || 0;
        hiddenChip.hidden = !hidden;
        hiddenChip.textContent = `${n(hidden)} hidden`;
      }
      if (type === "keywords") {
        store.keywordTree = null;              // an undo or redo may have changed the tree's counts
        renderRail();
        if (params.keyword || params.not_keyword || params.untagged) grid.reload?.();
      }
    });
  } else if (lens === "folders") {
    renderFolders();
  } else {
    explore = exploreView({ project: ctx.project, by: params.by || store.prefs.exploreBy, asked: !!params.by, filter: filterParams(),
      onBy: (k) => { setPref("exploreBy", k); params.by = k; ctx.setParams(params); },
      onCount: (k) => { count.textContent = plural(k, "photo"); },
      changed: () => explore.setFilter(filterParams()) });
    body.append(explore.el);
  }

  async function renderFolders() {
    clear(body).append(h("div", { class: "muted" }, "Loading…"));
    let d;
    try { d = await api(P(ctx.project, "/folders")); } catch (e) { clear(body).append(h("div", { class: "result err" }, e.message)); return; }
    const total = d.roots.reduce((a, r) => a + r.n, 0);
    count.textContent = plural(total, "photo");
    // Folders chosen to move to another library together (the owner's): a bar says how many.
    const chosen = new Map();
    const bar = h("div", { class: "folder-pick-bar", hidden: true, role: "region", "aria-label": "Chosen folders" });
    const moved = () => { store.overview = null; renderFolders(); };
    const paintBar = () => {
      bar.hidden = !chosen.size;
      const photos = [...chosen.values()].reduce((a, nd) => a + nd.n, 0);
      clear(bar).append(h("b", {}, plural(chosen.size, "folder")), h("span", { class: "muted" }, plural(photos, "photo")),
        h("span", { class: "spacer" }),
        h("button", { type: "button", class: "btn small primary", onclick: () => moveToLibrary({ folders: [...chosen.keys()], onDone: moved }) },
          icon("folder"), "Move to another library…"),
        h("button", { type: "button", class: "btn small ghost", onclick: () => {
          chosen.clear();
          body.querySelectorAll(".tree-check").forEach((c) => { c.checked = false; });
          paintBar();
        } }, "Clear"));
    };
    const pick = (nd) => canMove() ? h("label", { class: "tree-pick", title: "Choose this folder (to move several to another library)" },
      h("input", { type: "checkbox", class: "tree-check", checked: chosen.has(nd.path), "aria-label": `Choose ${nd.label || nd.name}`,
        onchange: (e) => { if (e.target.checked) chosen.set(nd.path, nd); else chosen.delete(nd.path); paintBar(); } })) : null;
    const node = (nd, depth) => {
      const open = depth < 1 || (params.folder && params.folder.startsWith(nd.path));
      const kids = h("div", { class: "tree-kids", hidden: !open }, open ? nd.children.map((c) => node(c, depth + 1)) : null);
      let loaded = open;
      const head = h("div", { class: `tree-row ${params.folder === nd.path ? "on" : ""}` },
        pick(nd),
        nd.children.length ? h("button", { type: "button", class: "twisty", "aria-label": `Show folders in ${nd.name || nd.path}`,
          "aria-expanded": open ? "true" : "false", onclick: (e) => {
            kids.hidden = !kids.hidden;
            if (!loaded) { kids.append(nd.children.map((c) => node(c, depth + 1))); loaded = true; }
            e.currentTarget.setAttribute("aria-expanded", kids.hidden ? "false" : "true");
            e.currentTarget.replaceChildren(icon(kids.hidden ? "right" : "down"));
          } }, icon(open ? "down" : "right")) : h("span", { class: "twisty" }),
        h("a", { href: `#/p/${encodeURIComponent(ctx.project)}/library?folder=${encodeURIComponent(nd.path)}`, class: "grow" },
          icon("folder"), h("b", {}, nd.label || nd.name), nd.label ? h("span", { class: "faint" }, ` ${nd.name}`) : null),
        nd.from ? h("span", { class: "faint hide-sm" }, `${nd.from.slice(0, 7)}${nd.to && nd.to.slice(0, 7) !== nd.from.slice(0, 7) ? " – " + nd.to.slice(0, 7) : ""}`) : null,
        nd.dups ? h("span", { class: "pill neutral", title: "Photos here that are duplicates of others" }, `${Math.round((100 * nd.dups) / Math.max(1, nd.own))}% dup`) : null,
        h("span", { class: "num muted" }, n(nd.n)),
        canMove() ? folderMenu(nd.path, { onDone: renderFolders, nameInApp: () => renameFolder(nd), isRoot: depth === 0,
          toLibrary: () => moveToLibrary({ folders: [nd.path], onDone: moved }) })
          : h("button", { class: "btn ghost small", title: "Give this folder a better name (in JoeyLab only)", onclick: () => renameFolder(nd) }, "Rename…"));
      return h("div", { class: "tree-node" }, head, kids);
    };
    clear(body).append(h("p", { class: "note" }, canMove() ? "Your folders as they are on disk. Click a folder to see its photos. With ⋯ you can rename or move a folder on disk, or make one inside it: JoeyLab follows, and History › Moves and renames can move it back. “Name in JoeyLab” gives a folder a name only JoeyLab shows. To treat some folders differently (wedding shoots among everyday photos), tick them and move them to another library."
      : "Your folders as they are on disk. Names you give here are only used in JoeyLab; the folders themselves are never renamed. Click a folder to see its photos."),
      bar, h("div", { class: "tree" }, d.roots.map((r) => node(r, 0))));
  }

  async function renameFolder(nd) {
    const name = await ask(`Name “${nd.name}”`, { value: nd.label || "", label: "Name in JoeyLab",
      note: "Only JoeyLab uses this name; the folder on disk keeps its own. Leave empty to remove it." });
    if (name === null) return;
    await api(P(ctx.project, "/folders/name"), { method: "POST", body: { path: nd.path, name } });
    renderFolders();
  }

  // ------------------------------------------------------------ rail
  /** The rail's people: the most photographed (and anyone chosen) as chips, with a box to find the rest. */
  function peopleFilter(people, selectedPeople) {
    const chosen = new Set(selectedPeople.concat(params.person ? [params.person] : []));
    const chip = (x) => {
      const on = chosen.has(String(x.id));
      return h("button", { type: "button", class: "chip", "aria-pressed": on ? "true" : "false",
        title: "Photos with everyone selected", onclick: () => {
          on ? chosen.delete(String(x.id)) : chosen.add(String(x.id));
          delete params.person;
          set("people", [...chosen].join(","));
        } }, x.id === store.peopleList?.me ? `${x.name} (you)` : x.name, h("span", { class: "n" }, n(x.photos)));
    };
    const box = roving(h("div", { class: "chips people-chips", role: "group", "aria-label": "People" }), { choose: false });
    const paint = (q) => {
      const list = q ? people.filter((x) => [x.name, ...(x.aliases || [])].some((nm) => nm.toLowerCase().includes(q))).slice(0, 40)
        : [...people].sort((a, b) => chosen.has(String(b.id)) - chosen.has(String(a.id)) || b.photos - a.photos).slice(0, 24)
          .sort((a, b) => a.name.localeCompare(b.name));
      clear(box).append(list.length ? list.map(chip) : h("span", { class: "muted" }, "Nobody by that name"));
      box.querySelectorAll("button").forEach((b, i) => { b.tabIndex = i ? -1 : 0; });
    };
    paint("");
    if (people.length <= 12) return box;
    const find = h("input", { class: "input small people-find", type: "search", placeholder: `Find among ${n(people.length)} people`,
      "aria-label": "Find a person", autocomplete: "off",
      oninput: (e) => paint(e.target.value.trim().toLowerCase()),
      onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); box.querySelector("button.chip")?.click(); } } });
    return [find, box];
  }

  /** Keywords: a tree to open, a box to find any of them by name, several at once (all of them must
   * be on a photo), and ones to leave out (−). */
  let kwQuery = "";
  let kwAll = false;
  function keywordFilter(kwTree) {
    const on = new Set(list(params.keyword)), off = new Set(list(params.not_keyword));
    const toggle = (key, id) => {
      const inc = new Set(list(params.keyword)), exc = new Set(list(params.not_keyword));
      const [mine, other] = key === "keyword" ? [inc, exc] : [exc, inc];
      mine.has(id) ? mine.delete(id) : mine.add(id);
      other.delete(id);
      params.keyword = [...inc].join(",");
      params.not_keyword = [...exc].join(",");
      if (!params.keyword) delete params.keyword;
      set("not_keyword", params.not_keyword || "");
    };
    const node = (k, flat) => {
      const id = String(k.id);
      const state = on.has(id) ? "true" : off.has(id) ? "mixed" : "false";
      return h("div", { class: `kw-node ${off.has(id) ? "excluded" : ""}`, style: { paddingLeft: `${flat ? 8 : 8 + k.depth * 14}px` } },
        !flat && k.children ? h("button", { type: "button", class: "twisty", "aria-label": `${openKw.has(k.id) ? "Close" : "Open"} ${k.name}`,
          "aria-expanded": openKw.has(k.id) ? "true" : "false",
          onclick: () => { openKw.has(k.id) ? openKw.delete(k.id) : openKw.add(k.id); paintTree(); } }, icon(openKw.has(k.id) ? "down" : "right"))
          : h("span", { class: "twisty" }),
        h("button", { type: "button", class: "kw-name grow", "aria-pressed": state, title: `${k.path}: photos with it (and anything inside it)`,
          onclick: () => toggle("keyword", id) }, flat ? k.path : k.name),
        h("span", { class: "n" }, n(k.total)),
        h("button", { type: "button", class: "kw-not", "aria-pressed": off.has(id) ? "true" : "false", title: `Leave out photos with ${k.path}`,
          "aria-label": `Leave out ${k.path}`, onclick: () => toggle("not_keyword", id) }, "−"));
    };
    const box = h("div", { class: "kw-tree" });
    const note = h("div", { class: "muted small" });
    function paintTree() {
      const q = kwQuery.trim().toLowerCase();
      let rows;
      if (q) rows = kwTree.filter((k) => k.path.toLowerCase().includes(q));
      else rows = kwTree.filter((k) => k.depth === 0 || openKw.has(k.parent_id) || on.has(String(k.id)) || off.has(String(k.id)));
      const cap = kwAll ? rows.length : RAIL_KEYWORDS;
      clear(box).append(rows.length ? rows.slice(0, cap).map((k) => node(k, !!q)) : h("div", { class: "muted small" }, "No keyword by that name."));
      clear(note).append(rows.length > cap ? [`${n(rows.length - cap)} more. `,
        h("button", { type: "button", class: "linkish", onclick: () => { kwAll = true; paintTree(); } }, "Show them all")] : null);
    }
    paintTree();
    const find = kwTree.length > 12 ? h("input", { class: "input small kw-find", type: "search", value: kwQuery, autocomplete: "off",
      placeholder: `Find among ${n(kwTree.length)} keywords`, "aria-label": "Find a keyword",
      oninput: (e) => { kwQuery = e.target.value; paintTree(); },
      onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); box.querySelector(".kw-name")?.click(); } } }) : null;
    return h("div", { class: "group kw-group" }, h("span", { class: "label" }, "Keywords"), find, box, note,
      h("div", { class: "row" },
        h("button", { type: "button", class: "chip", "aria-pressed": params.untagged ? "true" : "false", onclick: () => set("untagged", params.untagged ? "" : "1") }, "No keywords"),
        h("button", { type: "button", class: "btn ghost small", onclick: () => manageKeywords(() => renderRail()) }, "Manage…")));
  }

  /** Chips that are one thing (cameras, what was noticed): one Tab stop, the arrows move along them. */
  const chipGroup = (label, ...chips) => roving(h("div", { class: "chips", role: "group", "aria-label": label }, ...chips), { choose: false });

  /** Cameras as chips: one or several (photos from any of them). */
  function cameraFilter(cameras) {
    const chosen = new Set(list(params.camera));
    return h("div", { class: "stack-v cam-filter" }, h("span", { class: "label" }, "Cameras"),
      chipGroup("Cameras", cameras.filter((c) => c.serial || c.camera !== "Unknown camera").map((c) => {
        const v = c.serial || c.camera;
        return h("button", { type: "button", class: "chip", "aria-pressed": chosen.has(v) ? "true" : "false", title: c.serial ? `${c.camera}, serial ${c.serial}` : c.camera,
          onclick: () => { chosen.has(v) ? chosen.delete(v) : chosen.add(v); set("camera", [...chosen].join(",")); } },
        `${c.camera}${c.serial ? " …" + String(c.serial).slice(-4) : ""}`, h("span", { class: "n" }, n(c.n)));
      })));
  }

  async function renderRail() {
    let o = store.overview;
    if (!o || o.name !== ctx.project) {
      try { o = store.overview = await api(P(ctx.project, "/overview")); } catch { o = null; }
    }
    const seg = (key, opts, label) => choiceSeg(opts, params[key] || "", (v) => set(key, v), { label });
    const active = FILTER_KEYS.some((k) => params[k]);
    let peopleList = store.peopleList;
    if (!peopleList || peopleList.project !== ctx.project) {
      // Pets are filtered on like people; hidden people only name a filter someone already chose.
      try {
        const d = await api(P(ctx.project, "/people"), { params: { limit: 0 } });
        peopleList = store.peopleList = { project: ctx.project, people: d.people.concat(d.pets || []), hidden: d.hidden || [], me: d.me ?? null };
      } catch { peopleList = { people: [], hidden: [] }; }
    }
    const selectedPeople = (params.people || "").split(",").filter(Boolean);
    if (params.album) await isManualAlbum(params.album);        // its name, for the chip
    let kwTree = [];
    try { kwTree = await keywordTree(); } catch { kwTree = []; }
    for (const id of list(params.keyword).concat(list(params.not_keyword))) {
      for (const k of kwTree) if (keywordPath(id)?.startsWith(k.path + " › ")) openKw.add(k.id);
    }
    const chosen = (k) => params[k];
    // A place chosen elsewhere (a link, a reload): its name, from the summary of its photos.
    if (params.place && !placeName(params.place)) {
      try { notePlaces((await api(P(ctx.project, "/summary"), { params: { place: params.place } })).places); } catch { /* "A place" */ }
    }
    // Keywords and cameras can be several: one chip each, so each can go on its own.
    const inUse = FILTER_KEYS.filter((k) => params[k] && k !== "all")
      .flatMap((k) => (["keyword", "not_keyword", "camera"].includes(k) ? list(params[k]) : [params[k]]).map((v) => [k, v]));
    const chip = ([k, v]) => h("button", { type: "button", class: "chip", "aria-pressed": "true", title: "Remove this filter",
      onclick: () => set(k, list(params[k]).length > 1 ? list(params[k]).filter((x) => x !== v).join(",") : "") },
    describe(k, v, o, peopleList), icon("close"));
    const clearAll = () => h("button", { class: "btn ghost small reset", onclick: () => { FILTER_KEYS.forEach((k) => delete params[k]); set("status", ""); } }, "Clear all");
    filterLbl.textContent = inUse.length ? `Filters · ${n(inUse.length)}` : "Filters";
    filterBtn.setAttribute("aria-label", inUse.length ? `Filters, ${plural(inUse.length, "in use", "in use")}` : "Filters");
    chipRow.hidden = !inUse.length;
    clear(chipRow).append(inUse.length ? [...inUse.map(chip), clearAll()] : null);
    clear(rail).append(
      // The filters come first on the page: one key past them, to the photos.
      h("a", { class: "skip-link", href: "#", onclick: (e) => { e.preventDefault(); focusOn(grid?.el.querySelector(".grid") || body); } }, "Skip the filters"),
      h("div", { class: "rail-head" }, h("b", {}, "Filters"),
        h("button", { class: "btn small primary", type: "button", onclick: () => { openRail(false); filterBtn.focus(); } }, "Done")),
      active ? h("div", { class: "group active-filters" }, h("span", { class: "label" }, "Showing"),
        h("div", { class: "chips" }, inUse.map(chip)), clearAll()) : null,
      h("div", { class: "group" }, h("span", { class: "label" }, "Show"),
        seg("status", [["", "All"], ["undecided", "Undecided"], ["picks", "Picks"], ["rejects", "Rejects"], ["rated", "Rated"]], "Show")),
      h("div", { class: "group" }, h("span", { class: "label" }, "At least"),
        h("div", { class: "row" }, starsEl(+params.min_rating || 0, (s) => set("min_rating", s || ""), { label: "At least" }),
          params.min_rating ? h("button", { class: "btn ghost small", onclick: () => set("min_rating", "") }, "Any") : null)),
      h("div", { class: "group" }, h("span", { class: "label" }, "Label"),
        h("div", { class: "row label-pick" }, choiceSeg(Object.entries(LABEL_COLORS).map(([name, c]) => [name, null, {
          class: `label-dot ${params.label === name ? "on" : ""}`, style: { background: c }, title: name, "aria-label": `${name} label` }]),
        params.label || "", (name) => set("label", params.label === name ? "" : name), { label: "Label", cls: "label-pick" }),
        h("button", { class: `chip ${params.favs ? "" : ""}`, "aria-pressed": params.favs ? "true" : "false", onclick: () => set("favs", params.favs ? "" : "me") },
          icon("heart"), "Favourites"))),
      peopleList.people.length || params.cluster ? h("div", { class: "group" }, h("span", { class: "label" }, "People"),
        params.cluster ? h("div", { class: "row" }, h("span", { class: "pill accent" }, "One unnamed face group"),
          h("button", { class: "btn ghost small", onclick: () => set("cluster", "") }, "Clear")) :
          peopleFilter(peopleList.people, selectedPeople)) : null,
      kwTree.length || params.keyword || params.not_keyword ? keywordFilter(kwTree) : null,
      o && o.settings?.show_suggestions !== false ? h("div", { class: "group" }, h("span", { class: "label" }, "Suggestion"),
        seg("suggested", [["", "Any"], ["pick", "Suggested pick"], ["reject", "Suggested reject"], ["check", "Worth checking"]], "Suggestion")) : null,
      o && Object.keys(o.flags).length ? h("div", { class: "group" }, h("span", { class: "label" }, "Noticed"),
        chipGroup("Noticed", Object.entries(o.flags).filter(([f]) => f !== "best-in-group").map(([f, k]) => h("button", {
          type: "button", class: "chip", "aria-pressed": params.flag === f ? "true" : "false",
          onclick: () => set("flag", params.flag === f ? "" : f),
        }, flagLabel(f), h("span", { class: "n" }, n(k)))))) : null,
      h("div", { class: "group" }, h("span", { class: "label" }, "Kind"),
        seg("media", [["", "Everything"], ["photo", "Photos"], ["video", "Videos"]], "Kind")),
      // An identical copy on another drive (this library's or another's) is a backup; copies on the same drive aren't.
      o?.backups?.known || params.backup ? h("div", { class: "group" }, h("span", { class: "label" }, "Backups"),
        choiceSeg([["", "Any"],
          ["elsewhere", [h("span", {}, "Has a backup on another drive"), o?.backups ? h("span", { class: "n" }, n(o.backups.elsewhere)) : null],
            { title: "Every file of the photo has an identical copy on another drive" }],
          ["only", [h("span", {}, "Only on this drive"), o?.backups ? h("span", { class: "n" }, n(o.backups.only)) : null],
            { title: "A file of the photo has no identical copy on any other drive JoeyLab knows" }]],
        params.backup || "", (v) => set("backup", v), { label: "Backups", cls: "seg bk-seg" })) : null,
      // (Dates are in here: two date boxes are eight Tab stops, too many to pass on the way to the photos.)
      h("details", { class: "group more-filters", open: ["from", "to", "camera", "lens", "min_iso", "min_q", "edited", "undated", "gps", "folder", "filetype", "no_album", "raw"].some(chosen) },
        h("summary", { class: "label" }, "More filters"),
        h("span", { class: "label" }, "Taken between"),
        h("input", { type: "date", class: "input", id: "f-from", value: params.from || "", "aria-label": "From date",
          min: o?.span?.from?.slice(0, 10), max: o?.span?.to?.slice(0, 10), onchange: (e) => setDate("from", e.target.value) }),
        h("input", { type: "date", class: "input", id: "f-to", value: params.to || "", "aria-label": "To date",
          min: o?.span?.from?.slice(0, 10), max: o?.span?.to?.slice(0, 10), onchange: (e) => setDate("to", e.target.value) }),
        h("label", { class: "label", for: "f-q" }, `Quality ${params.min_q ? "≥ " + params.min_q : ""}`),
        h("input", { type: "range", id: "f-q", min: 0, max: 95, step: 5, value: params.min_q || 0, onchange: (e) => set("min_q", +e.target.value || "") }),
        o && o.cameras.length > 1 ? cameraFilter(o.cameras) : null,
        h("label", { class: "label", for: "f-lens" }, "Lens"),
        h("input", { class: "input", id: "f-lens", placeholder: "Lens contains…", value: params.lens || "", onchange: (e) => set("lens", e.target.value.trim()) }),
        h("label", { class: "label", for: "f-iso" }, "ISO"),
        h("div", { class: "row", style: { flexWrap: "nowrap" } },
          h("input", { class: "input", id: "f-iso", type: "number", placeholder: "from", "aria-label": "ISO from", value: params.min_iso || "", onchange: (e) => set("min_iso", e.target.value) }),
          h("input", { class: "input", type: "number", placeholder: "to", "aria-label": "ISO to", value: params.max_iso || "", onchange: (e) => set("max_iso", e.target.value) })),
        h("span", { class: "label" }, "Files"),
        seg("filetype", [["", "Any"], ["anyraw", "Has RAW"], ["raw", "RAW only"], ["jpeg", "JPEG only"], ["pair", "RAW + JPEG"]], "Files"),
        o?.counts?.pairs || params.raw ? [h("span", { class: "label" }, "RAW"), seg("raw", RAW_SEG, "RAW")] : null,
        chipGroup("Other filters",
          [["edited", "Edited"], ["undated", "Undated"], ["gps", "Has GPS"], ["c2pa", "Content Credentials"], ["no_album", "In no album"]].map(([k, t]) =>
            h("button", { type: "button", class: "chip", "aria-pressed": params[k] ? "true" : "false", onclick: () => set(k, params[k] ? "" : "1") }, t)),
          h("button", { type: "button", class: "chip", "aria-pressed": params.all ? "true" : "false",
            title: "Also show thumbnails, fragments and screenshots hidden while browsing", onclick: () => set("all", params.all ? "" : "1") }, "Hidden too")),
        o && o.folders.length > 1 ? h("a", { class: "linkish", href: `#/p/${encodeURIComponent(ctx.project)}/library?view=folders` }, "Choose a folder…") : null),
    );
  }
  renderRail();

  return {
    onKey(e) {
      if (e.key === "Escape" && railOpen()) { openRail(false); filterBtn.focus(); return true; }
      if (explore) {
        if (explore.onKey(e) || explore.grid.bulkKey?.(e)) return true;
        return decisionKey(e, explore.grid.targetIds());
      }
      if (!grid) return false;
      if (grid.onKey(e)) return true;
      if (grid.bulkKey?.(e)) return true;
      if (/^[0-9pPxXafctb]$/.test(e.key) && !e.metaKey && !e.ctrlKey && !grid.ensureTarget()) {
        toast("Selected the first photo. Press the key again, or click a photo to choose another.");
        return true;
      }
      return decisionKey(e, grid.targetIds());
    },
    refresh() { store.overview = null; renderRail(); if (lens === "photos") grid.reload(); else if (lens === "folders") renderFolders(); else explore.refresh(); },
    scrollState: () => grid?.scrollState(),
    restore: (s) => (grid ? grid.restore(s) : window.scrollTo(0, s.y || 0)),
    unmount() { grid?.destroy(); explore?.destroy(); offGrid(); narrow.removeEventListener("change", railReach); },
  };
}

/** A filter in words, for its chip: "Has GPS", "ISO from 100", "Portugal". */
function describe(k, v, o, peopleList) {
  const person = (id) => peopleList.people.concat(peopleList.hidden || []).find((x) => String(x.id) === String(id))?.name || "Someone";
  const yes = ["1", "true", "yes", "on"].includes(String(v).toLowerCase());
  const num = (x) => String(+x || x);       // ISO 3200, not 3,200
  switch (k) {
    case "status": return { undecided: "Undecided", picks: "Picks", rejects: "Rejects", rated: "Rated" }[v] || v;
    case "min_rating": return `${"★".repeat(+v)}+`;
    case "suggested": return sugLabel(v);
    case "keyword": return keywordPath(v) ? `#${keywordPath(v)}` : "A keyword";
    case "not_keyword": return keywordPath(v) ? `Not #${keywordPath(v)}` : "Not a keyword";
    case "untagged": return "No keywords";
    case "no_album": return "In no album";
    case "filetype": return { raw: "RAW only", jpeg: "JPEG only", pair: "RAW + JPEG", anyraw: "Has RAW" }[v] || v;
    case "raw": return rawFilterLabel(v);
    case "month": return `${monthLabel(`2000-${String(v).padStart(2, "0")}`).replace(/\s*2000$/, "")}, any year`;
    case "step": return "Changed in one step";
    case "ids": return plural(v.split(",").filter(Boolean).length, "chosen photo");
    case "camera": {
      const c = (o?.cameras || []).find((x) => String(x.serial) === v);
      return c ? `${c.camera} …${String(c.serial).slice(-4)}` : v;
    }
    case "lens": return `Lens: ${v}`;
    case "min_iso": return `ISO from ${num(v)}`;
    case "max_iso": return `ISO up to ${num(v)}`;
    case "min_focal": return `From ${num(v)} mm`;
    case "max_focal": return `Up to ${num(v)} mm`;
    case "max_f": return `f/${v} or wider`;
    case "min_q": return `Quality ≥ ${v}`;
    case "max_q": return `Quality ≤ ${v}`;
    case "flag": return v.split(",").map(flagLabel).join(" or ");
    case "no_flag": return `Not ${v.split(",").map((f) => flagLabel(f).toLowerCase()).join(" or ")}`;
    case "person": return person(v);
    case "people": return v.split(",").map(person).join(" + ");
    case "cluster": return "One unnamed face group";
    case "folder": return v.split("\n").map((x) => x.split("/").pop()).join(" or ");
    case "folder_like": return `Folder name has “${v}”`;
    case "q": return `Search: “${v}”`;
    case "from": return `From ${v}`;
    case "to": return `Until ${v}`;
    case "favs": return v === "me" || yes ? "Your favourites" : `${v}'s favourites`;
    case "album": return albumName(v) ? `Album “${albumName(v)}”` : "An album";
    case "event": return eventName(v) || "An event";
    case "trip": return eventName(v) || "A trip";
    case "place": return placeName(v) || "A place";
    case "country": return countryName(v);
    case "media": return v === "video" ? "Videos" : "Photos";
    case "label": return `${v} label`;
    case "gps": return yes ? "Has GPS" : "No GPS";
    case "edited": return yes ? "Edited" : "Not edited";
    case "undated": return yes ? "Undated" : "Dated";
    case "c2pa": return yes ? "Content Credentials" : "No Content Credentials";
    case "backup": return { elsewhere: "Has a backup on another drive", checked: "Backup checked byte for byte", only: "Only on this drive" }[v] || v;
    default: return `${k}: ${v}`;
  }
}

/** "PT" -> "Portugal" (in the browser's language); a name stays as it is. */
function countryName(v) {
  if (!/^[A-Za-z]{2}$/.test(v)) return v;
  try { return new Intl.DisplayNames(undefined, { type: "region" }).of(v.toUpperCase()) || v; } catch { return v; }
}
