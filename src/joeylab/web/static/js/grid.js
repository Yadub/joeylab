// Justified thumbnail grid, windowed: the layout is computed for every loaded photo, but only
// the rows near the screen exist in the page, so 13,000 photos stay light even on a phone.
// Optional month headers and a scrubber (jump to any month) when sorted by time.

import { img } from "./api.js";
import { emit, on, remember, store, decide, togglePick } from "./store.js";
import { clear, duration, h, icon, labelDot, monthLabel, photoName, plural, starsEl, toast } from "./ui.js";

const PAGE = 300;
const GAP = 6;
const HEADER_H = 46;
const BUFFER = 900;           // px above and below the screen that stay rendered
const ASK_BEFORE_CLEARING = 50;

// `offset(month)`: where a month starts in the whole listing (from the server), so the scrubber can
// load from there; the grid then holds photos from `start` on and loads earlier ones as you scroll up.
export function createGrid({ load, openAt, showScore = false, emptyText = "No photos match these filters.",
  headers = () => false, timeline = null, offset = null, showSuggestions = () => true }) {
  // One tab stop for the whole grid: arrows move between photos (the focused one is the listbox's
  // active option), Enter opens it. The buttons on each tile stay for the mouse.
  const gridEl = h("div", { class: "grid vgrid", role: "listbox", "aria-multiselectable": "true", "aria-label": "Photos", tabindex: "0",
    "data-skip": true });                     // where "Skip to the photos" goes
  const topEl = h("div", { class: "grid-end grid-top", hidden: true });
  const endEl = h("div", { class: "grid-end" });
  const scrub = h("nav", { class: "scrubber", "aria-label": "Jump to a month", hidden: true });
  const scrubTip = h("div", { class: "scrub-tip", hidden: true, "aria-hidden": "true" });
  const el = h("div", { class: "grid-wrap" }, topEl, gridEl, endEl, scrub, scrubTip);
  const g = {
    el, ids: [], start: 0, total: 0, sel: new Set(), focus: -1, anchor: -1, loading: false, done: false,
    tiles: new Map(), gen: 0, selListeners: new Set(), layout: { items: [], rows: [], height: 0 },
    stackN: new Map(),     // id -> frames in its stack in this listing (the rows other pages share don't say)
  };

  const H = () => Math.min(store.prefs.thumb || 200, window.innerWidth < 640 ? 118 : Infinity);
  const monthOf = (r) => (!r || !r.capture_str || r.time_src === "file") ? "undated" : r.capture_str.slice(0, 7);

  // ------------------------------------------------------------ layout
  function computeLayout() {
    const W = gridEl.clientWidth || window.innerWidth - 32;
    const target = H();
    const items = [];       // per photo index: {x, y, w, h, row}
    const rows = [];        // {y, h, start, end, header?}
    let y = 0, line = [], sum = 0;
    const withHeaders = headers();
    let month = null;
    const flush = (last) => {
      if (!line.length) return;
      const gaps = GAP * (line.length - 1);
      let rh = (W - gaps) / sum;
      if (last && rh > target * 1.25) rh = target;     // don't blow up an unfinished last row
      rh = Math.min(rh, target * 1.8);
      let x = 0;
      const r = { y, h: rh, start: line[0], end: line[line.length - 1] };
      for (const i of line) {
        const ar = arOf(i);
        const w = Math.max(24, ar * rh);
        items[i] = { x, y, w, h: rh, row: rows.length };
        x += w + GAP;
      }
      rows.push(r);
      y += rh + GAP;
      line = []; sum = 0;
    };
    for (let i = 0; i < g.ids.length; i++) {
      const r = store.rows.get(g.ids[i]);
      if (withHeaders) {
        const m = monthOf(r);
        if (m !== month) {
          flush(true);
          rows.push({ y, h: HEADER_H, header: m, start: i, end: i - 1 });
          y += HEADER_H;
          month = m;
        }
      }
      line.push(i);
      sum += arOf(i);
      if (sum * target + GAP * (line.length - 1) >= W) flush(false);
    }
    flush(true);
    g.layout = { items, rows, height: y, width: W };
    gridEl.style.height = `${Math.max(0, y)}px`;
  }
  const arOf = (i) => Math.min(Math.max(store.rows.get(g.ids[i])?.ar || 1.5, 0.4), 3.2);

  // ------------------------------------------------------------ rendering the window
  const headerEls = new Map();
  let raf = 0;
  function schedule() { if (!raf) raf = requestAnimationFrame(() => { raf = 0; render(); }); }

  function visibleRange() {
    const top = gridEl.getBoundingClientRect().top + window.scrollY;
    const y0 = window.scrollY - top - BUFFER, y1 = window.scrollY - top + window.innerHeight + BUFFER;
    const rows = g.layout.rows;
    let lo = 0, hi = rows.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (rows[mid].y + rows[mid].h < y0) lo = mid + 1; else hi = mid; }
    let last = lo;
    while (last < rows.length && rows[last].y <= y1) last++;
    return [lo, last];
  }

  function render() {
    const [r0, r1] = visibleRange();
    const want = new Set();
    const wantHeaders = new Set();
    for (let k = r0; k < r1; k++) {
      const row = g.layout.rows[k];
      if (row.header) { wantHeaders.add(k); continue; }
      for (let i = row.start; i <= row.end; i++) want.add(i);
    }
    for (const [id, t] of g.tiles) {
      const i = +t.dataset.i;
      if (!want.has(i) || g.ids[i] !== id) { t.remove(); g.tiles.delete(id); }
    }
    for (const [k, hd] of headerEls) if (!wantHeaders.has(k)) { hd.remove(); headerEls.delete(k); }
    const frag = document.createDocumentFragment();
    for (const i of want) {
      const id = g.ids[i];
      const pos = g.layout.items[i];
      let t = g.tiles.get(id);
      if (!t) {
        const r = store.rows.get(id);
        if (!r || !pos) continue;
        t = tile(r, i);
        g.tiles.set(id, t);
        frag.append(t);
      }
      Object.assign(t.style, { left: `${pos.x}px`, top: `${pos.y}px`, width: `${pos.w}px`, height: `${pos.h}px` });
      const sizes = `${Math.ceil(pos.w)}px`;      // the tile's width: a Retina screen takes the 768 px thumbnail
      if (t.firstChild.sizes !== sizes) t.firstChild.sizes = sizes;
      t.dataset.i = i;
      // Only the photos near the screen are in the page: say where each is in the whole list.
      t.setAttribute("aria-posinset", g.start + i + 1);
      t.setAttribute("aria-setsize", Math.max(g.total, g.start + g.ids.length));
      paintSelOne(t, id, i);
    }
    for (const k of wantHeaders) {
      if (headerEls.has(k)) continue;
      const row = g.layout.rows[k];
      const n = countMonth(row.start);
      // (Shown, not read out: each photo's name carries its date, and a list of photos holds only photos.)
      const hd = h("div", { class: "grid-head", "aria-hidden": "true", style: { top: `${row.y}px` } },
        h("b", {}, monthLabel(row.header)), n ? h("span", { class: "muted" }, plural(n, "photo")) : null);
      headerEls.set(k, hd);
      frag.append(hd);
    }
    gridEl.append(frag);
    const loadedBottom = g.layout.height;
    const top = gridEl.getBoundingClientRect().top + window.scrollY;
    if (!g.done && window.scrollY + window.innerHeight + 2500 > top + loadedBottom) more();
    if (g.start > 0 && window.scrollY < top + 2500) earlier();
  }

  function countMonth(start) {
    const m = monthOf(store.rows.get(g.ids[start]));
    const t = scrubData.find((x) => x.month === m);
    return t ? t.n : 0;
  }

  // ------------------------------------------------------------ tiles
  function tile(r, i) {
    const t = h("figure", { class: "tile", role: "option", id: `tile-${r.id}`, dataset: { id: r.id, i } });
    t.append(h("img", { src: img.thumb(store.project, r), srcset: thumbSrcset(r), loading: "lazy", decoding: "async", alt: r.stem, draggable: "false" }));
    t.append(h("div", { class: "tl" }), h("div", { class: "tr" }), h("div", { class: "foot" }));
    paint(t, r);
    return t;
  }

  function paint(t, r) {
    t.classList.toggle("is-pick", r.pick === 1);
    t.classList.toggle("is-reject", r.pick === -1);
    t.classList.toggle("has-rating", (r.rating || 0) > 0);
    const [tl, tr, foot] = t.querySelectorAll(".tl, .tr, .foot");
    clear(tl); clear(tr); clear(foot);
    if (r.media === "video") tl.append(h("span", { class: "tbadge", title: "Video" }, icon("play"), duration(r.duration)));
    const sn = g.stackN.get(r.id) || 1;
    if (sn > 1) tl.append(h("span", { class: "tbadge", title: `${sn} similar frames` }, icon("stack"), sn));
    if (r.similar_n > 1) tl.append(h("span", { class: "tbadge", title: `${r.similar_n - 1} more like it in this stack` }, `+${r.similar_n - 1}`));
    if (showScore && r.prob !== undefined && r.prob !== null) {
      const lvl = { strong: "Strong", possible: "Maybe", unlikely: "Unlikely" }[r.level] || "";
      tl.append(h("span", { class: `tbadge score lvl-${r.level}`, title: `Match probability ${(r.prob * 100).toFixed(1)}%` }, lvl));
      if (r.why) tl.append(h("span", { class: "tbadge", title: `Apple Vision label, ${Math.round((r.why_conf || 0) * 100)}% sure` }, r.why.replace(/_/g, " ")));
      if (r.why_text) tl.append(h("span", { class: "tbadge" }, "text"));
    } else if (showScore && r.score !== undefined && r.score !== null) tl.append(h("span", { class: "tbadge score" }, (+r.score).toFixed(2)));
    t.querySelector(".hl")?.remove();
    if (showScore && r.box) {
      const [x, y, w, bh] = r.box;
      t.append(h("div", { class: "hl", title: "Best match is here", style: { left: `${x * 100}%`, top: `${y * 100}%`, width: `${w * 100}%`, height: `${bh * 100}%` } }));
    }
    if (r.pick === 1) tr.append(h("span", { class: "tbadge pick", title: "Picked" }, icon("pick")));
    else if (r.pick === -1) tr.append(h("span", { class: "tbadge reject", title: "Rejected" }, icon("reject")));
    else if (!r.rating && r.suggestion && showSuggestions()) {
      tr.append(r.suggestion === "check"
        ? h("span", { class: "tbadge sug check", title: "Worth checking: something may be off (see Why in the viewer)" }, "!")
        : h("span", { class: `tbadge sug ${r.suggestion}`, title: `Suggested ${r.suggestion}` },
          icon(r.suggestion === "pick" ? "pick" : "reject"), "?"));
    }
    if (r.fav) tr.append(h("span", { class: "tbadge fav", title: "Your favourite" }, icon("heartOn")));
    foot.append(starsEl(r.rating || 0, (s) => decide([{ id: r.id, rating: s }], s ? `${s}★` : "clear stars")));
    foot.append(labelDot(r.label));
    foot.append(h("span", { class: "spacer" }));
    const flagBtn = (value, name, label) => h("button", {
      type: "button", class: `tbtn ${r.pick === value ? "on" : ""}`, title: `${label} (${value === 1 ? "P" : "X"})`, "aria-label": label,
      onclick: (e) => { e.stopPropagation(); togglePick([r.id], value); },
    }, icon(name));
    foot.append(flagBtn(1, "pick", "Pick"), flagBtn(-1, "reject", "Reject"));
    foot.querySelectorAll("button").forEach((b) => { b.tabIndex = -1; });
    if (r.quality !== null && r.quality !== undefined) foot.append(h("span", { class: "q", title: `Quality ${Math.round(r.quality)} of 100` }, Math.round(r.quality)));
    t.title = `${r.stem}${r.capture_str ? " · " + r.capture_str : ""}`;
    t.setAttribute("aria-label", [photoName(r), r.media === "video" ? "video" : "", r.pick === 1 ? "picked" : r.pick === -1 ? "rejected" : "",
      r.rating ? plural(r.rating, "star") : "", r.label ? `${r.label} label` : "", r.fav ? "favourite" : ""].filter(Boolean).join(", "));
  }

  function paintSelOne(t, id, i) {
    t.classList.toggle("is-sel", g.sel.has(id));
    t.classList.toggle("is-focus", i === g.focus);
    t.setAttribute("aria-selected", g.sel.has(id) ? "true" : "false");
    if (i === g.focus) gridEl.setAttribute("aria-activedescendant", t.id);
  }
  function paintSel() {
    for (const [id, t] of g.tiles) paintSelOne(t, id, +t.dataset.i);
    for (const fn of g.selListeners) fn(g);
  }

  // ------------------------------------------------------------ loading
  function more(pageSize = PAGE) {
    if (g.done) return Promise.resolve();
    if (g.loading) return g.pending;          // one page at a time; callers wait for the one in flight
    g.loading = true;
    const gen = g.gen;
    endEl.textContent = "Loading…";
    g.pending = page(gen, pageSize);
    return g.pending;
  }

  async function page(gen, pageSize) {
    try {
      const first = !g.ids.length;
      const res = await load(g.start + g.ids.length, pageSize, { first });
      if (gen !== g.gen) return;
      remember(res.rows);
      for (const r of res.rows) g.stackN.set(r.id, r.stack_n || 1);
      if (res.total !== null && res.total !== undefined) g.total = res.total;
      else if (first && !g.start) g.total = res.rows.length;
      if (first) g.response = res;
      for (const r of res.rows) { g.ids.push(r.id); if (g.matchAll) g.sel.add(r.id); }
      g.done = g.start + g.ids.length >= g.total || res.rows.length === 0;
      endEl.textContent = g.ids.length === 0 ? (typeof emptyText === "function" ? emptyText(g) : emptyText)
        : g.done ? plural(g.start + g.ids.length, "photo") : "";
      computeLayout();
      emit("grid", g);
      schedule();
    } catch (e) {
      if (gen === g.gen) {
        endEl.textContent = e.message;
        g.failed = true;
      }
    } finally {
      if (gen === g.gen) g.loading = false;   // a stale page (after a reload) mustn't touch the new one
    }
  }

  // After a jump the grid starts part-way through: the photos before come in above as you scroll
  // up, and the page keeps its place while they do.
  let loadingEarlier = false;
  async function earlier() {
    if (loadingEarlier || g.start <= 0 || g.restoring) return;
    loadingEarlier = true;
    const gen = g.gen;
    const from = Math.max(0, g.start - PAGE);
    topEl.hidden = false;
    topEl.textContent = "Loading…";
    try {
      const res = await load(from, g.start - from, {});
      if (gen !== g.gen) return;
      remember(res.rows);
      for (const r of res.rows) g.stackN.set(r.id, r.stack_n || 1);
      const ids = res.rows.map((r) => r.id).slice(0, g.start - from);
      const before = g.layout.height;
      g.ids.unshift(...ids);
      if (g.matchAll) ids.forEach((id) => g.sel.add(id));
      g.start = from;
      const k = ids.length;
      if (g.focus >= 0) g.focus += k;
      if (g.anchor >= 0) g.anchor += k;
      for (const t of g.tiles.values()) t.dataset.i = +t.dataset.i + k;
      for (const hd of headerEls.values()) hd.remove();
      headerEls.clear();
      computeLayout();
      const bar = g.start === 0 ? topEl.offsetHeight : 0;
      topEl.hidden = g.start === 0;
      topEl.textContent = "";
      window.scrollBy(0, g.layout.height - before - bar);
      emit("grid", g);
      schedule();
    } catch (e) {
      if (gen === g.gen) topEl.textContent = e.message;
    } finally {
      if (gen === g.gen) loadingEarlier = false;
    }
  }

  /** Start the grid afresh at a place in the whole listing (keeping the selection). */
  function restartAt(start) {
    g.gen++;
    g.ids = []; g.start = Math.max(0, start); g.focus = -1; g.anchor = -1; g.base = null;
    g.done = false; g.loading = false; g.pending = null;
    loadingEarlier = false;
    for (const t of g.tiles.values()) t.remove();
    g.tiles.clear();
    for (const hd of headerEls.values()) hd.remove();
    headerEls.clear();
    topEl.hidden = g.start === 0;
    computeLayout();
    return more();
  }

  // ------------------------------------------------------------ scrubber (jump to a month)
  // Years with a mark for each month that has photos: hover (or drag a finger down it) to see the
  // month, let go or click to go there.
  let scrubData = [];
  const descending = () => (store.prefs.sort || "time") === "time_desc";
  async function loadScrubber() {
    if (!timeline || !headers()) { scrub.hidden = true; scrubData = []; return; }
    try { scrubData = (await timeline()).months || []; } catch { scrubData = []; }
    clear(scrub);
    scrub.hidden = scrubData.length < 3;
    let year = null;
    const dated = scrubData.filter((m) => m.month !== "undated");
    // Photos without a capture date come last either way.
    const months = (descending() ? dated.reverse() : dated).concat(scrubData.filter((m) => m.month === "undated"));
    for (const m of months) {
      const y = m.month === "undated" ? "—" : m.month.slice(0, 4);
      if (y !== year) {
        year = y;
        scrub.append(h("button", { type: "button", class: "year", dataset: { month: m.month }, title: y === "—" ? "No capture date" : y,
          onclick: (e) => { if (!e.detail) jumpTo(m.month); } }, y === "—" ? "?" : y));
      }
      if (m.month !== "undated") {
        scrub.append(h("button", { type: "button", class: "month", tabindex: "-1", dataset: { month: m.month, n: m.n },
          "aria-label": `${monthLabel(m.month)}, ${plural(m.n, "photo")}` }));
      }
    }
    for (const hd of headerEls.values()) hd.remove();
    headerEls.clear();
    schedule();
  }
  let scrubbing = false;
  const markAt = (e) => document.elementFromPoint(e.clientX, e.clientY)?.closest?.(".scrubber [data-month]");
  function showTip(mark) {
    scrub.querySelectorAll(".on").forEach((b) => b.classList.remove("on"));
    if (!mark) { scrubTip.hidden = true; return; }
    mark.classList.add("on");
    const m = mark.dataset.month;
    const n = scrubData.find((x) => x.month === m)?.n;
    scrubTip.textContent = `${monthLabel(m)}${n ? ` · ${plural(n, "photo")}` : ""}`;
    scrubTip.hidden = false;
    const r = mark.getBoundingClientRect(), s = scrub.getBoundingClientRect();
    Object.assign(scrubTip.style, { top: `${r.top + r.height / 2}px`, right: `${window.innerWidth - s.left + 8}px` });
  }
  scrub.addEventListener("pointerdown", (e) => {
    scrubbing = true;
    scrub.setPointerCapture?.(e.pointerId);
    showTip(markAt(e));
    e.preventDefault();
  });
  scrub.addEventListener("pointermove", (e) => showTip(markAt(e)));
  scrub.addEventListener("pointerup", (e) => {
    if (!scrubbing) return;
    scrubbing = false;
    const mark = markAt(e);
    showTip(null);
    if (mark) jumpTo(mark.dataset.month);
  });
  scrub.addEventListener("pointercancel", () => { scrubbing = false; showTip(null); });
  scrub.addEventListener("pointerleave", () => { if (!scrubbing) showTip(null); });

  async function jumpTo(month) {
    const at = () => g.ids.findIndex((id) => monthOf(store.rows.get(id)) === month);
    let i = at();
    const gen = g.gen;
    if (i < 0 && offset) {
      // Ask where the month starts and load from there, instead of every page before it.
      endEl.textContent = `Loading ${monthLabel(month)}…`;
      let start = null;
      try { start = await offset(month); } catch { start = null; }
      if (gen !== g.gen) return;
      if (start !== null && start !== undefined) {
        g.failed = false;
        await restartAt(start);
        if (g.failed) return;
        i = at();
      }
    }
    // Without the server's help: load pages until the month (or one after it) arrives.
    const past = () => g.ids.findIndex((id) => {
      const m = monthOf(store.rows.get(id));
      return m === month || (descending() ? m < month : m > month);
    });
    if (i < 0 && !offset) {
      i = past();
      endEl.textContent = `Loading up to ${monthLabel(month)}…`;
      for (let guard = 0; i < 0 && !g.done && guard < 500; guard++) {
        const had = g.ids.length;
        g.failed = false;
        await more(1000);
        if (gen !== g.gen || g.failed || (g.ids.length === had && !g.done)) return;   // reloaded, failed or stuck
        i = past();
      }
    }
    if (i < 0) return;
    const row = g.layout.rows.find((r) => r.header && r.start === i) || g.layout.rows[g.layout.items[i]?.row];
    const top = gridEl.getBoundingClientRect().top + window.scrollY;
    window.scrollTo({ top: top + (row ? row.y : 0) - covered(), behavior: "instant" });
  }

  /** Where the page is, to come back to it (Back and Forward). */
  g.scrollState = () => ({ y: Math.round(window.scrollY), start: g.start, focus: g.focus });
  /** Come back to a place: load from where it started, then until the page reaches as far down. */
  g.restore = async ({ y = 0, start = 0, focus = -1 } = {}) => {
    g.restoring = true;                // the earlier photos wait until the page is back in place
    try {
      if (start > 0) await restartAt(start);
      else await g.pending;
      const gen = g.gen;
      const top = () => gridEl.getBoundingClientRect().top + window.scrollY;
      for (let guard = 0; gen === g.gen && !g.done && !g.failed && top() + g.layout.height < y + window.innerHeight && guard < 40; guard++) {
        await more(1000);
      }
      if (gen !== g.gen) return;
      window.scrollTo({ top: y, behavior: "instant" });
      if (focus >= 0 && focus < g.ids.length) { g.focus = focus; schedule(); }
    } finally {
      g.restoring = false;
    }
  };

  g.reload = () => {
    g.gen++;
    g.ids = []; g.start = 0; g.sel.clear(); g.matchAll = false; g.focus = -1; g.anchor = -1; g.base = null;
    g.done = false; g.loading = false; g.pending = null; g.total = 0;
    loadingEarlier = false;
    topEl.hidden = true;
    for (const t of g.tiles.values()) t.remove();
    g.tiles.clear();
    for (const hd of headerEls.values()) hd.remove();
    headerEls.clear();
    computeLayout();
    loadScrubber();
    more();
  };

  g.relayout = () => {
    computeLayout();
    for (const hd of headerEls.values()) hd.remove();
    headerEls.clear();
    schedule();
  };

  // ------------------------------------------------------------ selection + focus
  /** How much of the top of the window the top bar and a page's sticky toolbar cover. */
  function covered() {
    const bars = [document.getElementById("topbar"), el.closest(".lib-main")?.querySelector(".toolbar")].filter(Boolean);
    return Math.max(60, ...bars.map((b) => b.getBoundingClientRect().bottom)) + 8;
  }

  function scrollToIndex(i) {
    const pos = g.layout.items[i];
    if (!pos) return;
    const top = gridEl.getBoundingClientRect().top + window.scrollY + pos.y;
    const viewTop = window.scrollY + covered(), viewBottom = window.scrollY + window.innerHeight - 20;
    if (top < viewTop) window.scrollTo({ top: top - covered() });
    else if (top + pos.h > viewBottom) window.scrollTo({ top: top + pos.h - window.innerHeight + 20 });
  }

  function setFocus(i, { extend = false, toggle = false, keep = false, withFocused = false } = {}) {
    if (i < 0 || i >= g.ids.length) return;
    const before = g.focus;
    g.focus = i;
    const id = g.ids[i];
    if (extend && g.anchor < 0 && before >= 0) g.anchor = before;
    if (extend && g.anchor >= 0) {
      // ⇧ adds the range from the anchor to what was already selected; extending it again moves
      // the range's end (as in Finder), leaving the earlier selection as it was.
      if (!g.base) g.base = new Set(g.sel);
      g.sel = new Set(g.base);
      const [a, b] = [Math.min(g.anchor, i), Math.max(g.anchor, i)];
      for (let k = a; k <= b; k++) g.sel.add(g.ids[k]);
    } else if (toggle) {
      // Click one photo, then ⌘-click another: both are selected (as in Finder).
      if (withFocused && !g.sel.size && before >= 0 && before !== i) g.sel.add(g.ids[before]);
      g.sel.has(id) ? g.sel.delete(id) : g.sel.add(id);
      g.anchor = i;
      g.base = null;
    } else if (!keep) {
      g.sel.clear();
      g.anchor = i;
      g.base = null;
    }
    scrollToIndex(i);
    schedule();
    paintSel();
    if (i > g.ids.length - 40) more();
  }
  g.setFocus = setFocus;

  function tileIndex(e) {
    const t = e.target.closest(".tile");
    return t ? +t.dataset.i : -1;
  }

  let press = null;
  gridEl.addEventListener("pointerdown", (e) => {
    const i = tileIndex(e);
    if (i < 0 || e.pointerType !== "touch" || e.target.closest("button")) return;
    press = { i, fired: false, timer: setTimeout(() => { press.fired = true; setFocus(i, { toggle: true }); navigator.vibrate?.(10); }, 450) };
  });
  const cancelPress = () => { if (press) clearTimeout(press.timer); };
  gridEl.addEventListener("pointermove", cancelPress);
  gridEl.addEventListener("pointercancel", cancelPress);
  gridEl.addEventListener("click", (e) => {
    const i = tileIndex(e);
    if (i < 0) return;
    if (press) {
      clearTimeout(press.timer);
      const fired = press.fired;
      press = null;
      if (fired) return;
      if (g.sel.size) { setFocus(i, { toggle: true }); return; }  // touch: selection mode
      setFocus(i); openAt(i, g); return;
    }
    if (e.shiftKey) setFocus(i, { extend: true });
    else if (e.metaKey || e.ctrlKey) setFocus(i, { toggle: true, withFocused: true });
    else setFocus(i, { keep: false });
  });
  // Tabbing into the grid puts the keyboard on a photo: the one you were on, or the first in view.
  gridEl.addEventListener("focus", () => {
    if (g.focus >= 0 || !g.ids.length || !gridEl.matches(":focus-visible")) return;
    const y = window.scrollY - (gridEl.getBoundingClientRect().top + window.scrollY) + 110;
    const i = g.layout.items.findIndex((p) => p && p.y + p.h > y);
    setFocus(Math.max(0, i), { keep: true });
  });
  gridEl.addEventListener("dblclick", (e) => {
    const i = tileIndex(e);
    if (i >= 0) openAt(i, g);
  });

  function vertical(dir) {
    const cur = g.layout.items[g.focus];
    if (!cur) return 0;
    const rows = g.layout.rows;
    let k = cur.row + dir;
    while (k >= 0 && k < rows.length && rows[k].header) k += dir;
    if (k < 0 || k >= rows.length) return -1;
    const cx = cur.x + cur.w / 2;
    let best = -1, bestDx = Infinity;
    for (let i = rows[k].start; i <= rows[k].end; i++) {
      const p = g.layout.items[i];
      const dx = Math.abs(p.x + p.w / 2 - cx);
      if (dx < bestDx) { bestDx = dx; best = i; }
    }
    return best;
  }

  g.ensureTarget = () => {
    if (g.focus >= 0 || g.sel.size || !g.ids.length) return true;
    setFocus(0);
    return false;
  };
  g.targetIds = () => (g.sel.size ? [...g.sel] : g.focus >= 0 ? [g.ids[g.focus]] : []);

  g.onKey = (e) => {
    const k = e.key;
    if (k === "ArrowRight" || k === "ArrowLeft") {
      const i = g.focus < 0 ? 0 : g.focus + (k === "ArrowRight" ? 1 : -1);
      setFocus(Math.max(0, Math.min(g.ids.length - 1, i)), { extend: e.shiftKey });
      return true;
    }
    if (k === "ArrowDown" || k === "ArrowUp") {
      const i = g.focus < 0 ? 0 : vertical(k === "ArrowDown" ? 1 : -1);
      if (i >= 0) setFocus(i, { extend: e.shiftKey });
      return true;
    }
    if ((k === "Enter" || k === " ") && g.focus >= 0) { openAt(g.focus, g); return true; }
    if (k === "Escape" && g.sel.size) { clearSelection(); return true; }
    if ((e.metaKey || e.ctrlKey) && e.shiftKey && k.toLowerCase() === "i") {
      // Invert: what's loaded and not selected becomes the selection.
      g.matchAll = false;
      g.sel = new Set(g.ids.filter((id) => !g.sel.has(id)));
      g.base = null;
      paintSel();
      return true;
    }
    if ((e.metaKey || e.ctrlKey) && !e.shiftKey && k.toLowerCase() === "a") {
      // Once: everything loaded. Again: everything matching (the selection bar asks the server).
      const everything = g.ids.length && g.ids.every((id) => g.sel.has(id));
      if (everything && !g.done && !g.matchAll && g.selectAllMatching) g.selectAllMatching();
      else { g.ids.forEach((id) => g.sel.add(id)); paintSel(); }
      return true;
    }
    if (k === "Home") { toEnd(false, e.shiftKey); return true; }
    if (k === "End") { toEnd(true, e.shiftKey); return true; }
    if (k === "PageDown" || k === "PageUp") {
      const i = g.focus < 0 ? 0 : byScreen(k === "PageDown" ? 1 : -1);
      if (i >= 0) setFocus(i, { extend: e.shiftKey });
      return true;
    }
    return false;
  };

  /** Esc: a small selection just goes; a big one goes with a way to have it back. */
  function clearSelection() {
    const was = { sel: new Set(g.sel), matchAll: g.matchAll, total: g.matchTotal };
    g.sel.clear();
    g.matchAll = false;
    g.base = null;
    paintSel();
    if (was.sel.size > ASK_BEFORE_CLEARING || was.matchAll) {
      toast(`Cleared the selection of ${plural(was.matchAll ? was.total : was.sel.size, "photo")}`, { key: "selection", action: () => {
        const loaded = new Set(g.ids);
        g.sel = new Set([...was.sel].filter((id) => loaded.has(id)));
        g.matchAll = was.matchAll && g.sel.size === g.ids.length;
        g.matchTotal = was.total;
        paintSel();
      }, actionLabel: "Select again" });
    }
  }

  /** The photo about a screen above or below the focused one (nearest across). */
  function byScreen(dir) {
    const cur = g.layout.items[g.focus];
    if (!cur) return 0;
    const want = cur.y + dir * Math.max(200, window.innerHeight - 200);
    const rows = g.layout.rows.filter((r) => !r.header);
    if (!rows.length) return -1;
    let best = rows[0];
    for (const r of rows) if (Math.abs(r.y - want) < Math.abs(best.y - want)) best = r;
    const cx = cur.x + cur.w / 2;
    let pick = best.start, bestDx = Infinity;
    for (let i = best.start; i <= best.end; i++) {
      const p = g.layout.items[i];
      const dx = Math.abs(p.x + p.w / 2 - cx);
      if (dx < bestDx) { bestDx = dx; pick = i; }
    }
    return pick;
  }

  /** Home and End: the very first or last photo, loading from there when it isn't loaded yet. */
  async function toEnd(last, extend) {
    if (!last && g.start > 0) await restartAt(0);
    else if (last && !g.done && g.total) {
      const from = Math.max(0, g.total - PAGE);
      if (from > g.start + g.ids.length) await restartAt(from);
      else for (let guard = 0; !g.done && !g.failed && guard < 20; guard++) await more(1000);
    }
    if (g.ids.length) setFocus(last ? g.ids.length - 1 : 0, { extend: extend && g.anchor >= 0 });
  }

  g.more = more;
  g.jumpTo = jumpTo;
  g.selectAll = () => { g.ids.forEach((id) => g.sel.add(id)); paintSel(); };
  g.clearSel = () => { g.sel.clear(); g.base = null; paintSel(); };
  g.clearSelection = clearSelection;
  g.onSelection = (fn) => g.selListeners.add(fn);

  const off = on((type, ids) => {
    if (type !== "rows") return;
    for (const id of ids) {
      const t = g.tiles.get(id);
      const r = store.rows.get(id);
      if (t && r) paint(t, r);
    }
  });
  const onScroll = () => schedule();
  let lastW = 0;
  const onResize = () => {
    const w = gridEl.clientWidth;
    if (w && w !== lastW) { lastW = w; g.relayout(); }
  };
  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", onResize);
  const ro = new ResizeObserver(onResize);
  ro.observe(gridEl);
  g.destroy = () => { off(); window.removeEventListener("scroll", onScroll); window.removeEventListener("resize", onResize); ro.disconnect(); };

  loadScrubber();
  more();
  return g;
}

