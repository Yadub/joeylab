// Search: one box for everything. Plain language ("Mom in Cornwall in 2023 that I haven't picked"),
// field filters (person:"Alex" is:pick rating:>3 iso:>800) and what's in the picture, all at
// once. Also "photos like this one" and things you've taught JoeyLab.

import { api, img, P } from "../api.js";
import { createGrid } from "../grid.js";
import { openLoupe } from "../loupe.js";
import { row, store } from "../store.js";
import { clear, h, icon, n, plural, toast } from "../ui.js";
import { albumDialog, conceptDialog, decisionKey, selectionBar } from "./common.js";
import { waitNote } from "./waiting.js";

const EXAMPLES = ["people laughing", "group photo", "dog on a beach", "is:pick rating:>3", "close-up portrait", "food on the table",
  "sunset", "is:video", "a receipt", "black and white", "flowers", "cake with candles"];

export function mount(root, ctx) {
  const q = ctx.params.get("q") || "";
  const similar = +ctx.params.get("similar") || 0;
  const concept = +ctx.params.get("concept") || 0;
  const page = h("div", { class: "page", style: { maxWidth: "none" } });
  root.append(page);
  const top = document.querySelector(".search-box");
  if (top) top.hidden = true;

  const input = h("input", { class: "input", id: "search-q", type: "search", value: q, autocomplete: "off",
    placeholder: "Describe it, or filter: Alex laughing at the beach · person:\"Mom\" date:2023 is:undecided" });
  const form = h("form", { role: "search", onsubmit: (e) => { e.preventDefault(); go(input.value.trim()); } },
    h("label", { class: "sr-only", for: "search-q" }, "Search photos"), input,
    h("button", { class: "btn primary", type: "submit" }, icon("search"), "Search"));
  const status = h("div", { class: "muted" });
  const chips = h("div", { class: "row understood", hidden: true });
  const head = h("div", { class: "search-hero" },
    similar ? h("div", { class: "row" }, h("h1", { class: "display" }, "Photos like this one"),
      row(similar) ? h("img", { src: img.thumb(ctx.project, row(similar)), style: { height: "56px", borderRadius: "4px" }, alt: "" }) : null)
      : h("h1", { class: "sr-only" }, "Search"),
    form, chips,
    !q && !similar && !concept ? h("div", { class: "row", style: { gap: "6px" } },
      EXAMPLES.map((x) => h("button", { class: "chip", type: "button", onclick: () => go(x) }, x))) : null,
    status,
    // While the update indexes photos (filters, people, places and dates work before that).
    waitNote({ step: "search", when: () => (store.overview?.counts?.indexed ?? 0) < (store.overview?.counts?.photos ?? 1),
      text: () => (store.overview?.counts?.indexed ? "Newly added photos can be searched when indexing finishes"
        : "Search will be ready when indexing finishes (dates, people and is:pick work already)"),
      ready: "Indexing has finished", onReady: q || similar || concept ? () => reload() : null }));
  page.append(head);

  function go(text) {
    if (!text) return;
    ctx.setParams({ q: text }, true);
  }

  if (!q && !similar && !concept) {
    setTimeout(() => input.focus(), 0);
    page.append(h("p", { class: "note" }, "Search understands what's in the picture, not file names, and reads people, places, dates and decisions in what you type. It all runs on your Mac."));
    api(P(ctx.project, "/concepts")).then((d) => {
      if (!d.concepts.length) return;
      page.append(h("section", { class: "section" }, h("h2", {}, "Things you've taught JoeyLab"),
        h("div", { class: "row" }, d.concepts.map((c) => h("a", { class: "chip", href: `#/p/${encodeURIComponent(ctx.project)}/search?concept=${c.id}` }, icon("wand"), c.name)))));
    }).catch(() => {});
    return { unmount() { if (top) top.hidden = false; } };
  }

  let first = null;            // the first page's answer: what was understood, counts, the engine
  let showUnlikely = false;
  let collapse = true;
  let sort = ctx.params.get("sort") || "best";
  let allIds = null;           // every match, fetched when "select all" or an album needs them
  const more = h("button", { class: "btn", type: "button", hidden: true, onclick: () => { showUnlikely = true; more.hidden = true; reload(); } });
  const hiddenChip = h("button", { type: "button", class: "chip hidden-chip", hidden: true });
  const openLib = h("a", { class: "btn ghost", hidden: true }, icon("grid"), "Open in Library");
  // (Only a search of the pictures has stacks to fold; a filter lists every photo.)
  const burstBox = h("label", { class: "check", hidden: true }, h("input", { type: "checkbox", checked: collapse,
    onchange: (e) => { collapse = e.target.checked; reload(); } }), h("span", {}, "One per stack"));
  // This search's own match details only: a "similar" or concept result has no box or score, and
  // must not show the ones an earlier search left on the same photo.
  const fresh = { prob: null, box: null, level: null, score: null, why: null, why_conf: null, why_text: null };
  const searchParams = (extra) => ({ q, collapse: collapse ? 1 : 0, sort: sort === "best" ? "" : sort, unlikely: showUnlikely ? 1 : "",
    all: ctx.params.get("all") || "", ...extra });
  let cache = null;            // "similar" and concept results come whole
  const grid = createGrid({
    showScore: true,
    emptyText: "",
    load: async (offset, limit) => {
      if (similar || concept) {
        if (!cache) {
          status.textContent = store.meta?.ai_loaded ? "Searching…" : "Loading the search model (the first search takes a few seconds)…";
          try {
            const res = similar ? await api(P(ctx.project, `/similar/${similar}`), { params: { limit: 150 } }) : await api(P(ctx.project, `/concepts/${concept}`));
            cache = res.rows;
            if (store.meta) store.meta.ai_loaded = true;
            describe({ ...res, total: cache.length });
          } catch (e) { status.textContent = e.message; cache = []; }
        }
        return { rows: offset ? [] : cache.map((r) => ({ ...store.rows.get(r.id), ...fresh, ...r })), total: cache.length };
      }
      if (!offset) status.textContent = store.meta?.ai_loaded ? "Searching…" : "Loading the search model (the first search takes a few seconds)…";
      try {
        const res = await api(P(ctx.project, "/search"), { params: searchParams({ offset, limit }) });
        if (store.meta) store.meta.ai_loaded = true;
        if (!offset) { first = res; describe(res); }
        return { rows: res.rows.map((r) => ({ ...store.rows.get(r.id), ...fresh, ...r })), total: offset ? null : res.total ?? res.rows.length };
      } catch (e) {
        if (!offset) status.textContent = e.message;
        throw e;
      }
    },
    openAt: (i, g) => openLoupe({ ids: g.ids, index: i, onClose: (k) => g.setFocus(k, { keep: true }), onIndex: (k) => { g.focus = k; },
      loadMore: () => g.more(), total: () => g.total }),
  });
  function reload() { allIds = null; first = null; cache = null; grid.reload(); }

  // Everything this search matches, as a filter the rest of the app understands: what was
  // understood, when nothing is left to look for in the pictures; otherwise the matches themselves.
  async function matching() {
    if (first?.engine === "filter") return { ...first.filter, ...(ctx.params.get("all") ? { all: "1" } : {}) };
    if (!allIds) allIds = (await api(P(ctx.project, "/search"), { params: searchParams({ ids_only: 1 }) })).ids;
    return { ids: allIds.join(","), all: "1" };
  }

  function describe(res) {
    const counts = res.counts || {};
    const strong = counts.strong ?? res.rows?.filter((r) => r.level === "strong").length ?? 0;
    const possible = counts.possible ?? res.rows?.filter((r) => r.level === "possible").length ?? 0;
    const unlikely = counts.unlikely ?? 0;
    const parts = res.parts?.length > 1 ? res.parts : null;
    if (res.chips?.length || parts || (res.text && res.text !== q)) {
      chips.hidden = false;
      clear(chips).append(h("span", { class: "muted" }, "Understood:"),
        (res.chips || []).map((c) => h("span", { class: "pill accent" }, c)),
        parts ? h("span", { class: "pill neutral", title: "Each photo has to show all of these" }, `shows ${parts.map((x) => x.replace(/^(a|an|the)\s+/i, "")).join(" + ")}`)
          : res.text ? h("span", { class: "pill neutral" }, `looks like “${res.text}”`) : null);
    }
    // The whole search opens in the Library as what it shows: the filters understood, and the picture part
    // as a search there (its strong and possible matches, every frame), with the Library's summary, filters and Explore.
    const libParams = { ...(res.filter || {}), ...(res.text && res.engine === "siglip2" ? { q: res.text } : {}) };
    openLib.hidden = Boolean(similar || concept) || !Object.keys(libParams).length;
    openLib.href = `#/p/${encodeURIComponent(ctx.project)}/library?${new URLSearchParams(libParams).toString()}`;
    openLib.title = "Show these photos in the Library: its filters, months, Explore and a summary of when, who and where";
    burstBox.hidden = !res.engine || res.engine === "filter";
    hiddenChip.hidden = !res.hidden;
    hiddenChip.textContent = `${n(res.hidden || 0)} hidden`;
    hiddenChip.title = "Thumbnail-sized photos and screenshots are hidden while browsing. Include them";
    hiddenChip.onclick = () => ctx.setParams({ q, all: "1", ...(sort !== "best" ? { sort } : {}) }, true);
    const cover = res.indexed !== undefined && res.indexed < res.photos
      ? ` Search covers ${res.indexed.toLocaleString()} of ${plural(res.photos, "photo")} so far; indexing continues.` : "";
    const total = res.total ?? res.rows?.length ?? 0;
    page.querySelector(".search-tools")?.classList.toggle("none", !total);    // nothing to select or save
    // The picture match sees no photo yet: say so, and offer what was understood on its own.
    if (!similar && !concept && res.text && res.indexed === 0 && res.photos) {
      const asked = Object.keys(res.filter || {}).length;
      clear(status).append(`Picture search covers 0 of ${plural(res.photos, "photo")} so far, so nothing can match “${res.text}” yet. `,
        asked ? h("a", { class: "linkish", href: `#/p/${encodeURIComponent(ctx.project)}/library?${new URLSearchParams(res.filter).toString()}` },
          `Show the photos matching ${(res.chips || []).join(", ") || "the rest"} instead`) : null);
      return;
    }
    if (similar) status.textContent = `${plural(total, "photo")} that look most like this one.`;
    else if (concept) status.textContent = `${plural(total, "photo")} like your examples of “${res.name}”, best first.`;
    else if (res.engine === "filter") status.textContent = `${plural(total, "photo")} match${sort === "best" ? ", best first" : ""}.`;
    else if (res.engine === "clip") status.textContent = `${plural(total, "photo")}${sort === "best" ? ", best match first" : ""}.`;
    else if (strong + possible) {
      status.textContent = `${plural(strong, "strong match", "strong matches")}${possible ? ` and ${plural(possible, "possible match", "possible matches")}` : ""}`
        + (showUnlikely && unlikely ? `, and ${plural(unlikely, "less likely photo")}` : "")
        + (res.parts?.length > 1 ? `; photos showing all of ${res.parts.map((x) => `“${x}”`).join(" and ")} come first.` : ".")
        + (collapse ? " One photo per stack." : "") + cover;
    } else status.textContent = (showUnlikely ? `${plural(total, "photo")}, closest first.` : `No photos match “${q}”.`) + cover;
    if (!similar && !concept && res.engine === "siglip2" && unlikely && !showUnlikely) {
      more.hidden = false;
      more.textContent = strong + possible ? `Show ${plural(unlikely, "less likely photo")}` : "Show the closest photos anyway";
    }
  }

  const sortSel = h("select", { class: "input", id: "search-sort", style: { width: "auto" }, "aria-label": "Order",
    onchange: (e) => { sort = e.target.value; ctx.setParams({ q, ...(sort !== "best" ? { sort } : {}), ...(ctx.params.get("all") ? { all: "1" } : {}) }); reload(); } },
  [["best", "Best match"], ["newest", "Newest first"], ["oldest", "Oldest first"], ["quality", "Best quality"]]
    .map(([v, t]) => h("option", { value: v, selected: sort === v }, t)));

  page.append(h("div", { class: "row search-tools" },
    !similar && !concept ? sortSel : null,
    hiddenChip,
    h("button", { class: "btn ghost", title: "Select all loaded (⌘A); again for every match", onclick: () => grid.selectAll() }, "Select all"),
    h("button", { class: "btn ghost", title: "Save every match (or the photos selected) as an album", onclick: async () => {
      const name = (q || `like ${similar || concept}`).slice(0, 60);
      if (similar || concept) return albumDialog({ ids: grid.sel.size > 1 ? [...grid.sel] : grid.ids, name });
      if (grid.sel.size > 1 && !grid.matchAll) return albumDialog({ ids: [...grid.sel], name });
      try { albumDialog({ filter: await matching(), name }); } catch (e) { toast(e.message, { kind: "error" }); }
    } }, icon("album"), "Save as album…"),
    openLib,
    !similar && !concept ? burstBox : null,
    concept ? h("button", { class: "btn ghost", title: "Add more examples: select the right ones here", onclick: () => {
      const ids = grid.targetIds();
      if (ids.length < 3) { toast("Select at least 3 of the right photos first."); return; }
      conceptDialog(ids);
    } }, icon("wand"), "Teach again from selection") : null),
  grid.el, h("div", { class: "row", style: { justifyContent: "center", padding: "12px 0" } }, more),
  selectionBar(grid, similar || concept ? {} : { filter: matching }));

  return {
    onKey(e) {
      if (grid.onKey(e)) return true;
      if (grid.bulkKey?.(e)) return true;
      if (/^[0-9pPxXafctb]$/.test(e.key) && !e.metaKey && !e.ctrlKey && !grid.ensureTarget()) {
        toast("Selected the first photo. Press the key again, or click a photo to choose another.");
        return true;
      }
      return decisionKey(e, grid.targetIds());
    },
    scrollState: () => grid.scrollState(),
    restore: (s) => grid.restore(s),
    unmount() { grid.destroy(); if (top) top.hidden = false; },
  };
}
