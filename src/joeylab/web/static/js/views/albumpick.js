// "Add to album ▸": the albums you used last, a box to find any other (or name a new one), and
// one step to take it back. Used from the selection bar, the viewer and key B.

import { api, P } from "../api.js";
import { emit, store } from "../store.js";
import { clear, closePopover, h, icon, plural, popover, toast } from "../ui.js";

let cache = null;          // {project, at, albums}: hand-picked albums, most recently changed first

async function albums(force = false) {
  if (!force && cache && cache.project === store.project && Date.now() - cache.at < 30_000) return cache.albums;
  const d = await api(P(store.project, "/albums"), { params: { light: 1 } });
  cache = { project: store.project, at: Date.now(), albums: d.albums };
  return d.albums;
}
export function forgetAlbums() { cache = null; }

const libraryOf = (id) => `#/p/${encodeURIComponent(store.project)}/library?album=${id}`;

/** Put photos in an album: `ids`, or everything a Library filter matches (`filter`, with `count`).
 * Opens next to `anchor` (above it when there's no room below), or in the middle of the screen. */
export async function addToAlbum({ ids, filter, count, anchor, onDone } = {}) {
  if (!filter && !ids?.length) { toast("Select some photos first."); return; }
  const n = filter ? count : ids.length;
  const input = h("input", { class: "input", type: "search", placeholder: "Find an album, or name a new one", autocomplete: "off",
    "aria-label": "Album", role: "combobox", "aria-expanded": "true", "aria-controls": "album-pick-list", "aria-autocomplete": "list" });
  const list = h("div", { class: "album-pick-list", id: "album-pick-list", role: "listbox", "aria-label": "Albums" });
  const box = h("div", { class: "album-pick" },
    h("div", { class: "album-pick-head" }, h("b", {}, `Add ${plural(n || 0, "photo")} to an album`)), input, list);
  const el = popover(anchor || h("span"), box);      // (a stand-in when there's no button: any click outside closes it)
  el.classList.add("album-pick-pop");
  place(el, anchor);
  let all = [];
  let shown = [];
  let sel = 0;
  function paint() {
    const q = input.value.trim().toLowerCase();
    const hits = q ? all.filter((a) => a.name.toLowerCase().includes(q))
      .sort((a, b) => (b.name.toLowerCase().startsWith(q) - a.name.toLowerCase().startsWith(q))) : all.slice(0, 6);
    shown = hits.slice(0, 40).map((a) => ({ label: a.name, meta: plural(a.items, "photo"), run: () => add(a) }));
    if (q && !all.some((a) => a.name.toLowerCase() === q)) {
      shown.push({ label: `New album “${input.value.trim()}”`, icon: "plus", run: () => create(input.value.trim()) });
    }
    sel = Math.min(sel, Math.max(0, shown.length - 1));
    clear(list).append(!q && all.length ? h("div", { class: "album-pick-sub" }, "Recent") : null,
      shown.length ? shown.map((c, i) => h("button", { type: "button", class: `menu-item ${i === sel ? "on" : ""}`, role: "option", id: `album-opt-${i}`,
        "aria-selected": i === sel ? "true" : "false", onmouseenter: () => { sel = i; mark(); }, onclick: c.run },
      icon(c.icon || "album"), h("span", { class: "grow" }, c.label), c.meta ? h("span", { class: "meta" }, c.meta) : null))
        : h("div", { class: "muted album-pick-sub" }, all.length ? "No album by that name." : "No albums yet: type a name to make one."));
    place(el, anchor);
    mark();
  }
  function mark() {
    [...list.querySelectorAll(".menu-item")].forEach((b, i) => { b.classList.toggle("on", i === sel); b.setAttribute("aria-selected", i === sel ? "true" : "false"); });
    input.setAttribute("aria-activedescendant", shown.length ? `album-opt-${sel}` : "");
    // Keep the highlighted album in view inside the list (scrollIntoView would move the page too).
    const on = list.querySelector(".menu-item.on");
    if (on) {
      if (on.offsetTop < list.scrollTop) list.scrollTop = on.offsetTop;
      else if (on.offsetTop + on.offsetHeight > list.scrollTop + list.clientHeight) list.scrollTop = on.offsetTop + on.offsetHeight - list.clientHeight;
    }
  }
  input.addEventListener("input", () => { sel = 0; paint(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") sel = Math.min(shown.length - 1, sel + 1);
    else if (e.key === "ArrowUp") sel = Math.max(0, sel - 1);
    else if (e.key === "Enter") { shown[sel]?.run(); }
    else if (e.key === "Escape") { closePopover(); anchor?.focus?.({ preventScroll: true }); }
    else return;
    e.preventDefault(); e.stopPropagation();
    if (e.key.startsWith("Arrow")) mark();
  });
  setTimeout(() => input.focus({ preventScroll: true }), 0);
  try { all = await albums(); } catch (e) { toast(e.message, { kind: "error" }); }
  paint();
  place(el, anchor);

  async function add(a) {
    closePopover();
    try {
      const r = await api(P(store.project, `/albums/${a.id}/items`), { method: "POST", body: filter ? { filter, op: "add" } : { add: ids } });
      forgetAlbums();
      emit("albums");
      const already = (filter ? count : ids.length) - r.added;
      toast(`Added ${plural(r.added, "photo")} to “${a.name}”${already > 0 ? ` (${plural(already, "was", "were")} in it already)` : ""}`, r.added ? {
        action: async () => {
          await api(P(store.project, `/albums/${a.id}/items`), { method: "POST", body: { remove: r.added_ids } });
          forgetAlbums();
          emit("albums");
          toast(`Took them out of “${a.name}” again`);
        } } : {});
      onDone?.(r);
    } catch (e) { toast(e.message, { kind: "error" }); }
  }

  async function create(name) {
    closePopover();
    try {
      const r = await api(P(store.project, "/albums"), { method: "POST", body: filter ? { name, filter } : { name, ids } });
      forgetAlbums();
      emit("albums");
      toast(`Album “${name}” with ${plural(r.added, "photo")}`, { action: () => { location.hash = libraryOf(r.id); }, actionLabel: "Open" });
      onDone?.(r);
    } catch (e) { toast(e.message, { kind: "error" }); }
  }
}

/** Take photos out of the album being viewed (ids, or everything matching a filter). Undoable from its message. */
export async function removeFromAlbum({ album, ids, filter, onDone }) {
  try {
    const r = await api(P(store.project, `/albums/${album}/items`), { method: "POST", body: filter ? { filter, op: "remove" } : { remove: ids } });
    forgetAlbums();
    emit("albums");
    onDone?.(r);
    toast(`Took ${plural(r.removed, "photo")} out of “${r.album}”`, r.removed ? { action: async () => {
      await api(P(store.project, `/albums/${album}/items`), { method: "POST", body: { add: r.removed_ids } });
      forgetAlbums();
      onDone?.(r);
    } } : {});
  } catch (e) { toast(e.message, { kind: "error" }); }
}

/** A hand-picked album's name, when the list has been fetched. */
export function albumName(id) {
  return cache?.project === store.project ? cache.albums.find((a) => String(a.id) === String(id))?.name : undefined;
}

/** Is this a hand-picked album (one photos can be added to or taken out of)? */
export async function isManualAlbum(id) {
  try { return (await albums()).some((a) => String(a.id) === String(id)); } catch { return false; }
}

// Below its button when there's room, otherwise above it (the selection bar sits at the bottom);
// with no button, in the middle of the screen.
function place(el, anchor) {
  const w = el.offsetWidth, hgt = el.offsetHeight;
  if (!anchor) {
    Object.assign(el.style, { left: `${Math.max(16, (innerWidth - w) / 2)}px`, top: `${Math.max(16, innerHeight * 0.18)}px` });
    return;
  }
  const r = anchor.getBoundingClientRect();
  const below = r.bottom + 6 + hgt <= innerHeight - 8;
  Object.assign(el.style, { left: `${Math.max(16, Math.min(r.left, innerWidth - w - 16))}px`,
    top: `${below ? r.bottom + 6 : Math.max(8, r.top - hgt - 6)}px` });
}
