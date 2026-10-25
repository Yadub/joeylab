// Home: every library with its cover, plus creating a new one.

import { api } from "../api.js";
import { ago, clear, debounce, h, icon, n, plural, toast } from "../ui.js";
import { chooseModels, modelName } from "./facemodels.js";
import { folderPicker } from "./folderpicker.js";

export function mount(root) {
  const page = h("div", { class: "page" });
  root.append(page);

  let grid = null;                          // the library cards; the form below them is drawn once
  async function render() {
    clearTimeout(timer);
    let projects;
    try { projects = await api("/api/projects"); }
    catch (e) { clear(page).append(h("div", { class: "result err" }, e.message)); grid = null; return; }
    if (!projects.length) { clear(page).append(welcome()); grid = null; return; }
    if (!grid) {
      grid = h("div", { class: "proj-grid" });
      clear(page).append(
        h("div", { class: "page-head" },
          h("div", {}, h("h1", {}, "Your libraries"),
            h("div", { class: "sub" }, "Each library is a set of folders JoeyLab keeps track of. Your photos stay where they are."))),
        grid, newProjectForm());
    }
    clear(grid).append(projects.map((p) => p.error ? h("div", { class: "proj-card" },
      h("div", { class: "cover" }), h("div", {}, h("h3", {}, p.name), h("div", { class: "result err" }, p.error))) :
      h("a", { class: "proj-card", href: `#/p/${encodeURIComponent(p.name)}` },
        h("div", { class: "cover", style: p.cover ? { backgroundImage: `url(/img/${encodeURIComponent(p.name)}/t/${p.cover.id}?s=m&v=${p.cover.v})` } : {} }),
        h("div", {}, h("h3", {}, p.name),
          h("div", { class: "muted" }, [plural(p.photos, "photo"), p.purpose === "archive" ? "an archive" : `${n(p.picks)} picked`,
            p.last_scan ? `updated ${ago(p.last_scan)}` : "not read yet"].join(" · ")),
          p.job?.state === "running" ? h("div", { class: "pill accent", style: { marginTop: "6px" } }, h("span", { class: "spinner" }), p.job.stage) : null))));
    // A library being read: keep its card's progress current.
    if (projects.some((p) => p.job?.state === "running")) timer = setTimeout(() => { if (page.isConnected) render(); }, 3000);
  }

  // No library yet: say what JoeyLab does, what happens next, what it downloads, and ask for a folder.
  function welcome() {
    const step = (ic, title, text) => h("div", { class: "welcome-step" }, icon(ic), h("b", {}, title), h("span", { class: "muted" }, text));
    return h("div", { class: "stack-v welcome", style: { gap: "24px" } },
      h("div", { class: "page-head" },
        h("div", {}, h("h1", {}, "Welcome to JoeyLab"),
          h("div", { class: "sub" }, "Point JoeyLab at a folder of photos. It reads them, stacks bursts, flags weak frames and suggests the keepers. "
            + "Everything stays on this Mac, and it never moves or deletes anything on its own."))),
      h("div", { class: "welcome-steps" },
        step("folder", "1. Choose a folder", "A shoot, a year of photos or a whole drive. Its subfolders are included."),
        step("refresh", "2. JoeyLab reads it", "Stacks, duplicates and suggestions are ready a minute or two in; search, people and places follow. Your files are only read, never changed."),
        step("stack", "3. Cull, browse and tidy", "Pick the keepers, find people and places, and see what's duplicated.")),
      newProjectForm({ first: true }),
      h("details", { class: "welcome-downloads" },
        h("summary", {}, "What JoeyLab downloads, once"),
        h("p", { class: "note" }, "JoeyLab works offline. The first time a feature needs one, it downloads its model once and keeps it on this Mac. "
          + "Your photos are never uploaded."),
        h("ul", { class: "note" },
          h("li", {}, "The search model (SigLIP 2, about 1.5 GB) and the picture model for similar photos and scenes (CLIP, about 1.7 GB), during the first update."),
          h("li", {}, "A small model for closed eyes and smiles (4 MB)."),
          h("li", {}, "The offline list of towns, for naming places (GeoNames, about 5 MB)."),
          h("li", {}, "Face recognition models, only if you turn face recognition on (it's off until you do). You choose one of two sets: "
            + "the most accurate (InsightFace, about 130 MB), for personal, non-commercial use only, or ones free for any use, "
            + "including paid work (OpenCV's YuNet and SFace, about 39 MB)."))),
      faceChoice());
  }

  // Face recognition from the start, if wanted: off until its models are chosen (here, or later in People or Settings).
  function faceChoice() {
    const body = h("div", { class: "stack-v" });
    const paint = (st) => clear(body).append(st.enabled
      ? h("p", { class: "note" }, "Face recognition is on, with the models ", modelName(st.choices.find((m) => m.key === st.model)),
        ". Faces are found as your photos are read.")
      : [h("p", { class: "note" }, "JoeyLab can find and name the people in your photos, on this Mac only. It's off until you choose its models, here or later under People."),
        chooseModels(st, { current: st.model, onDone: paint })]);
    const box = h("details", { class: "welcome-downloads welcome-faces", ontoggle: async () => {
      if (!box.open || body.firstChild) return;
      try { paint(await api("/api/faces")); } catch (e) { body.append(h("div", { class: "result err" }, e.message)); }
    } }, h("summary", {}, "Face recognition (optional): choose its models"), body);
    return box;
  }

  let timer = null;
  render();
  return { refresh: render, unmount() { clearTimeout(timer); } };
}

