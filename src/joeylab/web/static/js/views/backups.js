// Backups: which photos have an identical copy on another drive, and which are only on one.
//   Per drive and per folder: how many photos are backed up elsewhere and where, how many are the
//   only copy on any drive JoeyLab knows (at risk if that drive fails), the space each takes,
//   and links to see them in the Library. Drives that aren't connected say when they were last seen.
// Also the small pieces other pages use to say where a backup is (the viewer, the Trash dialogs).

import { api, P } from "../api.js";
import { store } from "../store.js";
import { agree, bytes, clear, h, icon, n, plural, toast } from "../ui.js";

/** "3 Oct" this year, "3 Oct 2025" before. */
export function dayLabel(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const opt = { day: "numeric", month: "short" };
  if (d.getFullYear() !== new Date().getFullYear()) opt.year = "numeric";
  return d.toLocaleDateString("en-GB", opt);
}

/** "not connected, last seen 3 Oct" (or nothing for a drive that's connected). */
export function driveState(c) {
  if (c.connected) return "";
  return c.last_seen ? `not connected, last seen ${dayLabel(c.last_seen)}` : "not connected";
}

/** A path inside a drive, its folders shortened from the left if long: "pictures/…/2024 Lisbon/IMG_1.JPG". */
export function shortRel(rel, keep = 3) {
  const parts = (rel || "").split("/").filter(Boolean);
  return parts.length > keep + 1 ? `${parts[0]}/…/${parts.slice(-keep).join("/")}` : parts.join("/");
}

/** Where one backup is, as the viewer shows it: "Archive HDD › pictures/2024/…/IMG_1.JPG", the library
 * to open it in when it's another one, and whether it's been checked byte for byte. */
export function whereEl(c, project, link = (href) => ({ href }), prefix = null) {
  const other = c.library !== project;
  return h("div", { class: "bk-where" },
    h("span", { class: "bk-loc", title: `${c.drive_name} › ${c.rel}` }, prefix, h("b", {}, c.drive_name), " › ", h("span", { class: "mono" }, shortRel(c.rel))),
    h("span", { class: "bk-meta" },
      c.checked ? h("span", { class: "pill pick", title: "The whole file was read on both drives and is identical" }, "checked")
        : h("span", { class: "pill neutral", title: "Same size and the same first and last 256 KB; the whole file hasn't been compared yet" }, "probably"),
      driveState(c) ? h("span", { class: "faint" }, driveState(c)) : null,
      other ? h("a", { class: "bk-lib", ...link(`#/p/${encodeURIComponent(c.library)}/library?ids=${c.shot_id}`), title: `Open it in the library ${c.library}` },
        `in ${c.library}`) : null));
}

/** One line for a file in a Trash dialog: its checked backup, or that it's the only copy anywhere.
 * (Only checked backups are named; they never make a file go.) */
export function trashNote(f) {
  if (f.backup) {
    const s = driveState(f.backup);
    return h("span", { class: "bk-note ok" }, icon("shield"), h("span", {}, `backed up on ${f.backup.drive_name}`,
      h("span", { class: "faint" }, ` › ${shortRel(f.backup.rel, 2)}${s ? ` (${s})` : ""}`)));
  }
  if (f.only) return h("span", { class: "bk-note risk" }, icon("info"), h("span", {}, "the only copy anywhere"));
  if (f.unchecked_on) return h("span", { class: "bk-note" }, h("span", {}, `no checked backup yet (a possible copy on ${f.unchecked_on} hasn't been read)`));
  return h("span", { class: "bk-note" }, h("span", {}, "not on any other drive"));
}

/** The Trash dialogs' summary of backups for the files listed. */
export function trashSummary(s, noun = "file") {
  if (!s || !s.files) return null;
  const bits = [];
  if (s.backed_up) bits.push(`${plural(s.backed_up, noun)} ${agree(s.backed_up, "has", "have")} a checked backup on another drive`);
  if (s.only_copy) bits.push(`${plural(s.only_copy, noun)} ${agree(s.only_copy, "is the only copy", "are the only copies")} anywhere JoeyLab knows`);
  if (s.unchecked) bits.push(`${plural(s.unchecked, noun)} may have one that hasn't been checked yet`);
  if (!bits.length) bits.push(`None of these ${noun}s has a copy on another drive`);
  return h("p", { class: "note bk-summary" }, icon("shield"), h("span", {}, bits.join("; ") + ". Backups are shown for information: they never decide what goes."));
}

// ------------------------------------------------------------ Tidy › Backups
const FOLDERS_SHOWN = 12;