/** Both thumbnail sizes (400 and 768 px on the long side) by width, for a tile's srcset. */
export function thumbSrcset(r) {
  const w = (side) => Math.round(side * Math.min(1, r.ar || 1.5));
  return `${img.thumb(store.project, r)} ${w(400)}w, ${img.thumb(store.project, r, "m")} ${w(768)}w`;
}

/** A photo to open with a click or the keyboard: "Open DSCF0103, 14 Jun 2025". `cls` goes on the
 * picture, which keeps the look it had on its own. */
export function thumbButton(r, onOpen, { size = "s", cls = "" } = {}) {
  return h("button", { type: "button", class: "thumb", "aria-label": `Open ${photoName(r)}`, title: r.stem, onclick: onOpen },
    h("img", { class: cls || null, src: img.thumb(store.project, r, size), alt: "", loading: "lazy", draggable: "false" }));
}

/** A small, non-windowed strip of thumbnails (Home, Events, Albums), opening the loupe. */
export function thumbStrip(ids, onOpen, { size = "s", height = 120 } = {}) {
  return h("div", { class: "strip", style: { "--strip-h": `${height}px` } }, ids.map((id, i) => {
    const r = store.rows.get(id);
    return r ? thumbButton(r, () => onOpen(i), { size }) : null;
  }));
}
