// Moving and renaming photos and folders on disk, for the Mac's owner. Each operation is shown
// first ("Move 24 photos (51 files, 1.2 GB) to … · same drive, instant"), never overwrites anything,
// and can be moved back: from the message that follows it, or History › Moves and renames.

import { api, P } from "../api.js";
import { forgetDetails } from "../loupe.js";
import { emit, store } from "../store.js";
import { ask, bytes, choiceSeg, clear, closePopover, debounce, dialog, h, icon, n, plural, popover, toast } from "../ui.js";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export const canMove = () => !!store.meta?.owner;

async function follow(job, onProgress) {
  while (job.state === "moving" || job.state === "waiting") {
    onProgress?.(job);
    await sleep(job.state === "waiting" ? 1000 : 300);
    job = await api(P(store.project, `/moves/${job.token}`));
  }
  return job;
}

/** After files moved: every page, count and the viewer show the new places. */
function changed(result) {
  store.overview = null;
  store.overviewStale = true;
  forgetDetails();
  emit("history");
  emit("moved", result);
}

/** A folder by its last two names, the whole path on hover. */
function shortPath(path) {
  const parts = path.split("/").filter(Boolean);
  return parts.length > 2 ? `…/${parts.slice(-2).join("/")}` : path;
}

// ------------------------------------------------------------------ choosing a folder

/** The library's folders as a tree, with type-ahead and "New folder here". Resolves to a path or null.
 * `disable(path)` gives a reason a folder can't be chosen (or nothing). */
