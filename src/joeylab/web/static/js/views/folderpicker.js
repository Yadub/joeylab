// Browse folders on the Mac (home folder and attached drives) and choose one.

import { api } from "../api.js";
import { clear, h, icon, plural } from "../ui.js";

export function folderPicker({ start, onChoose, chooseLabel = "Use this folder" }) {
  const crumbs = h("div", { class: "crumbs" });
  const note = h("div", { class: "result", hidden: true });
  const dirs = h("div", { class: "dirs" });
  const here = h("span", { class: "muted small" });
  const choose = h("button", { class: "btn primary small", type: "button", disabled: true }, chooseLabel);
  const el = h("div", { class: "browser" }, crumbs, note, dirs, h("div", { class: "foot" }, here, h("span", { style: { flex: 1 } }), choose));
  let cur = null;

  async function open(path, { fallback = false } = {}) {
    clear(dirs).append(h("div", { class: "muted", style: { padding: "8px" } }, "Loading…"));
    let d;
    try { d = await api("/api/fs", { params: { path } }); }
    catch (e) {
      // A folder that's gone, or one outside your home folder and drives: start from home instead.
      if (path && fallback) {
        await open(undefined, { fallback: false });
        note.hidden = false;
        note.textContent = `Couldn't open ${path}: ${e.message} Showing your home folder instead.`;
        return;
      }
      clear(dirs).append(h("div", { class: "result err" }, e.message));
      return;
    }
    note.hidden = true;
    cur = d;
    choose.disabled = false;
    clear(crumbs);
    for (const place of d.places) {
      crumbs.append(h("button", { type: "button", onclick: () => open(place) }, place.startsWith("/Volumes/") ? place.slice(9) : "Home"));
    }
    crumbs.append(h("span", { class: "faint" }, "·"));
    const parts = d.path.split("/").filter(Boolean);
    let acc = "";
    parts.forEach((part, i) => {
      acc += "/" + part;
      const target = acc;
      crumbs.append(i < parts.length - 1
        ? h("button", { type: "button", onclick: () => open(target) }, part)
        : h("b", { style: { padding: "2px 4px" } }, part));
      if (i < parts.length - 1) crumbs.append(h("span", { class: "faint" }, "/"));
    });
    clear(dirs);
    if (d.parent) dirs.append(h("button", { type: "button", class: "menu-item", onclick: () => open(d.parent) }, icon("up"), ".."));
    for (const x of d.dirs) {
      dirs.append(h("button", { type: "button", class: "menu-item", onclick: () => open(x.path) }, icon("folder"), x.name));
    }
    if (!d.dirs.length) dirs.append(h("div", { class: "muted", style: { padding: "8px 10px" } }, "No folders inside."));
    here.textContent = d.images ? `${plural(d.images, "photo")} directly in this folder` : "No photos directly here (subfolders are included)";
  }
  choose.addEventListener("click", () => cur && onChoose(cur.path));
  open(start, { fallback: true });
  return el;
}
