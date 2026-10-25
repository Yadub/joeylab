// RAW + JPEG: keep the RAWs you may edit again, reject the rest and move those to the Trash. A RAW and
// its JPEG are one photo; removing the RAW leaves the photo, shown from its JPEG, with every decision.
// Per photo the choice is Keep RAW, Reject RAW, or none (rules decide, and nothing goes). Only rejected
// RAWs go to the Trash. Here: the Tidy page, a whole event's RAWs, the move to the Trash (owner only,
// typed count), the viewer's RAW pill and R key, and the selection bar's buttons.

import { api, P, qs } from "../api.js";
import { forgetDetails } from "../loupe.js";
import { emit, on, store, undo } from "../store.js";
import { agree, bytes, clear, dialog, h, icon, n, plural, toast } from "../ui.js";
import { trashNote, trashSummary } from "./backups.js";

const KEPT = ["you", "pick", "stars", "fav", "album", "edited"];
const GOES = ["notneeded", "unkept"];
// The Library's RAW filter (more filters › RAW), and its chips.
export const RAW_SEG = [["", "Any"], ["kept", "RAW kept"], ["notneeded", "RAW rejected"], ["unkept", "RAW undecided"]];
const FILTER_WORDS = {
  kept: "RAW kept", removable: "RAW rejected or undecided", notneeded: "RAW rejected", you: "Keep RAW (your choice)",
  pick: "RAW kept: picked", stars: "RAW kept: rated", fav: "RAW kept: favourite", album: "RAW kept: in an album",
  edited: "RAW kept: edited", unkept: "RAW undecided (no rule keeps it)", pair: "RAW + JPEG or HEIC", only: "RAW with no JPEG",
};
export const rawFilterLabel = (v) => FILTER_WORDS[v] || `RAW: ${v}`;

// ------------------------------------------------------------ each photo's RAW (viewer)
const known = new Map();            // photo id -> its RAW's status, until something changes it
on((type, ids) => {
  if (type === "rows" && Array.isArray(ids)) ids.forEach((id) => known.delete(id));
  else if (type === "history" || type === "raws") known.clear();
});

async function statusOf(id) {
  if (!known.has(id)) {
    known.set(id, api(P(store.project, "/raws/status"), { params: { ids: id } })
      .then((d) => d.status[String(id)] || null).catch((e) => { known.delete(id); throw e; }));
  }
  return known.get(id);
}

const PILL = {
  you: ["pick", "Keep RAW"], pick: ["neutral", "RAW kept"], stars: ["neutral", "RAW kept"], fav: ["neutral", "RAW kept"],
  album: ["neutral", "RAW kept"], edited: ["neutral", "RAW kept"], notneeded: ["reject", "RAW rejected"],
  unkept: ["warn", "RAW undecided"], only: ["neutral", "RAW only"], gone: ["neutral", "JPEG only"],
};

/** The viewer's RAW pill for a photo: what becomes of its RAW and why; a click (or R) toggles it.
 * Empty for a photo without a RAW. */
export function rawTag(id) {
  const el = h("button", { type: "button", class: "pill raw-tag", hidden: true, onclick: () => toggleRaw(id) });
  statusOf(id).then((s) => {
    if (!s || !PILL[s.state]) return;
    const [kind, text] = PILL[s.state];
    el.className = `pill raw-tag ${kind}`;
    el.textContent = text;
    el.title = s.state === "only" ? "No JPEG beside this RAW: it is the photo, so it always stays."
      : s.state === "gone" ? "Its RAW has gone (to the Trash, say). Put Back in History brings it back."
        : `${s.why[0].toUpperCase()}${s.why.slice(1)}. R: ${s.state === "notneeded" ? "Keep RAW" : "Reject RAW (the JPEG stays)"}`
          + (s.choice ? " · ⇧R: let the rules decide" : "");
    el.hidden = false;
  }).catch(() => {});
  return el;
}

/** R in the viewer: rejects the photo's RAW (the JPEG stays), or keeps a rejected one. back: no
 * choice (the rules decide again). */
