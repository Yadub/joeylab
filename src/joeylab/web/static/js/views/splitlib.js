// Moving folders to another library (split.py), for the Mac's owner: from a folder's ⋯ menu or several
// chosen folders in Library › Folders, and from Settings › Library. The plan is shown first ("Move
// 52,340 photos (48 GB of previews already made, …) from … to a new shoot library “weddings”."), and
// every move can be moved back. Settings also lists the folders a library leaves out.

import { api, P } from "../api.js";
import { emit, store } from "../store.js";
import { ago, choiceSeg, clear, debounce, dialog, h, icon, plural, toast } from "../ui.js";
import { pickFolder } from "./moves.js";
import { slug } from "./projects.js";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const lib = (name) => `#/p/${encodeURIComponent(name)}`;
const lastName = (path) => path.split("/").filter(Boolean).pop() || path;

/** Follow a move running in the background until it's done. */
async function follow(job, onStage) {
  while (job.state === "moving" || job.state === "waiting") {
    onStage?.(job.state === "waiting" ? "Waiting for an update of one of the libraries to finish…" : `${job.stage}…`);
    await sleep(job.state === "waiting" ? 1000 : 400);
    job = await api(`/api/split/${job.token}`);
  }
  return job;
}

/** After a move: say what happened, offer to open the other library or move them back. */
function moved(op, onDone) {
  store.overview = null;
  store.overviewStale = true;
  emit("moved", op);
  const c = op.counts || {};
  const back = !!op.back_of;
  const msg = back ? `Moved ${plural(c.photos || 0, "photo")} back to ${op.dst}.`
    : `Moved ${plural(c.photos || 0, "photo")} to ${op.dst}. Both libraries are being regrouped in the background.`;
  const actions = op.dst === store.project ? [] : [{ label: "Open", run: () => { location.hash = lib(op.dst); } }];
  if (!back) actions.push({ label: "Move back", run: () => moveBack(op, onDone) });
  toast(msg, { actions, ms: 12000 });
  onDone?.(op);
}

/** Move an earlier move back (the same move in reverse), after saying what it does. */
export function moveBack(op, onDone) {
  const c = op.counts || {};
  const status = h("div", { class: "muted", hidden: true, role: "status" });
  const dlg = dialog("Move them back?", h("div", { class: "stack-v" },
    h("p", { class: "move-what" }, `Move ${plural(c.photos || 0, "photo")} back from ${op.dst} to ${op.src}, with their decisions, names and history`
      + (op.new ? `. “${op.dst}” was made by the move, so it's removed once it's empty (a backup of its catalog is kept).` : ".")),
    h("p", { class: "note" }, "Only JoeyLab's catalogs change: no photo file is touched. Both catalogs are backed up first."),
    status), [
    { label: "Cancel" },
    { label: "Move them back", primary: true, run: async () => {
      status.hidden = false;
      status.className = "muted";
      try {
        let job = await api(`/api/split/${op.id}/back`, { method: "POST", body: {} });
        job = await follow(job, (s) => { status.textContent = s; });
        if (job.state === "error") throw new Error(job.error);
        dlg.close();
        moved(job.result, onDone);
      } catch (e) { status.className = "result err"; status.textContent = e.message; }
      return false;
    } },
  ]);
}

/** The dialog: which folders, where they go (a new library with its purpose, or one you have), the plan
 * in words, and then the move. `folders`: paths chosen already (more can be added). */