export function pickFolder({ title = "Choose a folder", intro = "", okLabel = "Choose", start = "", disable = () => null } = {}) {
  return new Promise((resolve) => {
    let folders = [], picked = null, hl = null, done = false, cursor = -1;
    const open = new Set();
    const find = h("input", { class: "input", type: "search", placeholder: "Type to find a folder", autocomplete: "off",
      "aria-label": "Find a folder", role: "combobox", "aria-controls": "move-tree", "aria-expanded": "true", "aria-autocomplete": "list",
      oninput: () => paint(), onkeydown: keys });
    // One Tab stop: ↑ ↓ move, → opens, ← closes (from the box too), Enter chooses.
    const list = h("div", { class: "move-tree", id: "move-tree", role: "tree", "aria-label": "Folders", tabindex: "0", onkeydown: keys });
    const here = h("div", { class: "move-chosen muted" }, "Choose a folder.");
    const out = h("div", { class: "result err", hidden: true });
    const newBtn = h("button", { type: "button", class: "btn ghost small", disabled: true, onclick: newHere }, icon("plus"), "New folder here…");
    const okBtn = h("button", { type: "button", class: "btn primary", disabled: true, onclick: () => finish(picked) }, okLabel);
    const dlg = dialog(title, h("div", { class: "stack-v move-pick" }, intro ? h("p", { class: "note" }, intro) : null, find, list,
      h("div", { class: "row move-pick-foot" }, here, h("span", { class: "spacer" }), newBtn), out), []);
    dlg.querySelector(".dlg-actions").append(h("button", { type: "button", class: "btn", onclick: () => finish(null) }, "Cancel"), okBtn);
    dlg.classList.add("dlg-wide");
    dlg.addEventListener("close", () => finish(null));

    function finish(v) {
      if (done) return;
      done = true;
      resolve(v);
      if (dlg.open) dlg.close();
    }

    const byPath = () => new Map(folders.map((f) => [f.path, f]));
    const parentOf = (p) => p.slice(0, p.lastIndexOf("/")) || "/";
    const kids = (p) => folders.filter((f) => f.path !== f.root && parentOf(f.path) === p)
      .sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" }));
    const rel = (f) => (f.path === f.root ? f.path : f.path.slice(f.root.length + 1).split("/").join(" / "));

    async function load() {
      clear(list).append(h("div", { class: "muted", style: { padding: "8px" } }, "Loading the library's folders…"));
      try { folders = (await api(P(store.project, "/moves/folders"))).folders; }
      catch (e) { clear(list).append(h("div", { class: "result err" }, e.message)); return; }
      for (const f of folders) if (f.path === f.root) open.add(f.path);
      if (start) {
        let p = start;
        while (p && p !== "/") { open.add(p); p = parentOf(p); }
        if (folders.some((f) => f.path === start)) choose(start, { quiet: true });
      }
      paint();
      setTimeout(() => find.focus(), 0);
    }

    /** Folders on disk inside one, empty ones too, as it opens. */
    async function expand(f) {
      open.add(f.path);
      if (!f.listed && f.available !== false) {
        f.listed = true;
        try {
          const d = await api(P(store.project, "/moves/folders"), { params: { path: f.path } });
          const have = byPath();
          for (const x of d.folders) if (!have.has(x.path)) folders.push({ ...x, label: null, own: 0, root: f.root });
        } catch { /* shown without them */ }
      }
      paint();
    }

    function choose(path, { quiet = false } = {}) {
      hl = path;
      const f = byPath().get(path);
      const why = f && (f.read_only ? "That drive is read-only." : disable(path));
      if (!f || why) {
        picked = null;
        okBtn.disabled = true;
        newBtn.disabled = !f || !!f?.read_only;
        here.textContent = why || "Choose a folder.";
      } else {
        picked = path;
        okBtn.disabled = false;
        newBtn.disabled = false;
        clear(here).append(icon("folder"), h("span", { class: "mono", title: path }, path));
      }
      if (f) newBtn.dataset.path = path;
      if (!quiet) paint();
    }

    function row(f, depth, flat) {
      const why = f.read_only ? "read-only" : disable(f.path);
      const has = flat ? false : (kids(f.path).length || !f.listed);
      const isOpen = open.has(f.path);
      const el = h("div", { class: `move-row ${picked === f.path ? "on" : ""} ${hl === f.path ? "hl" : ""} ${why ? "off" : ""}`, role: "treeitem", id: `move-row-${folders.indexOf(f)}`,
        "aria-expanded": has ? (isOpen ? "true" : "false") : null, "aria-disabled": why ? "true" : null,
        "aria-selected": picked === f.path ? "true" : "false", "aria-level": String(depth + 1), dataset: { path: f.path },
        style: { paddingLeft: `${6 + depth * 16}px` }, title: why ? `${f.path} · ${why}` : f.path, onclick: () => choose(f.path),
        ondblclick: () => { if (!why) finish(f.path); } },
      // (For the mouse: the row says whether it's open, and → ← open and close it.)
      flat ? null : h("button", { type: "button", class: "twisty", tabindex: "-1", "aria-hidden": "true",
        style: { visibility: has ? "visible" : "hidden" },
        onclick: (e) => { e.stopPropagation(); if (isOpen) { open.delete(f.path); paint(); } else expand(f); } }, icon(isOpen ? "down" : "right")),
      icon("folder"),
      h("span", { class: "grow move-name" }, f.label || (flat ? rel(f) : f.name),
        f.label ? h("span", { class: "faint" }, ` ${flat ? rel(f) : f.name}`) : null,
        f.path === f.root && f.drive ? h("span", { class: "faint" }, ` · ${f.drive}`) : null),
      f.read_only ? h("span", { class: "pill neutral" }, "read-only") : null,
      f.n ? h("span", { class: "num muted" }, n(f.n)) : null);
      return el;
    }

    function paint() {
      const words = find.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
      clear(list);
      if (words.length) {
        const hits = folders.filter((f) => { const t = `${f.name} ${f.label || ""} ${rel(f)}`.toLowerCase(); return words.every((w) => t.includes(w)); })
          .slice(0, 200);
        list.append(hits.length ? hits.map((f) => row(f, 0, true)) : h("div", { class: "muted", style: { padding: "8px" } }, "No folder by that name."));
      } else {
        const walk = (f, depth) => [row(f, depth, false), open.has(f.path) ? kids(f.path).map((k) => walk(k, depth + 1)) : null];
        list.append(folders.filter((f) => f.path === f.root).map((r) => walk(r, 0)));
      }
      const rows = [...list.querySelectorAll(".move-row")];
      cursor = rows.findIndex((r) => r.dataset.path === (hl ?? picked));
      rows[cursor]?.scrollIntoView({ block: "nearest" });
      for (const el of [find, list]) {
        if (rows[cursor]) el.setAttribute("aria-activedescendant", rows[cursor].id); else el.removeAttribute("aria-activedescendant");
      }
    }

    // ↑ ↓ move, → opens, ← closes, Enter chooses.
    function keys(e) {
      const rows = [...list.querySelectorAll(".move-row")];
      if (!rows.length) return;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        cursor = Math.max(0, Math.min(rows.length - 1, cursor + (e.key === "ArrowDown" ? 1 : -1)));
        choose(rows[cursor].dataset.path);
      } else if ((e.key === "ArrowRight" || e.key === "ArrowLeft") && e.target !== find && hl) {
        e.preventDefault();
        const f = byPath().get(hl);
        if (e.key === "ArrowRight") expand(f); else { open.delete(hl); paint(); }
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (picked) finish(picked);
        else if (rows.length === 1) choose(rows[0].dataset.path);
      }
    }

    async function newHere() {
      const parent = newBtn.dataset.path;
      if (!parent) return;
      const name = await ask("New folder", { label: "Name", note: `A new folder inside ${parent}.`, ok: "Make folder" });
      if (!name) return;
      try {
        let job = await api(P(store.project, "/moves/plan"), { method: "POST", body: { op: "new_folder", parent, name } });
        if (job.plan.refused) throw new Error(job.plan.refused);
        job = await follow(await api(P(store.project, `/moves/${job.token}/run`), { method: "POST", body: {} }));
        if (job.state === "error") throw new Error(job.error);
        const path = job.plan.target;
        folders.push({ path, name: path.split("/").pop(), label: null, n: 0, own: 0, root: byPath().get(parent)?.root || parent, listed: true });
        open.add(parent);
        find.value = "";
        choose(path);
        toast(job.result.label, { action: () => moveBack(job.result.id), actionLabel: "Undo" });
      } catch (e) { out.hidden = false; out.textContent = e.message; }
    }

    load();
  });
}

