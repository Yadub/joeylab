// Settings: what this library is for, its folders, getting work in and out, AI assistants, privacy,
// backups and other devices.

import { api, P, qs } from "../api.js";
import { saveSettings, settings, store } from "../store.js";
import { agree, ago, bytes, choiceSeg, clear, h, icon, n, plural, toast } from "../ui.js";
import { groupingControl } from "./common.js";
import { folderPicker } from "./folderpicker.js";
import { placesSettings } from "./places.js";
import { chooseModels, currentModels, switchNote } from "./facemodels.js";
import { checkLine, newProjectForm, slug } from "./projects.js";
import { leftOutSection, splitSection } from "./splitlib.js";

const PRESETS = [
  ["picks", "Picks", { status: "picks" }],
  ["four", "4 stars and up", { min_rating: 4 }],
  ["rated", "Anything rated", { status: "rated" }],
  ["library", "Current Library filter", null],
  ["all", "Every photo", {}],
];
const SECTIONS = [["library", "This library"], ["folders", "Folders"], ["export", "Export & import"], ["assistants", "AI assistants"],
  ["places", "Places & trips"], ["privacy", "People & privacy"], ["backups", "Backups"], ["devices", "Devices & sharing"], ["about", "About"]];

export function mount(root, ctx) {
  const page = h("div", { class: "page settings-page" });
  root.append(page);
  const p = ctx.project;
  let sec = ctx.params.get("s") || "library";

  async function render() {
    let roots, o, st;
    try { [roots, o, st] = await Promise.all([api(P(p, "/roots")), api(P(p, "/overview")), settings(true)]); }
    catch (e) { clear(page).append(h("div", { class: "result err" }, e.message)); return; }
    store.overview = o;
    const nav = h("nav", { class: "settings-nav", "aria-label": "Settings" }, SECTIONS.map(([k, t]) => h("button", { type: "button", "aria-current": sec === k ? "page" : null,
      onclick: () => { sec = k; ctx.setParams({ s: k }); render(); } }, t)));
    const body = h("div", { class: "settings-body stack-v", style: { gap: "28px" } });
    clear(page).append(
      h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Settings"),
        h("div", { class: "sub" }, "JoeyLab reads your folders and keeps everything it learns in its own catalog, on this Mac. It changes them only when you ask (sidecars, or moving what you choose to the Trash)."))),
      h("div", { class: "settings-layout" }, nav, body));
    ({ library: () => librarySection(body, st, o), folders: () => foldersSection(body, roots, o), export: () => exportSection(body, o),
      assistants: () => assistantsSection(body), places: () => placesSettings(body, p), privacy: () => privacySection(body), backups: () => backupsSection(body),
      devices: () => devicesSection(body), about: () => aboutSection(body) })[sec]();
  }

  // ------------------------------------------------------------ library
  function librarySection(body, st) {
    const toggle = (key, label, note) => h("label", { class: "check" },
      h("input", { type: "checkbox", checked: !!st[key], onchange: async (e) => { await saveSettings({ [key]: e.target.checked }); toast("Saved"); } }),
      h("span", {}, h("b", {}, label), note ? h("br") : null, note ? h("span", { class: "muted" }, note) : null));
    body.append(
      h("section", { class: "section" }, h("h2", {}, "What this library is for"),
        choiceSeg([["shoot", "A shoot to cull"], ["archive", "An archive to explore and tidy"]], st.purpose, async (k) => {
          await saveSettings({ purpose: k, show_suggestions: k === "shoot" }); render();
        }, { label: "What this library is for" }),
        h("p", { class: "note" }, "A shoot opens on culling progress and suggestions; an archive opens on years, events, people and tidying.")),
      h("section", { class: "section" }, h("h2", {}, "Browsing"),
        toggle("show_suggestions", "Show suggested picks and rejects on photos", "Useful while culling; noise when you're just looking."),
        toggle("hide_tiny", "Hide thumbnails and fragments", "Images under 640 px (recovered previews, web thumbnails). Always one click away under Library › More filters."),
        toggle("hide_utility", "Hide screenshots, receipts and documents", "Found by Apple Vision. They stay searchable.")),
      h("section", { class: "section" }, h("h2", {}, "Stacks"),
        h("p", { class: "note" }, "How readily frames are stacked together in Cull and the Library: from true bursts only to whole scenes shot minutes apart. Drag to see what each setting gives; nothing about your photos or decisions changes, only how they're stacked."),
        groupingControl(p, null)),
    );
    if (store.meta?.owner) {
      const host = h("div", { class: "stack-v", style: { gap: "28px" } });
      body.append(host, nameAndRemoval());
      splitSection(host, { refresh: render });
    }
  }

  // Renaming or removing the library: JoeyLab's own records only, never the photo folders.
  function nameAndRemoval() {
    const nameIn = h("input", { class: "input", id: "lib-rename", value: p, autocomplete: "off", spellcheck: "false", style: { maxWidth: "360px" } });
    const saved = h("span", { class: "muted np-saved" });
    nameIn.addEventListener("input", () => { const s = slug(nameIn.value); saved.textContent = s && s !== nameIn.value.trim() ? `Saved as ${s}` : ""; });
    const out = h("div", { class: "result err", hidden: true });
    const rename = async (e) => {
      e.preventDefault();
      out.hidden = true;
      if (!slug(nameIn.value) || slug(nameIn.value) === p) return;
      try {
        const r = await api(`/api/projects/${encodeURIComponent(p)}/rename`, { method: "POST", body: { to: nameIn.value } });
        toast(`Renamed to ${r.name}. Its folders and photos weren't touched.`);
        location.hash = `#/p/${encodeURIComponent(r.name)}/settings?s=library`;
      } catch (err) { out.hidden = false; out.textContent = err.message; }
    };
    const typed = h("input", { class: "input", id: "lib-remove", autocomplete: "off", spellcheck: "false", style: { maxWidth: "360px" } });
    const removeBtn = h("button", { class: "btn danger", type: "submit", disabled: true }, "Remove");
    typed.addEventListener("input", () => { removeBtn.disabled = typed.value.trim() !== p; });
    const removed = h("div", { class: "result err", hidden: true });
    const remove = async (e) => {
      e.preventDefault();
      removed.hidden = true;
      try {
        const r = await api(`/api/projects/${encodeURIComponent(p)}/remove`, { method: "POST", body: { confirm: typed.value.trim() } });
        toast(`Removed ${p} from JoeyLab. Your photo folders weren't touched. A backup of its catalog is kept in ${r.kept}.`, { persist: true });
        location.hash = "#/";
      } catch (err) { removed.hidden = false; removed.textContent = err.message; }
    };
    return h("section", { class: "section" }, h("h2", {}, "Name and removal"),
      h("form", { class: "stack-v", onsubmit: rename },
        h("label", { class: "field" }, h("span", {}, "Library name"),
          h("div", { class: "row", style: { flexWrap: "nowrap" } }, nameIn, h("button", { class: "btn", type: "submit" }, "Rename"))),
        saved, out),
      h("form", { class: "stack-v", onsubmit: remove, style: { marginTop: "12px" } },
        h("b", {}, "Remove this library"),
        h("p", { class: "note" }, "Removing the library takes it out of JoeyLab: its decisions, albums, names and thumbnails. "
          + "Your photo folders aren't touched, and a backup of its catalog is kept in JoeyLab's folder, so it can be brought back."),
        h("label", { class: "field" }, h("span", {}, `Type ${p} to remove it`),
          h("div", { class: "row", style: { flexWrap: "nowrap" } }, typed, removeBtn)),
        removed));
  }

  // ------------------------------------------------------------ folders
  function foldersSection(body, roots, o) {
    const pathIn = h("input", { class: "input mono", id: "add-folder", placeholder: "/Volumes/Photos/2024" });
    const pickerHost = h("div");
    const addOut = h("div", { class: "result err", hidden: true });
    async function add(path, emptyOk = false) {
      addOut.hidden = true;
      try {
        const r = await api(P(p, "/roots"), { method: "POST", body: { path, empty_ok: emptyOk } });
        toast(r.note ? r.note : "Folder added. Reading its photos in the background.");
        ctx.pollJob();
        render();
      } catch (e) {
        addOut.hidden = false;
        addOut.className = "result err";
        // A folder with no photos yet can still be added (photos copied in later are found by the next update).
        clear(addOut).append(h("span", {}, e.message), /^No photos in/.test(e.message)
          ? h("button", { class: "btn small", type: "button", style: { marginLeft: "8px" }, onclick: () => add(path, true) }, "Add it anyway") : null);
      }
    }
    pathIn.addEventListener("change", async () => {
      const path = pathIn.value.trim();
      if (!path) return;
      try {
        const c = await api("/api/fs/check", { params: { path, library: p } });
        if (pathIn.value.trim() === path) { addOut.hidden = false; addOut.className = `result ${c.ok ? "" : "err"}`; addOut.textContent = checkLine(c); }
      } catch { /* said when adding */ }
    });
    const job = o.job;
    body.append(h("section", { class: "section" },
      h("h2", {}, "Folders JoeyLab reads"),
      h("div", { class: "table-wrap" }, h("table", { class: "plain" },
        h("thead", {}, h("tr", {}, h("th", {}, "Folder"), h("th", {}, "State"), h("th", { style: { textAlign: "right" } }, "Files"), h("th", {}))),
        h("tbody", {}, roots.length ? roots.map((r) => h("tr", {},
          h("td", { class: "mono" }, r.path),
          h("td", {}, r.available ? h("span", { class: "pill pick" }, "Connected")
            : h("span", { class: "pill warn", title: "The drive may be unplugged. Nothing is marked missing while it's away." },
              `Not connected${r.last_seen ? " · seen " + ago(r.last_seen) : ""}`),
          r.mount?.read_only ? h("span", { class: "pill accent", style: { marginLeft: "6px" }, title: "Mounted read-only: nothing can change it" }, "read-only") : null,
          r.missing ? h("span", { class: "pill neutral", style: { marginLeft: "6px" } }, `${n(r.missing)} missing`) : null),
          h("td", { class: "num" }, n(r.files)),
          h("td", { style: { textAlign: "right" } }, removeButton(r)))) :
          h("tr", {}, h("td", { colspan: 4, class: "muted" }, "No folders yet. Add one below."))))),
      h("div", { class: "row" },
        h("button", { class: "btn", onclick: () => startJob(false) }, icon("refresh"), "Update now"),
        h("button", { class: "btn ghost", title: "Read every photo again and work out every score, for example after JoeyLab itself was updated", onclick: () => startJob(true) }, "Analyse everything again"),
        o.counts.missing ? h("button", { class: "btn ghost danger", onclick: (e) => confirmInline(e.currentTarget,
          `Forget ${plural(o.counts.missing, "missing photo")} and their ratings? (Catalog only.)`, async () => {
            const r = await api(P(p, "/prune"), { method: "POST", body: {} });
            toast(`Forgot ${plural(r.forgotten, "photo")}`); render();
          }) }, `Forget ${n(o.counts.missing)} missing`) : null,
        h("span", { class: "muted small" },
          job ? `Last update ${ago(job.finished || job.started)} · ` : "",
          store.meta?.watch_minutes ? `checks for changes every ${store.meta.watch_minutes} min` : "automatic checks are off")),
      h("form", { class: "stack-v", onsubmit: (e) => { e.preventDefault(); if (pathIn.value.trim()) add(pathIn.value.trim()); } },
        h("label", { class: "field" }, h("span", {}, "Add a folder (its subfolders are included)"),
          h("div", { class: "row", style: { flexWrap: "nowrap" } }, pathIn,
            h("button", { class: "btn", type: "button", onclick: () => {
              if (pickerHost.firstChild) { clear(pickerHost); return; }
              pickerHost.append(folderPicker({ start: pathIn.value.trim() || undefined, onChoose: (path) => { clear(pickerHost); add(path); }, chooseLabel: "Add this folder" }));
            } }, "Browse…"),
            h("button", { class: "btn primary", type: "submit" }, "Add"))),
        pickerHost, addOut)));
    const leftOut = h("div");
    body.append(leftOut, h("section", { class: "section" }, h("h2", {}, "Another library"), newProjectForm()));
    if (store.meta?.owner) leftOutSection(leftOut, { refresh: render });
  }

  function removeButton(r) {
    return h("button", { class: "btn ghost small", onclick: (e) => confirmInline(e.currentTarget,
      "Stop tracking this folder? Your files stay exactly where they are; their photos are kept as missing until you forget them.", async () => {
        await api(P(p, `/roots/${r.id}`), { method: "DELETE" });
        toast("Stopped tracking it. Nothing on disk changed."); render();
      }) }, "Stop tracking");
  }

  async function startJob(force) {
    try {
      await api(P(p, "/jobs"), { method: "POST", body: { force } });
      ctx.pollJob();
      toast(force ? "Analysing every photo again…" : "Updating: looking for new and changed photos…");
    } catch (e) { toast(e.message, { kind: "error" }); }
  }

  // ------------------------------------------------------------ export and import
  function exportSection(body) {
    let preset = store.libraryParams && Object.keys(store.libraryParams).length ? "library" : "picks";
    const filterFor = (key) => (key === "library" ? store.libraryParams || {} : PRESETS.find((x) => x[0] === key)[2]);
    const matchN = h("span", { class: "muted" });
    const seg = h("div");
    const links = h("div", { class: "row" });
    async function update() {
      clear(seg).append(choiceSeg(PRESETS.filter(([k]) => k !== "library" || Object.keys(store.libraryParams || {}).length).map(([k, label]) => [k, label]),
        preset, (k) => { preset = k; update(); }, { label: "Which photos" }));
      const f = filterFor(preset);
      try {
        const r = await api(P(p, "/shots"), { params: { ...f, limit: 1, all: 1 } });
        matchN.textContent = `${plural(r.total, "photo")} selected`;
      } catch { matchN.textContent = ""; }
      clear(links).append(
        h("a", { class: "btn", href: `${P(p, "/export/csv")}?${qs(f)}` }, icon("download"), "Spreadsheet (CSV)"),
        h("a", { class: "btn", href: `${P(p, "/export/list")}?${qs({ ...f, files: "all" })}` }, icon("download"), "List of file paths"));
    }

    const dest = h("input", { class: "input mono", id: "exp-dest", value: `${store.meta?.home || "~"}/Pictures/JoeyLab/${p}-picks` });
    const files = h("select", { class: "input", id: "exp-files" },
      h("option", { value: "preferred" }, "RAW when there is one, otherwise JPEG"),
      h("option", { value: "jpeg" }, "JPEG only"), h("option", { value: "raw" }, "RAW only"), h("option", { value: "all" }, "Every file"));
    const copy = h("input", { type: "checkbox", id: "exp-copy" });
    const clean = h("input", { type: "checkbox", id: "exp-clean", checked: true });
    const albumOut = h("div", { class: "result", hidden: true });

    const xConfirm = h("input", { type: "checkbox", id: "xmp-ok" });
    const xStyle = h("select", { class: "input", id: "xmp-style" },
      h("option", { value: "stem" }, "IMG_0001.xmp (Lightroom, Capture One, Bridge)"),
      h("option", { value: "full" }, "IMG_0001.CR3.xmp (darktable)"));
    const xUpdate = h("input", { type: "checkbox", id: "xmp-update" });
    const inc = Object.fromEntries([["rating", true], ["label", true], ["keywords", false], ["people", false], ["regions", false]]
      .map(([k, on]) => [k, h("input", { type: "checkbox", checked: on })]));
    const xOut = h("div", { class: "result", hidden: true });
    const fileIn = h("input", { type: "file", accept: ".json,application/json", "aria-label": "Decisions file to import" });
    const overwrite = h("input", { type: "checkbox" });
    const lrPath = h("input", { class: "input mono", id: "lr-path", placeholder: `${store.meta?.home || "~"}/Pictures/Lightroom/Lightroom Catalog.lrcat` });
    const impOut = h("div", { class: "result", hidden: true });

    update();
    body.append(
      h("section", { class: "section" },
        h("h2", {}, "Which photos"),
        h("div", { class: "row" }, seg, matchN)),
      h("div", { class: "panel stack-v" },
        h("b", {}, "Album folder"),
        h("p", { class: "note" }, "A folder of shortcuts to the selected photos, named by capture time so they sort in order. Re-running it updates the same folder, and only ever removes shortcuts JoeyLab made. It can't go inside a folder JoeyLab reads."),
        h("div", { class: "form-grid" },
          h("label", { class: "field span-2" }, h("span", {}, "Folder"), dest),
          h("label", { class: "field" }, h("span", {}, "Files"), files)),
        h("label", { class: "check" }, clean, h("span", {}, "Remove shortcuts for photos that no longer match")),
        h("label", { class: "check" }, copy, h("span", {}, "Copy the files instead (for sharing or a USB stick; uses disk space)")),
        albumOut,
        h("div", {}, h("button", { class: "btn primary", onclick: async () => {
          albumOut.hidden = false;
          try {
            const r = await api(P(p, "/export/album"), { method: "POST",
              body: { filter: filterFor(preset), dest: dest.value.trim(), files: files.value, copy: copy.checked, clean: clean.checked } });
            albumOut.className = "result";
            albumOut.textContent = `${copy.checked ? "Copied" : "Linked"} ${n(r.created)} new, ${n(r.skipped)} already there${r.removed ? `, removed ${n(r.removed)}` : ""} → ${r.dest}`;
          } catch (e) { albumOut.className = "result err"; albumOut.textContent = e.message; }
        } }, icon("folder"), "Make album folder"))),
      h("div", { class: "panel stack-v" }, h("b", {}, "Downloads"), links),
      h("div", { class: "panel stack-v", id: "xmp" },
        h("b", {}, "For Lightroom, Capture One, darktable or digiKam (XMP sidecars)"),
        h("p", { class: "note" }, "Writes small .xmp files next to each photo so your editor sees your work. The photos themselves are never touched."),
        h("div", { class: "form-grid" }, h("label", { class: "field" }, h("span", {}, "File naming"), xStyle)),
        h("div", { class: "row" }, h("span", { class: "muted" }, "Include:"),
          [["rating", "Stars, picks, rejects"], ["label", "Colour labels"], ["keywords", "Keywords (people, event, place)"],
            ["people", "People in image"], ["regions", "Face regions"]].map(([k, t]) => h("label", { class: "check" }, inc[k], h("span", {}, t)))),
        h("label", { class: "check" }, xUpdate, h("span", {}, "Also update .xmp files that already exist (never lowers a rating or replaces a label another app set; the old files are kept so this can be undone)")),
        h("label", { class: "check" }, xConfirm, h("span", {}, "I understand this adds .xmp files to my photo folders")),
        xOut,
        h("div", {}, h("button", { class: "btn", onclick: async () => {
          xOut.hidden = false;
          try {
            const include = Object.entries(inc).filter(([, el]) => el.checked).map(([k]) => k);
            const r = await api(P(p, "/export/xmp"), { method: "POST", body: { confirm: xConfirm.checked, style: xStyle.value, update_existing: xUpdate.checked, include, filter: filterFor(preset) } });
            xOut.className = r.errors.length ? "result err" : "result";
            clear(xOut).append(`${n(r.written)} written, ${n(r.updated)} updated, ${n(r.skipped_existing)} left alone`,
              r.kept_theirs ? `, ${n(r.kept_theirs)} kept another app's rating or label` : "", ".",
              r.backup_dir ? h("button", { class: "btn small", style: { marginLeft: "8px" }, onclick: async () => {
                const u = await api(P(p, "/export/xmp/restore"), { method: "POST", body: { backup_dir: r.backup_dir } });
                toast(`Put back ${plural(u.restored, "sidecar")}`);
              } }, "Undo the updates") : null,
              r.errors.map((e) => h("div", { class: "path" }, e)));
          } catch (e) { xOut.className = "result err"; xOut.textContent = e.message; }
        } }, "Write .xmp files"))),
      h("div", { class: "panel stack-v" },
        h("b", {}, "Your work, portable"),
        h("p", { class: "note" }, "Every decision, album, person, event and folder name in one file, matched by each photo's content, so it can be brought back into a fresh catalog even after files move."),
        h("div", { class: "row" }, h("a", { class: "btn", href: P(p, "/export/decisions.json") }, icon("download"), "Download decisions (JSON)"),
          fileIn, h("label", { class: "check" }, overwrite, h("span", {}, "replace decisions I already made")),
          h("button", { class: "btn", onclick: async () => {
            const f = fileIn.files?.[0];
            if (!f) { toast("Choose a decisions file first"); return; }
            try {
              const data = JSON.parse(await f.text());
              const r = await api(P(p, "/import/decisions"), { method: "POST", body: { data, overwrite: overwrite.checked } });
              toast(`Imported ${plural(r.decisions, "decision")}, ${plural(r.albums, "album")}, ${plural(r.people, "named face")} (${n(r.unmatched)} not found)`);
            } catch (e) { toast(e.message, { kind: "error" }); }
          } }, "Import"))),
      h("div", { class: "panel stack-v" },
        h("b", {}, "Bring decisions in"),
        h("p", { class: "note" }, "Stars, picks, rejects and colour labels from Lightroom Classic (a copy of the catalog is read; Lightroom's own file is never opened), or stars you set in camera. Photos you've already decided are left alone unless you tick replace. One undoable step."),
        h("label", { class: "field" }, h("span", {}, "Lightroom catalog (.lrcat)"), lrPath),
        h("div", { class: "row" },
          h("button", { class: "btn", onclick: async () => {
            impOut.hidden = false;
            try { const r = await api(P(p, "/import/lightroom"), { method: "POST", body: { path: lrPath.value.trim(), overwrite: overwrite.checked } });
              impOut.className = "result"; impOut.textContent = `Imported ${n(r.imported)} of ${plural(r.in_catalog, "photo")} in that catalog (${n(r.unmatched)} ${agree(r.unmatched, "isn't", "aren't")} in this library).`; }
            catch (e) { impOut.className = "result err"; impOut.textContent = e.message; }
          } }, "Import from Lightroom"),
          h("button", { class: "btn", onclick: async () => {
            try { const r = await api(P(p, "/import/camera-ratings"), { method: "POST", body: {} }); toast(`Used ${plural(r.n, "in-camera rating")}`); }
            catch (e) { toast(e.message, { kind: "error" }); }
          } }, "Use stars set in camera")),
        impOut));
  }

  // ------------------------------------------------------------ AI assistants (MCP)
  function assistantsSection(body) {
    const bin = store.meta?.joeylab_bin || "joeylab";
    const line = (cmd) => h("div", { class: "row" }, h("code", { class: "mono block grow" }, cmd),
      h("button", { class: "btn small", onclick: async () => {
        try { await navigator.clipboard.writeText(cmd); toast("Copied"); } catch { toast(cmd); }
      } }, "Copy"));
    body.append(h("section", { class: "section" }, h("h2", {}, "Use with an AI assistant"),
      h("p", { class: "note" }, "JoeyLab never calls an AI service itself. If you use an assistant such as Claude Code or Codex, add JoeyLab to it once (as an MCP server) and it can search and organise your libraries from a conversation: \"make an album of the 30 best photos of Alex from 2025\". Whatever it changes is logged under its name and can be undone here; photo files are never touched."),
      h("h3", {}, "Claude Code"), line(`claude mcp add joeylab -- ${bin} mcp`),
      h("h3", {}, "Codex"), line(`codex mcp add joeylab -- ${bin} mcp`),
      h("p", { class: "note" }, "Any other MCP client: run ", h("code", { class: "mono" }, bin), " with the argument ", h("code", { class: "mono" }, "mcp"),
        ". Add ", h("code", { class: "mono" }, "--read-only"), " after mcp if the assistant should only look.")));
  }

  // ------------------------------------------------------------ people and privacy
  function aboutSection(body) {
    const m = store.meta || {};
    // [name, what it's for, licence]: the models and data JoeyLab downloads, and the fonts it shows.
    const credits = [
      ["SigLIP 2 (Google)", "search", "Apache-2.0"],
      ["CLIP ViT-L/14 (OpenAI)", "stacking whole scenes, similar photos and taste", "MIT"],
      ["LAION aesthetic predictor", "how good a photo looks", "Apache-2.0"],
      ["MediaPipe face landmarker (Google)", "closed eyes and smiles", "Apache-2.0"],
      ["InsightFace buffalo_s (SCRFD and ArcFace)", "face recognition, if you turn it on with the most accurate models", "Non-commercial research use only"],
      ["OpenCV YuNet and SFace", "face recognition, if you turn it on with the models free for any use", "MIT and Apache-2.0"],
      ["GeoNames (geonames.org)", "place names", "CC BY 4.0"],
      ["Natural Earth (naturalearthdata.com)", "the land and borders of the map in Places, which comes with JoeyLab", "Public domain"],
      ["© OpenStreetMap contributors", "street map tiles, when you turn them on", "ODbL"],
      ["Hanken Grotesk, JetBrains Mono and Newsreader", "the fonts", "SIL Open Font License 1.1"],
    ];
    body.append(
      h("section", { class: "section" }, h("h2", {}, "JoeyLab"),
        h("p", {}, h("b", {}, `Version ${m.version || ""}`), h("br"), `Made by ${m.author || "Yaduraj Bhageria"}. Free and open, under the MIT licence.`),
        h("p", { class: "note" }, "JoeyLab runs entirely on this Mac. Your photos are only ever read; what it learns about them stays in its own catalog in ~/.joeylab, and nothing about them is sent anywhere. It never deletes anything on its own: anything you choose to remove goes to the Trash, after you confirm.")),
      h("section", { class: "section" }, h("h2", {}, "Credits"),
        h("p", { class: "note" }, "JoeyLab is built on other people's work. The models and data it downloads, and the fonts it uses:"),
        h("ul", { class: "note", style: { margin: "0", paddingLeft: "18px", display: "grid", gap: "4px" } },
          credits.map(([name, use, licence]) => h("li", {}, h("b", {}, name), `: ${use}. ${licence}.`))),
        h("p", { class: "note" }, "Leaflet, Natural Earth's outlines, exiftool and the open-source libraries JoeyLab uses are listed with their licences in THIRD_PARTY_NOTICES.md, which comes with JoeyLab's source code.")),
    );
  }

  async function privacySection(body) {
    const confirm = h("input", { class: "input", id: "forget-faces", autocomplete: "off", style: { maxWidth: "260px" } });
    let rec = null;
    try { rec = await api("/api/faces"); } catch { /* shown without the switch */ }
    // Who you are: one of the people named in this library (yours alone; each tailnet account has its own).
    let people = [], me = null, sw = null;
    try {
      const d = await api(P(p, "/people"), { params: { limit: 0 } });
      people = d.people || []; me = d.me ?? null; sw = d.switch || null;
    } catch { /* no faces */ }
    // Face recognition for the whole Mac: off until turned on here (or with `joeylab faces enable`),
    // choosing its models after seeing what each is good at and its licence. Once on: the models in
    // use, switching to the others, and how far this library is after a switch.
    function recognitionSwitch() {
      const ask = h("div");
      const turnOff = async () => {
        try {
          await api("/api/faces", { method: "POST", body: { enabled: false } });
          toast("Face recognition is off. Faces already found are kept.");
          render();
        } catch (e) { toast(e.message, { kind: "error" }); }
      };
      const was = rec.choices.find((x) => x.key === rec.model);
      const box = h("input", { type: "checkbox", checked: rec.enabled, onchange: (e) => {
        const on = e.target.checked;
        e.target.checked = rec.enabled;             // changes only once confirmed below
        clear(ask).append(on
          ? h("div", { class: "panel" }, chooseModels(rec, { current: rec.model, library: p, onDone: () => render(), onCancel: () => clear(ask) }))
          : h("div", { class: "row" }, h("span", { class: "muted" }, "Turn off face recognition for every library? Faces and names already found are kept."),
              h("button", { class: "btn small danger", onclick: turnOff }, "Turn off"),
              h("button", { class: "btn small ghost", onclick: () => clear(ask) }, "Cancel")));
      } });
      const running = store.job?.state === "running";
      return h("div", { class: "stack-v" },
        h("label", { class: "check" }, box, h("span", {}, h("b", {}, "Find and recognise faces"), h("br"),
          h("span", { class: "muted" }, rec.enabled ? "On for every library on this Mac." + (rec.installed ? "" : " Not installed yet: uv sync --extra faces")
            : was ? `Off. When you turn it on, you can keep the “${was.title}” models or choose the others.`
              : "Off. You choose its models when you turn it on: the most accurate, for personal, non-commercial use only, or ones free for any use, including paid work."))),
        ask,
        rec.enabled && was ? currentModels(rec, { library: p, onDone: () => render() }) : null,
        rec.enabled ? switchNote(sw, rec, { running, onStart: async () => {
          try { await api(P(p, "/jobs"), { method: "POST", body: {} }); toast("Finding faces again in this library."); } catch (e) { toast(e.message, { kind: "error" }); }
        } }) : null);
    }
    const meSel = h("select", { class: "input", id: "who-me", style: { width: "auto" }, onchange: async (e) => {
      try {
        await api(P(p, "/me"), { method: "POST", body: { person_id: e.target.value ? +e.target.value : null } });
        store.peopleList = null;
        toast(e.target.value ? "Got it: that's you" : "No one is marked as you now");
      } catch (err) { toast(err.message, { kind: "error" }); }
    } }, h("option", { value: "", selected: me === null }, "Not set"),
    [...people].sort((a, b) => a.name.localeCompare(b.name)).map((x) => h("option", { value: x.id, selected: x.id === me }, x.name)));
    body.append(
      h("section", { class: "section" }, h("h2", {}, "You"),
        people.length
          ? h("div", { class: "row", style: { gap: "10px", alignItems: "center" } }, h("label", { for: "who-me" }, "Who are you?"), meSel)
          : h("p", { class: "muted" }, "Name yourself under People first, then choose yourself here."),
        h("p", { class: "note" }, "JoeyLab then finds photos of you when you search \"me\" (\"me in Lisbon 2023\"), lists you first, and tells assistants who you are. If you're named the same in your other libraries, you're you there too.")),
      h("section", { class: "section" }, h("h2", {}, "Faces"),
        h("p", { class: "note" }, "Face recognition runs only on this Mac. Each face becomes a list of numbers that identifies the person: biometric data about your family and friends. You can remove all of it."),
        rec ? recognitionSwitch() : null,
        h("div", { class: "row" }, h("label", { class: "muted", for: "forget-faces" }, `Type ${p} to confirm`), confirm, h("button", { class: "btn danger", onclick: async () => {
          try { await api(P(p, "/faces"), { method: "DELETE", params: { confirm: confirm.value.trim() } }); toast("Every face, name and face crop is gone, and faces won't be looked for again here."); }
          catch (e) { toast(e.message, { kind: "error" }); }
        } }, "Forget all face data")),
        h("p", { class: "note" }, "Turn faces back on from the People page.")),
      h("section", { class: "section" }, h("h2", {}, "Who can open JoeyLab"),
        h("p", { class: "note" }, "Only you: at this Mac, or on your own phone and other devices signed in to your Tailscale account. Anyone else who reaches it sees a notice that it's private. To show someone photos, send them a share link for an album or an event (Albums › Sharing)."),
        h("p", { class: "note" }, "Changes are only accepted from JoeyLab's own pages (other websites can't make your browser change anything here), and only on this Mac's own names.")),
      h("section", { class: "section" }, h("h2", {}, "Network"),
        h("p", { class: "note" }, "Search, faces, places and everything else run on this Mac. Network use: the one-time model and town-list downloads, and map tiles if you turn on Street map. JoeyLab never calls an AI service itself.")));
  }

  // ------------------------------------------------------------ backups
  async function backupsSection(body) {
    let d;
    try { d = await api(P(p, "/backups")); } catch (e) { body.append(h("div", { class: "result err" }, e.message)); return; }
    body.append(h("section", { class: "section" }, h("h2", {}, "Catalog backups"),
      h("p", { class: "note" }, "Your photos are never in the catalog; your decisions, names, albums and settings are. JoeyLab copies the catalog every day (after checking it's intact) and keeps 14 daily, 8 weekly and 12 monthly copies, plus one before every upgrade."),
      h("div", { class: "stack-v" }, d.backups.map((b) => h("div", { class: "drive-row" }, icon("shield"), h("span", { class: "grow mono" }, b.name),
        h("span", { class: "muted" }, `${bytes(b.size)} · ${ago(b.modified)}`)))),
      h("div", { class: "row" }, h("button", { class: "btn", onclick: async () => { await api(P(p, "/backups"), { method: "POST", body: {} }); toast("Backed up"); render(); } }, "Back up now")),
      h("p", { class: "note" }, "To bring a backup back, in Terminal:"),
      h("code", { class: "mono block" }, `joeylab service stop\njoeylab restore -p ${p} --yes        # or --from <file>\njoeylab service start`),
      h("p", { class: "note" }, "The catalog being replaced is kept beside the backups, so a restore can itself be undone.")));
  }

  // ------------------------------------------------------------ devices
  function devicesSection(body) {
    const m = store.meta || {};
    const url = m.tailnet_url || null;
    body.append(
      h("section", { class: "section" }, h("h2", {}, "Open on other devices"),
        h("div", { class: "panel stack-v" },
          url ? h("div", { class: "row" }, h("span", { class: "mono" }, url),
            h("button", { class: "btn small", onclick: async () => {
              try { await navigator.clipboard.writeText(url); toast("Link copied"); } catch { toast(url); }
            } }, "Copy link")) : h("div", { class: "muted" }, "JoeyLab isn't published to your tailnet. Start it with Tailscale running (joeylab serve)."),
          h("p", { class: "note" }, "Any phone, tablet or laptop signed in to your Tailscale network can open this link. Nobody else can connect. On an iPhone or iPad, use Share › Add to Home Screen to get JoeyLab as an app."),
          h("div", { class: "muted small" }, m.ai ? (m.ai_loaded ? "AI model loaded." : "AI model loads on first search.") : "AI layer not installed."))));
  }

  render();
  return { refresh: render };
}

function confirmInline(btn, question, run) {
  const box = h("span", { class: "row", style: { gap: "6px", display: "inline-flex" } },
    h("span", { class: "muted small" }, question),
    h("button", { class: "btn small danger", onclick: async () => {
      try { await run(); } catch (e) { toast(e.message, { kind: "error" }); }
    } }, "Yes"),
    h("button", { class: "btn small ghost", onclick: () => box.replaceWith(btn) }, "No"));
  btn.replaceWith(box);
}
