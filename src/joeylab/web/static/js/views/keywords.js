// Keywords: your own tree ("Activities › Climbing"), kept in the catalog. Tag a selection, the photo
// in the viewer, or manage the tree (rename, move, merge, remove). Tagging is undoable like a rating.

import { api, P } from "../api.js";
import { emit, store, undo } from "../store.js";
import { agree, clear, dialog, h, icon, n, plural, toast } from "../ui.js";

/** The tree for the current library, cached until something changes it. */
export async function keywordTree(force = false) {
  const c = store.keywordTree;
  if (!force && c && c.project === store.project) return c.tree;
  const tree = (await api(P(store.project, "/keywords"))).tree;
  store.keywordTree = { project: store.project, tree };
  return tree;
}

export function keywordPath(id) {
  return store.keywordTree?.tree.find((k) => String(k.id) === String(id))?.path;
}

function changed() {
  store.keywordTree = null;
  emit("keywords");
}

async function assign(ids, { add = [], remove = [] }) {
  const r = await api(P(store.project, "/keywords/assign"), { method: "POST", body: { ids, add, remove } });
  changed();
  if (r.n) toast(`Keywords changed on ${plural(r.n, "photo")}`, { action: () => undo(r.batch).then(changed) });
  return r;
}

function pathInput(tree, placeholder) {
  const listId = `kw-list-${Math.random().toString(36).slice(2, 8)}`;
  return [h("input", { class: "input", list: listId, placeholder, autocomplete: "off", "aria-label": "Keyword" }),
    h("datalist", { id: listId }, tree.map((k) => h("option", { value: k.path })))];
}

/** Add or remove keywords on photos (T in the grid and viewer). */
export async function keywordDialog(ids, onDone) {
  if (!ids.length) { toast("Select some photos first."); return; }
  const tree = await keywordTree();
  const [input, list] = pathInput(tree, "Climbing, or Activities › Climbing");
  const current = h("div", { class: "chips" });
  async function paint() {
    const d = await api(P(store.project, "/keywords/of"), { params: { ids: ids.join(",") } });
    clear(current).append(d.keywords.length ? d.keywords.map((k) => h("span", { class: "chip kw-chip" }, k.path,
      ids.length > 1 ? h("span", { class: "n" }, `${n(k.n)} of ${n(ids.length)}`) : null,
      h("button", { type: "button", class: "btn ghost icon-only small", title: `Remove ${k.path}`, "aria-label": `Remove ${k.path}`,
        onclick: async () => { await assign(ids, { remove: [k.id] }); paint(); onDone?.(); } }, icon("close"))))
      : h("span", { class: "muted" }, "No keywords yet."));
  }
  const top = [...tree].sort((a, b) => b.total - a.total).slice(0, 14);
  const quick = h("div", { class: "chips" }, top.map((k) => h("button", { type: "button", class: "chip", title: k.path,
    onclick: async () => { await assign(ids, { add: [k.id] }); paint(); onDone?.(); } }, k.name)));
  dialog(ids.length === 1 ? "Keywords" : `Keywords for ${n(ids.length)} photos`, h("div", { class: "stack-v kw-dlg" },
    h("p", { class: "note" }, "Type a keyword and press Enter. Use › (or >) for a place in the tree: Activities › Climbing. Kept in JoeyLab's catalog; exported to Lightroom with your XMP sidecars if you choose."),
    h("div", { class: "row", style: { flexWrap: "nowrap" } }, input, list),
    current, top.length ? h("div", { class: "stack-v" }, h("span", { class: "label" }, "Used most"), quick) : null), [
    { label: "Done", primary: false, run: () => onDone?.() },
    { label: "Add", primary: true, submit: true, run: async () => {
      const v = input.value.trim();
      if (!v) return true;
      try { await assign(ids, { add: v.split(/\s*,\s*/).filter(Boolean) }); input.value = ""; paint(); onDone?.(); }
      catch (e) { toast(e.message, { kind: "error" }); }
      return false;            // stay open for the next one
    } },
  ]);
  paint();
  setTimeout(() => input.focus(), 0);
}

