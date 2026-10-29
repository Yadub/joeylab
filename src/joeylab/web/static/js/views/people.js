// People (and pets): a fast naming stream, the groups still to name, who appears together, and
// whether everyone you care about is covered by good photos.

import { api, P } from "../api.js";
import { store } from "../store.js";
import { agree, clear, dialog, h, icon, n, plural, tabBar, toast } from "../ui.js";
import { chooseModels, switchNote } from "./facemodels.js";
import { waitNote } from "./waiting.js";

export function mount(root, ctx) {
  const page = h("div", { class: "page" });
  root.append(page);
  const p = ctx.project;
  const faceUrl = (fid) => `/img/${encodeURIComponent(p)}/f/${fid}`;
  const lib = (q) => `#/p/${encodeURIComponent(p)}/library?${q}`;
  let tab = ctx.params.get("tab") || "people";
  let showSmall = false;
  let showAll = false;
  const FIRST = 30;
  let keyHandler = null;
  let groupsLeft = () => {};
  let groupGrid = null;                    // the cards of groups still to name
  let renderGen = 0;                       // a late answer for an earlier render is ignored
  let meId = null;                         // the person who is you (Settings › People & privacy, or Edit)
  let everyone = [];                       // everyone named, hidden too, with their other names

  // A name, whatever its case and spacing, belongs to one person: as their name or another name.
  const norm = (s) => String(s || "").trim().replace(/\s+/g, " ").toLowerCase();
  function whoIs(name, except = null) {
    const k = norm(name);
    if (!k) return null;
    for (const x of everyone) {
      if (x.id === except) continue;
      if (norm(x.name) === k) return { x, alias: false };
      if ((x.aliases || []).some((a) => norm(a) === k)) return { x, alias: true };
    }
    return null;
  }
  /** Type-ahead for naming: every name, and each other name pointing at who it is ("Papa → Dad"). */
  const nameOptions = (people) => people.flatMap((x) => [h("option", { value: x.name }),
    ...(x.aliases || []).map((a) => h("option", { value: a, label: `→ ${x.name}` }))]);
  /** Under a name box: says when what's typed is someone's other name, so it's clear who gets the faces. */
  function aliasHint(input) {
    const el = h("div", { class: "aka-hint", "aria-live": "polite" });
    input.addEventListener("input", () => {
      const w = whoIs(input.value);
      el.textContent = w?.alias ? `That's another name for ${w.x.name}.` : "";
    });
    return el;
  }

  // Whether faces are still being looked for: a scan or an update is reading photos. While there are
  // none yet, the page fills in by itself when the update finishes.
  let reading = false, waiting = false;
  async function render() {
    keyHandler = null;
    renderGen++;
    store.peopleList = null;                // names change here: the Library's people list is refetched
    store.overviewStale = true;
    let d, job;
    try {
      [d, job] = await Promise.all([api(P(p, "/people"), { params: { min_photos: showSmall ? 1 : 2, limit: showAll ? 5000 : FIRST } }),
        api(P(p, "/jobs/current")).then((r) => r.job).catch(() => null)]);
    } catch (e) { clear(page).append(h("div", { class: "result err" }, e.message)); return; }
    reading = job?.state === "running";
    waiting = reading && !d.faces;
    // Face recognition off: choose its models here. After a switch of models: how far this library is.
    let rec = null;
    if ((!d.recognition || d.switch) && store.meta?.owner) rec = await api("/api/faces").catch(() => null);
    meId = d.me ?? null;
    everyone = d.people.concat(d.pets || [], d.hidden || []);
    const named = d.people.length + (d.pets || []).length;
    const toName = (d.groups_total || 0) + (d.pet_groups_total || 0);
    const panel = h("div", { class: "stack-v", style: { gap: "28px" } });
    const tabs = tabBar([["people", `People${named ? " · " + n(named) : ""}`],
      ["stream", `Name faces${toName ? " · " + n(toName) : ""}`], ["together", "Together"]], tab,
    (k) => { tab = k; ctx.setParams({ tab: k }); render(); }, { label: "People", id: "people", panel });
    panel.append(tab === "people" ? peopleTab(d) : tab === "stream" ? streamTab(d) : togetherTab());
    clear(page).append(
      h("div", { class: "page-head" },
        h("div", {}, h("h1", {}, "People"),
          h("div", { class: "sub" }, !d.recognition ? "Face recognition is off until you turn it on."
            : !d.enabled ? "Face recognition is off for this library."
            : !d.available ? "Face recognition isn't installed. To add it: uv sync --extra faces"
              : d.faces ? `${plural(d.faces, "face")}${d.pets_found ? ` and ${plural(d.pets_found, "pet")}` : ""} found. Name a group once and JoeyLab finds them everywhere.`
                : reading ? "Looking for faces while JoeyLab reads your photos. They appear here as it finishes."
                  : "Name a group of faces once and JoeyLab finds that person everywhere.")),
        h("div", { class: "actions" }, tabs)),
      !d.recognition ? (rec ? h("section", { class: "panel stack-v faces-off" },
        h("h2", {}, "Turn on face recognition"),
        h("p", { class: "note" }, "To find and name the people in your photos, choose the models face recognition uses. "
          + "Faces are found and recognised on this Mac only."),
        chooseModels(rec, { current: rec.model, library: p, onDone: () => render() }))
        : h("div", { class: "result row" }, "Face recognition is off on this Mac."))
        : !d.enabled ? h("div", { class: "result row" }, "Faces aren't being looked for in this library.",
          h("button", { class: "btn small", onclick: async () => { await api(P(p, "/faces/enable"), { method: "POST", body: { enabled: true } }); toast("Faces will be found during the next update."); render(); } }, "Turn back on")) : null,
      d.recognition && d.enabled ? switchNote(d.switch, rec, { running: reading, onStart: async () => {
        try { await api(P(p, "/jobs"), { method: "POST", body: {} }); toast("Finding faces again in this library."); } catch (e) { toast(e.message, { kind: "error" }); }
      } }) : null,
      d.recognition && d.enabled ? waitNote({ step: "places", progressOf: "faces", text: "People will appear once faces are found and grouped",
        when: () => !d.faces, ready: "Faces are found and grouped", onReady: render }) : null,
      panel,
    );
  }

  /** What to say when there's nobody to show, or null when there are faces to name. */
  function noFaces(d) {
    if (d.faces) return null;
    return h("div", { class: "empty" },
      h("h3", {}, !d.enabled || !d.available ? "Faces aren't being looked for" : reading ? "Faces appear as photos are read" : "No faces in these photos"),
      h("p", {}, !d.recognition ? "Turn on face recognition above to find people in this library."
        : !d.enabled ? "Turn face recognition back on above to find people in this library."
        : !d.available ? "Install face recognition, then update the library, to find people in it."
          : reading ? "JoeyLab looks for faces in every photo it reads. Come back when the update finishes."
            : "JoeyLab looked for faces in every photo here and didn't find any. Photos added later are looked at too."));
  }

  // ------------------------------------------------------------ named people
  function peopleTab(d) {
    const nothing = noFaces(d);
    return h("div", { class: "stack-v", style: { gap: "28px" } },
      d.people.length || d.pets.length ? (d.people.length && store.settings?.purpose === "shoot" ? coverage(d.people) : null) : nothing || h("div", { class: "empty" }, h("h3", {}, "Nobody named yet"),
        h("p", {}, d.groups_total ? `${plural(d.groups_total, "group")} of faces ${agree(d.groups_total, "is", "are")} waiting. Naming the biggest few takes a minute.`
          : "Faces were found, but none recognisably in more than one photo. Name them one by one under Name faces."),
        h("button", { class: "btn primary", onclick: () => { tab = "stream"; ctx.setParams({ tab }); render(); } }, "Start naming")),
      d.people.length && meId === null ? whoAreYou(d.people) : null,
      d.people.length ? h("section", { class: "section" }, h("h2", {}, "People"), h("div", { class: "people-grid" }, d.people.map(personCard))) : null,
      d.pets.length ? h("section", { class: "section" }, h("h2", {}, "Pets"), h("div", { class: "people-grid" }, d.pets.map(personCard))) : null,
      d.hidden.length ? h("details", { class: "section" }, h("summary", { class: "label" }, `Hidden · ${d.hidden.length}`),
        h("div", { class: "people-grid" }, d.hidden.map(personCard))) : null);
  }

  function coverage(people) {
    const rows = [...people].sort((a, b) => a.picks - b.picks || a.good - b.good || a.name.localeCompare(b.name));
    const state = (x) => x.picks === 0 ? ["warn", "No picks yet"] : x.good <= 1 ? ["warn", "Only one good photo"] : ["pick", "Covered"];
    const gaps = rows.filter((x) => state(x)[0] === "warn").length;
    return h("section", { class: "section" },
      h("h2", {}, "Coverage"),
      h("p", { class: "note" }, gaps
        ? `${plural(gaps, "person", "people")} could use more attention before you deliver. A good photo is one where they're not rejected, their eyes are open, and the photo scores 55+ or you've rated or picked it.`
        : "Everyone you've named has picks and several good photos."),
      h("div", { class: "table-wrap" }, h("table", { class: "plain" },
        h("thead", {}, h("tr", {}, h("th", {}, "Person"), h("th", {}, "Seen"), h("th", { style: { textAlign: "right" } }, "Photos"),
          h("th", { style: { textAlign: "right" } }, "Good"), h("th", { style: { textAlign: "right" } }, "Picked"), h("th", {}, ""))),
        h("tbody", {}, rows.map((x) => {
          const [kind, label] = state(x);
          return h("tr", {},
            h("td", {}, h("a", { class: "person-inline", href: lib(`person=${x.id}`) }, x.cover ? h("img", { src: faceUrl(x.cover), alt: "" }) : null, x.name)),
            h("td", { class: "muted" }, x.first ? `${x.first.slice(0, 4)}${x.last && x.last.slice(0, 4) !== x.first.slice(0, 4) ? "–" + x.last.slice(0, 4) : ""}` : ""),
            h("td", { class: "num" }, n(x.photos)), h("td", { class: "num" }, n(x.good)), h("td", { class: "num" }, n(x.picks)),
            h("td", {}, h("span", { class: `pill ${kind}` }, label),
              x.picks === 0 ? h("a", { class: "btn small ghost", style: { marginLeft: "8px" }, href: `#/p/${encodeURIComponent(p)}/cull?status=undecided&person=${x.id}` }, "Cull their photos") : null));
        })))));
  }

  function personCard(x) {
    return h("div", { class: "person-card" },
      h("a", { href: lib(`person=${x.id}`), class: "avatar", title: `Show ${x.name}'s photos` },
        x.cover ? h("img", { src: faceUrl(x.cover), alt: x.name, loading: "lazy" }) : null),
      h("div", { class: "who" },
        h("b", {}, x.name, x.id === meId ? h("span", { class: "pill accent", style: { marginLeft: "6px" } }, "You") : null),
        x.aliases?.length ? h("span", { class: "aka", title: `Also called ${x.aliases.join(", ")}` }, `Also called ${x.aliases.join(", ")}`) : null,
        h("span", { class: "muted" }, `${plural(x.photos, "photo")} · ${n(x.picks)} picked`)),
      h("div", { class: "row", style: { gap: "4px" } },
        h("button", { class: "btn small ghost", "aria-label": `Check faces of ${x.name}`, onclick: () => review(x) }, "Check faces"),
        h("button", { class: "btn small ghost", "aria-label": `Edit ${x.name}`, onclick: () => rename(x) }, "Edit")));
  }

  // Which person is you: "photos of me" in search, you first in lists, a "You" badge.
  async function setMe(pid) {
    try {
      await api(P(p, "/me"), { method: "POST", body: { person_id: pid } });
      store.peopleList = null;
      toast(pid ? "Got it: that's you" : "No one is marked as you now");
      render();
    } catch (e) { toast(e.message, { kind: "error" }); }
  }

  function whoAreYou(people) {
    const pick = h("select", { class: "input", style: { width: "auto" }, "aria-label": "Which of these is you?",
      onchange: (e) => e.target.value && setMe(+e.target.value) },
    h("option", { value: "" }, "Choose…"), [...people].sort((a, b) => a.name.localeCompare(b.name)).map((x) => h("option", { value: x.id }, x.name)));
    return h("div", { class: "result row", style: { gap: "10px", alignItems: "center" } },
      h("span", {}, "Which of these is you? JoeyLab can then find photos of you (search \"me\") and list you first."), pick);
  }

  // Undo for a merge: the one who went comes back with their faces and names.
  const unmergeAction = (undo, who, after = () => {}) => async () => {
    try {
      await api(P(p, "/people/unmerge"), { method: "POST", body: { undo } });
      store.peopleList = null;
      toast(`${who} is back, with their photos and names`);
      after(); render();
    } catch (e) { toast(e.message, { kind: "error" }); }
  };

  function rename(x) {
    const input = h("input", { class: "input", value: x.name, id: "rename-person", autocomplete: "off" });
    const isMe = h("input", { type: "checkbox", checked: x.id === meId });
    const kind = h("select", { class: "input" }, h("option", { value: "person", selected: x.kind !== "pet" }, "A person"),
      h("option", { value: "pet", selected: x.kind === "pet" }, "A pet"));
    // Other names, as they'll be saved: nothing changes until Save, except a merge you ask for.
    let names = [...(x.aliases || [])];
    let merged = false;
    const has = (nm) => names.some((a) => norm(a) === norm(nm));
    const keep = h("input", { type: "checkbox", checked: true });
    const keepText = h("span", {});
    const keepRow = h("label", { class: "check", hidden: true }, keep, keepText);
    const note = h("p", { class: "note" });
    const chips = h("div", { class: "aka-chips" });
    const clash = h("div", { class: "result aka-clash", hidden: true, role: "alert" });
    const add = h("input", { class: "input", placeholder: "Add a name and press Enter", "aria-label": "Add another name", autocomplete: "off",
      onkeydown: (e) => {
        if (e.key !== "Enter" || !add.value.trim()) return;       // Enter in an empty box saves, as anywhere in the dialog
        e.preventDefault();
        addName(add.value);
      },
      oninput: () => { clash.hidden = true; } });
    // The old name can stay as another name when renaming (unless it's one already, or merging).
    const keepShown = () => !!input.value.trim() && norm(input.value) !== norm(x.name) && !has(x.name) && !whoIs(input.value, x.id);
    const paint = () => {
      clear(chips).append(names.length ? names.map((nm) => h("span", { class: "chip aka-chip" },
        h("span", { class: "aka-name", title: nm }, nm),
        h("button", { type: "button", class: "aka-main", title: `Show ${nm} as the name everywhere`, onclick: () => makeMain(nm) }, "Make this the main name"),
        h("button", { type: "button", class: "aka-x", title: `Remove ${nm}`, "aria-label": `Remove ${nm}`, onclick: () => removeName(nm) }, icon("close"))))
        : h("span", { class: "muted" }, "None yet: a nickname, a full name, what the family calls them…"));
      keepRow.hidden = !keepShown();
      keepText.textContent = `Keep “${x.name}” as another name`;
      const w = whoIs(input.value, x.id);
      note.textContent = w ? `${w.alias ? `“${input.value.trim()}” is another name for ${w.x.name}` : `${w.x.name} is already someone in your library`}. `
        + `Saving makes them one person: ${x.name}'s photos and names join ${w.x.name}. You can undo it straight after.`
        : "Search, ⌘K and naming faces find them by any of these names. Only the main name is written into files.";
      note.classList.toggle("warn-note", !!w);
    };
    const makeMain = (nm) => {
      const old = input.value.trim();
      names = names.map((a) => (a === nm ? old : a)).filter(Boolean);
      input.value = nm;
      paint();
    };
    const removeName = (nm) => {
      names = names.filter((a) => a !== nm);
      if (norm(nm) === norm(x.name)) keep.checked = false;       // you took the old name off: it stays off
      paint();
      add.focus();
    };
    // Add what's typed; false (with the reason shown) when it can't be added.
    function addName(raw) {
      const nm = raw.trim().replace(/\s+/g, " ");
      clash.hidden = true;
      if (!nm) return true;
      if (nm.length > 80) { showClash("Up to 80 characters, please."); return false; }
      if (norm(nm) === norm(input.value) || has(nm)) { add.value = ""; return true; }
      const w = whoIs(nm, x.id);
      if (w) {
        showClash(`${w.alias ? `“${nm}” is already another name for ${w.x.name}` : `${w.x.name} is already someone in your library`}. `
          + "A name can belong to only one person. If they're the same person, merge them:",
        h("div", { class: "row" },
          h("button", { type: "button", class: "btn small primary", onclick: () => mergeIn(w.x) }, `Merge ${w.x.name} into ${x.name}`),
          h("button", { type: "button", class: "btn small ghost", onclick: () => { clash.hidden = true; add.select(); } }, "Not the same person")));
        return false;
      }
      names.push(nm);
      add.value = "";
      paint();
      return true;
    }
    function showClash(msg, actions = null) {
      clear(clash).append(h("span", {}, msg), actions);
      clash.hidden = false;
    }
    // The other person's photos join this one now (Undo is offered), and their names show here.
    async function mergeIn(other) {
      let r;
      try { r = await api(P(p, `/people/${x.id}/merge`), { method: "POST", body: { other: other.id } }); }
      catch (e) { toast(e.message, { kind: "error" }); return; }
      merged = true;
      store.peopleList = null;
      everyone = everyone.filter((y) => y.id !== other.id);
      for (const nm of [other.name, ...(other.aliases || [])]) if (!has(nm) && norm(nm) !== norm(input.value)) names.push(nm);
      clash.hidden = true;
      add.value = "";
      paint();
      add.focus();
      toast(`Merged ${other.name} into ${x.name}`, { ms: 12000, action: unmergeAction(r.undo, other.name, () => { merged = false; dlg.close(); }) });
    }
    input.addEventListener("input", paint);
    paint();
    const dlg = dialog(`Edit ${x.name}`, h("div", { class: "stack-v" },
      h("label", { class: "field" }, h("span", {}, "Name"), input), keepRow,
      h("div", { class: "field" }, h("span", {}, "Also called"), chips, add, clash), note,
      h("label", { class: "field" }, h("span", {}, "This is"), kind),
      h("label", { class: "check" }, isMe, h("span", {}, "This is me"))), [
      { label: "Forget…", run: () => { forget(x); } },
      { label: "Hide", run: async () => { await api(P(p, `/people/${x.id}/hidden`), { method: "POST", body: { hidden: true } }); render(); } },
      { label: "Cancel" },
      { label: "Save", primary: true, submit: true, run: async () => {
        if (!addName(add.value)) return false;                 // a name typed but not yet added counts
        const name = input.value.trim().replace(/\s+/g, " ");
        if (!name) { input.focus(); return false; }
        const aliases = names.filter((a) => norm(a) !== norm(name)).concat(keepShown() && keep.checked ? [x.name] : []);
        const into = whoIs(name, x.id);
        try {
          if (kind.value !== (x.kind || "person")) await api(P(p, `/people/${x.id}/kind`), { method: "POST", body: { kind: kind.value } });
          const r = await api(P(p, `/people/${x.id}/names`), { method: "POST", body: { name, aliases, merge: !!into } });
          if (isMe.checked !== (x.id === meId)) await api(P(p, "/me"), { method: "POST", body: { person_id: isMe.checked ? (r.id ?? x.id) : null } });
          store.peopleList = null;
          const same = !r.merged && r.before.name === r.name && r.before.aliases.join("\n") === r.aliases.join("\n");
          if (r.merged) toast(`Merged ${x.name} into ${r.name}`, { ms: 12000, action: unmergeAction(r.undo, x.name) });
          else if (same) toast("Saved");
          else toast("Saved", { ms: 8000, action: async () => {
            try { await api(P(p, `/people/${x.id}/names`), { method: "POST", body: r.before }); store.peopleList = null; toast("Undone"); render(); }
            catch (e) { toast(e.message, { kind: "error" }); }
          } });
          merged = false;
          render();
        } catch (e) { toast(e.message, { kind: "error" }); return false; }
      } },
    ]);
    dlg.addEventListener("close", () => { if (merged) render(); });   // a merge made here shows when it closes
  }

  // Taking a name off every face is hours of naming: ask first, and offer Undo straight after.
  function forget(x) {
    const dlg = dialog(`Forget ${x.name}?`, h("p", { class: "note" },
      `${x.name}'s name comes off every face it's on (${plural(x.photos, "photo")}). The faces stay, unnamed.`
      + `${x.aliases?.length ? ` Their other names (${x.aliases.join(", ")}) go too.` : ""} You can undo this straight after.`), [
      { label: "Cancel", primary: true },
      { label: `Forget ${x.name}`, run: async () => {
        try {
          const r = await api(P(p, `/people/${x.id}`), { method: "DELETE" });
          store.peopleList = null;
          toast(`Forgot ${x.name}`, { ms: 12000, action: async () => {
            await api(P(p, "/people/name"), { method: "POST", body: { name: r.name, kind: r.kind, faces: r.faces, aliases: r.aliases } });
            store.peopleList = null;
            toast(`${x.name} is back on ${plural(r.faces.length, "face")}`);
            render();
          } });
          render();
        } catch (e) { toast(e.message, { kind: "error" }); }
      } },
    ]);
    dlg.querySelector(".dlg-actions .btn.primary")?.focus();
  }

  async function review(x) {
    const d = await api(P(p, `/people/${x.id}/faces`));
    const grid = h("div", { class: "face-review" }, d.faces.map((f) => {
      const cell = h("div", { class: "fr" },
        h("img", { src: faceUrl(f.id), alt: "", loading: "lazy" }),
        h("button", { class: "btn small ghost", type: "button", title: "This isn't them", onclick: async () => {
          await api(P(p, `/faces/${f.id}/unassign`), { method: "POST", body: {} });
          cell.remove();
        } }, icon("close"), "Not them"));
      return cell;
    }));
    const dlg = dialog(`${x.name}: ${plural(d.faces.length, "face")}`, h("div", { class: "stack-v" },
      h("p", { class: "note" }, "Remove any face that isn't them. JoeyLab remembers and won't put it back."), grid),
    [{ label: "Done", primary: true, run: () => { render(); } }]);
    dlg.querySelector(".dlg-actions .btn.primary")?.focus();    // Enter closes; it never removes a face
  }

  // ------------------------------------------------------------ naming stream
  function streamTab(d) {
    const nothing = noFaces(d);
    if (nothing && !d.pet_groups?.length) return nothing;
    const host = h("div", { class: "stack-v", style: { gap: "24px" } });
    const datalist = h("datalist", { id: "people-names" }, nameOptions(d.people.concat(d.pets)));
    host.append(datalist);
    const quick = h("section", { class: "section" }, h("h2", {}, "Is this…?"), h("div", { class: "muted" }, "Looking for likely matches…"));
    host.append(quick);
    const myGen = renderGen;
    api(P(p, "/people/suggestions")).then((s) => {
      if (myGen !== renderGen || tab !== "stream" || !quick.isConnected) return;   // you've moved on
      if (!s.suggestions.length) { quick.remove(); return; }
      let i = 0;
      const card = h("div", { class: "ask-card" });
      const likely = (x) => `${{ very: "very likely", likely: "likely" }[x.likely] || "possibly"} the same ${x.kind === "pet" ? "pet" : "person"}`;
      const faces = (ids) => h("div", { class: "ask-faces" }, ids.filter(Boolean).map((fid) => h("img", { src: faceUrl(fid), alt: "", loading: "lazy" })));
      const paint = () => {
        const x = s.suggestions[i];
        if (!x) { quick.replaceWith(h("div", { class: "result" }, "That's every likely match for now. Name more groups below.")); keyHandler = null; return; }
        clear(card).append(
          h("div", { class: "ask-compare" },
            h("figure", { class: "ask-side" }, faces(x.sample),
              h("figcaption", { class: "muted" }, `This group · ${plural(x.photos, "photo")} · `, h("a", { href: lib(`cluster=${x.cluster}`) }, "see them"))),
            h("figure", { class: "ask-side theirs" }, faces(x.person_faces || [x.person_cover]),
              h("figcaption", { class: "muted" }, `${x.name} · `, h("a", { href: lib(`person=${x.person}`) }, "see them")))),
          h("div", { class: "ask-q" }, h("span", { class: "muted" }, likely(x)), h("b", { class: "ask-name" }, `Is this ${x.name}?`)),
          h("div", { class: "row ask-actions" },
            h("button", { class: "btn primary", type: "button", onclick: yes }, "Yes", h("kbd", {}, "Y")),
            h("button", { class: "btn", type: "button", onclick: no, title: "Not this person: they won't be suggested for this group again" }, "No", h("kbd", {}, "N")),
            h("button", { class: "btn ghost", type: "button", onclick: other }, "Someone else…", h("kbd", {}, "S")),
            h("button", { class: "btn ghost", type: "button", onclick: skip, title: "Decide later: this may be asked again" }, "Skip", h("kbd", {}, "→")),
            h("span", { class: "muted" }, `${i + 1} of ${s.suggestions.length}`)));
      };
      // Put an answered question back in front of you (after Undo).
      const again = (x) => { s.suggestions.splice(i, 0, x); paint(); };
      const name = async (x, who) => {
        let r;
        try { r = await api(P(p, "/people/name"), { method: "POST", body: { cluster: x.cluster, name: who } }); }
        catch (e) { toast(e.message, { kind: "error" }); return false; }
        namedElsewhere(x.cluster, r);
        toast(`Named ${r.name || who} · ${plural(x.photos, "photo")}${r.joined ? ` and ${plural(r.joined, "similar group")}` : ""}`, {
          action: async () => {
            try { await api(P(p, `/people/${r.id}/unname`), { method: "POST", body: { faces: r.assigned || [] } }); again(x); toast("Undone"); }
            catch (e) { toast(e.message, { kind: "error" }); }
          } });
        return true;
      };
      const yes = async () => { const x = s.suggestions[i]; if (await name(x, x.name)) { i++; paint(); } };
      const no = async () => {
        const x = s.suggestions[i];
        try { await api(P(p, `/people/${x.person}/not-same`), { method: "POST", body: { cluster: x.cluster } }); }
        catch (e) { toast(e.message, { kind: "error" }); return; }
        toast(`Not ${x.name}: won't ask again`, { action: async () => {
          try { await api(P(p, `/people/${x.person}/not-same`), { method: "POST", body: { cluster: x.cluster, undo: true } }); again(x); }
          catch (e) { toast(e.message, { kind: "error" }); }
        } });
        i++; paint();
      };
      const skip = () => { i++; paint(); };
      // "Someone else…": say who it is right here, in the question's place. Nothing is saved until you
      // type a name and press Enter; Esc goes back to the question.
      const other = () => {
        const x = s.suggestions[i];
        const input = h("input", { class: "input", list: "people-names", placeholder: "Who is it?", "aria-label": "Who is it?",
          autocomplete: "off", onkeydown: (e) => {
            if (e.key === "Enter") { e.preventDefault(); save(); }
            else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); paint(); }
          } });
        const save = async () => {
          const who = input.value.trim();
          if (!who) { input.focus(); return; }
          if (await name(x, who)) { i++; paint(); }
        };
        clear(card.querySelector(".ask-actions")).append(
          h("span", { class: "muted" }, `Not ${x.name}? Then who is it:`), input,
          h("button", { class: "btn primary", type: "button", onclick: save }, "Save"),
          h("button", { class: "btn ghost", type: "button", onclick: () => paint() }, "Back"), aliasHint(input));
        input.focus();
      };
      // The keys answer the question at the top, unless you're on a group's card (its own keys).
      keyHandler = (e) => {
        if (document.activeElement?.closest?.(".person-card.group") || e.metaKey || e.ctrlKey || e.altKey) return false;
        const k = e.key;
        if (k === "y") { yes(); return true; }
        if (k === "n") { no(); return true; }
        if (k === "s") { other(); return true; }
        if (k === "ArrowRight") { skip(); return true; }
        return false;
      };
      clear(quick).append(h("h2", {}, "Is this…?"),
        h("p", { class: "note" }, "Groups that look like someone you've named. Compare the faces, then answer: Y yes, N no (remembered), S someone else, → skip. Every answer can be undone."), card);
      paint();
    }).catch(() => quick.remove());

    if (d.people.length) host.append(acrossYears());
    if (d.pet_groups?.length) {
      // Dogs and cats Apple Vision found, grouped by how alike they look. Named once, a pet is asked about
      // in its other groups ("Is this Joey?" above).
      host.append(h("section", { class: "section" },
        h("h2", {}, "Pets to name"),
        h("p", { class: "note" }, "Dogs and cats, grouped by how alike they look. Name your pet's biggest group and JoeyLab asks about its other groups above. ",
          h("kbd", {}, "Esc"), " then ", h("kbd", {}, "S"), " hides a group as not yours, ", h("kbd", {}, "X"), " as not an animal."),
        h("div", { class: "people-grid" }, d.pet_groups.map((g) => groupCard(g, true)))));
    }
    if (d.animal_faces) host.append(animalFaces(d.animal_faces));
    if (nothing) { host.append(nothing); return host; }
    let left = d.groups_total ?? d.groups.length;
    const leftEl = h("span", { class: "muted n" }, `${plural(left, "group")} left`);
    groupsLeft = (k) => { left = Math.max(0, left - k); leftEl.textContent = `${plural(left, "group")} left`; };
    groupGrid = h("div", { class: "people-grid" }, (showAll ? d.groups : d.groups.slice(0, FIRST)).map((g) => groupCard(g)));
    host.append(h("section", { class: "section" },
      h("div", { class: "row", style: { justifyContent: "space-between", alignItems: "baseline" } },
        h("h2", {}, d.people.length ? "Still to name" : "Who's who"), d.groups.length ? leftEl : null),
      h("p", { class: "note" }, "Type a name and press Enter to save and go to the next. Biggest groups first. ",
        h("kbd", {}, "Esc"), " then ", h("kbd", {}, "S"), " hides a group as strangers, ", h("kbd", {}, "X"), " as not a face; arrows move between groups."),
      d.groups.length ? null : h("div", { class: "muted" }, "Everyone recognisable has a name."),
      groupGrid,
      !showAll && d.groups_total > FIRST ? h("button", { class: "btn", onclick: () => { showAll = true; render(); } }, `Show all ${n(d.groups_total)} groups`) : null,
      (d.smaller_groups || d.smaller_pet_groups) && !showSmall ? h("button", { class: "btn ghost", onclick: () => { showSmall = true; showAll = true; render(); } },
        `Show ${[d.smaller_groups ? plural(d.smaller_groups, "face") : "", d.smaller_pet_groups ? plural(d.smaller_pet_groups, "pet") : ""].filter(Boolean).join(" and ")} seen in only one photo`) : null));
    if (d.unclear?.length) {
      host.append(h("details", { class: "section unclear-faces" },
        h("summary", { class: "label" }, `Unclear faces · ${plural(d.unclear.length, "group")}`),
        h("p", { class: "note" }, "These groups mix blurred, turned-away and partial faces (and the odd dog) rather than showing one person, so they're kept out of the way. Name any that are really one person, or mark them as strangers or not faces."),
        h("div", { class: "people-grid" }, d.unclear.map((g) => groupCard(g)))));
    }
    return host;
  }

  // Faces the face models found on a dog or cat where Apple Vision sees no one: kept out of people's
  // groups. Any that's a person's goes back with one press, and Undo sets it aside again.
  function animalFaces(total) {
    const grid = h("div", { class: "face-review" });
    const box = h("details", { class: "section" },
      h("summary", { class: "label" }, `Animals' faces · ${n(total)}`),
      h("p", { class: "note" }, "Faces found on a dog or cat where no one is, so they're kept out of people's groups. If one is a person's, choose A person."),
      grid);
    box.addEventListener("toggle", async () => {
      if (!box.open || grid.childElementCount) return;
      let d;
      try { d = await api(P(p, "/people/animal-faces")); } catch (e) { toast(e.message, { kind: "error" }); return; }
      const kind = (f, k) => api(P(p, "/faces/kind"), { method: "POST", body: { faces: [f.id], kind: k } });
      grid.append(...d.faces.map((f) => {
        const cell = h("div", { class: "fr" },
          h("img", { src: faceUrl(f.id), alt: "", loading: "lazy" }),
          h("button", { class: "btn small ghost", type: "button", title: "A person's face: back among people's faces", onclick: async () => {
            try { await kind(f, "person"); } catch (e) { toast(e.message, { kind: "error" }); return; }
            cell.hidden = true;
            toast("Back among people's faces", { action: async () => {
              try { await kind(f, "pet"); cell.hidden = false; } catch (e) { toast(e.message, { kind: "error" }); }
            } });
          } }, "A person"));
        return cell;
      }));
    });
    return box;
  }

  // Children change year by year, so they end up in one group per age. These join them up.
  function acrossYears() {
    const host = h("section", { class: "section" });
    const years = (span) => {
      const [a, b] = span.map((t) => new Date(t * 1000).getFullYear());
      return a === b ? String(a) : `${a}–${b}`;
    };
    api(P(p, "/people/across-years")).then((d) => {
      if (!d.suggestions.length) { host.remove(); return; }
      const list = h("div", { class: "stack-v" });
      const card = (x) => {
        const el = h("div", { class: "years-card" },
          h("div", { class: "years-faces" },
            h("div", { class: "stack-v" }, h("div", { class: "row" }, x.group_faces.map((fid) => h("img", { src: faceUrl(fid), alt: "" }))),
              h("span", { class: "muted" }, `This group · ${years(x.group_span)}`)),
            icon(x.direction === "younger" ? "right" : "left"),
            h("div", { class: "stack-v" }, h("div", { class: "row" }, x.person_faces.map((fid) => h("img", { src: faceUrl(fid), alt: "" }))),
              h("span", { class: "muted" }, `${x.name} · ${x.direction === "younger" ? "from" : "until"} ${years(x.direction === "younger" ? [x.person_span[0], x.person_span[0]] : [x.person_span[1], x.person_span[1]])}`))),
          h("div", { class: "stack-v" },
            h("b", {}, x.direction === "younger" ? `${x.name}, younger?` : `${x.name}, later on?`),
            h("span", { class: "muted" }, `${plural(x.photos, "photo")} from ${years(x.group_span)}, `
              + (x.direction === "younger" ? `before the first photos named ${x.name}` : `after the last photos named ${x.name}`)
              + `; never in a photo with ${x.name}.`),
            h("div", { class: "row" },
              h("button", { class: "btn primary small", onclick: async () => {
                try { namedElsewhere(x.cluster, await api(P(p, "/people/name"), { method: "POST", body: { cluster: x.cluster, name: x.name } })); el.remove(); toast(`Added to ${x.name}`); }
                catch (e) { toast(e.message, { kind: "error" }); }
              } }, `Yes, that's ${x.name}`),
              h("button", { class: "btn small", onclick: async () => {
                try { await api(P(p, `/people/${x.person}/not-same`), { method: "POST", body: { cluster: x.cluster } }); el.remove(); }
                catch (e) { toast(e.message, { kind: "error" }); }
              } }, "No"))));
        return el;
      };
      list.append(...d.suggestions.map(card));
      host.append(h("h2", {}, "Across the years"),
        h("p", { class: "note" }, "A child's face changes year by year, so they can end up in a different group for each age. These groups are from before or after the years someone is named in, are never in a photo with them, and look like them at the nearest age. A No is remembered."),
        list);
    }).catch(() => host.remove());
    return host;
  }

  // Naming moves card to card from the keyboard: Enter saves and goes to the next; Esc leaves the
  // name box, then S hides the group as strangers, X as not a face, arrows move, Enter types a name.
  function nextCard(card, back = false) {
    let c = back ? card.previousElementSibling : card.nextElementSibling;
    while (c && !c.classList.contains("group")) c = back ? c.previousElementSibling : c.nextElementSibling;
    return c;
  }
  function gone(card, joined = 0, toInput = true) {
    const next = nextCard(card) || nextCard(card, true);
    card.remove();
    if (!card.dataset.pet) groupsLeft(1 + joined);           // (the count is of people's groups)
    if (next) (toInput ? next.querySelector("input.input") : next)?.focus();
  }

  /** A group named from the questions above the list: its card (and any group that joined it) goes. */
  function namedElsewhere(cluster, r) {
    const card = page.querySelector(`.person-card.group[data-cluster="${cluster}"]`);
    card?.remove();
    if (!card?.dataset.pet) groupsLeft(1 + (r?.joined || 0));
    if (r?.joined && r.id) dropJoined(r.id);
  }

  // Groups that joined the one just named (clearly the same person) are named too: take their cards
  // away. Face ids stay put when groups are worked out again, so they're what's compared.
  async function dropJoined(pid) {
    let faces;
    try { faces = new Set((await api(P(p, `/people/${pid}/faces`))).faces.map((f) => f.id)); } catch { return; }
    page.querySelectorAll(".person-card.group[data-faces]").forEach((c) => {
      if (c.dataset.faces.split(",").some((fid) => faces.has(+fid))) {
        if (c.contains(document.activeElement)) (nextCard(c) || nextCard(c, true))?.querySelector("input.input")?.focus();
        c.remove();
      }
    });
  }

  /** A group still to name: of faces, or with `animal`, of pets (named as pets, hidden as not yours). */
  function groupCard(g, animal = false) {
    const input = h("input", { class: "input", placeholder: "Who is this?", list: "people-names", "aria-label": `Who is this? A group in ${plural(g.photos, "photo")}`,
      onkeydown: (e) => { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); card.focus(); } } });
    const pet = h("input", { type: "checkbox", title: "A pet", checked: !!animal });
    const [hideAs, notAs] = animal ? ["Other animals", "Not an animal"] : ["Strangers", "Not a face"];
    const card = h("div", { class: "person-card group", tabindex: "-1", dataset: { cluster: g.cluster, faces: (g.sample || []).join(","), pet: animal ? "1" : "" }, onkeydown: (e) => {
      if (e.target !== card || e.metaKey || e.ctrlKey || e.altKey) return;
      const k = e.key.toLowerCase();
      if (k === "s") ignore(g, hideAs, card);
      else if (k === "x") ignore(g, notAs, card);
      else if (k === "enter") input.focus();
      else if (k === "arrowright" || k === "arrowdown") nextCard(card)?.focus();
      else if (k === "arrowleft" || k === "arrowup") nextCard(card, true)?.focus();
      else return;
      e.preventDefault(); e.stopPropagation();
    } });
    const form = h("form", { class: "row", style: { gap: "6px", flexWrap: "nowrap" }, onsubmit: async (e) => {
      e.preventDefault();
      const name = input.value.trim();
      if (!name) return;
      try {
        // (A pets' group makes a new name a pet by itself; someone named already stays as they are.)
        const r = await api(P(p, "/people/name"), { method: "POST", body: { cluster: g.cluster, name, kind: !animal && pet.checked ? "pet" : undefined } });
        toast(`Named ${r.name || name}${r.joined ? ` (and ${plural(r.joined, "similar group")})` : ""}. Looking for them in other photos…`);
        gone(card, r.joined || 0);
        if (r.joined) dropJoined(r.id);
      } catch (err) { toast(err.message, { kind: "error" }); }
    } }, input, h("button", { class: "btn small primary", type: "submit" }, "Save"));
    card.append(
      h("a", { class: "faces4", href: lib(`cluster=${g.cluster}`), title: "Show these photos" },
        g.sample.map((fid) => h("img", { src: faceUrl(fid), alt: "", loading: "lazy" }))),
      h("div", { class: "row", style: { justifyContent: "space-between" } }, h("span", { class: "muted" }, plural(g.photos, "photo")),
        animal ? null : h("label", { class: "check muted small" }, pet, h("span", {}, "pet"))),
      form, aliasHint(input),
      animal ? h("div", { class: "row", style: { gap: "4px" } },
        h("button", { class: "btn ghost small", title: "Someone else's pet, or a stray: hidden from naming", onclick: () => ignore(g, hideAs, card) }, "Not ours"),
        h("button", { class: "btn ghost small", title: "Not a dog or cat (a toy, a statue, a pattern)", onclick: () => ignore(g, notAs, card) }, notAs))
        : h("div", { class: "row", style: { gap: "4px" } },
          h("button", { class: "btn ghost small", title: "People you don't know: hidden from naming and coverage", onclick: () => ignore(g, hideAs, card) }, "Strangers"),
          h("button", { class: "btn ghost small", title: "Not a face (a statue, a print, a pattern)", onclick: () => ignore(g, notAs, card) }, notAs)));
    return card;
  }

  async function ignore(g, as, card) {
    try { await api(P(p, `/clusters/${g.cluster}/ignore`), { method: "POST", body: { as } }); gone(card, 0, false); toast(`Hidden as ${as.toLowerCase()}`); }
    catch (e) { toast(e.message, { kind: "error" }); }
  }

  // ------------------------------------------------------------ together
  function togetherTab() {
    const host = h("section", { class: "section" }, h("h2", {}, "Often photographed together"), h("div", { class: "muted" }, "Loading…"));
    api(P(p, "/people/together")).then((d) => {
      if (!d.pairs.length) { clear(host).append(h("h2", {}, "Often photographed together"), h("div", { class: "muted" }, "Name a few people first.")); return; }
      const max = d.pairs[0].photos;
      clear(host).append(h("h2", {}, "Often photographed together"),
        h("p", { class: "note" }, "Pairs of named people and how many photos they share. Click to see those photos."),
        h("div", { class: "stack-v" }, d.pairs.map((x) => h("a", { class: "hbar together", href: lib(`people=${x.a},${x.b}`), style: { textDecoration: "none" } },
          h("span", {}, `${x.a_name} & ${x.b_name}`), h("span", { class: "track" }, h("i", { style: { width: `${(100 * x.photos) / max}%` } })),
          h("span", { class: "num muted" }, n(x.photos))))));
    }).catch((e) => { clear(host).append(h("div", { class: "muted" }, e.message)); });
    return host;
  }

  render();
  return {
    refresh: render,
    waiting: () => waiting,
    onKey(e) { return keyHandler ? keyHandler(e) : false; },
  };
}