// ------------------------------------------------------------------ showing a plan, then doing it

/** The plan in words, with what's taken and what stays, then (when asked) the move itself. */
function confirmPlan(job, { verb = "Move", onDone } = {}) {
  const d = job.plan;
  const body = h("div", { class: "stack-v move-plan" });
  const title = { move_photos: "Move these photos?", rename_folder: "Rename this folder?", move_folder: "Move this folder?",
    move_back: "Move back?" }[d.op] || `${verb}?`;
  const dlg = dialog(title, body, [{ label: "Cancel" }]);
  const actions = dlg.querySelector(".dlg-actions");
  if (d.refused) {
    body.append(h("div", { class: "result err" }, d.refused));
    actions.firstChild.textContent = "Close";
    return;
  }
  let keepBoth = true;
  body.append(h("p", { class: "move-what" }, d.what));
  if (d.conflicts) {
    const seg = h("div");
    const paintSeg = () => {
      const had = seg.contains(document.activeElement);
      clear(seg).append(choiceSeg([[true, "Keep both"], [false, "Skip them"]], keepBoth, (v) => { keepBoth = v; paintSeg(); }, { label: "Names that are taken" }));
      if (had) seg.querySelector('[aria-checked="true"]')?.focus();
    };
    paintSeg();
    const ex = d.conflict_examples[0];
    body.append(h("div", { class: "move-taken" },
      h("p", {}, h("b", {}, plural(d.conflicts, "name")), ` ${d.conflicts === 1 ? "is" : "are"} taken there already (for example ${ex.name}). `,
        "Nothing is ever replaced: keep both (yours is called ", h("b", {}, ex.keep_both), ") or skip those."), seg));
  }
  if (d.skipped.length) {
    body.append(h("details", { class: "muted small" }, h("summary", {}, `${plural(d.skipped.reduce((a, s) => a + s.n, 0), "photo")} left as they are`),
      d.skipped.map((s) => h("div", {}, `${n(s.n)}: ${s.why}`))));
  }
  body.append(h("p", { class: "note" },
    d.how.startsWith("same drive") ? "Nothing is copied: the files keep their content and only change place, so it's instant. JoeyLab keeps track of them as they go."
      : d.how.startsWith("the originals") ? "The originals come back from the Trash to where they were, and the copies go to the Trash."
        : "Each file is copied to the other drive and read back to check it. Only then does the original go to the Trash on its drive (it's not deleted): the space comes back when you empty the Trash.",
    " Afterwards you can move it back, from the message that follows or from History."));
  const status = h("div", { class: "muted", hidden: true });
  body.append(status);
  const go = h("button", { type: "button", class: "btn primary", onclick: run }, d.op === "move_back" ? "Move back" : verb);
  actions.append(go);
  setTimeout(() => go.focus(), 0);

  async function run() {
    go.disabled = true;
    status.hidden = false;
    status.textContent = "Starting…";
    let r;
    try {
      r = await api(P(store.project, `/moves/${job.token}/run`), { method: "POST", body: { keep_both: keepBoth } });
      r = await follow(r, (j) => {
        status.textContent = j.state === "waiting" ? "Waiting for the library's update to finish…"
          : j.total ? `${d.how.startsWith("same") ? "Moving" : "Copying and checking"}… ${n(j.done)} of ${n(j.total)}` : "Moving…";
      });
    } catch (e) { status.className = "result err"; status.textContent = e.message; return; }
    if (r.state === "error") { status.className = "result err"; status.textContent = r.error; return; }
    dlg.close();
    done(r.result, onDone);
  }
}

