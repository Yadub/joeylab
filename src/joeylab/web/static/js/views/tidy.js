// Tidy: everything for a cleaner library, none of it touching your folders.
//   Duplicates — folders that repeat each other on one drive, and the list of extras you can remove yourself
//   Backups    — which photos have an identical copy on another drive, and which are on one drive only
//   RAW + JPEG — which RAWs to keep, and moving the others to the Trash (the photos stay, with their JPEGs)
//   Health     — unreadable, undated, thumbnails, wrong extensions, drives, catalog backups, the "untouched" check
//   Clocks     — line several cameras up on one timeline
//   Names      — better names for "SD CARD 20260930"-style folders (in JoeyLab only)

import { api, P } from "../api.js";
import { store } from "../store.js";
import { agree, ago, bytes, clear, h, icon, n, offsetLabel, plural, tabBar, toast } from "../ui.js";
import * as backups from "./backups.js";
import * as dupes from "./dupes.js";
import * as raws from "./raws.js";
import { trashDialog } from "./trash.js";
import { waitNote } from "./waiting.js";

export function mount(root, ctx) {
  const p = ctx.project;
  let tab = ctx.params.get("tab") || "dupes";
  const page = h("div", { class: "page" });
  const body = h("div", { class: "embedded" });
  root.append(page);
  let child = null;
  const link = (view, q) => `#/p/${encodeURIComponent(p)}/${view}${q ? "?" + q : ""}`;

  function render() {
    child?.unmount?.();
    child = null;
    clear(page).append(
      h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Tidy"),
        h("div", { class: "sub" }, "Find what to clean up. JoeyLab never deletes, moves or renames anything on its own: you decide, and what you choose to remove goes to the Trash after you confirm.")),
        h("div", { class: "actions" }, tabBar([["dupes", "Duplicates"], ["backups", "Backups"], ["raws", "RAW + JPEG"], ["health", "Health"], ["clock", "Camera clocks"], ["names", "Folder names"]],
          tab, (k) => { tab = k; ctx.setParams({ tab }); render(); }, { label: "Tidy", id: "tidy", panel: body }))),
      // The first update: duplicates are found straight after analysis.
      tab === "dupes" ? waitNote({ step: "group", progressOf: "analyse", text: "Duplicates will appear when analysis finishes",
        when: () => !store.overview?.counts?.groups, ready: "Duplicates are found", onReady: render }) : null,
      body);
    clear(body);
    if (tab === "dupes") child = dupes.mount(body, ctx);
    else if (tab === "backups") child = backups.mount(body, ctx);
    else if (tab === "raws") child = raws.mount(body, ctx);
    else if (tab === "health") health();
    else if (tab === "clock") clocks();
    else names();
  }

  // ------------------------------------------------------------ health
  // Folders left with nothing in them (after removing duplicates or rejects, say). Looking is
  // read-only; moving them to the Trash asks you to type the count (here, or in Terminal).
  function emptyFoldersSection() {
    const host = h("div", { class: "stack-v" });
    const section = h("section", { class: "section", id: "empty-folders" }, h("h2", {}, "Empty folders"), host);
    const look = async () => {
      clear(host).append(h("div", { class: "muted" }, "Looking through your folders (read-only)…"));
      try {
        const d = await api(P(p, "/health/empty-folders"));
        clear(host).append(
          d.unavailable.length ? h("p", { class: "note" }, `Not looked at (not connected): ${d.unavailable.join(", ")}`) : null,
          d.folders.length ? [
            h("b", {}, `${plural(d.folders.length, "empty folder")}`),
            h("div", { class: "stack-v" }, d.folders.slice(0, 300).map((f) => h("div", { class: "drive-row", title: f.path },
              icon("folder"), h("span", { class: "grow mono" }, f.root ? f.path.slice(f.root.replace(/\/$/, "").length + 1) : f.path),
              h("span", { class: "muted" }, [f.leftovers ? plural(f.leftovers, "leftover file") : "", f.inside ? plural(f.inside, "empty folder") + " inside" : ""]
                .filter(Boolean).join(" · "))))),
            d.folders.length > 300 ? h("p", { class: "muted" }, `…and ${n(d.folders.length - 300)} more.`) : null,
            h("p", { class: "note" }, "Leftover files are things like .DS_Store that Finder or Windows leave behind. Moving these folders to the Trash checks each is still empty and asks you to type the count first."),
            store.meta?.can_trash ? h("div", { class: "row" }, h("button", { class: "btn danger", onclick: () => trashDialog({ what: "folders", onDone: () => setTimeout(look, 1500) }) },
              icon("folder"), "Move empty folders to Trash…")) : null,
            h("p", { class: "muted small" }, "Or in Terminal: ", h("code", { class: "mono", style: { userSelect: "all" } }, `${store.meta?.joeylab_bin || "joeylab"} empty-folders -p ${p} --trash`)),
          ] : h("div", { class: "muted" }, "No empty folders."));
      } catch (e) { clear(host).append(h("div", { class: "result err" }, e.message)); }
    };
    host.append(h("button", { class: "btn", onclick: look }, icon("folder"), "Look for empty folders"));
    if (ctx.params.get("empty")) {                 // from ⌘K: look straight away
      look();
      requestAnimationFrame(() => section.scrollIntoView({ block: "start" }));
    }
    return section;
  }

  async function health() {
    const deep = ctx.params.get("deep") === "1";
    body.append(h("div", { class: "muted" }, deep ? "Looking for sidecars whose photo is gone (read-only)…" : "Loading…"));
    let d;
    try { d = await api(P(p, "/health"), { params: { deep: deep ? 1 : 0 } }); } catch (e) { clear(body).append(h("div", { class: "result err" }, e.message)); return; }
    const u = d.untouched;
    // A count of 0 isn't a card: it goes in the "None found" line under them (`none` says what).
    const none = [];
    const card = (title, value, text, href, kind = "", count = value, nothing = title.toLowerCase()) => {
      if (count === 0 || count === "0") { none.push(nothing); return null; }
      return h(href ? "a" : "div", { class: `health-card ${kind}`, href },
        h("span", { class: "label" }, title), h("b", {}, value), text ? h("span", { class: "muted" }, text) : null);
    };
    clear(body).append(
      h("section", { class: "section" }, h("h2", {}, "Your photos are untouched"),
        u ? h("div", { class: `untouched ${u.ok ? "ok" : "warn"}` }, icon(u.ok ? "shield" : "info"),
          h("div", {}, h("b", {}, u.ok ? `All ${n(u.checked)} files and folders were exactly as before the last update` :
            `${plural(u.changed, "file or folder", "files or folders")} changed during the last update`),
          h("div", { class: "muted" }, `${n(u.runs_ok)} of ${n(u.runs)} checked updates found nothing changed · last ${ago(u.ts)}. `
            + "Every tracked file's size, times and identity are compared before and after each update."),
          !u.ok ? h("div", { class: "stack-v", style: { marginTop: "8px" } }, h("span", { class: "muted" }, "JoeyLab doesn't write there, so another app or a copy in progress did:"),
            (u.details.changed || []).slice(0, 12).map((c) => h("div", { class: "path" }, `${c.path} (${c.what.join(", ")})`)),
            (u.details.gone || []).slice(0, 12).map((g) => h("div", { class: "path" }, `${g} (gone)`))) : null))
          : h("div", { class: "muted" }, "Checked after the next update.")),
      h("section", { class: "section" }, h("h2", {}, "Drives"),
        h("div", { class: "stack-v" }, d.drives.map((dr) => {
          const m = dr.mount || {};
          return h("div", { class: "drive-row" }, icon(dr.available ? "check2" : "info"),
            h("div", { class: "grow" }, h("b", { class: "mono" }, dr.path),
              h("div", { class: "muted" }, dr.available ? [m.fs, m.read_only ? "read-only: the system itself prevents any change" : "writable",
                m.noatime ? "reading never updates access times (noatime)" : null,
                dr.known_by_id ? "known by its drive ID, so renaming the drive is fine" : null].filter(Boolean).join(" · ")
                : `not connected · last seen ${dr.last_seen ? ago(dr.last_seen) : "never"}. Browsing, search, people and ratings still work.`),
              dr.available && !m.read_only && m.how_to_read_only ? h("details", {}, h("summary", { class: "linkish" }, "Make it read-only while JoeyLab reads it"),
                h("p", { class: "note" }, "Optional belt and braces: mount the drive read-only, so nothing (JoeyLab or any other app) can change it. Run this in Terminal, and unplug and replug the drive to undo:"),
                h("code", { class: "mono" }, m.how_to_read_only)) : null));
        }))),
      h("section", { class: "section" }, h("h2", {}, "To tidy"),
        h("div", { class: "health-grid" },
          card("Duplicates", bytes(d.duplicates.bytes), `${plural(d.duplicates.extra_files, "extra file")} in ${plural(d.duplicates.sets, "set")}`, link("tidy", "tab=dupes"), "", d.duplicates.sets, "duplicates"),
          card("Couldn't read", n(d.failed.n), "corrupt or unsupported files", null, "warn", d.failed.n, "unreadable files"),
          d.damaged?.n ? card("Damaged, partly shown", n(d.damaged.n), `${n(d.damaged.with_copy)} have an intact copy elsewhere`, link("library", "flag=damaged"), "warn") : null,
          card("No capture date", n(d.undated), "dated by when the file was saved", link("library", "undated=1"), "", d.undated, "undated photos"),
          card("Thumbnails and fragments", n(d.tiny), "under 640 px; hidden while browsing", link("library", "flag=tiny&all=1"), "", d.tiny),
          card("Screenshots and documents", n(d.utility), "receipts, screenshots, whiteboards", link("library", "flag=utility&all=1"), "", d.utility),
          card("Wrong file extension", n(d.wrong_extension.n), "contents don't match the name", null, "warn", d.wrong_extension.n, "wrong file extensions"),
          card("Fixable in editing", n(d.rescuable), "exposure problems, but there's a RAW", link("library", "flag=rescuable"), "", d.rescuable, "photos fixable in editing"),
          card("Edited", n(d.edited), "have develop settings in a sidecar", link("library", "edited=1"), "", d.edited, "edited photos"),
          card("Missing", n(d.missing), "no longer on disk; decisions kept", link("settings"), "", d.missing, "missing photos"),
          card("Versions", n(d.versions), "edited or resized copies of originals", link("tidy", "tab=dupes&kinds=export"), "", d.versions, "edited or resized copies"),
          card("RAW + JPEG apart", n(d.pairs_apart), "the two halves in different folders", link("tidy", "tab=dupes&kinds=pair"), "", d.pairs_apart, "RAW + JPEG pairs in different folders"),
          card("Content Credentials", n(d.c2pa), "files carrying signed provenance", link("library", "c2pa=1"), "", d.c2pa, "files with Content Credentials")),
        none.length ? h("p", { class: "note all-clear" }, icon("check"), `None found: ${none.join(", ")}.`) : null),
      d.failed.examples.length ? h("details", { class: "section" }, h("summary", { class: "label" }, "Files that couldn't be read"),
        d.failed.examples.map((x) => h("div", { class: "path" }, `${x.dir}/${x.stem} — ${x.error}`))) : null,
      d.wrong_extension.examples.length ? h("details", { class: "section" }, h("summary", { class: "label" }, "Wrong extensions"),
        d.wrong_extension.examples.map((x) => h("div", { class: "path" }, `${x.path} is really ${x.is}`))) : null,
      d.to_sort.length ? h("section", { class: "section" }, h("h2", {}, "Folders waiting to be sorted"),
        h("div", { class: "stack-v" }, d.to_sort.map((x) => h("a", { class: "drive-row", href: link("library", `folder=${encodeURIComponent(x.dir)}`), title: x.dir },
          icon("folder"), h("span", { class: "grow mono" }, rel(x.dir, d.drives)), h("span", { class: "muted" }, plural(x.n, "photo"))))),
        h("p", { class: "note" }, "A suggested layout (year / month + event) for everything, as a spreadsheet you can review and apply yourself: ",
          h("a", { href: P(p, "/health/reorganise.csv") }, "download the reorganise plan"), ". JoeyLab won't move anything.")) : null,
      emptyFoldersSection(),
      h("section", { class: "section" }, h("h2", {}, "Backed up?"),
        d.copies?.known ? h("a", { class: `untouched ${d.copies.only ? "warn" : "ok"}`, href: link("tidy", "tab=backups") }, icon("shield"),
          h("div", {}, h("b", {}, `${plural(d.copies.elsewhere, "photo")} ${agree(d.copies.elsewhere, "has a backup", "have backups")} on another drive · ${n(d.copies.only)} only on one drive`),
            h("div", { class: "muted" }, `${n(d.copies.checked)} checked byte for byte. Compared across every JoeyLab library; a copy on the same drive is a duplicate, not a backup. Per drive and folder in Tidy › Backups.`)))
          : h("p", { class: "note" }, "Not looked at yet: this library's files are compared with every other library's at its next update (",
            h("a", { href: link("tidy", "tab=backups") }, "Tidy › Backups"), ")."),
        deep ? (d.orphan_sidecars?.length ? h("details", {}, h("summary", { class: "label" }, `${plural(d.orphan_sidecars.length, "sidecar")} whose photo is gone`),
          d.orphan_sidecars.slice(0, 50).map((x) => h("div", { class: "path" }, x))) : h("p", { class: "muted" }, "No sidecars whose photo is gone."))
          : h("button", { class: "btn", onclick: () => { ctx.setParams({ tab: "health", deep: 1 }, true); } }, icon("search"), "Look for sidecars whose photo is gone")),
      h("section", { class: "section" }, h("h2", {}, "Catalog backups"),
        h("p", { class: "note" }, "Your decisions, names and albums live in JoeyLab's catalog. It's backed up daily (14 daily, 8 weekly and 12 monthly copies kept)."),
        h("div", { class: "stack-v" }, d.backups.slice(0, 5).map((b) => h("div", { class: "drive-row" }, icon("shield"), h("span", { class: "grow mono" }, b.name),
          h("span", { class: "muted" }, `${bytes(b.size)} · ${ago(b.modified)}`)))),
        h("div", { class: "row" }, h("button", { class: "btn", onclick: async () => { await api(P(p, "/backups"), { method: "POST", body: {} }); toast("Backed up"); health(); } }, "Back up now"),
          h("span", { class: "muted" }, "To bring one back: stop the service, then run joeylab restore -p " + p))));
  }

  function rel(path, drives) {
    const root = (drives || []).map((x) => x.path).find((r) => path === r || path.startsWith(r + "/"));
    return root ? path.slice(root.length + 1) || path : path;
  }

  // ------------------------------------------------------------ camera clocks
  async function clocks() {
    body.append(h("div", { class: "muted" }, "Comparing cameras (looking for the same moments shot by different bodies)…"));
    let d;
    try { d = await api(P(p, "/clock")); } catch (e) { clear(body).append(h("div", { class: "result err" }, e.message)); return; }
    const sugg = new Map(d.suggestions.map((s) => [s.camera, s]));
    d_cameras = d.cameras;
    clear(body).append(
      h("p", { class: "note" }, "When two cameras' clocks disagree, their photos interleave in the wrong order and events split. A correction here only changes JoeyLab's timeline; the photos' own dates are never rewritten."),
      // (On a phone each camera is a card: data-label names what a cell is.)
      d.cameras.length ? h("div", { class: "table-wrap" }, h("table", { class: "plain clock-table" },
        h("thead", {}, h("tr", {}, h("th", {}, "Camera"), h("th", { class: "num" }, "Photos"), h("th", {}, "Used"), h("th", {}, "Correction"), h("th", {}, ""))),
        h("tbody", {}, d.cameras.map((c) => {
          const s = sugg.get(c.key);
          const was = Math.round(c.offset_s);
          // Apply is there to press once the correction has been changed.
          const applyBtn = h("button", { class: "btn small", disabled: true, onclick: () => apply(c.key, +input.value) }, "Apply");
          const input = h("input", { class: "input", type: "number", step: 1, value: was, style: { width: "110px" }, "aria-label": "Seconds to add",
            oninput: () => { applyBtn.disabled = input.value === "" || +input.value === was; } });
          return h("tr", {},
            h("td", { "data-label": "Camera" }, h("b", {}, c.camera), c.serial ? h("span", { class: "faint mono" }, ` …${String(c.serial).slice(-4)}`) : null),
            h("td", { class: "num", "data-label": "Photos" }, n(c.n)),
            h("td", { class: "muted", "data-label": "Used" }, `${(c.from || "").slice(0, 7)} – ${(c.to || "").slice(0, 7)}`),
            h("td", { "data-label": "Correction" }, h("div", { class: "row", style: { flexWrap: "nowrap" } }, input, h("span", { class: "muted" }, "s"),
              s ? h("span", { class: "pill accent", title: `${s.pairs} matching moments with ${s.relative_to.split("|")[0]} (${Math.round(s.share * 100)}% agree)` },
                `Suggested ${offsetLabel(s.offset_s)}`) : null)),
            h("td", { class: "clock-acts" }, h("div", { class: "row", style: { gap: "4px", flexWrap: "wrap" } },
              s ? h("button", { class: "btn small primary", title: `If ${s.relative_to.split("|")[0]}'s clock was right`, onclick: () => apply(c.key, s.offset_s) }, "Use suggestion") : null,
              s ? reverseBtn(c, s) : null,
              applyBtn)));
        })))) : h("div", { class: "muted" }, "No camera has dated photos yet."),
      !d.suggestions.length ? h("p", { class: "note" }, "No disagreement found between cameras that shot at the same time. (Suggestions need two bodies photographing the same moments, like two shooters at a wedding.)") : null);
  }

  // The suggestion moves this camera to match the other one; when this camera was the one that was
  // right, move the other by the same amount the opposite way instead.
  function reverseBtn(c, s) {
    const other = d_cameras.find((x) => x.key === s.relative_to);
    if (!other) return null;
    const off = Math.round((other.offset_s || 0) - (s.offset_s - (c.offset_s || 0)));
    return h("button", { class: "btn small", title: `If ${c.camera}'s clock was right: correct ${other.camera} by ${offsetLabel(off)} instead`,
      onclick: () => apply(other.key, off) }, `Move ${other.camera} instead`);
  }
  let d_cameras = [];

  async function apply(key, off) {
    try {
      await api(P(p, "/clock"), { method: "POST", body: { camera: key, offset_s: off } });
      toast(`Corrected by ${offsetLabel(off)}. Finding events again…`);
      ctx.pollJob();
      clocks();
    } catch (e) { toast(e.message, { kind: "error" }); }
  }

  // ------------------------------------------------------------ folder names
  async function names() {
    body.append(h("div", { class: "muted" }, "Loading…"));
    let d;
    try { d = await api(P(p, "/folders/suggest-names")); } catch (e) { clear(body).append(h("div", { class: "result err" }, e.message)); return; }
    clear(body).append(
      h("p", { class: "note" }, "Folders with names like “SD CARD 20260930” or “100_FUJI”, with a better name worked out from their dates, events and places. A name you accept is used in JoeyLab only; the folder on disk keeps its name. Rename any folder under Library › Folders."),
      d.suggestions.length ? h("div", { class: "stack-v" }, d.suggestions.map((s) => {
        const input = h("input", { class: "input", value: s.suggestion, "aria-label": `Name for ${s.path.split("/").pop()}` });
        return h("div", { class: "drive-row" }, icon("folder"),
          h("div", { class: "grow" }, h("div", { class: "mono" }, s.path.split("/").slice(-2).join("/")),
            h("div", { class: "muted" }, `${plural(s.n, "photo")} · ${(s.from || "").slice(0, 10)} – ${(s.to || "").slice(0, 10)}`)),
          input,
          h("button", { class: "btn small primary", onclick: async (e) => {
            const btn = e.currentTarget;            // (gone from the event once it's awaited)
            try { await api(P(p, "/folders/name"), { method: "POST", body: { path: s.path, name: input.value } }); }
            catch (err) { toast(err.message, { kind: "error" }); return; }
            btn.textContent = "Named";
            btn.disabled = true;
          } }, "Use"));
      })) : h("div", { class: "muted" }, "Every folder already has a descriptive name (or no events have been found yet)."));
  }

  render();
  return {
    refresh: render,
    unmount() { child?.unmount?.(); },
    // Back to a tab that draws after a fetch (Duplicates): it puts the scroll back once it has drawn.
    restore: (s) => (child?.restore ? child.restore(s) : setTimeout(() => window.scrollTo({ top: s.y || 0, behavior: "instant" }), 120)),
    onKey(e) { return child?.onKey?.(e) || false; },
    get keys() { return child?.keys; },
  };
}