/** A library name from anything typed, as the server makes it: "Smith Wedding 2024" is "smith-wedding-2024". */
export const slug = (s) => (s || "").normalize("NFKD").replace(/[^\x00-\x7f]/g, "").toLowerCase()
  .replace(/[^a-z0-9_.]+/g, "-").replace(/([-_.])[-_.]+/g, "$1").replace(/^[-_.]+|[-_.]+$/g, "").slice(0, 64).replace(/[-_.]+$/, "");
const nameFrom = (path) => slug(path.split("/").filter(Boolean).pop() || "");

/** What a folder check found, in a line: "1,240 photos and 12 videos", or why it can't be used. */
export function checkLine(c) {
  if (c.ok) {
    const parts = [c.photos ? plural(c.photos, "photo") : null, c.videos ? plural(c.videos, "video") : null].filter(Boolean);
    return `${c.partial ? "At least " : ""}${parts.join(" and ")} in this folder and its subfolders${c.note ? `. ${c.note}` : ""}`;
  }
  return c.reason;
}

export function newProjectForm({ first = false } = {}) {
  const name = h("input", { class: "input", id: "np-name", placeholder: "smith-wedding", autocomplete: "off" });
  const path = h("input", { class: "input mono", id: "np-path", placeholder: "~/Pictures/Wedding", required: true, autocomplete: "off" });
  const saved = h("span", { class: "muted np-saved", "aria-live": "polite" });
  const found = h("div", { class: "result", hidden: true, "aria-live": "polite" });
  const anyway = h("input", { type: "checkbox", id: "np-anyway" });
  const anywayBox = h("label", { class: "check", hidden: true }, anyway,
    h("span", {}, "Use it anyway: I'll copy photos into it later (each update finds new ones)"));
  let named = false;                        // a name you typed yourself isn't replaced
  const showName = () => {
    const s = slug(name.value);
    saved.textContent = name.value.trim() && s !== name.value.trim() ? (s ? ` · saved as ${s}` : " · use at least one letter or digit") : "";
  };
  name.addEventListener("input", () => { named = !!name.value; showName(); });
  const look = debounce(async () => {
    const folder = path.value.trim();
    if (!folder) { found.hidden = true; anywayBox.hidden = true; return; }
    let c;
    try { c = await api("/api/fs/check", { params: { path: folder } }); } catch { return; }
    if (path.value.trim() !== folder) return;           // typed on since
    found.hidden = false;
    found.className = `result ${c.ok ? "" : c.empty ? "warn-result" : "err"}`;
    found.textContent = checkLine(c);
    anywayBox.hidden = !c.empty;
  }, 450);
  path.addEventListener("input", () => { if (!named) { name.value = nameFrom(path.value.trim()); showName(); } look(); });
  const pickerHost = h("div");
  const out = h("div", { class: "result err", hidden: true });
  const purpose = h("select", { class: "input", id: "np-purpose" },
    h("option", { value: "shoot" }, "A shoot to cull (wedding, event, trip)"),
    h("option", { value: "archive" }, "An archive to explore and tidy"));
  const form = h("form", { class: "panel stack-v", novalidate: true, onsubmit: async (e) => {
    e.preventDefault();
    out.hidden = true;
    const folder = path.value.trim();
    const nm = slug(name.value) || nameFrom(folder);
    const fail = (msg, el) => { out.hidden = false; out.textContent = msg; el?.focus(); };
    if (!folder) return fail("Choose the folder of photos first: type its path, or press Browse…", path);
    if (!nm) return fail("Give the library a name with at least one letter or digit.", name);
    try {
      const r = await api("/api/projects", { method: "POST", body: { name: nm, path: folder, purpose: purpose.value, empty_ok: anyway.checked } });
      toast(`Created the library ${r.name}. Reading its photos in the background.`);
      location.hash = `#/p/${encodeURIComponent(r.name)}`;
    } catch (err) {
      fail(err.message);
      if (/^No photos in/.test(err.message)) anywayBox.hidden = false;
    }
  } },
  h("h2", { class: "label" }, first ? "Start with a folder" : "New library"),
  h("div", { class: "form-grid" },
    h("label", { class: "field span-all" }, h("span", {}, "Folder of photos"),
      h("div", { class: "row", style: { flexWrap: "nowrap" } }, path,
        h("button", { class: "btn", type: "button", onclick: () => {
          if (pickerHost.firstChild) { clear(pickerHost); return; }
          pickerHost.append(folderPicker({ start: path.value.trim() || undefined, onChoose: (pth) => {
            path.value = pth; clear(pickerHost);
            if (!named) { name.value = nameFrom(pth); showName(); }
            look();
          } }));
        } }, "Browse…"))),
    h("label", { class: "field" }, h("span", {}, "Library name", saved), name),
    h("label", { class: "field span-2" }, h("span", {}, "It's"), purpose)),
  pickerHost, found, anywayBox, out,
  h("div", {}, h("button", { class: "btn primary", type: "submit" }, first ? "Create the library and read the photos" : "Create the library")));
  return form;
}