/** Say what happened, with Undo (move back), and let the page catch up. */
function done(res, onDone) {
  changed(res);
  const left = res.skipped?.length ? ` · ${plural(res.skipped.length, "item")} left as ${res.skipped.length === 1 ? "it was" : "they were"}` : "";
  const failed = res.trash_failed?.length ? ` · ${plural(res.trash_failed.length, "original")} couldn't go to the Trash and ${res.trash_failed.length === 1 ? "is" : "are"} still there` : "";
  toast(`${res.label}${left}${failed}`, res.photos ? { action: () => moveBack(res.id, onDone), actionLabel: "Undo", ms: 9000 } : {});
  onDone?.(res);
}

async function planAndConfirm(body, opts) {
  try {
    const job = await api(P(store.project, "/moves/plan"), { method: "POST", body });
    confirmPlan(job, opts);
  } catch (e) { toast(e.message, { kind: "error" }); }
}

/** Move an earlier move or rename back, at once (it's shown in the message that follows). */
export async function moveBack(id, onDone) {
  try {
    let job = await api(P(store.project, `/moves/${id}/back`), { method: "POST", body: { run: true } });
    job = await follow(job);
    if (job.state === "error") throw new Error(job.error);
    const res = job.result;
    changed(res);
    const left = res.skipped?.length ? ` (${plural(res.skipped.length, "item")} couldn't: ${res.skipped[0].why})` : "";
    toast(`Moved back${left}`, res.photos ? { action: () => moveBack(res.id, onDone), actionLabel: "Redo" } : {});
    onDone?.(res);
  } catch (e) { toast(e.message, { kind: "error" }); }
}

// ------------------------------------------------------------------ photos

/** Move photos (ids, or everything matching a filter) to a folder of the library. */
export async function movePhotos({ ids, filter, count, from, onDone } = {}) {
  const k = ids ? ids.length : count;
  const into = await pickFolder({ title: `Move ${plural(k || 0, "photo")} to…`, okLabel: "Next", start: from,
    intro: "Every file of each photo goes (its RAW, JPEG, Live Photo video and sidecars), keeping its name.",
    disable: (p) => (from && p === from ? "They're in this folder already." : null) });
  if (!into) return;
  planAndConfirm({ op: "move_photos", ids, filter, into }, { verb: "Move", onDone });
}