/** Add or remove a keyword on every photo a Library filter matches (not just the loaded ones). */
export async function bulkKeywordDialog(filter, count, onDone) {
  const tree = await keywordTree();
  const [input, list] = pathInput(tree, "Climbing, or Activities › Climbing");
  const send = async (body) => {
    const r = await api(P(store.project, "/bulk"), { method: "POST", body: { filter, ...body } });
    changed();
    onDone?.(r);
    toast(`Keywords changed on ${plural(r.n, "photo")}`, r.batch ? { action: () => undo(r.batch).then(changed) } : {});
  };
  dialog(`Keywords for ${plural(count, "photo")}`, h("div", { class: "stack-v kw-dlg" },
    h("p", { class: "note" }, "Every photo matching the filter, including the ones not loaded yet. One step, so one Undo takes it back."),
    h("div", { class: "row", style: { flexWrap: "nowrap" } }, input, list)), [
    { label: "Cancel" },
    { label: "Remove from all", run: async () => {
      const v = input.value.trim().replace(/\s*>\s*/g, " › ");
      const k = tree.find((x) => x.path.toLowerCase() === v.toLowerCase()) || tree.find((x) => x.name.toLowerCase() === v.toLowerCase());
      if (!k) { toast(v ? `No keyword called ${v}` : "Type a keyword first", { kind: "error" }); return false; }
      try { await send({ keywords_remove: [k.id] }); } catch (e) { toast(e.message, { kind: "error" }); return false; }
    } },
    { label: "Add to all", primary: true, submit: true, run: async () => {
      const v = input.value.trim();
      if (!v) return false;
      try { await send({ keywords_add: v.split(/\s*,\s*/).filter(Boolean) }); } catch (e) { toast(e.message, { kind: "error" }); return false; }
    } },
  ]);
  setTimeout(() => input.focus(), 0);
}

/** Keywords of one photo, for the viewer's info panel. */
export function keywordBlock(sid, kws, onChange) {
  const box = h("div", { class: "chips" });
  box.append(...kws.map((k) => h("span", { class: "chip kw-chip", title: k.path }, k.name,
    h("button", { type: "button", class: "btn ghost icon-only small", "aria-label": `Remove ${k.path}`,
      onclick: async () => { await assign([sid], { remove: [k.id] }); onChange?.(); } }, icon("close")))),
  h("button", { type: "button", class: "chip", title: "Add keywords (T)", onclick: () => keywordDialog([sid], onChange) }, icon("plus"), "Keyword"));
  return box;
}

/** Rename, move, merge or remove keywords. Hundreds of them: the list is searchable and only the rows
 * in view are drawn; "inside" and "merge into" are one box that finds the keyword by name. */
