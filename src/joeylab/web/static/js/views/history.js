// History: every step (stars, picks, labels, keywords, dates, undos), newest first, filterable by
// kind and date, each with its photos: show them in the Library, undo or redo. And, for the Mac's
// owner, the moves to the Trash, with Put Back for files still in it.

import { api, img, P } from "../api.js";
import { emit, on, redo, remember, row, store, undo } from "../store.js";
import { bytes, choiceSeg, clear, dialog, h, icon, n, plural, tabBar, toast } from "../ui.js";
import { movesTab } from "./moves.js";

const KINDS = [["", "Everything"], ["picks", "Picks and rejects"], ["stars", "Stars"], ["labels", "Labels"], ["keywords", "Keywords"],
  ["dates", "Dates"], ["undo", "Undo and redo"]];
const SOURCES = { web: "", cull: "in Cull", mcp: "by an assistant", undo: "", import: "imported", accept: "accepted suggestions" };
const PAGE = 50;

export function mount(root, ctx) {
  const p = ctx.project;
  const owner = !!store.meta?.can_trash;
  let tab = owner && ["trash", "moves"].includes(ctx.params.get("tab")) ? ctx.params.get("tab") : "changes";
  const page = h("div", { class: "page history-page" });
  root.append(page);
  const lib = (q) => `#/p/${encodeURIComponent(p)}/library?${new URLSearchParams(q).toString()}`;
  let focusAt = -1;
  let quiet = false;                 // an Undo pressed in a row: that row changes, not the whole page
  let redoHere = null;               // the Redo button's place at the top of the list

  function params() {
    const q = { tab: tab === "changes" ? "" : tab };
    for (const k of ["kind", "since", "until"]) if (ctx.params.get(k)) q[k] = ctx.params.get(k);
    return q;
  }
  function setFilter(k, v) {
    const q = { ...params(), [k]: v };
    ctx.setParams(q);
    render();
  }

  function render() {
    const body = h("div", { class: "stack-v", style: { gap: "18px" } });
    clear(page).append(
      h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "History"),
        h("div", { class: "sub" }, "Every change to stars, picks, labels, keywords and dates, newest first. Undo any step: whatever you've changed since on the same photos stays as it is.")),
        owner ? h("div", { class: "actions" }, tabBar([["changes", "Changes"], ["trash", "Moved to the Trash"], ["moves", "Moves and renames"]],
          tab, (k) => { tab = k; ctx.setParams(params()); render(); }, { label: "Show", id: "history", panel: body })) : null),
      body);
    if (tab === "trash") trash(body);
    else if (tab === "moves") movesTab(body);
    else changes(body);
  }

  // ------------------------------------------------------------ changes
  async function changes(body) {
    const kind = ctx.params.get("kind") || "";
    const since = ctx.params.get("since") || "", until = ctx.params.get("until") || "";
    const list = h("div", { class: "history-list", role: "list" });
    const moreBtn = h("button", { class: "btn", hidden: true }, "Show more");
    const redoSlot = redoHere = h("span");
    const total = h("span", { class: "muted" });
    const day = (id, label, value, other) => h("label", { class: "field-inline" }, h("span", { class: "muted" }, label),
      h("input", { type: "date", class: "input", id, value, "aria-label": label,
        onchange: (e) => { const v = e.target.value; if (!v || /^\d{4}-\d{2}-\d{2}$/.test(v)) setFilter(other, v); } }));
    body.append(
      h("div", { class: "row history-filters" },
        choiceSeg(KINDS, kind, (k) => setFilter("kind", k), { label: "Kind of change" }),
        day("h-since", "From", since, "since"), day("h-until", "to", until, "until"),
        kind || since || until ? h("button", { class: "btn ghost small", onclick: () => { ctx.setParams({ tab: "" }); render(); } }, "Clear") : null,
        h("span", { class: "spacer" }), total, redoSlot),
      list, h("div", { class: "row", style: { justifyContent: "center" } }, moreBtn));
    let offset = 0;
    let lastDay = null;
    const load = async () => {
      moreBtn.disabled = true;
      let d;
      try { d = await api(P(p, "/history/steps"), { params: { kind, since, until, offset, limit: PAGE } }); }
      catch (e) { list.append(h("div", { class: "result err" }, e.message)); return; }
      remember(d.rows);
      if (!offset) {
        total.textContent = plural(d.total, "step");
        clear(redoSlot).append(d.redo ? h("button", { class: "btn small", title: "Put back what you last undid (⇧U)",
          onclick: () => redo() }, icon("refresh"), "Redo") : null);
        if (!d.steps.length) list.append(h("div", { class: "empty" }, h("h3", {}, kind || since || until ? "No changes like that" : "No changes yet"),
          h("p", {}, "Everything you decide shows up here, and can be undone, even after a reload.")));
      }
      for (const s of d.steps) {
        const dd = dayLabel(s.ts);
        if (dd !== lastDay) { lastDay = dd; list.append(h("h2", { class: "history-day" }, dd)); }
        list.append(stepRow(s, d.redo));
      }
      offset += d.steps.length;
      moreBtn.hidden = offset >= d.total;
      moreBtn.disabled = false;
      moreBtn.textContent = `Show more (${n(d.total - offset)} left)`;
    };
    moreBtn.onclick = load;
    await load();
  }

  function stepRow(s, redoable) {
    const isUndo = s.source === "undo";
    const what = [plural(s.n, "photo"), new Date(s.ts * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }),
      SOURCES[s.source] ?? s.source, s.viewer || "", s.undone ? (isUndo ? "redone since" : "undone") : ""].filter(Boolean).join(" · ");
    const thumbs = h("div", { class: "history-thumbs", "aria-hidden": "true" }, s.ids.slice(0, 6).map((id) => {
      const r = row(id);
      return r ? h("img", { src: img.thumb(p, r), alt: "", loading: "lazy" }) : h("span", { class: "gone" });
    }));
    return h("div", { class: `history-row ${s.undone ? "undone" : ""}`, role: "listitem", tabindex: "-1", dataset: { batch: s.batch } },
      thumbs,
      h("div", { class: "grow history-what" }, h("b", {}, s.label), h("div", { class: "muted small" }, what)),
      h("div", { class: "history-acts" },
        h("a", { class: "btn ghost small", href: lib({ step: s.batch, all: "1" }), title: "Open the Library on exactly these photos" }, "Show these photos"),
        !s.undone && !isUndo ? h("button", { class: "btn small", onclick: (e) => undoStep(s, e.currentTarget.closest(".history-row")) }, icon("undo"), "Undo") : null,
        isUndo && s.batch === redoable ? h("button", { class: "btn small", onclick: () => redo() }, icon("refresh"), "Redo") : null));
  }

  /** Undo a step from its row: the row says it's undone and the page stays where it was (with the
   * steps loaded so far), rather than starting again from the top. */
  async function undoStep(s, rowEl) {
    quiet = true;
    let res;
    try { res = await undo(s.batch); } finally { quiet = false; }
    if (!res?.undone || !rowEl?.isConnected) return;
    const now = stepRow({ ...s, undone: 1 }, null);
    rowEl.replaceWith(now);
    now.focus({ preventScroll: true });
    if (redoHere) clear(redoHere).append(h("button", { class: "btn small", title: "Put back what you last undid (⇧U)", onclick: () => redo() }, icon("refresh"), "Redo"));
  }

  // ------------------------------------------------------------ moves to the Trash
  async function trash(body) {
    body.append(h("p", { class: "note" }, "Files you moved to the Trash from JoeyLab, from its Trash logs. Put Back moves a file that's still in the Trash back to where it was, and it shows in the library again. If something else is at that place now, both stay as they are."),
      h("div", { class: "muted" }, "Loading…"));
    let d;
    try { d = await api(P(p, "/history/trash")); } catch (e) { clear(body).append(h("div", { class: "result err" }, e.message)); return; }
    body.lastChild.remove();
    if (!d.moves.length) {
      body.append(h("div", { class: "empty" }, h("h3", {}, "Nothing moved to the Trash"), h("p", {}, "When you move rejected photos, extra copies or empty folders to the Trash, each move is listed here.")));
      return;
    }
    body.append(d.moves.map((m) => trashCard(m)));
  }

  function trashCard(m) {
    const files = m.what === "files";
    const items = h("div", { class: "trash-items", hidden: true });
    const left = m.items.filter((it) => it.state === "in_trash");
    const toggle = h("button", { class: "btn ghost small", "aria-expanded": "false", onclick: (e) => {
      items.hidden = !items.hidden;
      e.currentTarget.setAttribute("aria-expanded", items.hidden ? "false" : "true");
      e.currentTarget.textContent = items.hidden ? `Show the ${plural(m.moved, files ? "file" : "folder")}` : "Hide the list";
      if (!items.childElementCount) items.append(m.items.slice(0, 500).map((it) => h("div", { class: "trash-item" },
        h("span", { class: "grow mono", title: it.path }, short(it.path)),
        h("span", { class: `pill ${it.state === "in_trash" ? "neutral" : it.state === "back" ? "accent" : ""}` },
          { in_trash: "In the Trash", back: "Back in place", gone: "Not in the Trash any more" }[it.state]),
        it.state === "in_trash" ? h("button", { class: "btn ghost small", onclick: () => putBack(m, [it.path]) }, "Put Back") : null)),
      m.items.length > 500 ? h("p", { class: "muted" }, `…and ${n(m.items.length - 500)} more.`) : null);
    } }, `Show the ${plural(m.moved, files ? "file" : "folder")}`);
    return h("section", { class: "trash-move" },
      h("div", { class: "row" }, icon(files ? "close" : "folder"),
        h("div", { class: "grow" }, h("b", {}, `Moved ${plural(m.moved, files ? "file" : "empty folder")} to the Trash`),
          h("div", { class: "muted small" }, [new Date(m.ts * 1000).toLocaleString(undefined, { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }),
            m.bytes ? bytes(m.bytes) : "", left.length === m.moved ? "all still in the Trash" : left.length ? `${n(left.length)} still in the Trash` : "none left in the Trash"]
            .filter(Boolean).join(" · "))),
        left.length ? h("button", { class: "btn small", onclick: () => putBack(m, null, left) }, `Put back ${left.length === m.moved ? "all" : n(left.length)}`) : null,
        toggle),
      items);
  }

  function putBack(m, paths, left = null) {
    const which = paths || (left || []).map((it) => it.path);
    const k = which.length;
    dialog(`Put back ${plural(k, m.what === "files" ? "file" : "folder")}?`, h("div", { class: "stack-v" },
      h("p", {}, k === 1 ? "It goes back from the Trash to where it was:" : "They go back from the Trash to where they were, for example:"),
      h("div", { class: "stack-v" }, which.slice(0, 5).map((x) => h("code", { class: "mono small" }, x))),
      k > 5 ? h("p", { class: "muted" }, `…and ${n(k - 5)} more.`) : null,
      h("p", { class: "note" }, "Only to their own places in this library's folders, and never over anything that is there now.")), [
      { label: "Cancel" },
      { label: "Put back", primary: true, run: async () => {
        try {
          const r = await api(P(p, "/history/putback"), { method: "POST", body: { log: m.log, paths: which } });
          emit("history");
          store.overview = null;
          const stayed = r.results.filter((x) => x.result !== "put back");
          toast(r.moved ? `Put back ${plural(r.moved, m.what === "files" ? "file" : "folder")}` : "Nothing was put back",
            r.moved && m.what === "files" ? { action: () => { location.hash = lib({}); }, actionLabel: "Library" } : {});
          if (stayed.length) {
            dialog(`${n(stayed.length)} stayed in the Trash`, h("div", { class: "stack-v" },
              stayed.slice(0, 20).map((x) => h("div", {}, h("code", { class: "mono small" }, x.path), h("div", { class: "muted small" }, x.result)))),
            [{ label: "Close", primary: true }]);
          }
          later();
        } catch (e) { toast(e.message, { kind: "error" }); }
      } }]);
  }

  render();
  // Undo and Redo (here, from U and ⇧U, or anywhere else) and Put Back show at once.
  let soon = 0;
  function later() { clearTimeout(soon); soon = setTimeout(render, 60); }
  const off = on((type) => { if (type === "history" && !quiet) later(); });

  return {
    // J and K (or the arrows) move between steps; Enter shows the photos of the one in focus.
    onKey(e) {
      if (e.metaKey || e.ctrlKey || e.altKey) return false;
      const rows = [...page.querySelectorAll(".history-row")];
      if (!rows.length) return false;
      const k = e.key;
      if (k === "j" || k === "k" || k === "ArrowDown" || k === "ArrowUp") {
        focusAt = Math.max(0, Math.min(rows.length - 1, focusAt + (k === "j" || k === "ArrowDown" ? 1 : -1)));
        rows[focusAt].focus();
        rows[focusAt].scrollIntoView({ block: "nearest" });
        return true;
      }
      if (k === "Enter" && rows.includes(document.activeElement)) { document.activeElement.querySelector("a")?.click(); return true; }
      return false;
    },
    refresh: render,
    unmount() { off(); clearTimeout(soon); },
  };
}

/** A file's place, by its last folders and name ("…/2024/Japan/DSCF0012.JPG"); the whole path is its tooltip. */
function short(path) {
  const parts = path.split("/").filter(Boolean);
  return parts.length > 3 ? `…/${parts.slice(-3).join("/")}` : path;
}

function dayLabel(ts) {
  const d = new Date(ts * 1000);
  const today = new Date();
  const y = new Date(today);
  y.setDate(today.getDate() - 1);
  if (d.toDateString() === today.toDateString()) return "Today";
  if (d.toDateString() === y.toDateString()) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long", year: "numeric" });
}