export async function toggleRaw(id, { back = false } = {}) {
  let s;
  try { s = await statusOf(id); } catch (e) { toast(e.message, { kind: "error" }); return; }
  if (!s || ![...KEPT, ...GOES].includes(s.state)) {
    toast(s?.state === "only" ? "This RAW has no JPEG beside it: it is the photo, so it always stays."
      : s?.state === "gone" ? "This photo's RAW has gone already." : "This photo has no RAW.");
    return;
  }
  if (back && !s.choice) { toast("The rules decide this RAW already"); return; }
  return choose({ ids: [id] }, back ? "" : s.state === "notneeded" ? "keep" : "remove");
}

const SAID = { keep: "Kept the RAW of", remove: "Rejected the RAW of", "": "The rules decide the RAW of" };
const ALREADY = { keep: "Their RAWs are kept already", remove: "Their RAWs are rejected already", "": "The rules decide their RAWs already" };

/** Keep RAW, Reject RAW or "" (the rules decide) for photos ({ids} or a Library {filter}): one step
 * that Undo takes back. After rejecting, the message offers the move to the Trash (for the owner). */
export async function choose(which, choice) {
  let r;
  try { r = await api(P(store.project, "/raws/choose"), { method: "POST", body: { ...which, choice } }); }
  catch (e) { toast(e.message, { kind: "error" }); return null; }
  const k = r.changed.length;
  emit("rows", r.changed);
  emit("history");
  const rest = r.not_pairs ? ` (${plural(r.not_pairs, "photo")} without both a RAW and a JPEG left as ${agree(r.not_pairs, "it is", "they are")})` : "";
  if (!k) { toast(r.not_pairs ? `None of these has both a RAW and a JPEG` : ALREADY[choice]); return r; }
  const actions = [];
  if (r.batch) actions.push({ label: "Undo", run: () => undo(r.batch) });
  if (choice === "remove" && store.meta?.can_trash) actions.push({ label: "Move to the Trash…", run: () => rawTrashDialog({ ids: r.changed }) });
  toast(`${SAID[choice]} ${plural(k, "photo")}${choice === "remove" ? " (the JPEGs stay)" : ""}${rest}`, { actions, ms: 8000 });
  return r;
}

/** "Keep RAW" and "Reject RAW" on the Library's selection bar (shown when the library has RAWs). */
export function rawActions(bar, grid, { filter }) {
  const which = async () => (grid.matchAll && filter ? { filter: await filter() } : { ids: grid.targetIds() });
  const group = h("span", { class: "raw-acts", hidden: store.overview?.counts?.pairs === 0 },
    h("span", { class: "sep" }),
    h("button", { class: "btn ghost small", title: "Keep these photos' RAWs (R in the viewer)",
      onclick: async () => choose(await which(), "keep") }, "Keep RAW"),
    h("button", { class: "btn ghost small", title: "Their RAWs can then go to the Trash; the photos stay, shown from their JPEGs (R in the viewer)",
      onclick: async () => choose(await which(), "remove") }, "Reject RAW"));
  bar.insertBefore(group, bar.lastElementChild);       // before Deselect
  return group;
}

// ------------------------------------------------------------ a whole event's RAWs
const RULE_KEPT = ["pick", "stars", "fav", "album", "edited"];

/** Keep or reject the RAWs of every photo a Library filter matches (an event or a trip, say), with
 * what that would free. Photos you said Keep RAW for stay kept; those a rule keeps (picked, rated,
 * favourite, in an album, edited) only if you tick them in. */