export async function moveToLibrary({ folders = [], onDone } = {}) {
  const p = store.project;
  const chosen = [...new Set(folders)];
  let libs = [];
  try { libs = (await api("/api/projects")).filter((x) => x.name !== p && !x.error); } catch { /* new only */ }
  let where = "new", purpose = "shoot", plan = null, seq = 0;
  const list = h("div", { class: "split-folders", role: "list", "aria-label": "Folders to move" });
  const nameIn = h("input", { class: "input", id: "split-name", placeholder: "weddings", autocomplete: "off", spellcheck: "false" });
  const savedAs = h("span", { class: "muted np-saved", "aria-live": "polite" });
  const libSel = h("select", { class: "input", id: "split-lib" }, libs.map((x) => h("option", { value: x.name },
    `${x.name} · ${plural(x.photos, "photo")}${x.purpose ? ` · ${x.purpose === "shoot" ? "a shoot" : "an archive"}` : ""}`)));
  const whereHost = h("div", { class: "stack-v" });
  const planBox = h("div", { class: "split-plan", "aria-live": "polite" });
  const status = h("div", { class: "muted", hidden: true, role: "status" });
  const lighter = h("div", { class: "split-lighter note" });
  const dlg = dialog("Move to another library", h("div", { class: "stack-v split-dialog" },
    h("p", { class: "note" }, "The folders become another library's, with their own purpose, rules, people, events and decisions. "
      + "Everything JoeyLab knows about their photos goes with them, so nothing is read again; no photo file is touched."),
    h("div", { class: "field" }, h("span", { class: "label-line" }, "Folders"), list),
    h("div", { class: "field" }, h("span", { class: "label-line" }, "Where they go"), whereHost),
    planBox, lighter, status), [{ label: "Cancel" }]);
  dlg.classList.add("dlg-wide");
  const go = h("button", { type: "button", class: "btn primary", disabled: true, onclick: run }, "Move");
  dlg.querySelector(".dlg-actions").append(go);

  function paintFolders() {
    clear(list).append(
      chosen.map((f) => h("div", { class: "split-folder", role: "listitem" }, icon("folder"),
        h("span", { class: "grow" }, h("b", {}, lastName(f)), h("span", { class: "faint mono split-path", title: f }, h("bdi", {}, f))),
        h("button", { type: "button", class: "btn ghost small icon-only", "aria-label": `Don't move ${lastName(f)}`,
          onclick: () => { chosen.splice(chosen.indexOf(f), 1); paintFolders(); refresh(); } }, icon("close")))),
      h("div", {}, h("button", { type: "button", class: "btn small", onclick: addFolder }, icon("plus"), chosen.length ? "Add another folder…" : "Choose a folder…")));
    paintLighter();
  }

  async function addFolder() {
    const f = await pickFolder({ title: "Choose a folder to move", okLabel: "Add", start: chosen[chosen.length - 1] || "",
      intro: "Its subfolders go with it.", disable: (x) => (chosen.some((c) => x === c || x.startsWith(c + "/")) ? "Chosen already." : null) });
    if (f && !chosen.includes(f)) { chosen.push(f); paintFolders(); refresh(); }
  }

  function paintWhere() {
    const seg = choiceSeg([["new", "A new library"], ...(libs.length ? [["existing", "A library you have"]] : [])], where,
      (k) => { where = k; paintWhere(); refresh(); }, { label: "Where they go" });
    clear(whereHost).append(seg, where === "new"
      ? h("div", { class: "form-grid" },
        h("label", { class: "field" }, h("span", {}, "Name", savedAs), nameIn),
        h("div", { class: "field span-2" }, h("span", {}, "It's"),
          choiceSeg([["shoot", "A shoot to cull"], ["archive", "An archive to tidy"]], purpose, (k) => { purpose = k; paintWhere(); refresh(); },
            { label: "What the new library is for" })))
      : h("label", { class: "field" }, h("span", {}, "Library"), libSel));
  }

  function paintLighter() {
    clear(lighter).append("Only want to see them together, not treat them differently? A smart album shows ",
      chosen.length === 1 ? "this folder" : "these folders", " without changing anything. ",
      chosen.length ? h("button", { type: "button", class: "linkish", onclick: smartAlbum }, "Make a smart album instead") : null);
  }

  async function smartAlbum() {
    const name = where === "new" && nameIn.value.trim() ? nameIn.value.trim() : chosen.length === 1 ? lastName(chosen[0]) : "Chosen folders";
    try {
      await api(P(p, "/albums"), { method: "POST", body: { name, smart: { folder: chosen.join("\n") } } });
      dlg.close();
      toast(`Made the smart album “${name}”. It keeps following ${chosen.length === 1 ? "the folder" : "the folders"}.`,
        { actions: [{ label: "Albums", run: () => { location.hash = `${lib(p)}/albums`; } }] });
    } catch (e) { toast(e.message, { kind: "error" }); }
  }

  const target = () => (where === "new" ? slug(nameIn.value) : libSel.value);

  const refresh = debounce(async () => {
    const mine = ++seq;
    plan = null;
    go.disabled = true;
    const to = target();
    const s = slug(nameIn.value);
    savedAs.textContent = nameIn.value.trim() && s !== nameIn.value.trim() ? (s ? ` · saved as ${s}` : " · use at least one letter or digit") : "";
    if (!chosen.length || !to) {
      clear(planBox).append(h("p", { class: "muted" }, !chosen.length ? "Choose the folders to move." : "Give the new library a name."));
      return;
    }
    clear(planBox).append(h("p", { class: "muted" }, "Working out what moves…"));
    try {
      const r = await api(P(p, "/split/plan"), { method: "POST", body: { folders: chosen, to, purpose: where === "new" ? purpose : null } });
      if (mine !== seq) return;
      plan = r;
      const more = [r.decisions && plural(r.decisions, "decision"), r.history && plural(r.history, "history entry", "history entries"),
        r.albums && plural(r.albums, "album"), r.keywords && plural(r.keywords, "keyword"),
        r.people_names?.length && `named: ${r.people_names.join(", ")}${r.people > r.people_names.length ? "…" : ""}`].filter(Boolean);
      clear(planBox).append(h("p", { class: "move-what" }, r.what),
        more.length ? h("p", { class: "muted small" }, more.join(" · ")) : null,
        h("p", { class: "note" }, "Both catalogs are backed up first. Afterwards you can move them back from the message that follows or from Settings › Library."));
      go.disabled = false;
    } catch (e) {
      if (mine !== seq) return;
      clear(planBox).append(h("div", { class: "result err" }, e.message));
    }
  }, 300);

  async function run() {
    if (!plan) return;
    go.disabled = true;
    status.hidden = false;
    status.className = "muted";
    try {
      let job = await api(P(p, "/split/run"), { method: "POST", body: { folders: chosen, to: plan.dst, purpose: plan.new ? purpose : null } });
      job = await follow(job, (s) => { status.textContent = s; });
      if (job.state === "error") throw new Error(job.error);
      dlg.close();
      moved(job.result, onDone);
    } catch (e) { status.className = "result err"; status.textContent = e.message; go.disabled = false; }
  }

  nameIn.addEventListener("input", refresh);
  libSel.addEventListener("change", refresh);
  paintFolders();
  paintWhere();
  refresh();
  if (!chosen.length) addFolder();
  else setTimeout(() => nameIn.focus(), 0);
}