const ROW_H = 40;
export async function manageKeywords(onDone) {
  let tree = await keywordTree(true);
  let query = "";
  let shown = [];
  const find = h("input", { class: "input", type: "search", placeholder: "Find a keyword", "aria-label": "Find a keyword", autocomplete: "off" });
  const count = h("span", { class: "muted small" });
  const spacer = h("div", { class: "kw-vspace" });
  const scroller = h("div", { class: "kw-vlist", role: "list", "aria-label": "Keywords" }, spacer);
  const panel = h("div", { class: "kw-panel", hidden: true });
  const reload = async () => { changed(); tree = await keywordTree(true); filter(); };
  const edit = async (id, payload, quiet = false) => {
    const was = tree.find((k) => k.id === id);
    try {
      const r = await api(P(store.project, `/keywords/${id}`), { method: "POST", body: payload });
      await reload();
      if (r?.batch) toast(`Merged: ${plural(r.moved, "photo")} moved over`, { action: () => undo(r.batch).then(reload) });
      // A rename or a move changes no photos, so it isn't in the history; its toast can put it back.
      else if (!quiet && was && "name" in payload) toast(`Renamed ${was.name} to ${payload.name}`, { action: () => edit(id, { name: was.name }, true) });
      else if (!quiet && was && "parent_id" in payload) toast(`Moved ${was.name}`, { action: () => edit(id, { parent_id: was.parent_id ?? null }, true) });
    } catch (e) { toast(e.message, { kind: "error" }); await reload(); }
  };

  function filter() {
    const q = query.trim().toLowerCase();
    shown = q ? tree.filter((k) => k.path.toLowerCase().includes(q)) : tree;
    count.textContent = q ? `${n(shown.length)} of ${plural(tree.length, "keyword")}` : plural(tree.length, "keyword");
    spacer.style.height = `${shown.length * ROW_H}px`;
    paint(true);
  }

  // Only the rows in view (and a few either side) exist.
  const drawn = new Map();
  function paint(all = false) {
    if (all) { for (const el of drawn.values()) el.remove(); drawn.clear(); }
    const top = scroller.scrollTop, hgt = scroller.clientHeight || 400;
    const a = Math.max(0, Math.floor(top / ROW_H) - 8), b = Math.min(shown.length, Math.ceil((top + hgt) / ROW_H) + 8);
    for (const [i, el] of drawn) if (i < a || i >= b) { el.remove(); drawn.delete(i); }
    for (let i = a; i < b; i++) if (!drawn.has(i)) { const el = rowEl(shown[i], i); drawn.set(i, el); spacer.append(el); }
    if (!shown.length) spacer.append(drawn.set(-1, h("p", { class: "muted kw-none" }, tree.length ? "No keyword by that name." : "No keywords yet. Select photos and press T to add some.")).get(-1));
  }
  scroller.addEventListener("scroll", () => paint(), { passive: true });

  function rowEl(k, i) {
    const flat = !!query.trim();
    const name = h("button", { type: "button", class: "kw-rename", title: `Rename ${k.path}`, onclick: () => rename(k, name) },
      flat ? k.path : k.name);
    return h("div", { class: "kw-row", role: "listitem", style: { top: `${i * ROW_H}px`, paddingLeft: `${flat ? 4 : 4 + k.depth * 16}px` } },
      name, h("span", { class: "muted n", title: "Photos with it or anything inside it" }, n(k.total)),
      h("button", { type: "button", class: "btn ghost small", title: `Put ${k.name} inside another keyword`, onclick: () => choose(k, "move") }, "Move"),
      h("button", { type: "button", class: "btn ghost small", title: `Merge ${k.name} into another keyword`, onclick: () => choose(k, "merge") }, "Merge"),
      h("button", { type: "button", class: "btn ghost small", title: `Remove ${k.name}`, onclick: async () => {
        if (!confirmTwice(k)) return;
        try {
          const r = await api(P(store.project, `/keywords/${k.id}`), { method: "DELETE" });
          toast(`Removed ${k.name} from ${plural(r.removed_from, "photo")}`, r.batch ? { action: () => undo(r.batch).then(reload) } : {});
        } catch (e) { toast(e.message, { kind: "error" }); }
        await reload();
      } }, "Remove"));
  }

  function rename(k, el) {
    const input = h("input", { class: "input small kw-rename-in", value: k.name, "aria-label": `New name for ${k.path}` });
    let done = false;
    const finish = (save) => {
      if (done) return;
      done = true;
      const v = input.value.trim();
      input.replaceWith(el);
      if (save && v && v !== k.name) edit(k.id, { name: v });
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); finish(false); }
    });
    input.addEventListener("blur", () => finish(true));
    el.replaceWith(input);
    input.focus();
    input.select();
  }

  /** The box below the list: find the keyword to move this one inside, or to merge it into. */
  function choose(k, what) {
    const others = tree.filter((x) => x.id !== k.id && !x.path.startsWith(k.path + " › "));
    const input = h("input", { class: "input", type: "search", autocomplete: "off", role: "combobox", "aria-expanded": "true",
      "aria-controls": "kw-choices", placeholder: "Type a keyword's name", "aria-label": what === "move" ? `Move ${k.name} inside` : `Merge ${k.name} into` });
    const results = h("div", { class: "kw-choices", id: "kw-choices", role: "listbox" });
    let hits = [], sel = 0;
    const pick = (x) => {
      panel.hidden = true;
      if (what === "move") edit(k.id, { parent_id: x ? x.id : null });
      else edit(k.id, { merge_into: x.id });
    };
    function list() {
      const q = input.value.trim().toLowerCase();
      hits = (q ? others.filter((x) => x.path.toLowerCase().includes(q)) : [...others].sort((a, b) => b.total - a.total))
        .sort((a, b) => (q ? b.name.toLowerCase().startsWith(q) - a.name.toLowerCase().startsWith(q) : 0)).slice(0, 8);
      if (what === "move" && k.parent_id) hits.unshift(null);           // "At the top"
      sel = Math.min(sel, Math.max(0, hits.length - 1));
      clear(results).append(hits.length ? hits.map((x, i) => h("button", { type: "button", class: `menu-item ${i === sel ? "on" : ""}`, role: "option",
        "aria-selected": i === sel ? "true" : "false", onclick: () => pick(x) },
      x ? icon("tag") : icon("up"), h("span", { class: "grow" }, x ? x.path : "At the top (inside nothing)"), x ? h("span", { class: "meta" }, n(x.total)) : null))
        : h("div", { class: "muted small" }, "No other keyword by that name."));
    }
    input.addEventListener("input", () => { sel = 0; list(); });
    input.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") sel = Math.min(hits.length - 1, sel + 1);
      else if (e.key === "ArrowUp") sel = Math.max(0, sel - 1);
      else if (e.key === "Enter") { if (hits.length) pick(hits[sel]); }
      else if (e.key === "Escape") { panel.hidden = true; e.stopPropagation(); }
      else return;
      e.preventDefault();
      list();
    });
    clear(panel).append(h("div", { class: "row kw-panel-head" },
      h("b", { class: "grow" }, what === "move" ? `Move “${k.path}” inside…` : `Merge “${k.path}” into…`),
      h("button", { type: "button", class: "btn ghost small", onclick: () => { panel.hidden = true; } }, "Cancel")),
    what === "merge" ? h("p", { class: "note" }, k.n ? `Its ${plural(k.n, "photo")} get the other keyword instead (one step: Undo puts them back); what's inside it moves over too.`
      : "No photo has it on its own; what's inside it moves over.") : null,
    input, results);
    panel.hidden = false;
    list();
    input.focus({ preventScroll: true });
    panel.scrollIntoView({ block: "nearest" });
  }

  const pending = new Set();
  function confirmTwice(k) {
    if (!k.total || pending.has(k.id)) return true;
    pending.add(k.id);
    toast(`${k.name} is on ${plural(k.total, "photo")}. Press Remove again to take it off ${agree(k.total, "it", "all of them")}.`, { ms: 6000 });
    return false;
  }
  find.addEventListener("input", () => { query = find.value; scroller.scrollTop = 0; filter(); });
  dialog("Keywords", h("div", { class: "stack-v kw-manage" },
    h("p", { class: "note" }, "Rename a keyword by clicking its name; move it inside another, or merge it into one. Removing one takes it off its photos; what's inside it moves up a level. Your photo files are never changed."),
    h("div", { class: "row", style: { flexWrap: "nowrap" } }, find, count),
    scroller, panel,
    h("button", { type: "button", class: "btn ghost small", style: { justifySelf: "start" }, onclick: async () => {
      try { const r = await api(P(store.project, "/import/keywords"), { method: "POST", body: {} });
        toast(r.photos ? `Keywords from .xmp sidecars added to ${plural(r.photos, "photo")}` : "No keywords found in .xmp sidecars", r.photos ? { action: () => undo(r.batch).then(changed) } : {});
        await reload(); }
      catch (e) { toast(e.message, { kind: "error" }); }
    } }, icon("download"), "Bring in keywords from .xmp sidecars")),
  [{ label: "Done", primary: true, run: () => onDone?.() }]);
  filter();
  requestAnimationFrame(() => { paint(); find.focus(); });
}