export async function groupRawsDialog({ filter, title, onChange } = {}) {
  let c;
  try { c = await api(P(store.project, "/raws/count"), { method: "POST", body: { filter } }); }
  catch (e) { toast(e.message, { kind: "error" }); return; }
  if (!c.pairs) { toast("No photo here has both a RAW and a JPEG"); return; }
  const st = (k) => c.states[k] || { photos: 0, bytes: 0, ids: [] };
  const sum = (keys) => keys.reduce((a, k) => ({ photos: a.photos + st(k).photos, bytes: a.bytes + st(k).bytes, ids: a.ids.concat(st(k).ids) }),
    { photos: 0, bytes: 0, ids: [] });
  const rejected = st("notneeded"), undecided = st("unkept"), byRule = sum(RULE_KEPT), byYou = st("you");
  const alsoRules = h("input", { type: "checkbox" });
  const target = () => (alsoRules.checked ? sum(["unkept", ...RULE_KEPT]) : undecided);
  const line = (label, x, extra = "") => (x.photos ? h("div", { class: "raw-count-row" }, h("span", {}, label),
    h("b", { class: "num" }, n(x.photos)), h("span", { class: "muted num" }, bytes(x.bytes)), extra ? h("span", { class: "faint small" }, extra) : null) : null);
  const go = h("button", { type: "button", class: "btn primary" });
  const paintGo = () => {
    const t = target();
    go.disabled = !t.photos;
    go.textContent = t.photos ? `Reject ${plural(t.photos, "RAW")} (${bytes(t.bytes)})` : "Nothing more to reject";
  };
  alsoRules.addEventListener("change", paintGo);
  paintGo();
  const canTrash = !!store.meta?.can_trash;
  const dlg = dialog(title ? `RAWs in “${title}”` : "RAWs", h("div", { class: "stack-v" },
    h("p", {}, h("b", {}, plural(c.pairs, "photo")), ` here ${agree(c.pairs, "has", "have")} a RAW and a JPEG. Their RAWs take `, h("b", {}, bytes(c.bytes)), "."),
    h("div", { class: "raw-counts" },
      line("Rejected", rejected, "go when you move rejected RAWs to the Trash"),
      line("Undecided", undecided, "no rule keeps them"),
      line("Kept by a rule", byRule, "picked, rated, favourite, in an album or edited"),
      line("Kept by you", byYou, "Keep RAW")),
    byRule.photos ? h("label", { class: "check" }, alsoRules,
      h("span", {}, `Also reject the ${plural(byRule.photos, "RAW")} a rule keeps (${bytes(byRule.bytes)})`)) : null,
    h("p", { class: "note" }, "The JPEGs always stay, and so do the photos with every decision. A rejected RAW stays where it is until you move rejected RAWs to the Trash; Undo takes this back.")), [
    { label: "Cancel" },
    { label: "Keep all their RAWs", run: async () => { await choose({ ids: [...rejected.ids, ...undecided.ids, ...byRule.ids] }, "keep"); onChange?.(); } },
    rejected.photos && canTrash ? { label: `Move ${n(rejected.photos)} rejected to the Trash…`, run: () => { setTimeout(() => rawTrashDialog({ filter, onDone: onChange }), 0); } } : null,
  ].filter(Boolean));
  go.addEventListener("click", async () => {
    const t = target();
    if (!t.photos) return;
    dlg.close();
    await choose({ ids: t.ids }, "remove");
    onChange?.();
  });
  dlg.querySelector(".dlg-actions, .actions, footer")?.append(go);
  if (!go.isConnected) dlg.querySelector(".stack-v").append(h("div", { class: "row" }, go));
}

// ------------------------------------------------------------ moving them to the Trash
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function follow(p, job, onProgress) {
  while (job.state === "checking" || job.state === "moving") {
    onProgress(job);
    await sleep(job.state === "moving" ? 500 : 700);
    job = await api(P(p, `/raws/trash/${job.token}`));
  }
  return job;
}

function folderName(path) {
  const parts = path.split("/").filter(Boolean);
  return h("span", { class: "mono dup-path", title: path },
    parts.length > 1 ? h("span", { class: "faint" }, `${parts.at(-2)}/`) : null, h("b", {}, parts.at(-1) || path));
}
const base = (path) => path.split("/").pop();

/** Check, then move the RAWs you rejected (all, or those of a Library filter or ids) to the Trash
 * after their count is typed. Each is checked again just before it goes. */