/** The selection bar's "Move…": move the photos to a folder, or rename their files. */
export function fileActions(anchor, opts) {
  const item = (text, hint, fn) => h("button", { type: "button", class: "menu-item", role: "menuitem", title: hint,
    onclick: () => { closePopover(); fn(); } }, text);
  popover(anchor, h("div", { class: "move-menu" },
    item("Move to folder…", "Every file of each photo goes, keeping its name", () => movePhotos(opts)),
    item("Rename files…", "New names from the date, event, place and a counter", () => renamePhotos(opts))));
}

const TOKENS = [["{date}", "Date taken"], ["{event}", "Event"], ["{place}", "Place"], ["{folder}", "Folder"], ["{name}", "Current name"], ["{n}", "Counter"]];

/** Rename photos' files from a pattern ("2026-01-24 Phuket 001"), with a preview. */
export function renamePhotos({ ids, filter, count, onDone } = {}) {
  const k = ids ? ids.length : count;
  const pattern = h("input", { class: "input mono", value: "{date} {event} {n}", autocomplete: "off" });
  const start = h("input", { class: "input", type: "number", min: 0, value: 1, style: { width: "90px" } });
  const preview = h("div", { class: "move-preview", role: "status", "aria-label": "The new names" });
  const note = h("div", { class: "muted small" });
  const keep = h("input", { type: "checkbox", checked: true });
  const keepRow = h("label", { class: "check", hidden: true }, keep, h("span", {}, "Where a name is taken, keep both (adds a number). Nothing is ever replaced."));
  let plan = null;
  const dlg = dialog(`Rename ${plural(k || 0, "photo")}`, h("div", { class: "stack-v" },
    h("p", { class: "note" }, "Each photo's files (RAW, JPEG, Live Photo video, sidecars) get the same new name, in the folder they're in. Empty parts close up, and the counter goes in the order they were taken."),
    h("label", { class: "field" }, h("span", {}, "New names"), pattern),
    h("div", { class: "chips", role: "group", "aria-label": "Add to the pattern" }, TOKENS.map(([t, label]) => h("button", { type: "button", class: "chip", title: t,
      onclick: () => { insert(t); } }, label))),
    h("label", { class: "field-inline" }, h("span", { class: "muted" }, "Counter starts at"), start),
    preview, note, keepRow), [
    { label: "Cancel" },
    { label: "Rename", primary: true, run: async () => run() },
  ]);
  dlg.classList.add("dlg-wide");
  const btn = dlg.querySelector(".dlg-actions .primary");

  function insert(t) {
    const at = pattern.selectionStart ?? pattern.value.length;
    const v = pattern.value;
    pattern.value = `${v.slice(0, at)}${at && v[at - 1] !== " " ? " " : ""}${t}${v.slice(at)}`.replace(/\s+/g, " ");
    pattern.focus();
    refresh();
  }

  const refresh = debounce(async () => {
    try {
      const r = await api(P(store.project, "/moves/plan"), { method: "POST",
        body: { op: "rename_photos", ids, filter, pattern: pattern.value, start: +start.value || 0, preview: true } });
      plan = r.plan;
      paint();
    } catch (e) { clear(preview).append(h("div", { class: "result err" }, e.message)); btn.disabled = true; }
  }, 250);

  function paint() {
    clear(preview);
    btn.disabled = !!plan.refused;
    if (plan.refused) { preview.append(h("div", { class: "result err" }, plan.refused)); note.textContent = ""; keepRow.hidden = true; return; }
    preview.append(h("div", { class: "move-preview-rows" }, plan.preview.slice(0, 8).map((p) => h("div", { class: `move-preview-row ${p.taken ? "taken" : ""}` },
      h("span", { class: "mono muted" }, p.from), icon("right"), h("span", { class: "mono" }, p.to),
      p.taken ? h("span", { class: "pill warn", title: "That name is taken: this one gets a number" }, "taken") : null))));
    note.textContent = [plan.preview.length > 8 ? `…and ${n(plan.preview.length - 8)} more` : "", `${plural(plan.photos, "photo")}, ${plural(plan.files, "file")}`,
      plan.skipped.length ? plan.skipped.map((s) => `${n(s.n)} left as ${s.n === 1 ? "it is" : "they are"}: ${s.why}`).join(" · ") : ""].filter(Boolean).join(" · ");
    keepRow.hidden = !plan.conflicts;
  }

  async function run() {
    try {
      let job = await api(P(store.project, "/moves/plan"), { method: "POST",
        body: { op: "rename_photos", ids, filter, pattern: pattern.value, start: +start.value || 0 } });
      if (job.plan.refused) throw new Error(job.plan.refused);
      job = await follow(await api(P(store.project, `/moves/${job.token}/run`), { method: "POST", body: { keep_both: keep.checked } }));
      if (job.state === "error") throw new Error(job.error);
      done(job.result, onDone);
      return true;
    } catch (e) { clear(preview).append(h("div", { class: "result err" }, e.message)); return false; }
  }

  pattern.addEventListener("input", refresh);
  start.addEventListener("input", refresh);
  refresh();
  setTimeout(() => { pattern.focus(); pattern.select(); }, 0);
}

