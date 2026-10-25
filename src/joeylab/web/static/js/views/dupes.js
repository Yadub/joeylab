// Duplicates: grouped by the folders they live in (sortable), or every set. Nothing is deleted here.

import { api, P, qs } from "../api.js";
import { thumbButton } from "../grid.js";
import { openLoupe } from "../loupe.js";
import { remember, store, undo } from "../store.js";

const store_home = () => store.meta?.home || "~";
import { agree, bytes, clear, dialog, h, icon, n, plural, tabBar, toast } from "../ui.js";
import { groupKeys } from "./common.js";
import { folderLosses, trashDialog } from "./trash.js";

const KINDS = [
  ["exact", "Identical files", "Byte-for-byte the same file, on the same drive. Safe to remove all but one."],
  ["copy", "Re-saved copies", "Same photo and file name, saved again (often at another quality). JoeyLab keeps the best."],
  ["export", "Edited or renamed versions", "A renamed export of an original, often your edit. Not a duplicate; for review."],
  ["pair", "RAW + JPEG apart", "A RAW and its camera JPEG in different folders. Not a duplicate; for tidying."],
];
const SORTS = [["space", "Most space"], ["files", "Most extra files"], ["sets", "Most sets"], ["name", "Folder name"]];
const KIND_WORDS = { exact: ["identical file"], copy: ["re-saved copy", "re-saved copies"],
  export: ["edited or renamed version"], pair: ["RAW + JPEG pair apart", "RAW + JPEG pairs apart"] };
const GROUP_PAGE = 24, LIST_PAGE = 60;
/** The keys, for the shortcuts list. */
export const KEYS = { name: "Duplicates", rows: [["J / K", "Next / previous folder group (or set, or selection)"],
  ["Enter", "Open it: a group's sets, a set's copies in the viewer, a selection's album"]] };