export async function rawTrashDialog({ filter, ids, onDone } = {}) {
  const p = store.project;
  const body = h("div", { class: "stack-v trash-dlg raw-dlg" });
  const dlg = dialog("Move rejected RAWs to the Trash", body, [{ label: "Close" }]);
  const progress = (j) => {
    const verb = j.state === "moving" ? "Moving to the Trash" : "Checking each RAW and the JPEG that stays (read-only)";
    clear(body).append(h("div", { class: "muted" }, j.total ? `${verb}… ${n(j.done)} of ${n(j.total)}` : `${verb}…`),
      j.total ? h("div", { class: "bar" }, h("i", { style: { width: `${Math.round((100 * j.done) / j.total)}%` } })) : null);
  };
  let job;
  try {
    job = await api(P(p, "/raws/trash/plan"), { method: "POST", body: { filter, ids } });
    job = await follow(p, job, progress);
  } catch (e) { clear(body).append(h("div", { class: "result err" }, e.message)); return; }
  if (!dlg.open) return;
  if (job.state === "error") { clear(body).append(h("div", { class: "result err" }, job.error)); return; }
  const j = job, go = j.go;
  const goBytes = go.bytes + go.sidecar_bytes;
  const rows = j.by_folder.filter((f) => f.raws);
  clear(body).append(
    go.raws ? h("p", {}, h("b", {}, `${plural(go.raws, "rejected RAW")} (${bytes(goBytes)})`), ` can go. ${agree(go.raws, "Its photo stays", "Their photos stay")}, shown from the JPEG beside ${agree(go.raws, "it", "each")}, with stars, picks, albums, keywords and faces.`)
      : h("p", { class: "muted" }, "No rejected RAW can go here. Reject RAWs first: for a whole event (its ⋯ menu › RAWs…), on a selection in the Library, or with R in the viewer."),
    go.sidecars ? h("p", { class: "muted small" }, go.sidecars === 1 ? "1 sidecar (a RAW's own .xmp file, kept with it) goes too."
      : `${n(go.sidecars)} sidecars (the RAWs' own .xmp files, kept with them) go too.`, " A JPEG's own sidecar stays.") : null,
    rows.length ? h("div", { class: "folder-losses" },
      h("div", { class: "fl-row fl-head" }, h("span", {}, "Folder"), h("span", {}, "RAWs"), h("span", {}, "Frees")),
      rows.slice(0, 12).map((f) => h("div", { class: "fl-row" }, folderName(f.path), h("b", {}, n(f.raws)), h("span", { class: "muted" }, bytes(f.bytes)))),
      rows.length > 12 ? h("div", { class: "muted small" }, `…and ${plural(rows.length - 12, "more folder")}`) : null) : null,
    j.skipped.length ? h("div", { class: "muted small" }, j.skipped.map((s) => h("div", {}, `${n(s.n)} left alone: ${s.why}`))) : null,
    j.backups ? trashSummary(j.backups, "RAW") : null,
    j.items.length ? h("details", { class: "raw-files", open: !!j.backups && j.items.length <= 6 }, h("summary", { class: "label" }, "Each RAW, the JPEG that stays and its backups"),
      h("div", { class: "stack-v trash-list" }, j.items.map((it) => h("div", { class: "path" },
        h("span", { class: "mono" }, base(it.raw)), " → stays: ", h("span", { class: "mono" }, base(it.stays)),
        it.sidecars.length ? h("span", { class: "muted" }, ` · with ${it.sidecars.join(", ")}`) : null,
        h("span", { class: "faint" }, ` · ${it.raw.split("/").slice(-2, -1)[0] || ""}`),
        j.backups ? h("div", {}, trashNote(it)) : null))),
      h("a", { class: "linkish", href: P(p, `/raws/trash/${j.token}/list.tsv`) }, go.raws > j.items.length ? `All ${n(go.raws)} in a list (TSV)` : "Download this list (TSV)")) : null,
    confirmBox());

  function confirmBox() {
    const box = h("div", { class: "stack-v" });
    if (!go.raws) return box;
    const count = go.raws;
    const btn = h("button", { type: "button", class: "btn primary danger-fill", disabled: true, onclick: () => run(count) }, icon("close"), "Move to Trash");
    const input = h("input", { class: "input", inputmode: "numeric", autocomplete: "off", "aria-label": `Type ${count} to confirm`,
      oninput: () => { btn.disabled = input.value.replace(/[,\s]/g, "") !== String(count); },
      onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); if (!btn.disabled) run(count); } } });
    box.append(
      h("p", { class: "note" }, "They go to the Trash, not away for good: until you empty it, Put Back (in History, or Finder) returns them. Each RAW is checked again just before it goes: if its JPEG has gone, changed or can't be read whole, or you've taken back Reject RAW since, it stays."),
      h("label", { class: "field" }, h("span", {}, `Type ${count} to move ${plural(count, "RAW")} (${bytes(goBytes)}) to the Trash`),
        h("div", { class: "row", style: { flexWrap: "nowrap" } }, input, btn)));
    setTimeout(() => input.focus(), 0);
    return box;
  }

  async function run(count) {
    let r;
    try {
      r = await api(P(p, `/raws/trash/${j.token}/run`), { method: "POST", body: { count } });
      r = await follow(p, r, progress);
    } catch (e) { clear(body).append(h("div", { class: "result err" }, e.message)); return; }
    if (r.state === "error") { clear(body).append(h("div", { class: "result err" }, r.error)); return; }
    const res = r.result;
    // The catalog already shows the RAWs gone: counts, filters and the viewer follow at once.
    store.overview = null;
    store.overviewStale = true;
    forgetDetails();
    emit("raws");
    emit("rows", res.ids);
    clear(body).append(
      h("div", { class: "result" }, h("b", {}, `Moved ${plural(res.moved, "RAW")} (${bytes(res.bytes)}) to the Trash.`),
        res.sidecars ? ` ${plural(res.sidecars, "sidecar")} went with them.` : "", " Their photos stay, shown from their JPEGs."),
      h("p", { class: "note" }, "To get the space back, empty the Trash in Finder. Until then, Put Back returns any of them: ",
        h("a", { href: `#/p/${encodeURIComponent(p)}/history?tab=trash` }, "History › Moved to the Trash"), "."),
      res.left.length ? h("details", {}, h("summary", { class: "label" }, `${plural(res.left.length, "RAW")} left alone`),
        res.left.map((x) => h("div", { class: "path" }, `${x.path}: ${x.why.replace(/^left alone: /, "")}`))) : null,
      h("div", { class: "muted small mono" }, `Log: ${res.log}`));
    onDone?.(res);
  }
}