// ------------------------------------------------------------------ folders

export async function renameFolder(path, { onDone } = {}) {
  const name = path.split("/").pop();
  const typed = await ask(`Rename “${name}”`, { value: name, label: "New name", ok: "Next",
    note: "This renames the folder itself, on disk. Its photos (and anything else in it) stay inside it, and JoeyLab follows: your stars, albums, names and rules stay with them." });
  if (!typed || typed === name) return;
  planAndConfirm({ op: "rename_folder", path, name: typed }, { verb: "Rename", onDone });
}

export async function newFolder(parent, { onDone } = {}) {
  const name = await ask("New folder", { label: "Name", note: `A new, empty folder inside ${parent}.`, ok: "Make folder" });
  if (!name) return;
  try {
    let job = await api(P(store.project, "/moves/plan"), { method: "POST", body: { op: "new_folder", parent, name } });
    if (job.plan.refused) throw new Error(job.plan.refused);
    job = await follow(await api(P(store.project, `/moves/${job.token}/run`), { method: "POST", body: {} }));
    if (job.state === "error") throw new Error(job.error);
    done({ ...job.result, made: job.plan.target }, onDone);
  } catch (e) { toast(e.message, { kind: "error" }); }
}

export async function moveFolder(path, { onDone } = {}) {
  const name = path.split("/").pop();
  const parent = path.slice(0, path.lastIndexOf("/"));
  const into = await pickFolder({ title: `Move “${name}” into…`, okLabel: "Next", start: parent,
    intro: "The folder moves with everything in it, and JoeyLab follows.",
    disable: (p) => (p === path || p.startsWith(path + "/") ? "A folder can't go inside itself."
      : p === parent ? "It's in this folder already." : null) });
  if (!into) return;
  planAndConfirm({ op: "move_folder", path, into }, { verb: "Move", onDone });
}

/** "⋯" for a folder: rename it, make a folder inside it, move it (and, given, name it in JoeyLab,
 * or move it to another library with everything known about its photos). */