export function mount(root, ctx) {
  const page = h("div", { class: "stack-v dupes-view" });
  root.append(page);
  const p = ctx.project;
  const st = {
    kinds: new Set((ctx.params.get("kinds") || "exact,copy").split(",")),
    tab: ctx.params.get("dtab") || "folders",
    sort: ctx.params.get("sort") || "space",
    open: new Set(),
  };
  let data = null;

  const save = () => ctx.setParams({ tab: "dupes", dtab: st.tab, sort: st.sort, kinds: [...st.kinds].join(",") });

  async function load() {
    try { data = await api(P(p, "/dupes/groups"), { params: { kinds: [...st.kinds].join(",") } }); }
    catch (e) { clear(page).append(h("div", { class: "result err" }, e.message)); return; }
    render();
  }

  function rel(path) {
    const r = (data.roots || []).find((x) => path === x || path.startsWith(x + "/"));
    return r ? path.slice(r.length + 1) || "(top folder)" : path;
  }
  const leaf = (path) => rel(path).split("/").slice(-2).join("/");      // "Old Laptop/Pictures", "DCIM/100_FUJI"

  // Folder paths share long prefixes; dim the parent so the folder's own name stands out.
  function pathEl(path) {
    const r = rel(path);
    const i = r.lastIndexOf("/");
    return h("span", { class: "mono dup-path", title: path }, i > 0 ? h("span", { class: "faint" }, r.slice(0, i + 1)) : null, h("b", {}, r.slice(i + 1)));
  }

  function sorted(groups) {
    const key = {
      space: (g) => -g.extra_bytes, files: (g) => -g.extra_files, sets: (g) => -g.sets,
      name: (g) => rel(g.folders[0].path).toLowerCase(),
    }[st.sort];
    return [...groups].sort((a, b) => (key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : 0));
  }

  function render() {
    const c = data.counts || {};
    const t = data.totals;
    const cmd = `${store.meta?.joeylab_bin || "joeylab"} trash-list ~/Downloads/${p}-duplicate-extras.txt -p ${p}`;
    const panel = h("div", {}, st.tab === "folders" ? folderTable() : st.tab === "selections" ? selectionList() : setList());
    clear(page).append(
      h("div", { class: "page-head" },
        h("div", {}, h("h2", { class: "sub-title" }, "Duplicates"),
          h("div", { class: "sub" }, t.sets
            ? `${plural(t.sets, "set")} in ${plural(t.groups, "folder group")} · ${plural(t.extra_files, "extra file")} · ${bytes(t.extra_bytes)} could be freed`
            : "Nothing matches these filters.")),
        h("div", { class: "actions" },
          t.sets ? h("button", { class: "btn", type: "button", title: "Reject the extra copies in every set listed here, not just the ones on screen, then move them to the Trash if you like",
            onclick: () => rejectExtras(data.groups.flatMap((g) => g.set_ids), `all ${plural(t.groups, "folder group")} listed`) }, icon("reject"), "Reject all extra copies") : null,
          t.sets ? h("a", { class: "btn", href: `${P(p, "/dupes/extras")}?${qs({ ids: data.groups.flatMap((g) => g.set_ids).join(",") })}` },
            icon("download"), "List of every extra copy") : null)),
      h("div", { class: "row" },
        KINDS.map(([k, label, hint]) => h("button", {
          type: "button", class: "chip", title: hint, "aria-pressed": st.kinds.has(k) ? "true" : "false",
          onclick: () => { st.kinds.has(k) ? st.kinds.delete(k) : st.kinds.add(k); save(); load(); },
        }, label, h("span", { class: "n" }, n(c[k] || 0))))),
      h("p", { class: "note" }, "Nothing is deleted here. Check which copy each group keeps, then reject the extra copies and move them to the Trash, or download the list and remove them yourself. ",
        h("button", { class: "linkish", onclick: verifyDialog }, "Check a list right before deleting"), "."),
      backupsNote(),
      h("details", { class: "dup-terminal" }, h("summary", { class: "label" }, "Terminal"),
        h("p", { class: "note" }, "With the downloaded list of extra copies, this makes the same check, shows what stays, and moves them to the Trash after you type the count:"),
        h("code", { class: "mono", style: { userSelect: "all" } }, cmd)),
      rulesPanel(),
      h("div", { class: "row" },
        tabBar([["folders", "By folders"], ["selections", "Selections"], ["sets", "All sets"]], st.tab,
          (k) => { st.tab = k; save(); render(); }, { label: "Duplicates", id: "dupes", panel }),
        st.tab === "folders" ? h("label", { class: "row", style: { gap: "6px" } }, h("span", { class: "muted" }, "Sort"),
          h("select", { class: "input", id: "dup-sort", style: { width: "auto" },
            onchange: (e) => { st.sort = e.target.value; save(); render(); } },
          SORTS.map(([v, label]) => h("option", { value: v, selected: st.sort === v }, label)))) : null),
      panel,
    );
  }

  // Copies on other drives are backups, not duplicates: never in these sets, never extras.
  function backupsNote() {
    const b = data.backups;
    const href = `#/p/${encodeURIComponent(p)}/tidy?tab=backups`;
    return h("p", { class: "note bk-summary" }, icon("shield"), h("span", {},
      "Only copies on the same drive count as duplicates. An identical copy on another drive is a backup: it's never an extra copy here, and never rejected or moved to the Trash for being one. ",
      b?.known ? [`${plural(b.elsewhere, "photo")} in this library ${agree(b.elsewhere, "has", "have")} a backup on another drive`,
        b.elsewhere > b.checked ? ` (${n(b.checked)} checked byte for byte so far)` : "", ". "] : null,
      h("a", { href }, "See Backups"), "."));
  }

  // ------------------------------------------------------------ which copy is kept
  function rulesPanel() {
    const rules = data.rules || [];
    return h("div", { class: "keep-rules" },
      h("span", { class: "label" }, "Always keep copies under"),
      rules.length ? rules.map((r) => h("span", { class: "keep-rule", title: r }, pathEl(r),
        store.meta?.writer !== false ? h("button", { type: "button", class: "btn ghost small icon-only", title: "Remove this rule",
          "aria-label": `Stop always keeping copies under ${rel(r)}`, onclick: () => setRule(r, false) }, icon("close")) : null))
        : h("span", { class: "muted" }, "no folder yet"),
      store.meta?.writer !== false ? h("button", { type: "button", class: "btn ghost small", onclick: () => ruleDialog() }, icon("plus"), "Add a folder…") : null,
      h("details", { class: "keep-why" }, h("summary", { class: "linkish" }, "How the kept copy is chosen"),
        h("ol", { class: "note" },
          h("li", {}, "A folder you chose: “Keep the copies in” for one group, then the folders you always keep copies under."),
          h("li", {}, "Not a folder named like a copy: backup, copy, export, dump, desktop, old, shared, selected and the like."),
          h("li", {}, "Not a folder whose photos are almost all copies of photos elsewhere, such as a selection picked from a card. The originals are kept; the selection is saved as an album (see Selections)."),
          h("li", {}, "A dated, named or camera folder (“2024-05 Lisbon”, “DCIM/100_FUJI”)."),
          h("li", {}, "The folder with the most photos found nowhere else."),
          h("li", {}, "For re-saved copies, the best-quality file first; among files as good as each other, the folder decides."))));
  }

  async function setRule(folder, on) {
    try {
      const r = await api(P(p, "/dupes/rules"), { method: "POST", body: { folder, on } });
      toast(on ? `Always keeping copies under ${leaf(folder)}: ${plural(r.changed, "set")} changed` : `Rule removed: ${plural(r.changed, "set")} changed`);
      load();
    } catch (e) { toast(e.message, { kind: "error" }); }
  }

  // Every folder a group lives in, and the folders above them up to the library's folders.
  function ruleFolders() {
    const out = new Set();
    for (const g of data.groups) for (const f of g.folders) {
      let d = f.path;
      while (d && d !== "/") {
        out.add(d);
        if ((data.roots || []).includes(d)) break;
        d = d.slice(0, d.lastIndexOf("/"));
      }
    }
    return [...out].filter((d) => !(data.rules || []).includes(d)).sort();
  }

  function ruleDialog(folder = "") {
    if ((data.rules || []).some((r) => folder === r || folder.startsWith(r + "/"))) {
      toast(`Copies in ${leaf(folder)} are always kept already`);
      return;
    }
    const opts = ruleFolders();
    if (!opts.length) { toast("No folders with duplicates to choose from"); return; }
    const sel = h("select", { class: "input", "aria-label": "Folder", size: Math.min(10, opts.length), style: { height: "auto", padding: "4px" } },
      opts.map((d) => h("option", { value: d, selected: d === folder }, rel(d) === "(top folder)" ? `${d.split("/").pop()} (the whole library folder)` : rel(d))));
    if (!folder) sel.value = opts[0];
    dialog("Always keep copies under a folder", h("div", { class: "stack-v" },
      h("p", { class: "note" }, "In every group, copies in this folder (or any folder inside it) are the ones kept, now and after every update. Where you chose a folder for one group with “Keep the copies in”, that choice still comes first."),
      sel), [
      { label: "Cancel" },
      { label: "Always keep these", primary: true, submit: true, run: () => setRule(sel.value, true) },
    ]);
  }

  // "Keeping 97 in 2024-05 Lisbon · removing 97 from Desktop dump"
  function keepLine(g) {
    const kept = g.folders.filter((f) => f.kept_here), gone = g.folders.filter((f) => f.removed_here);
    if (!gone.length) return h("div", { class: "dup-keep muted" }, "For review: nothing here is an extra copy.");
    const name = (f) => h("b", { title: f.path }, leaf(f.path));
    const list = (fs, key, word) => fs.flatMap((f, i) => [i ? ", " : "", `${n(f[key])} ${word} `, name(f)]);
    return h("div", { class: "dup-keep" },
      kept.length ? ["Keeping ", ...list(kept, "kept_here", "in"), " · "] : null,
      "removing ", ...list(gone, "removed_here", "from"));
  }

  // ------------------------------------------------------------ by folders
  function folderTable() {
    if (!data.groups.length) return h("div", { class: "empty" }, h("h3", {}, "No duplicates of these kinds"));
    return h("div", { class: "dup-groups" }, sorted(data.groups).map((g) => {
      const key = g.folders.map((f) => f.path).join("|");
      const open = st.open.has(key);
      const body = h("div", { class: "dup-group-body", hidden: !open });
      const head = h("button", { type: "button", class: "dup-group-head", "aria-expanded": open ? "true" : "false" },
        h("div", { class: "dup-figs" },
          h("b", {}, bytes(g.extra_bytes)),
          h("span", {}, `${plural(g.extra_files, "extra file")} · ${plural(g.sets, "set")}`),
          h("span", { class: "kinds" }, Object.entries(g.kinds).map(([k, v]) => plural(v, ...KIND_WORDS[k])).join(" · ")
            + (g.smaller ? ` (${n(g.smaller)} the same edit saved again, much smaller)` : ""))),
        h("div", { class: "dup-folders" }, g.folders.map((f) => h("div", { class: "dup-folder" },
          h("span", { class: `pct ${f.pct >= 95 ? "full" : ""}`, title: `${f.dup_files} of this folder's ${plural(f.total_files, "file")} ${agree(f.dup_files, "is", "are")} duplicated in this group` },
            `${f.pct}%`),
          pathEl(f.path),
          h("span", { class: "muted" }, `${n(f.dup_files)} of ${n(f.total_files)}`),
          f.preferred ? h("span", { class: "pill pick", title: "You chose to keep the copies in this folder" }, "your choice")
            : f.rule ? h("span", { class: "pill pick", title: `Copies under ${f.rule} are always kept` }, "always kept") : null)),
        keepLine(g)),
        icon(open ? "up" : "down"));
      head.addEventListener("click", () => {
        st.open.has(key) ? st.open.delete(key) : st.open.add(key);
        body.hidden = !st.open.has(key);
        head.setAttribute("aria-expanded", body.hidden ? "false" : "true");
        if (!body.hidden && !body.firstChild) loadSets(body, g);
      });
      if (open) loadSets(body, g);
      return h("section", { class: "dup-group" }, head, body);
    }));
  }

  function loadSets(body, g) {
    const removable = (g.kinds.exact || 0) + (g.kinds.copy || 0) > 0;
    const keeping = [...g.folders].sort((a, b) => b.kept_here - a.kept_here)[0];
    const keepSel = h("select", { class: "input", style: { width: "auto", maxWidth: "100%" }, "aria-label": "Folder to keep" },
      g.folders.map((f) => h("option", { value: f.path, selected: f.path === keeping.path }, rel(f.path))));
    const exact = (g.kinds.exact || 0) > 0;
    const list = h("div");
    const foot = h("div", { class: "row" });
    clear(body).append(
      h("div", { class: "row" },
        removable ? h("label", { class: "row", style: { gap: "6px" } }, h("span", { class: "muted" }, "Keep the copies in"), keepSel) : null,
        exact ? h("button", { class: "btn small", type: "button", title: "Keep the copies in this folder for this group's identical files",
          onclick: async () => {
            const r = await api(P(p, "/dupes/keep"), { method: "POST", body: { folder: keepSel.value, set_ids: g.set_ids } });
            toast(`Keeping ${rel(keepSel.value)} for ${plural(r.changed, "set")}`);
            load();
          } }, "For this group") : null,
        removable ? h("button", { class: "btn small", type: "button", title: "Always keep copies in this folder (and the folders inside it), in every group",
          onclick: () => ruleDialog(keepSel.value) }, "Always…") : null,
        h("span", { style: { flex: 1 } }),
        h("button", { class: "btn small", type: "button", title: "Give each kept copy the best stars and pick any of its copies has",
          onclick: async () => {
            const r = await api(P(p, "/dupes/combine"), { method: "POST", body: { set_ids: g.set_ids } });
            toast(r.changed ? `Moved ratings onto ${plural(r.changed, "kept copy", "kept copies")}` : "Kept copies already have the best ratings",
              r.changed ? { action: () => undo(r.batch), actionLabel: "Undo" } : {});
          } }, "Combine ratings"),
        h("a", { class: "btn small", href: `${P(p, "/dupes/extras")}?${qs({ ids: g.set_ids.join(",") })}` }, icon("download"), "List of this group's extra copies"),
        // Only identical files and re-saved copies: edited versions and RAW+JPEG halves are never "extras".
        removable ? h("button", { class: "btn small", type: "button",
          title: "Reject the extra copies in every set of this folder group, not just the ones on screen, then move them to the Trash if you like",
          onclick: () => rejectExtras(g.set_ids, "this folder group") }, icon("reject"), "Reject this group's extra copies") : null),
      list, foot);
    let shown = 0;
    async function more() {
      const ids = g.set_ids.slice(shown, shown + GROUP_PAGE);
      clear(foot).append(h("span", { class: "muted" }, "Loading…"));
      let d;
      try { d = await api(P(p, "/dupes"), { params: { ids: ids.join(",") } }); }
      catch (e) { clear(foot).append(h("div", { class: "result err" }, e.message)); return; }
      d.sets.forEach((s) => remember(s.members.map((m) => m.row).filter(Boolean)));
      list.append(...d.sets.map(setCard));
      shown += ids.length;
      const left = g.set_ids.length - shown;
      clear(foot).append(left > 0 ? [
        h("span", { class: "muted" }, `Showing ${n(shown)} of ${plural(g.set_ids.length, "set")}. Rejecting and the list of extra copies cover them all.`),
        h("button", { class: "btn small", type: "button", onclick: more }, `Show ${n(Math.min(left, GROUP_PAGE))} more`)] : null);
    }
    more();
  }

  // Reject the extra copies of whole sets at once (a folder group, or everything listed): the server
  // works through every set, not just the ones loaded on screen. What each folder loses comes first;
  // then one undoable step, and for the owner the Trash step, limited to these sets' extras.
  async function rejectExtras(setIds, scope) {
    let r;
    try { r = await api(P(p, "/dupes/reject-extras"), { method: "POST", body: { set_ids: setIds, dry: true } }); }
    catch (e) { toast(e.message, { kind: "error" }); return; }
    if (!r.extras) { toast(`No extra copies to remove in ${scope}`); return; }
    const canTrash = !!store.meta?.can_trash;
    const combine = h("input", { type: "checkbox", checked: true });
    const reject = async () => {
      try { return await api(P(p, "/dupes/reject-extras"), { method: "POST", body: { set_ids: setIds, combine: combine.checked } }); }
      catch (e) { toast(e.message, { kind: "error" }); return null; }
    };
    const undoable = (done) => () => undo(done.batch).then(load);
    const what = `${plural(r.extras, "extra copy", "extra copies")} (${plural(r.files, "file")}, ${bytes(r.bytes)})`;
    const actions = [{ label: "Cancel" }];
    if (r.to_reject) {
      actions.push({ label: canTrash ? "Only reject" : `Reject ${n(r.to_reject)}`, primary: !canTrash, submit: !canTrash, run: async () => {
        const done = await reject();
        if (!done) return false;
        load();
        toast(`Rejected ${plural(done.rejected, "extra copy", "extra copies")}`, done.batch ? { action: undoable(done), actionLabel: "Undo" } : {});
      } });
    }
    if (canTrash) {
      actions.push({ label: r.to_reject ? `Reject ${n(r.to_reject)} and move to the Trash…` : `Move to the Trash…`, primary: true, submit: true, run: async () => {
        const done = r.to_reject ? await reject() : null;
        if (r.to_reject && !done) return false;
        if (done) load();
        const intro = done ? h("p", { class: "muted" }, `Rejected ${plural(done.rejected, "extra copy", "extra copies")} just now`
          + (done.combined ? `, after moving stars or picks onto ${plural(done.combined, "kept copy", "kept copies")}` : "") + ". ",
        done.batch ? h("button", { type: "button", class: "linkish", onclick: async (e) => { e.target.closest("dialog")?.close(); await undoable(done)(); } }, "Undo") : null) : null;
        setTimeout(() => trashDialog({ what: "rejects", set_ids: setIds, intro, onDone: () => load() }), 0);
      } });
    }
    if (actions.length === 1) actions[0].label = "Close";
    dialog(r.to_reject ? "Reject the extra copies" : "Extra copies", h("div", { class: "stack-v" },
      h("p", {}, h("b", {}, what), ` in ${plural(r.sets, "set")}, ${scope}.`,
        r.already ? (r.to_reject ? ` ${n(r.already)} of them ${agree(r.already, "is", "are")} rejected already.` : " All of them are rejected already.") : ""),
      folderLosses(r.folders),
      r.unique_skipped ? h("p", { class: "muted" }, `${plural(r.unique_skipped, "photo")} with a file that has no copy anywhere (a RAW beside a duplicated JPEG, say) ${r.unique_skipped === 1 ? "is" : "are"} left alone.`) : null,
      r.kept_skipped ? h("p", { class: "muted" }, `${plural(r.kept_skipped, "photo")} that ${r.kept_skipped === 1 ? "is" : "are"} the kept copy in another set stay${r.kept_skipped === 1 ? "s" : ""} as ${r.kept_skipped === 1 ? "it is" : "they are"}.`) : null,
      r.to_reject ? h("label", { class: "check" }, combine, h("span", {}, "First give each kept copy the best stars and pick any of its copies has")) : null,
      h("p", { class: "note" }, "Only identical files and re-saved copies; edited versions and the halves of a RAW + JPEG pair are never extra copies. Rejecting is one step that one Undo takes back. ",
        canTrash ? "Moving to the Trash checks each file and its kept copy first, and asks you to type the count."
          : "Moving them to the Trash is done at this Mac, by its owner.")), actions);
  }

  function verifyDialog() {
    const ta = h("textarea", { class: "input", rows: 8, placeholder: "Paste the downloaded list of extra copies here (one path per line)", "aria-label": "List of extra copies",
      style: { height: "160px", padding: "8px" } });
    const out = h("div", { class: "result", hidden: true });
    dialog("Check before deleting", h("div", { class: "stack-v" },
      h("p", { class: "note" }, "For each file: is it still an extra copy, and is its kept copy still on disk and identical? Files are only read."), ta, out), [
      { label: "Close" },
      { label: "Check", primary: true, submit: true, run: async () => {
        out.hidden = false; out.className = "result"; out.textContent = "Checking…";
        try {
          const r = await api(P(p, "/dupes/verify"), { method: "POST", body: { paths: ta.value } });
          out.className = r.problems.length ? "result err" : "result";
          clear(out).append(h("b", {}, `${n(r.ok)} of ${n(r.results.length)} safe to remove.`),
            r.problems.slice(0, 30).map((x) => h("div", { class: "path" }, `keep: ${x.path} — ${x.why}`)));
        } catch (e) { out.className = "result err"; out.textContent = e.message; }
        return false;
      } },
    ]);
  }

  // ------------------------------------------------------------ selections
  function selectionList() {
    const host = h("div", { class: "muted" }, "Loading…");
    api(P(p, "/selections")).then((d) => {
      const lib = (q) => `#/p/${encodeURIComponent(p)}/library?${q}`;
      const sel = d.selections.map((s) => {
        const also = JSON.parse(s.also_in || "[]");
        const linkable = s.identical === s.matched;
        return h("section", { class: "sel-card", tabindex: "-1" },
          h("div", { class: "sel-main" },
            h("div", {}, h("div", { class: "sel-name" }, pathEl(s.folder)),
              h("div", { class: "muted" }, "picked from ", pathEl(s.parent), ` (${plural(s.parent_files, "photo")})`),
              also.length ? h("div", { class: "faint small" }, "also found in ", also.map((a) => rel(a)).join(", ")) : null),
            h("div", { class: "sel-figs" },
              h("b", {}, bytes(s.identical_bytes)), h("span", {}, "stored twice"),
              h("span", { class: "muted" }, `${n(s.matched)} of ${plural(s.folder_files, "photo")} ${agree(s.matched, "is a copy", "are copies")}` + (linkable ? "" : ` · ${n(s.matched - s.identical)} re-saved, not identical`)))),
          h("div", { class: "row" },
            s.album_id ? h("a", { class: "btn small", href: lib(`album=${s.album_id}`) }, icon("folder"), `View album (${n(s.album_items)})`) : null,
            s.album_id ? h("button", { class: "btn small", type: "button", onclick: () => linkDialog(s) }, icon("similar"), "Make link folder on this Mac…") : null,
            h("a", { class: "btn small ghost", href: P(p, `/selections/${s.id}/plan.csv`) }, icon("download"), "Link plan (CSV)")));
      });
      const mir = d.mirrors.map((s) => h("div", { class: "sel-mirror" },
        h("span", { class: "pct full" }, "="),
        h("div", {}, pathEl(s.folder), h("div", { class: "muted" }, "and ", pathEl(s.parent))),
        h("span", { class: "muted" }, s.identical ? `${plural(s.identical, "identical photo")} · ${bytes(s.identical_bytes)} twice`
          : `${plural(s.matched, "photo")} saved differently (not identical files)`)));
      host.replaceWith(h("div", { class: "stack-v" },
        h("p", { class: "note" }, "A selection is a folder you picked from another one, kept as full copies. Each is saved as a JoeyLab album that points at the originals, so the selection is kept even if you later delete the copies. Nothing here changes your drive."),
        d.selections.length ? sel : h("div", { class: "empty" }, h("h3", {}, "No selection folders found")),
        d.mirrors.length ? h("section", { class: "section" }, h("h2", {}, "Whole folders copied twice"), mir) : null));
    }).catch((e) => { host.textContent = e.message; });
    return host;
  }

  function linkDialog(s) {
    const leafName = s.folder.split("/").pop();
    const dest = h("input", { class: "input mono", id: "link-dest", value: `${store_home()}/Pictures/JoeyLab/Selections/${leafName}` });
    const out = h("div", { class: "result", hidden: true });
    dialog("Make a link folder on this Mac", h("div", { class: "stack-v" },
      h("p", { class: "note" }, `Creates a folder of shortcuts to the ${n(s.album_items)} original photos, named as they are in “${leafName}”. It's made on this Mac's own disk; your external drive isn't written to. The shortcuts work while the drive is connected.`),
      h("label", { class: "field" }, h("span", {}, "Folder"), dest), out), [
      { label: "Cancel" },
      { label: "Make link folder", primary: true, submit: true, run: async () => {
        try {
          const r = await api(P(p, `/albums/${s.album_id}/linkfolder`), { method: "POST", body: { dest: dest.value.trim() } });
          toast(`Made ${plural(r.made, "link")} in ${r.dest}` + (r.skipped ? ` (${r.skipped} already there)` : ""));
          return true;
        } catch (e) { out.hidden = false; out.className = "result err"; out.textContent = e.message; return false; }
      } },
    ]);
  }

  // ------------------------------------------------------------ all sets
  function setList() {
    const host = h("div");
    const foot = h("div", { class: "row" });
    let shown = 0;
    async function more() {
      clear(foot).append(h("span", { class: "muted" }, "Loading…"));
      let d;
      try { d = await api(P(p, "/dupes"), { params: { kinds: [...st.kinds].join(","), limit: LIST_PAGE, offset: shown } }); }
      catch (e) { clear(foot).append(h("div", { class: "result err" }, e.message)); return; }
      d.sets.forEach((s) => remember(s.members.map((m) => m.row).filter(Boolean)));
      host.append(...d.sets.map(setCard));
      shown += d.sets.length;
      const left = data.totals.sets - shown;
      clear(foot).append(left > 0 && d.sets.length ? [
        h("span", { class: "muted" }, `Showing ${n(shown)} of ${plural(data.totals.sets, "set")}.`),
        h("button", { class: "btn small", type: "button", onclick: more }, `Show ${n(Math.min(left, LIST_PAGE))} more`)] : null);
    }
    more();
    return h("div", {}, host, foot);
  }

  function setCard(s) {
    const info = KINDS.find((k) => k[0] === s.kind);
    const review = s.kind === "pair" || s.kind === "export";
    return h("section", { class: "dup-set", tabindex: "-1" },
      h("header", {}, h("span", { class: "pill neutral", title: info[2] }, info[1]), h("span", { class: "muted" }, plural(s.members.length, "file"))),
      h("div", { class: "dup-members" }, s.members.map((m, i) => h("div", { class: "dup-card" },
        m.row ? thumbButton(m.row, () => openLoupe({ ids: s.members.filter((x) => x.row).map((x) => x.row.id), index: i })) : null,
        h("div", { class: "row", style: { gap: "6px" } },
          review ? h("span", { class: "pill neutral" }, m.path.split(".").pop().toUpperCase())
            : h("span", { class: `pill ${m.keep ? "pick" : "neutral"}` }, m.keep ? "Keep" : "Extra"),
          m.row?.pick === -1 ? h("span", { class: "pill reject" }, "Rejected") : null,
          h("span", { class: "muted" }, bytes(m.size))),
        h("div", { class: "path" }, pathEl(m.path))))));
  }

  // J and K: from one folder group (or set, or selection) of the tab in view to the next; Enter opens it.
  const onKey = groupKeys(() => [...page.querySelectorAll({ folders: ".dup-group", sets: ".dup-set", selections: ".sel-card" }[st.tab])], {
    focusOf: (el) => el.querySelector(":scope > .dup-group-head") || el,
    open: (el) => (el.querySelector(".dup-card img") || el.querySelector("a[href*='album=']"))?.click(),
  });

  load();
  return { refresh: load, onKey, keys: KEYS };
}
