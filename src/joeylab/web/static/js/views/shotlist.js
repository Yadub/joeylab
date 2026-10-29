// The shot list from Cull: which must-have moments a pick covers, which are still to cover, and
// saying the frame in view covers one ("covers this shot") where search can't tell.

import { api, P } from "../api.js";
import { row, store } from "../store.js";
import { agree, clear, dialog, h, n, plural, toast } from "../ui.js";

/** An item's state in words: covered by picks, tagged but not picked yet, or not covered yet. */
export function shotState(it) {
  if (it.done) return `${plural(it.covered.length, "pick")} ${agree(it.covered.length, "covers", "cover")} this`;
  const waiting = it.tagged.filter((id) => !it.covered.includes(id)).length;
  if (waiting) return `Tagged in ${plural(waiting, "frame")}, not picked yet`;
  if (it.covered.length) return `${plural(it.covered.length, "pick")} of ${n(it.min_count)} wanted`;
  return it.candidates.length ? "Not picked yet; these look right:" : "Not covered yet";
}

/** "3 of 12 covered by a pick · 9 to go". */
export const shotSummary = (items) => {
  const done = items.filter((i) => i.done).length;
  return `${n(done)} of ${n(items.length)} covered by a pick${done < items.length ? ` · ${n(items.length - done)} to go` : ""}`;
};

/** The shot list, with a box per item to say whether the photo `id` covers it. Each tick is saved
 * at once; `onChange(items)` follows every change. Keys 1–9 tick the first nine. */
export async function coverDialog(id, { onChange } = {}) {
  const p = store.project;
  let items;
  try { items = (await api(P(p, "/shotlist"))).items; } catch (e) { toast(e.message, { kind: "error" }); return; }
  if (!items.length) {
    // An archive's Home has no shot list: it's a shoot's, so say how to make this library one.
    const archive = store.settings?.purpose === "archive";
    toast(archive ? "A shot list is for a shoot, and this library is an archive. To use one, make it a shoot with the button at the top of Home."
      : "This library has no shot list yet: add the must-have moments on Home.",
    { action: () => { location.hash = `#/p/${encodeURIComponent(p)}/home${archive ? "" : "?tab=shotlist"}`; }, actionLabel: "Home", ...(archive ? { ms: 8000 } : {}) });
    return;
  }
  const r = row(id) || {};
  const summary = h("p", { class: "muted" });
  const list = h("div", { class: "cover-list" });
  let boxes = [], states = [], rows = [];
  // The rows are made once (a box keeps the focus while ticking); their words follow each change.
  const build = () => {
    boxes = items.map((it, k) => h("input", { type: "checkbox", checked: it.tagged.includes(id), onchange: () => toggle(k, boxes[k].checked) }));
    states = items.map(() => h("span", { class: "muted" }));
    rows = items.map((it, k) => h("label", { class: "check cover-item" }, boxes[k],
      h("span", { class: "grow" }, h("b", {}, it.title), states[k]), k < 9 ? h("kbd", { "aria-hidden": "true" }, String(k + 1)) : null));
    clear(list).append(rows);
  };
  const paint = () => {
    summary.textContent = shotSummary(items);
    items.forEach((it, k) => { states[k].textContent = shotState(it); rows[k].classList.toggle("done", it.done); });
  };
  async function toggle(k, on) {
    try {
      await api(P(p, `/shotlist/${items[k].id}/tag`), { method: "POST", body: { ids: [id], on } });
      const was = items.length;
      items = (await api(P(p, "/shotlist"))).items;
      if (items.length !== was) build();                 // the list changed on another page meanwhile
    } catch (e) { toast(e.message, { kind: "error" }); }
    paint();
    onChange?.(items);
  }
  build();
  paint();
  const picked = r.pick === 1 || (r.rating || 0) >= 4;
  const dlg = dialog("Shot list", h("div", { class: "stack-v" },
    h("p", { class: "note" }, `Tick the moments ${r.stem || "this photo"} shows.`,
      picked ? "" : " It covers them once it's picked."),
    summary, list), [
    { label: "Edit the list", run: () => { location.hash = `#/p/${encodeURIComponent(p)}/home`; } },
    { label: "Done", primary: true },
  ]);
  setTimeout(() => boxes[0]?.focus(), 0);            // the first box, not the list around them
  dlg.addEventListener("keydown", (e) => {
    const k = +e.key;
    if (!(k >= 1 && k <= 9) || e.metaKey || e.ctrlKey || e.altKey || !boxes[k - 1]) return;
    e.preventDefault();
    boxes[k - 1].checked = !boxes[k - 1].checked;
    toggle(k - 1, boxes[k - 1].checked);
  });
  return dlg;
}