export function folderMenu(path, { onDone, nameInApp, toLibrary, isRoot = false, label = "" } = {}) {
  if (!canMove()) return null;
  const btn = h("button", { type: "button", class: `btn ghost small ${label ? "" : "icon-only"}`, "aria-haspopup": "menu",
    title: "Rename, move, or make a folder inside it", "aria-label": `More for ${path.split("/").pop()}`,
    onclick: (e) => {
      e.stopPropagation();
      const item = (text, fn, hint) => h("button", { type: "button", class: "menu-item", role: "menuitem", title: hint || "",
        onclick: () => { closePopover(); fn(); } }, text);
      popover(btn, h("div", { class: "move-menu" },
        nameInApp ? item("Name in JoeyLab…", nameInApp, "A name only JoeyLab shows; the folder itself keeps its name") : null,
        item("Rename folder…", () => renameFolder(path, { onDone }), "Renames the folder on disk; JoeyLab follows"),
        item("New folder inside…", () => newFolder(path, { onDone })),
        isRoot ? null : item("Move to…", () => moveFolder(path, { onDone })),
        toLibrary ? item("Move to another library…", toLibrary,
          "Its photos become another library's, with their decisions, names and history; the folder stays where it is") : null,
        h("a", { class: "menu-item", role: "menuitem", href: `#/p/${encodeURIComponent(store.project)}/history?tab=moves`, onclick: closePopover },
          "Moves and renames…")));
    } }, label ? [icon("folder"), label, h("span", { class: "caret", "aria-hidden": "true" }, "▾")] : h("span", { "aria-hidden": "true" }, "⋯"));
  return btn;
}

/** The Library filtered to one folder: its name and what can be done to it. */
export function folderBar(path, { onDone, isRoot = false, toLibrary } = {}) {
  if (!canMove()) return null;
  return h("div", { class: "move-folder-bar" }, icon("folder"), h("b", {}, path.split("/").pop()),
    h("span", { class: "faint mono grow", title: path }, shortPath(path)),
    folderMenu(path, { onDone, isRoot, toLibrary, label: "Folder" }));
}

// ------------------------------------------------------------------ History › Moves and renames

/** The History page's list of moves and renames, each with Move back. */
export async function movesTab(body) {
  body.append(h("p", { class: "note" }, "Every move and rename made in JoeyLab, newest first. Move back puts the files where they were (and their names back); a move to another drive brings the originals back from the Trash while they're still in it."),
    h("div", { class: "muted" }, "Loading…"));
  let d;
  try { d = await api(P(store.project, "/moves")); } catch (e) { clear(body).append(h("div", { class: "result err" }, e.message)); return; }
  body.lastChild.remove();
  if (!d.moves.length) {
    body.append(h("div", { class: "empty" }, h("h3", {}, "Nothing moved or renamed yet"),
      h("p", {}, "Move photos from the selection bar, or rename and move folders from the Library's Folders view (⋯). Each one is listed here.")));
    return;
  }
  const list = h("div", { class: "history-list", role: "list" });
  const repaint = () => { clear(body); movesTab(body); };
  for (const m of d.moves) {
    const when = new Date(m.ts * 1000).toLocaleString(undefined, { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
    const meta = [when, m.photos ? plural(m.photos, "photo") : "", m.bytes && !m.same_drive ? bytes(m.bytes) : "",
      m.same_drive === false ? "to another drive" : "", { "moved back": "moved back", stopped: "stopped half way", "nothing moved": "nothing moved" }[m.state] || "",
      m.recovered ? "finished after a stop" : "", m.left ? `${n(m.left)} left as they were` : ""].filter(Boolean).join(" · ");
    list.append(h("div", { class: `history-row move-row-h ${m.state === "moved back" ? "undone" : ""}`, role: "listitem" },
      h("span", { class: "move-ico" }, icon(m.op === "new_folder" ? "plus" : m.op?.includes("rename") ? "tag" : "folder")),
      h("div", { class: "grow history-what" }, h("b", { title: m.what || "" }, m.label), h("div", { class: "muted small" }, meta)),
      h("div", { class: "history-acts" },
        m.state === "stopped" ? h("button", { class: "btn small", onclick: async () => {
          try { await api(P(store.project, "/moves/recover"), { method: "POST", body: {} }); toast("Finished from its log"); repaint(); }
          catch (e) { toast(e.message, { kind: "error" }); }
        } }, "Finish it") : null,
        m.can_move_back ? h("button", { class: "btn small", onclick: () => moveBack(m.id, repaint) }, icon("undo"), "Move back") : null)));
  }
  body.append(list);
}