export function mount(root, ctx) {
  const p = ctx.project;
  const page = h("div", { class: "stack-v backups-view" });
  root.append(page);
  const lib = (q) => `#/p/${encodeURIComponent(p)}/library?${q}`;
  const open = new Set();

  async function load() {
    let d;
    try { d = await api(P(p, "/copies")); } catch (e) { clear(page).append(h("div", { class: "result err" }, e.message)); return; }
    render(d);
  }

  async function check(btn) {
    btn.disabled = true;
    try {
      const r = await api(P(p, "/copies/check"), { method: "POST", body: {} });
      toast(r.candidates ? `Checking ${plural(r.candidates, "possible backup")} byte for byte, in the background` : "Up to date: nothing left to check");
      load();
    } catch (e) { toast(e.message, { kind: "error" }); btn.disabled = false; }
  }

  function render(d) {
    const writer = store.meta?.writer !== false;
    if (!d.known) {
      clear(page).append(head(),
        h("div", { class: "empty" }, h("h3", {}, "Not looked at yet"),
          h("p", { class: "muted" }, "This library's files are compared with every other library's at its next update. Or look now: it only reads the catalogs."),
          writer ? h("button", { class: "btn primary", type: "button", onclick: (e) => check(e.currentTarget) }, icon("shield"), "Look now") : null));
      return;
    }
    const checking = d.checking?.running;
    clear(page).append(
      head(),
      h("div", { class: "health-grid" },
        card("Backed up elsewhere", n(d.backed), `${bytes(d.backed_bytes)} · ${n(d.checked)} checked byte for byte`, lib("backup=elsewhere"), ""),
        card("Only on one drive", n(d.only), `${bytes(d.only_bytes)} · lost if that drive fails`, lib("backup=only"), d.only ? "warn" : ""),
        d.unchecked_files ? card("Not checked yet", n(d.unchecked_files), `${agree(d.unchecked_files, "file", "files")} with the same size and fingerprint as a file on another drive`, null, "") : null),
      d.unchecked_files ? h("div", { class: "row" },
        h("span", { class: "note" }, checking ? "Reading them whole now, slowly, so other work isn't held up. Nothing on any drive is changed."
          : "They're read whole in the background, slowly, when no update is running: “probably” until then. Nothing on any drive is changed."),
        writer && !checking ? h("button", { class: "btn small", type: "button", onclick: (e) => check(e.currentTarget) }, icon("refresh"), "Check now") : null) : null,
      d.drives.map((dr) => driveSection(dr)),
      d.holders.length ? h("section", { class: "section" }, h("h2", {}, "Where the backups are"),
        h("div", { class: "stack-v" }, d.holders.map((x) => h("div", { class: "drive-row bk-holder" },
          icon(x.connected ? "check2" : "info"),
          h("div", { class: "grow" }, h("b", {}, x.name),
            h("div", { class: "muted" }, [x.connected ? "connected" : driveState(x),
              x.libraries.length ? `${agree(x.libraries.length, "library", "libraries")} ${x.libraries.join(", ")}` : ""].filter(Boolean).join(" · "))),
          h("span", {}, `${plural(x.photos, "photo")} of this library`),
          x.libraries.filter((l) => l !== p).slice(0, 2).map((l) => h("a", { class: "btn small ghost", href: `#/p/${encodeURIComponent(l)}/tidy?tab=backups` }, `Open ${l}`)))))) : null,
      h("p", { class: "note" }, "A photo is backed up when each of its files has an identical copy on another drive, in this library or any other JoeyLab library. ",
        "Copies on the same drive are duplicates, not backups. A re-saved or exported version is a different file, so it isn't counted. ",
        "To check against a drive that isn't a library yet, add it as a library: it's only read."),
    );
  }

  function head() {
    return h("div", { class: "page-head" },
      h("div", {}, h("h2", { class: "sub-title" }, "Backups"),
        h("div", { class: "sub" }, "Which photos have an identical copy on another drive, and which exist on one drive only.")));
  }

  function card(title, value, text, href, kind) {
    return h(href ? "a" : "div", { class: `health-card ${kind}`, href },
      h("span", { class: "label" }, title), h("b", {}, value), text ? h("span", { class: "muted" }, text) : null);
  }

  function share(dr) {
    const pc = dr.photos ? Math.round((100 * dr.backed) / dr.photos) : 0;
    return h("div", { class: "bk-bar", role: "img", "aria-label": `${pc}% backed up` },
      h("i", { class: "ok", style: { width: `${dr.photos ? (100 * dr.checked) / dr.photos : 0}%` } }),
      h("i", { class: "maybe", style: { width: `${dr.photos ? (100 * (dr.backed - dr.checked)) / dr.photos : 0}%` } }));
  }

  function driveSection(dr) {
    const state = dr.local ? "this Mac's own disk" : dr.connected ? "connected" : driveState(dr);
    const fs = dr.folders;
    const showAll = open.has(dr.key);
    const shown = showAll ? fs : fs.slice(0, FOLDERS_SHOWN);
    const list = h("div", { class: "bk-folders" },
      h("div", { class: "bk-folder bk-head", "aria-hidden": "true" }, h("span", {}, "Folder"), h("span", {}, "Photos"), h("span", {}, "Backed up"),
        h("span", {}, "Only here"), h("span", {}, "At risk")),
      shown.map((f) => {
        const name = relName(f.path, dr.roots);
        const q = `folder=${encodeURIComponent(f.path)}`;
        return h("div", { class: "bk-folder" },
          h("span", { class: "bk-path", title: f.path }, icon("folder"), h("span", { class: "mono" }, name),
            f.where.length ? h("span", { class: "faint bk-on" }, `on ${f.where.map((w) => w.name).join(", ")}`) : null),
          h("span", { "data-label": "Photos" }, n(f.photos)),
          h("span", { "data-label": "Backed up" }, f.backed ? h("a", { href: lib(`${q}&backup=elsewhere`), title: `See the ${plural(f.backed, "photo")} backed up` }, n(f.backed)) : "0"),
          h("span", { "data-label": "Only here", class: f.only ? "risk" : "" }, f.only ? h("a", { href: lib(`${q}&backup=only`), title: `See the ${plural(f.only, "photo")} only here` }, n(f.only)) : "0"),
          h("span", { "data-label": "At risk", class: "muted" }, f.only ? bytes(f.only_bytes) : "—"));
      }));
    return h("section", { class: "section bk-drive" },
      h("div", { class: "bk-drive-head" },
        icon(dr.connected || dr.local ? "check2" : "info"),
        h("div", { class: "grow" }, h("h3", {}, dr.name), h("div", { class: "muted" }, [state, dr.roots.length ? `${plural(dr.roots.length, "library folder")} here` : ""].filter(Boolean).join(" · "))),
        h("div", { class: "bk-figs" }, h("b", {}, `${n(dr.backed)} of ${plural(dr.photos, "photo")}`), h("span", { class: "muted" }, "backed up elsewhere"))),
      share(dr),
      h("div", { class: "bk-lines" },
        h("span", {}, h("span", { class: "bk-key ok" }), `${n(dr.checked)} checked byte for byte`),
        dr.backed - dr.checked ? h("span", {}, h("span", { class: "bk-key maybe" }), `${n(dr.backed - dr.checked)} probably (not checked yet)`) : null,
        h("span", {}, h("span", { class: "bk-key risk" }),
          dr.only ? h("a", { href: lib(`backup=only${dr.roots.length === 1 ? `&folder=${encodeURIComponent(dr.roots[0])}` : ""}`) }, `${n(dr.only)} only here`) : "0 only here",
          dr.only ? ` (${bytes(dr.only_bytes)})` : "")),
      dr.where.length ? h("div", { class: "row bk-on-drives" }, h("span", { class: "label" }, "Backed up on"),
        dr.where.map((w) => h("span", { class: "chip", title: w.connected ? "Connected" : driveState(w) },
          w.name, h("span", { class: "n" }, n(w.photos)), w.connected ? null : h("span", { class: "faint" }, driveState(w))))) : null,
      fs.length ? list : null,
      fs.length > FOLDERS_SHOWN || dr.more_folders ? h("button", { class: "btn small ghost", type: "button",
        onclick: () => { showAll ? open.delete(dr.key) : open.add(dr.key); load(); } },
      showAll ? "Show fewer folders" : `Show all ${n(fs.length + (dr.more_folders || 0))} folders`) : null,
      showAll && dr.more_folders ? h("p", { class: "muted small" }, `…and ${plural(dr.more_folders, "more folder")}, with fewer photos at risk.`) : null);
  }

  function relName(path, roots) {
    const r = (roots || []).find((x) => path === x || path.startsWith(x + "/"));
    if (!r) return path.split("/").slice(-2).join("/");
    const inside = path.slice(r.length + 1);
    return inside ? `${r.split("/").pop()}/${inside}` : r.split("/").pop();
  }

  load();
  return { refresh: load };
}