/** Settings › Library: moving folders out, and the moves to and from this library. */
export async function splitSection(body, { refresh } = {}) {
  const p = store.project;
  const sec = h("section", { class: "section" }, h("h2", {}, "Move folders to another library"),
    h("p", { class: "note" }, "When some folders should be treated differently (wedding shoots to cull among everyday photos to tidy), "
      + "move them to a library of their own. Their decisions, names, faces and history go with them; nothing is read again and no photo file is touched."),
    h("div", { class: "row" },
      h("button", { class: "btn", type: "button", onclick: () => moveToLibrary({ onDone: refresh }) }, icon("folder"), "Choose folders…"),
      h("a", { class: "btn ghost", href: `${lib(p)}/library?view=folders` }, "Choose them in Library › Folders")));
  body.append(sec);
  let d;
  try { d = await api(P(p, "/split")); } catch { return; }
  if (!d.moves.length) return;
  sec.append(h("h3", {}, "Moves between libraries"), h("div", { class: "history-list", role: "list" }, d.moves.map((m) => {
    const out = m.src === p;
    const c = m.counts || {};
    const what = out ? `${plural(c.photos || 0, "photo")} moved to ${m.dst}` : `${plural(c.photos || 0, "photo")} moved here from ${m.src}`;
    const stopped = m.state === "copied" || m.state === "removed";
    const state = m.moved_back ? "moved back" : stopped ? "stopped half way" : m.state === "done" ? "" : "stopped; nothing changed";
    return h("div", { class: `history-row ${m.moved_back ? "undone" : ""}`, role: "listitem" },
      h("span", { class: "move-ico" }, icon("folder")),
      h("div", { class: "grow history-what" }, h("b", {}, what),
        h("div", { class: "muted small" }, [ago(m.ts), m.folders.map(lastName).join(", "), state].filter(Boolean).join(" · "))),
      h("div", { class: "history-acts" },
        stopped ? h("button", { class: "btn small", type: "button", onclick: async () => {
          try { await api(P(p, "/split/finish"), { method: "POST", body: { id: m.id } }); toast("Finished: the photos are in one library now."); refresh?.(); }
          catch (e) { toast(e.message, { kind: "error" }); }
        } }, "Finish it") : null,
        m.can_move_back ? h("button", { class: "btn small", type: "button", onclick: () => moveBack(m, refresh) }, icon("undo"), "Move them back") : null));
  })));
}

/** Settings › Folders: the folders this library leaves out, each with Include again. */
export async function leftOutSection(body, { refresh } = {}) {
  const p = store.project;
  let d;
  try { d = await api(P(p, "/split")); } catch { return; }
  if (!d.excluded.length) return;
  const out = h("div", { class: "result err", hidden: true });
  body.append(h("section", { class: "section" }, h("h2", {}, "Left out of this library"),
    h("p", { class: "note" }, "These folders were moved to another library, so this one's updates skip them. Including one again reads its photos afresh here; "
      + "to bring back their decisions, names and history, move them back from the other library instead."),
    h("div", { class: "stack-v" }, d.excluded.map((e) => h("div", { class: "drive-row" }, icon("folder"),
      h("span", { class: "split-left" }, h("b", {}, lastName(e.folder)), h("span", { class: "faint mono split-path", title: e.folder }, h("bdi", {}, e.folder)),
        h("span", { class: "muted small" }, e.library ? (e.there ? `In the library ${e.library} now` : `Was moved to ${e.library}, which has been removed`) : "Left out")),
      e.there ? h("a", { class: "btn small ghost", href: lib(e.library) }, `Open ${e.library}`) : null,
      h("button", { class: "btn small", type: "button", onclick: async () => {
        out.hidden = true;
        try {
          await api(P(p, "/split/include"), { method: "POST", body: { folder: e.folder } });
          toast("Included again. Reading its photos in the background.");
          refresh?.();
        } catch (err) { out.hidden = false; out.textContent = err.message; }
      } }, "Include again")))), out));
}