// ------------------------------------------------------------ Tidy › RAW + JPEG
const RULE_ROWS = [
  ["pick", "Picked"], ["stars", null], ["fav", "Favourites"], ["album", "In an album"],
  ["edited", "Edited", "develop settings in a sidecar, or the RAW saved over an hour after its JPEG"],
];

export function mount(root, ctx) {
  const p = ctx.project;
  const page = h("div", { class: "stack-v raws-view" });
  root.append(page);
  let d = null;
  let allFolders = false;
  const lib = (q) => `#/p/${encodeURIComponent(p)}/library?${qs({ ...q, all: 1 })}`;
  const writer = store.meta?.writer !== false;

  async function load() {
    try { d = await api(P(p, "/raws")); } catch (e) { clear(page).append(h("div", { class: "result err" }, e.message)); return; }
    render();
  }

  function rel(path) {
    const r = (d.roots || []).find((x) => path === x || path.startsWith(x + "/"));
    return r ? path.slice(r.length + 1) || path.split("/").pop() : path;
  }

  async function setRule(change) {
    try {
      await api(P(p, "/raws/rules"), { method: "POST", body: change });
      emit("raws");                     // this page, and every RAW's status, read again
    } catch (e) { toast(e.message, { kind: "error" }); }
  }

  function render() {
    const pr = d.pairs, st = d.states, ru = d.rules;
    const canTrash = !!store.meta?.can_trash;
    const move = () => rawTrashDialog();
    const count = (k) => st[k] || { photos: 0, bytes: 0 };
    const figs = (x, q) => [
      x.photos ? h("a", { class: "num", href: lib(q), title: "Show them in the Library" }, n(x.photos)) : h("span", { class: "num muted" }, "0"),
      h("span", { class: "muted num" }, x.photos ? bytes(x.bytes) : "")];
    const ruleRow = ([k, label, hint]) => {
      const box = h("input", { type: "checkbox", checked: !!ru[k], disabled: !writer, onchange: (e) => setRule({ [k]: e.target.checked }) });
      const stars = k === "stars" ? h("select", { class: "input", disabled: !writer, "aria-label": "Fewest stars", style: { width: "auto", height: "26px" },
        onchange: (e) => setRule({ min_stars: +e.target.value, stars: true }) },
      [1, 2, 3, 4, 5].map((s) => h("option", { value: s, selected: ru.min_stars === s }, `★${s}`))) : null;
      return h("div", { class: `raw-rule ${ru[k] ? "" : "off"}` },
        h("label", { class: "check" }, box, h("span", {}, stars ? ["Rated ", stars, " or more"] : label,
          hint ? h("span", { class: "faint small" }, ` · ${hint}`) : null)),
        ...figs(count(k), { raw: k }));
    };
    const fixed = (k, label, hint, extra = null) => h("div", { class: "raw-rule" },
      h("span", {}, h("b", {}, label), hint ? h("span", { class: "faint small" }, ` · ${hint}`) : null, extra), ...figs(count(k), { raw: k }));
    const rejectUndecided = () => {
      const u = count("unkept");
      dialog(`Reject ${plural(u.photos, "undecided RAW")}?`, h("div", { class: "stack-v" },
        h("p", {}, `The RAWs of ${plural(u.photos, "photo")} that no rule keeps (${bytes(u.bytes)}) become rejected. The JPEGs stay.`),
        h("p", { class: "note" }, "Nothing moves yet: review them in the Library first if you like, then move rejected RAWs to the Trash. Undo takes this back."),
        h("a", { href: lib({ raw: "unkept" }) }, "Review them in the Library")), [
        { label: "Cancel" },
        { label: `Reject ${n(u.photos)}`, primary: true, submit: true, run: () => { choose({ filter: { raw: "unkept", all: 1 } }, "remove"); } },
      ]);
    };
    const folders = allFolders ? d.folders : d.folders.slice(0, 15);
    const table = (rows, first, link) => h("div", { class: "table-wrap" }, h("table", { class: "plain raw-table" },
      h("thead", {}, h("tr", {}, h("th", {}, first), h("th", { class: "num" }, "Photos"), h("th", { class: "num" }, "RAW kept"),
        h("th", { class: "num" }, "Rejected"), h("th", { class: "num" }, "Undecided"), h("th", { class: "num" }, "Rejected free"))),
      h("tbody", {}, rows.map((r) => h("tr", {}, h("td", {}, link.name(r)), h("td", { class: "num" }, n(r.photos)),
        h("td", { class: "num" }, r.kept ? h("a", { href: lib({ ...link.q(r), raw: "kept" }) }, n(r.kept)) : "0"),
        h("td", { class: "num" }, r.rejected ? h("a", { href: lib({ ...link.q(r), raw: "notneeded" }) }, n(r.rejected)) : "0"),
        h("td", { class: "num" }, r.removable - r.rejected ? h("a", { href: lib({ ...link.q(r), raw: "unkept" }) }, n(r.removable - r.rejected)) : "0"),
        h("td", { class: "num" }, r.rejected ? bytes(r.rejected_bytes) : ""))))));
    const year = { name: (r) => r.year || "No date", q: (r) => (r.year ? { from: `${r.year}-01-01`, to: `${r.year}-12-31` } : { undated: 1 }) };
    const folder = { name: (r) => h("span", { class: "mono dup-path", title: r.path }, rel(r.path)), q: (r) => ({ folder: r.path }) };
    const cmd = `${store.meta?.joeylab_bin || "joeylab"} raw-list -p ${p}`;
    clear(page).append(
      h("div", { class: "page-head" },
        h("div", {}, h("h2", { class: "sub-title" }, "RAW + JPEG"),
          h("div", { class: "sub" }, pr.photos ? `${plural(pr.photos, "photo")} ${agree(pr.photos, "has", "have")} both a RAW and a JPEG. Their RAWs take ${bytes(pr.bytes)}.`
            : "No photo here has both a RAW and a JPEG.")),
        h("div", { class: "actions" },
          pr.rejected ? h("a", { class: "btn", href: lib({ raw: "notneeded" }) }, icon("grid"), `Review the ${plural(pr.rejected, "rejected RAW")}`) : null,
          pr.rejected && canTrash ? h("button", { class: "btn danger", type: "button", onclick: move }, icon("close"), `Move ${plural(pr.rejected_raws, "rejected RAW")} to the Trash…`) : null)),
      h("p", { class: "note" }, "Reject the RAWs you won't edit again, then move the rejected ones to the Trash. A photo whose RAW goes stays in the library, shown from its JPEG, with its stars, picks, albums, keywords and faces. Only RAWs you rejected ever go; nothing moves until you confirm, and Put Back (in History) returns it."),
      pr.photos || d.raw_only.photos ? h("div", { class: "health-grid" },
        h("a", { class: "health-card", href: lib({ raw: "kept" }) }, h("span", { class: "label" }, "RAWs kept"), h("b", {}, n(pr.kept)),
          h("span", { class: "muted" }, `${bytes(pr.kept_bytes)} · by your choice or a rule`)),
        h("a", { class: "health-card", href: lib({ raw: "notneeded" }) }, h("span", { class: "label" }, "RAWs you rejected"), h("b", {}, n(pr.rejected)),
          h("span", { class: "muted" }, pr.rejected ? `${bytes(pr.rejected_bytes)} would be freed` : "none yet: reject them per event or on a selection")),
        pr.removable - pr.rejected ? h("a", { class: "health-card", href: lib({ raw: "unkept" }) }, h("span", { class: "label" }, "RAWs undecided"), h("b", {}, n(pr.removable - pr.rejected)),
          h("span", { class: "muted" }, `${bytes(pr.removable_bytes - pr.rejected_bytes)} · no rule keeps them; they stay unless you reject them`)) : null,
        d.raw_only.photos ? h("a", { class: "health-card", href: lib({ raw: "only" }) }, h("span", { class: "label" }, "RAW with no JPEG"), h("b", {}, n(d.raw_only.photos)),
          h("span", { class: "muted" }, `${bytes(d.raw_only.bytes)} · always kept: each is the photo itself`)) : null) : null,
      pr.photos ? h("section", { class: "section" }, h("h2", {}, "Which RAWs stay"),
        h("p", { class: "note" }, "Your own choice for a photo comes first. For the others, the RAW stays when one of these rules applies, and is undecided when none does: it stays until you reject it. Each photo is counted once, under the first reason that applies."),
        h("div", { class: "raw-rules" },
          h("div", { class: "raw-rule raw-rule-head" }, h("span", {}, ""), h("span", { class: "num" }, "Photos"), h("span", { class: "num" }, "RAWs take")),
          fixed("you", "Keep RAW", "your choice"),
          h("div", { class: "raw-rule-sub label" }, "Keep the RAW of photos that are"),
          RULE_ROWS.map(ruleRow),
          h("div", { class: "raw-rule-sub label" }, "The rest"),
          fixed("notneeded", "Reject RAW", "your choice: these go when you move rejected RAWs to the Trash"),
          fixed("unkept", "Undecided", "no rule keeps them; they stay unless you reject them",
            count("unkept").photos && writer ? h("button", { class: "btn small ghost", type: "button", onclick: rejectUndecided, style: { marginLeft: "8px" } }, "Reject all…") : null)),
        h("p", { class: "note" }, "To keep or reject RAWs: for a whole event or trip, use RAWs… in its ⋯ menu on the Events page or above it in the Library; for any photos, select them in the Library and use Keep RAW or Reject RAW on the bar, or press R in the viewer.")) : null,
      d.years.length > 1 ? h("section", { class: "section" }, h("h2", {}, "By year"), table(d.years, "Year", year)) : null,
      d.folders.length ? h("section", { class: "section" }, h("h2", {}, "By folder"), table(folders, "Folder", folder),
        d.folders.length > 15 && !allFolders ? h("button", { class: "btn small", type: "button", onclick: () => { allFolders = true; render(); } },
          `Show all ${plural(d.folder_count, "folder")}`) : null,
        allFolders && d.folder_count > d.folders.length ? h("p", { class: "muted small" }, `The ${n(d.folders.length)} with the most space to free are shown.`) : null) : null,
      pr.photos ? h("details", { class: "dup-terminal" }, h("summary", { class: "label" }, "Terminal"),
        h("p", { class: "note" }, "The same list in Terminal, read-only; add --trash to move the rejected RAWs after typing the count:"),
        h("code", { class: "mono", style: { userSelect: "all" } }, cmd)) : null,
      !pr.photos && !d.raw_only.photos ? h("div", { class: "empty" }, h("h3", {}, "No RAW files in this library"),
        h("p", {}, "When a camera saves a RAW and a JPEG of each photo, JoeyLab keeps them together as one photo. This page then helps you keep the RAWs you need.")) : null,
    );
  }

  const off = on((type) => { if (type === "raws") load(); });
  load();
  return { refresh: load, unmount: off };
}
