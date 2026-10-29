// Cull: one stack (burst / scene / single) at a time on a dark stage, decided with single keys
// (or deliberate swipes on a phone). P and X set a frame's pick or reject and move on to the next
// undecided frame; once every frame is decided, on to the next stack. C compares up to six frames
// side by side with one zoom; G shows everyone's face across the burst.

import { api, img, P } from "../api.js";
import { flash, openLoupe, picture, prefetch, preload, zoomControl, zoomLock } from "../loupe.js";
import { acceptSuggestions, clearDecision, decide, getLocal, on, remember, row, setLabel, setLocal, setPick, setPref, settings, store, toggleFav, undo } from "../store.js";
import { clear, closePopover, dialog, flagLabel, h, icon, n, plural, popover, starsEl, sugLabel, toast, when } from "../ui.js";
import { albumDialog, groupingControl, keysDialog } from "./common.js";
import { keywordDialog } from "./keywords.js";
import { coverDialog } from "./shotlist.js";
import { trashDialog } from "./trash.js";
import { waitNote } from "./waiting.js";

const PASS = ["person", "people", "cluster", "suggested", "flag", "camera", "folder", "from", "to", "min_q", "min_rating", "event", "place", "ids"];
const LABEL_KEYS = { 6: "Red", 7: "Yellow", 8: "Green", 9: "Blue" };
// Keep only this and Accept ask first before rejecting more than this many frames, or a stack
// spanning longer than this (a whole scene rather than a burst).
const BIG = { frames: 10, seconds: 60 };

/** The keys, for the shortcuts list. */
export const KEYS = { name: "Cull", rows: [
  ["P", "Pick, then on to the next undecided frame (pressing it again changes nothing)"],
  ["X", "Reject, then on to the next undecided frame"],
  ["0", "Clear the pick or reject and the stars (stays on the frame)"],
  ["1 – 5", "Stars"], ["6 7 8 9", "Red, yellow, green, blue label"],
  ["⇧ P", "Keep this frame, reject the rest of the stack (asks first above 10 frames or a minute)"],
  ["A", "Accept: keep the best frame, reject the rest of the stack (asks the same way)"],
  ["→ / L", "Next frame (at the last one, again for the next stack)"], ["← / H", "Previous frame"],
  ["↓ / J / N / Enter", "Next stack"], ["↑ / K / B", "Previous stack"],
  ["C", "Compare 2–6 frames side by side, with one zoom"], ["G", "Everyone's faces across the burst"],
  ["W", "Why: how this frame ranks in its stack"], ["S", "Shot list: tick the moments this frame covers"],
  ["Z / ⇧ Z", "Zoom to 100% / 50% (again for the whole photo)"], ["Space", "Open in the viewer"],
  ["F", "Favourite"], ["T", "Keywords"], ["U / ⇧ U", "Undo / redo"], ["?", "This list"],
] };

const undecided = (r) => !!r && !r.pick && !r.rating;
const ordinal = (k) => `${k}${10 <= k % 100 && k % 100 <= 20 ? "th" : { 1: "st", 2: "nd", 3: "rd" }[k % 10] || "th"}`;
/** "12 s", "1 min 43 s", "1 h 5 min". */
function span(s) {
  s = Math.round(s || 0);
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min${s % 60 ? ` ${s % 60} s` : ""}`;
  return `${Math.floor(s / 3600)} h${Math.round((s % 3600) / 60) ? ` ${Math.round((s % 3600) / 60)} min` : ""}`;
}

export function mount(root, ctx) {
  const status = ctx.params.get("status") || "undecided";
  const extra = Object.fromEntries(PASS.map((k) => [k, ctx.params.get(k)]).filter(([, v]) => v));
  const resumeKey = `joeylab.cull.${ctx.project}.${status}`;
  const st = { stacks: [], si: 0, fi: 0, survey: false, faces: false, why: false, cfg: {}, picks: 0, lock: zoomLock(),
    bump: null, cells: new Map(), count: 4, start: 0, at: null, covers: new Map() };

  // ------------------------------------------------------------ the page
  const where = h("b", { class: "where" });
  const stats = h("span", { class: "stats" });
  const kind = h("span", { class: "kind" });
  const progress = h("i");
  const progressBox = h("div", { class: "progress", "aria-hidden": "true" }, progress);
  const target = h("span", { class: "target-pill", hidden: true });
  const filterSel = h("select", { class: "input", id: "cull-filter", "aria-label": "Which photos to cull",
    onchange: (e) => ctx.setParams({ ...Object.fromEntries(ctx.params), status: e.target.value }, true) },
  [["undecided", "Undecided"], ["all", "Everything"], ["picks", "Picks"], ["rejects", "Rejects"], ["rated", "Rated"]]
    .map(([v, t]) => h("option", { value: v, selected: v === status }, t)));
  const compareBtn = h("button", { class: "btn ghost small", title: "Compare frames side by side (C)", "aria-label": "Compare frames",
    onclick: () => toggleSurvey() }, icon("compare"), h("span", { class: "lbl" }, "Compare"));
  const facesBtn = h("button", { class: "btn ghost small", title: "Everyone's face across the burst (G)", "aria-label": "Faces across the burst",
    onclick: () => toggleFaces() }, icon("faces"), h("span", { class: "lbl" }, "Faces"));
  // Shown once the library has a shot list.
  const shotBtn = h("button", { class: "btn ghost small", hidden: true, title: "Shot list: tick the moments this frame covers (S)",
    "aria-label": "Shot list", onclick: () => shotList() }, icon("check2"), h("span", { class: "lbl" }, "Shot list"));
  const auto = h("input", { type: "checkbox", id: "cull-auto", checked: store.prefs.autoAdvance,
    onchange: (e) => setPref("autoAdvance", e.target.checked) });
  const head = h("div", { class: "cull-head" }, h("h1", { class: "sr-only" }, "Cull"),
    h("button", { class: "btn ghost icon-only prev-stack", title: "Previous stack (↑)", "aria-label": "Previous stack", onclick: () => goStack(st.si - 1) }, icon("up")),
    h("div", { class: "title" }, h("div", { class: "line" }, where, stats), kind,
      // While an update analyses new photos: their stacks and suggestions come once that's done.
      waitNote({ step: "group", progressOf: "analyse", ready: "Stacks and suggestions are ready", onReady: () => load(),
        text: () => (st.stacks.some((s) => s.ids.length > 1) ? "New photos join their stacks when analysis finishes"
          : "Stacks and suggestions will appear when analysis finishes") })),
    progressBox, target,
    h("div", { class: "right" },
      filterSel, compareBtn, facesBtn, shotBtn,
      h("label", { class: "check auto-wide", title: "Move to the next stack once every frame in this one is decided" }, auto, h("span", {}, "Auto-advance")),
      h("button", { class: "btn ghost small", title: "Culling options: auto-advance, where a stack starts, how strictly frames are stacked",
        "aria-label": "Culling options", onclick: (e) => options(e.currentTarget) }, icon("layers"), h("span", { class: "lbl" }, "Options")),
      h("button", { class: "btn ghost icon-only", title: "Keyboard shortcuts (?)", "aria-label": "Keyboard shortcuts", onclick: () => keysDialog(KEYS) }, "?")));

  const pc = picture({ lock: st.lock });
  const tags = h("div", { class: "overlay-tags" });
  const whyBox = h("div", { class: "why-box", hidden: true });
  const caption = h("div", { class: "caption" });
  const cue = h("div", { class: "swipe-cue", "aria-hidden": "true" });
  const viewerEl = h("div", { class: "viewer" }, pc.el, tags, whyBox, caption, cue);
  const surveyBar = h("div", { class: "survey-count" });
  const surveyGrid = h("div", { class: "survey-cells" });
  const surveyEl = h("div", { class: "cull-survey", hidden: true }, surveyBar, surveyGrid);
  const doneEl = h("div", { class: "cull-done", hidden: true });
  const stage = h("div", { class: "cull-stage" }, viewerEl, surveyEl, doneEl);
  const facesPanel = h("div", { class: "faces-panel", hidden: true });
  const strip = h("div", { class: "cull-strip", role: "listbox", "aria-label": "Frames in this stack" });
  const zc = zoomControl((v) => setZoom(v));
  const barMain = h("div", { class: "bar-main" });
  // A phone or tablet: a second row for the stars, Accept and the rest, so Pick, Reject, Undo and Next
  // stay in reach on the first.
  const barMore = h("div", { class: "bar-more", hidden: true });
  const narrowBar = window.matchMedia("(max-width: 900px)");
  const nextBtn = h("button", { class: "btn primary next", title: "Next stack (↓)", onclick: () => goStack(st.si + 1) }, "Next", icon("down"));
  const bar = h("div", { class: "cull-bar" }, barMain, nextBtn, barMore);
  const onNarrow = () => { if (stack()) render(); };
  narrowBar.addEventListener("change", onNarrow);
  const swipeHint = h("div", { class: "swipe-hint" }, "Swipe up to pick, down to reject, sideways between frames · tap to open");
  const shell = h("div", { class: "cull stage-ui" }, head, h("div", { class: "cull-main" }, stage, facesPanel), strip, bar, swipeHint);
  root.append(shell);
  // Messages sit at the top of the stage, under this header (however tall it is on this screen).
  const headSize = new ResizeObserver(() => document.documentElement.style.setProperty("--stage-top", `${Math.round(head.getBoundingClientRect().bottom)}px`));
  headSize.observe(head);

  // ------------------------------------------------------------ data
  const stack = () => st.stacks[st.si];
  const frames = () => (stack()?.ids || []).map(row).filter(Boolean);
  const focusId = () => stack()?.ids[st.fi];
  const loaded = (s) => s.ids.every((id) => store.rows.has(id));
  const undecidedIn = (s) => (loaded(s) ? s.ids.filter((id) => undecided(row(id))).length : s.und ?? 0);
  const burstOf = (s, id) => { const i = s.ids.indexOf(id); return (s.breaks || []).filter((b) => b <= i).length; };

  /** Rows for this stack and the next few, in one request. */
  async function ensureRows(si, ahead = 3) {
    const missing = st.stacks.slice(Math.max(0, si), si + 1 + ahead).flatMap((s) => s.ids).filter((id) => !store.rows.has(id));
    if (missing.length) remember((await api(P(ctx.project, "/rows"), { params: { ids: missing.join(",") } })).rows);
  }

  function bestIndex(ids) {
    let best = 0, rank = Infinity;
    ids.forEach((id, i) => { const r = row(id); if (r && (r.group_rank ?? 99) < rank) { rank = r.group_rank ?? 99; best = i; } });
    return best;
  }
  /** Where a stack opens: its best frame, or (an option) its first undecided frame. */
  function startIndex(ids) {
    if (store.prefs.cullStart === "first") return Math.max(0, ids.findIndex((id) => undecided(row(id))));
    return bestIndex(ids);
  }
  function nextUndecided(from = st.fi) {
    const ids = stack()?.ids || [];
    for (let k = 1; k < ids.length; k++) { const i = (from + k) % ids.length; if (undecided(row(ids[i]))) return i; }
    return -1;
  }

  async function load() {
    doneEl.hidden = false;
    viewerEl.hidden = surveyEl.hidden = true;
    clear(doneEl).append(h("span", { class: "spinner" }));
    st.cfg = await settings();
    paintTarget();
    api(P(ctx.project, "/shotlist")).then((d) => setShots(d.items)).catch(() => {});
    let res, job;
    try {
      [res, job] = await Promise.all([api(P(ctx.project, "/stacks"), { params: { status: status === "all" ? "" : status, ...extra } }),
        api(P(ctx.project, "/jobs/current")).then((r) => r.job).catch(() => null)]);
    } catch (e) { clear(doneEl).append(h("p", {}, e.message)); return; }
    st.stacks = res.stacks;
    st.reading = job?.state === "running";
    // Nothing to cull: the counts say whether that's "all decided" or an empty library.
    if (!st.stacks.length && !store.overview) {
      try { store.overview = await api(P(ctx.project, "/overview")); } catch { /* said without them */ }
    }
    const resumeId = +(ctx.params.get("at") || getLocal(resumeKey, 0));
    const at = resumeId ? st.stacks.findIndex((s) => s.ids.includes(resumeId)) : -1;
    goStack(at >= 0 ? at : 0, { fresh: true });
  }

  async function paintTarget() {
    const t = st.cfg.delivery_target;
    if (!t) { target.hidden = true; return; }
    try { st.picks = (await api(P(ctx.project, "/shots"), { params: { status: "picks", limit: 1 } })).total; } catch { return; }
    target.hidden = false;
    target.textContent = `${n(st.picks)} / ${n(t)} picked`;
    target.classList.toggle("over", st.picks > t);
    target.title = st.picks > t ? `${st.picks - t} more than you deliver: the Albums page can suggest a balanced ${t}` : `${t - st.picks} to go`;
  }

  // ------------------------------------------------------------ moving
  function goStack(si, { fromEnd = false, fresh = false, at = null } = {}) {
    if (si < 0) { note("This is the first stack"); return; }
    if (si >= st.stacks.length) { st.si = st.stacks.length; renderDone(); return; }
    if (si !== st.si || fresh) {             // another stack: back to the whole photo, a fresh comparison
      st.lock.level = 0;
      dropCells();
      st.start = 0;
    }
    st.si = si;
    st.bump = null;
    const s = stack();
    const enter = () => {
      if (st.si !== si) return;
      st.fi = at !== null && s.ids.includes(at) ? s.ids.indexOf(at) : fromEnd ? s.ids.length - 1 : startIndex(s.ids);
      setLocal(resumeKey, s.ids[0]);
      render();
      if (st.faces) paintFaces();
      warm();
    };
    // Rows are usually here already (fetched a few stacks ahead): the stack shows at once.
    if (loaded(s)) enter();
    else {
      paintHead();
      ensureRows(si).then(enter).catch((e) => toast(e.message, { kind: "error" }));
    }
  }

  function goFrame(fi) {
    const ids = stack()?.ids || [];
    if (fi >= ids.length || fi < 0) {
      if (ids.length < 2) return fi < 0 ? goStack(st.si - 1, { fromEnd: true }) : goStack(st.si + 1);
      // The end of a stack is a stop: a second press moves on.
      const dir = fi < 0 ? -1 : 1;
      if (st.bump?.dir === dir && Date.now() - st.bump.at < 2500) {
        st.bump = null;
        return dir > 0 ? goStack(st.si + 1) : goStack(st.si - 1, { fromEnd: true });
      }
      st.bump = { dir, at: Date.now() };
      note(dir > 0 ? "Last frame of this stack · → again for the next stack" : "First frame of this stack · ← again for the previous stack");
      stage.classList.remove("bump-next", "bump-prev");
      void stage.offsetWidth;
      stage.classList.add(dir > 0 ? "bump-next" : "bump-prev");
      return;
    }
    st.bump = null;
    st.fi = fi;
    render();
    if (st.faces) highlightFaces();
    warm();
  }

  /** Go to a photo's stack and frame (Show, after Undo or a decision on a stack you've left). */
  function goToPhoto(ids) {
    const k = st.stacks.findIndex((s) => ids.some((id) => s.ids.includes(id)));
    if (k < 0) return false;
    goStack(k, { at: ids.find((id) => st.stacks[k].ids.includes(id)) });
    return true;
  }
  store.show = goToPhoto;

  /** Have what comes next ready: where P, X and → lead decoded, the rest of this stack and the
   * start of the next one fetched. */
  function warm() {
    const s = stack();
    if (!s) return;
    const p = ctx.project, ids = s.ids;
    const url = (id) => { const r = row(id); return r && r.media !== "video" ? img.preview(p, r) : null; };
    const go = () => {
      if (stack() !== s) return;
      // First where ↓, → and P/X lead, decoded; then, quietly, the rest of this stack and the next one's start.
      const nx = st.stacks[st.si + 1];
      const first = nx && loaded(nx) ? startIndex(nx.ids) : -1;
      const urgent = [first >= 0 ? url(nx.ids[first]) : null, ...[st.fi + 1, nextUndecided()].filter((i) => i >= 0 && i < ids.length).map((i) => url(ids[i]))];
      for (const u of new Set(urgent.filter(Boolean))) preload(u).catch(() => {});
      prefetch([...ids.slice(st.fi + 1), ...ids.slice(0, st.fi)].map(url)
        .concat(first >= 0 ? [first + 1, first - 1, 0].filter((i) => i >= 0 && i < nx.ids.length).map((i) => url(nx.ids[i])) : []));
    };
    const nx = st.stacks[st.si + 1];
    if (nx && !loaded(nx)) ensureRows(st.si + 1).then(go).catch(() => {});
    else go();
  }

  // ------------------------------------------------------------ drawing
  function paintHead() {
    const s = stack();
    if (!s) return;
    const cur = row(focusId());
    where.textContent = `Stack ${n(s.no ?? st.si + 1)}`;
    const left = st.stacks.reduce((a, x) => a + undecidedIn(x), 0);
    const stacksLeft = st.stacks.filter((x) => undecidedIn(x) > 0).length;
    const here = s.ids.length - undecidedIn(s);
    // Going through picks, rejects or rated photos, nothing is undecided: say where you are instead.
    // Otherwise the photos left, and the stacks they're in (what the top bar's Cull counts).
    const progressText = ["picks", "rejects", "rated"].includes(status) ? `${n(st.si + 1)} of ${plural(st.stacks.length, "stack")}`
      : stacksLeft && stacksLeft !== left ? `${n(left)} undecided left in ${plural(stacksLeft, "stack")}` : `${n(left)} undecided left`;
    clear(stats).append(h("span", { class: "sep" }, " · "), progressText,
      s.ids.length > 1 ? ` · ${n(here)}/${n(s.ids.length)} decided here` : "");
    const bursts = (s.breaks?.length || 0) + 1;
    kind.textContent = [s.ids.length > 1 ? `${n(s.ids.length)} frames` : "Single photo",
      bursts > 1 ? `${bursts} bursts over ${span(s.span)}` : s.span >= 1 ? `over ${span(s.span)}` : null,
      cur ? when(cur.capture_str) : null, cur?.camera].filter(Boolean).join(" · ");
    progress.style.width = `${(100 * st.si) / Math.max(1, st.stacks.length)}%`;
    progressBox.title = `${n(st.si + 1)} of ${plural(st.stacks.length, "stack")} here`;
    compareBtn.hidden = facesBtn.hidden = s.ids.length < 2;
    compareBtn.setAttribute("aria-pressed", st.survey ? "true" : "false");
    facesBtn.setAttribute("aria-pressed", st.faces ? "true" : "false");
  }

  function render() {
    const s = stack();
    if (!s) return renderDone();
    const cur = row(focusId());
    if (!cur) return;
    doneEl.hidden = true;
    strip.hidden = s.ids.length < 2;
    paintHead();
    if (st.survey && s.ids.length > 1) paintSurvey();
    else { surveyEl.hidden = true; viewerEl.hidden = false; paintViewer(cur, s); }
    paintStrip(s);
    paintBar(cur, s);
  }

  function tagsFor(r, s) {
    const out = [];
    if (s.ids.length > 1) out.push(h("span", { class: "pill neutral" }, `${s.ids.indexOf(r.id) + 1} of ${s.ids.length}`));
    if (r.media === "video") out.push(h("span", { class: "pill neutral" }, icon("play"), "Video · open to play"));
    if (r.pick === 1) out.push(h("span", { class: "pill pick" }, "Picked"));
    if (r.pick === -1) out.push(h("span", { class: "pill reject" }, "Rejected"));
    if (r.rating) out.push(h("span", { class: "pill warn" }, "★".repeat(r.rating)));
    if (!r.pick && !r.rating && r.suggestion) out.push(h("button", { class: `pill ${r.suggestion === "check" ? "warn" : "accent"} why-btn`, title: "Why? (W)", onclick: () => toggleWhy() },
      sugLabel(r.suggestion), icon("info")));
    for (const f of r.flags || []) if (f !== "best-in-group") out.push(h("span", { class: "pill neutral" }, flagLabel(f)));
    const covers = st.covers.get(r.id);
    if (covers) out.push(h("button", { class: "pill accent cover-pill", title: `Covers ${covers.join(", ")} on the shot list (S)`, onclick: () => shotList() },
      covers.length === 1 ? `Covers ${covers[0]}` : `Covers ${covers.length} shots`));
    return out;
  }

  function paintViewer(cur, s) {
    pc.show(ctx.project, cur);
    clear(tags).append(tagsFor(cur, s));
    clear(caption).append(h("b", {}, cur.stem), ` · ${when(cur.capture_str)}`,
      cur.quality !== null && cur.quality !== undefined ? ` · quality ${Math.round(cur.quality)}` : "",
      s.ids.length > 1 && cur.group_rank ? ` · ${ordinal(cur.group_rank)} of ${s.ids.length}` : "");
    whyBox.hidden = !st.why;
    if (st.why && whyBox.dataset.id !== String(cur.id)) {
      whyBox.dataset.id = cur.id;
      clear(whyBox).append(h("div", { class: "muted" }, "…"));
      api(P(ctx.project, `/shot/${cur.id}`)).then((d) => {
        if (whyBox.dataset.id === String(cur.id)) clear(whyBox).append(h("ul", { class: "why" }, d.why.map((w) => h("li", { class: `w-${w.kind}` }, w.text))));
      }).catch(() => {});
    }
  }

  // ---- Compare: frames side by side, one zoom and one centre for all of them
  function dropCells() {
    for (const c of st.cells.values()) c.pic.destroy();
    st.cells.clear();
  }
  function paintSurvey() {
    const s = stack(), ids = s.ids, k = Math.min(st.count, ids.length);
    let start = Math.min(st.start, ids.length - k);
    if (st.fi < start) start = st.fi;
    if (st.fi >= start + k) start = st.fi - k + 1;
    st.start = Math.max(0, start);
    const shown = ids.slice(st.start, st.start + k);
    for (const [id, c] of st.cells) if (!shown.includes(id)) { c.pic.destroy(); st.cells.delete(id); }
    surveyGrid.style.setProperty("--cols", k <= 3 ? k : k === 4 ? 2 : 3);
    surveyGrid.dataset.n = k;
    const cells = shown.map((id) => {
      let c = st.cells.get(id);
      if (!c) {
        const pic = picture({ lock: st.lock });
        const label = h("div", { class: "cell-bar" });
        const cell = h("div", { class: "cmp-cell" }, pic.el, label);
        cell.addEventListener("click", () => { if (!pic.dragged()) goFrame(stack().ids.indexOf(id)); });
        cell.addEventListener("dblclick", () => openStackLoupe(false));
        c = { pic, cell, label };
        st.cells.set(id, c);
      }
      return c;
    });
    // Cells move only when the frames shown change (moving one loses its place at 100%).
    if (surveyGrid.children.length !== cells.length || [...surveyGrid.children].some((el, i) => el !== cells[i].cell)) {
      clear(surveyGrid).append(cells.map((c) => c.cell));
    }
    clear(surveyBar).append(h("span", { class: "faint" }, `${ids.indexOf(shown[0]) + 1}–${ids.indexOf(shown[k - 1]) + 1} of ${ids.length} side by side · one zoom for all`),
      h("span", { class: "spacer" }), h("span", { class: "faint" }, "Show"),
      [2, 3, 4, 6].filter((x) => x <= ids.length || x === 2).map((x) => h("button", { type: "button", class: "btn ghost small",
        "aria-pressed": x === k ? "true" : "false", title: `${x} frames at a time`, onclick: () => { st.count = x; render(); } }, String(x))));
    viewerEl.hidden = true;
    surveyEl.hidden = false;
    shown.forEach((id, i) => {
      const r = row(id), c = cells[i];
      c.pic.show(ctx.project, r);
      c.cell.classList.toggle("cur", id === focusId());
      c.cell.classList.toggle("rejected", r.pick === -1);
      clear(c.label).append(h("b", {}, `${ids.indexOf(id) + 1}`), h("span", { class: "stem" }, r.stem),
        r.group_rank ? h("span", { class: "faint" }, ordinal(r.group_rank)) : null,
        r.pick === 1 ? h("span", { class: "pill pick" }, "Picked") : r.pick === -1 ? h("span", { class: "pill reject" }, "Rejected") : null,
        r.rating ? h("span", { class: "pill warn" }, "★".repeat(r.rating)) : null);
    });
  }
  function toggleSurvey() {
    if ((stack()?.ids.length || 0) < 2) return;
    st.survey = !st.survey;
    if (!st.survey) dropCells();
    render();
  }

  function paintStrip(s) {
    // One Tab stop, the frame in view; the keyboard stays on it as ← and → move along the stack.
    const had = strip.contains(document.activeElement);
    clear(strip);
    if (s.ids.length < 2) return;
    const breaks = new Set(s.breaks || []);
    s.ids.forEach((id, i) => {
      const r = row(id);
      if (!r) return;
      if (breaks.has(i)) strip.append(h("span", { class: "burst-gap", title: "A new burst starts here" }));
      const badges = h("div", { class: "tl" });
      if (r.pick === 1) badges.append(h("span", { class: "tbadge pick" }, icon("pick")));
      if (r.pick === -1) badges.append(h("span", { class: "tbadge reject" }, icon("reject")));
      if (r.flags?.includes("best-in-group")) badges.append(h("span", { class: "tbadge", title: "Suggested best" }, icon("star")));
      const f = h("button", { type: "button", class: `f ${i === st.fi ? "cur" : ""} ${r.pick === -1 ? "reject" : ""} ${undecided(r) ? "" : "decided"}`,
        role: "option", "aria-selected": i === st.fi ? "true" : "false", tabindex: i === st.fi ? "0" : "-1", onclick: () => goFrame(i),
        "aria-label": [r.stem, r.group_rank ? `${ordinal(r.group_rank)} best of ${s.ids.length}` : "", r.pick === 1 ? "picked" : r.pick === -1 ? "rejected" : "",
          r.rating ? plural(r.rating, "star") : ""].filter(Boolean).join(", "),
        title: `${r.stem}${r.group_rank ? ` · ${ordinal(r.group_rank)} best of ${s.ids.length}` : ""}` },
      h("img", { src: img.thumb(ctx.project, r), alt: r.stem, draggable: "false" }), badges,
      r.group_rank ? h("span", { class: "rk" }, ordinal(r.group_rank)) : null,
      h("span", { class: "n" }, r.rating ? "★".repeat(r.rating) : r.quality !== null && r.quality !== undefined ? Math.round(r.quality) : ""));
      strip.append(f);
      if (i === st.fi) requestAnimationFrame(() => f.scrollIntoView({ inline: "nearest", block: "nearest" }));
    });
    if (had) strip.querySelector(".f.cur")?.focus({ preventScroll: true });
  }

  function paintBar(cur, s) {
    const decided = !undecided(cur);
    const stars = starsEl(cur.rating || 0, (v) => decideFocus({ rating: v }));
    const pick = h("button", { class: `btn pick-btn ${cur.pick === 1 ? "on-pick" : ""}`, title: "Pick (P)", onclick: () => pickReject(1) }, icon("pick"), "Pick", h("kbd", {}, "P"));
    const reject = h("button", { class: `btn reject-btn ${cur.pick === -1 ? "on-reject" : ""}`, title: "Reject (X)", onclick: () => pickReject(-1) }, icon("reject"), "Reject", h("kbd", {}, "X"));
    const clearBtn = decided ? h("button", { class: "btn ghost clear-btn", title: "Clear the pick or reject and the stars (0)", onclick: clearFocus }, "Clear", h("kbd", {}, "0")) : null;
    const keep = s.ids.length > 1 ? h("button", { class: "btn keep-btn", title: "Pick this frame and reject the others (⇧P)", onclick: keepOnly },
      icon("keep"), "Keep only this", h("kbd", {}, "⇧P")) : null;
    const accept = h("button", { class: "btn ghost accept-btn", title: s.ids.length > 1 ? "Accept: keep the best frame, reject the rest (A)" : "Accept the suggestion (A)", onclick: acceptStack },
      icon("sparkle"), h("span", {}, "Accept"), h("kbd", {}, "A"));
    const fav = h("button", { class: `btn ghost icon-only ${cur.fav ? "on-fav" : ""}`, title: "Favourite (F)", "aria-label": "Favourite", onclick: () => toggleFav([cur.id], !cur.fav) }, icon(cur.fav ? "heartOn" : "heart"));
    const undoBtn = h("button", { class: "btn ghost icon-only undo-btn", title: "Undo (U)", "aria-label": "Undo", onclick: () => undo() }, icon("undo"));
    if (narrowBar.matches) {
      clear(barMain).append(pick, reject, h("span", { class: "spacer" }), undoBtn);
      clear(barMore).append(accept, stars, fav, clearBtn, keep, zc.el);
      barMore.hidden = false;
    } else {
      clear(barMain).append(stars, pick, reject, clearBtn, keep, accept, fav, h("span", { class: "spacer" }), undoBtn, zc.el);
      clear(barMore);
      barMore.hidden = true;
    }
    zc.paint(st.lock.level);
  }

  function renderDone() {
    clear(strip); strip.hidden = true; clear(barMain); clear(barMore); barMore.hidden = true;
    facesPanel.hidden = true;
    kind.textContent = stats.textContent = "";
    viewerEl.hidden = surveyEl.hidden = true;
    doneEl.hidden = false;
    // An empty list while JoeyLab is still reading the library isn't "everything decided".
    st.waiting = !st.stacks.length && st.reading;
    if (st.waiting) {
      where.textContent = "Reading your photos";
      progress.style.width = "0%";
      clear(doneEl).append(
        h("h2", {}, "JoeyLab is still reading this library"),
        h("p", { class: "muted" }, "Stacks to cull appear here once photos are read and stacked. This page fills in by itself."),
        h("div", { class: "row", style: { justifyContent: "center" } },
          h("a", { class: "btn", href: `#/p/${encodeURIComponent(ctx.project)}/home` }, "See the progress")));
      return;
    }
    where.textContent = st.stacks.length ? "All stacks seen" : "Nothing to cull";
    progress.style.width = "100%";
    const o = store.overview?.counts;
    const base = `#/p/${encodeURIComponent(ctx.project)}`;
    clear(doneEl).append(
      h("h2", {}, st.stacks.length ? "You've been through every stack" : o && !o.photos ? "No photos in this library yet"
        : status === "undecided" && !Object.keys(extra).length ? "Everything here has been decided" : "No photos match"),
      o && !o.photos ? h("p", { class: "muted" }, o.failed ? `JoeyLab couldn't read ${plural(o.failed, "file")} in its folders as photos (Tidy › Health lists them). Add another folder under Settings › Folders.`
        : "Add a folder with photos in it under Settings › Folders; they appear here to cull.")
        : o ? h("p", { class: "muted" }, `${plural(o.picks, "pick")} and ${plural(o.rejects, "reject")} so far.`) : null,
      o && !o.photos ? h("div", { class: "row", style: { justifyContent: "center" } },
        h("a", { class: "btn primary", href: `${base}/settings?s=folders` }, icon("folder"), "Folders")) :
        h("div", { class: "row", style: { justifyContent: "center" } },
          h("a", { class: "btn primary", href: `${base}/library?status=picks` }, "See your picks"),
          h("a", { class: "btn", href: `${base}/settings?s=export`, title: "An album folder, XMP sidecars for Lightroom and others, or a list" }, icon("download"), "Export picks…"),
          h("button", { class: "btn", onclick: () => albumDialog({ filter: { status: "picks" }, name: `${ctx.project} picks` }) }, icon("album"), "Save picks as album"),
          st.cfg.delivery_target ? h("a", { class: "btn", href: `${base}/albums?tab=balance` }, icon("keep"), "Balanced set for delivery") : null),
      // Rejecting only marks a photo; removing them is this step, when you choose it.
      store.meta?.can_trash && (!o || o.rejects) ? h("div", { class: "stack-v", style: { justifyItems: "center", gap: "8px" } },
        h("p", { class: "muted", style: { margin: 0 } }, "Your rejects are only marked: nothing has moved. When you want them gone, move them to the Trash."),
        h("button", { class: "btn", title: "Each file is checked first; nothing moves until you confirm, and Finder's Put Back returns it",
          onclick: () => trashDialog({ what: "rejects", filter: { status: "rejects" } }) }, icon("close"), "Move rejects to the Trash…")) : null,
      h("div", { class: "row", style: { justifyContent: "center" } },
        st.stacks.length ? h("button", { class: "btn ghost", onclick: () => { setLocal(resumeKey, 0); goStack(0, { fresh: true }); } }, "Start over") : null,
        status !== "all" && !(o && !o.photos) ? h("button", { class: "btn ghost", onclick: () => ctx.setParams({ status: "all" }, true) }, "Cull everything") : null));
  }

  /** A short line at the top of the stage (the end of a stack, a stack fully decided). */
  function note(text) {
    let el = stage.querySelector(":scope > .stage-note");
    if (!el) stage.append(el = h("div", { class: "stage-note", role: "status" }));
    el.textContent = text;
    el.classList.remove("on");
    void el.offsetWidth;
    el.classList.add("on");
    clearTimeout(el.off);
    el.off = setTimeout(() => el.classList.remove("on"), 2700);
  }

  // ------------------------------------------------------------ faces across the burst
  function toggleFaces() {
    st.faces = !st.faces;
    facesPanel.hidden = !st.faces;
    if (st.faces) paintFaces(true); else render();
    facesBtn.setAttribute("aria-pressed", st.faces ? "true" : "false");
  }

  /** `asked`: G or Faces was just pressed (so a stack without faces says so). */
  async function paintFaces(asked = false) {
    const s = stack();
    if (!s || s.ids.length < 2) { facesPanel.hidden = true; return; }
    facesPanel.hidden = false;
    clear(facesPanel).append(h("div", { class: "muted" }, "Finding everyone's face…"));
    let d;
    try { d = await api(P(ctx.project, "/compare-faces"), { params: { ids: s.ids.join(",") } }); }
    catch (e) { clear(facesPanel).append(h("div", { class: "muted" }, e.message)); return; }
    if (stack() !== s) return;
    // No faces here: the panel stays out of the way (it would take half the stage on every stack).
    if (!d.people.length) {
      facesPanel.hidden = true;
      if (!store.overview?.counts?.faces) {             // none in the whole library: recognition is off, or found nothing
        st.faces = false;
        facesBtn.setAttribute("aria-pressed", "false");
        toast("This library has no faces to compare. Face recognition can be turned on in Settings › People & privacy.",
          { action: () => ctx.go("settings", { s: "privacy" }), actionLabel: "Settings", key: "faces" });
      } else if (asked) note("No faces large enough to compare in this stack");
      return;
    }
    const frameIds = d.frames;
    // Most eyes open first; between frames level on that, the faces that look best (face quality, smiles
    // and focus: a turned head alone never decides).
    const bestFrame = frameIds.reduce((b, id) => {
      const sc = d.score[String(id)];
      const sb = d.score[String(b)];
      const ka = sc.closed + sc.missing, kb = sb.closed + sb.missing;
      return ka < kb || (ka === kb && (sc.look ?? 0) > (sb.look ?? 0) + 0.02) ? id : b;
    }, frameIds[0]);
    clear(facesPanel).append(
      h("div", { class: "faces-head" }, h("b", {}, "Faces across the burst"),
        h("span", { class: "muted" }, "Pick the frame where everyone looks right. Red: eyes closed."),
        h("button", { class: "btn small", title: "Most eyes open; between those, the best face quality, smiles and focus",
          onclick: () => goFrame(s.ids.indexOf(bestFrame)) }, "Go to the best frame for faces")),
      h("div", { class: "faces-grid", style: { "--cols": frameIds.length } },
        h("div", { class: "fh" }),
        frameIds.map((id, i) => h("button", { type: "button", class: `fh frame-col ${s.ids[st.fi] === id ? "cur" : ""}`, onclick: () => goFrame(s.ids.indexOf(id)) },
          `${i + 1}`, h("span", { class: "faint" }, ` ${d.score[String(id)].closed ? "· " + d.score[String(id)].closed + " closed" : "· all open"}`))),
        d.people.flatMap((person) => [
          h("div", { class: "fname" }, person.name || "Someone"),
          ...frameIds.map((id) => {
            const f = person.faces[String(id)];
            return f ? h("div", { class: `fcell ${f.closed ? "closed" : ""} ${s.ids[st.fi] === id ? "cur" : ""}`, onclick: () => goFrame(s.ids.indexOf(id)),
              title: `${f.closed ? "Eyes closed" : "Eyes open"}${f.smile !== null ? ` · smile ${Math.round(f.smile * 100)}%` : ""} · focus ${Math.round(f.sharp)}`
                + (f.caught !== null && f.caught !== undefined ? ` · face quality ${Math.round(f.caught * 100)}%` : "") },
            h("img", { src: `/img/${encodeURIComponent(ctx.project)}/f/${f.id}`, alt: "" })) : h("div", { class: "fcell none" }, "—");
          }),
        ])));
  }

  function highlightFaces() {
    const s = stack();
    if (!s) return;
    const id = s.ids[st.fi];
    facesPanel.querySelectorAll(".frame-col, .fcell").forEach((el) => el.classList.remove("cur"));
    // columns are in frame order: header buttons first, then rows of cells
    const cols = facesPanel.querySelectorAll(".frame-col");
    const k = s.ids.indexOf(id);
    cols[k]?.classList.add("cur");
    const count = cols.length;
    facesPanel.querySelectorAll(".fcell").forEach((el, i) => { if (i % count === k) el.classList.add("cur"); });
  }

  // ------------------------------------------------------------ deciding
  function decideFocus(change) {
    const id = focusId();
    if (!id) return;
    decide([{ id, ...change }], change.rating !== undefined ? (change.rating ? `${change.rating}★` : "clear stars") : "change");
  }

  /** P and X: set (never toggle), say so on the stage, and move on to the next undecided frame. */
  function pickReject(value) {
    const id = focusId();
    if (!id) return;
    flash(stage, value === 1 ? "Picked" : "Rejected", value === 1 ? "pick" : "reject", row(id)?.stem);
    setPick([id], value);
    decided({ advance: true });
  }

  /** 0: back to undecided, and stay on it. */
  function clearFocus() {
    const id = focusId();
    if (!id) return;
    clearDecision([id]);
    flash(stage, "Cleared", "", row(id)?.stem);
    decided({ advance: false });
  }

  function decided({ advance }) {
    if (st.cfg.delivery_target) paintTarget();
    const s = stack();
    if (!advance || !s) return;
    const next = nextUndecided();
    if (next >= 0) { goFrame(next); return; }
    if (undecidedIn(s)) return;                // only this frame is left, still undecided
    if (store.prefs.autoAdvance) {
      const si = st.si;
      setTimeout(() => { if (st.si === si && stack() === s) goStack(si + 1); }, 450);
    } else note("Every frame here is decided · ↓ for the next stack");
  }

  /** "Kept IMG_0005 · rejected 15", with Undo and Show (back to that stack). */
  function told(msg, batch, s, id) {
    toast(msg, { action: () => undo(batch), actionLabel: "Undo", actions: [{ label: "Show", run: () => goToPhoto([id]) }], key: "cull-step" });
  }

  /** Rejecting many frames, or a stack spanning a whole scene, is asked about first, with the
   * choice of rejecting only the kept frame's own burst. Resolves to "all", "burst" or null. */
  function askBig(s, keepId, rejects) {
    const fresh = rejects.filter((x) => row(x)?.pick !== -1);
    if (fresh.length <= BIG.frames && (s.span || 0) <= BIG.seconds) return Promise.resolve("all");
    const bursts = (s.breaks?.length || 0) + 1;
    const own = fresh.filter((x) => burstOf(s, x) === burstOf(s, keepId)).length;
    const offer = bursts > 1 && own < fresh.length;
    return new Promise((resolve) => {
      let answered = false;
      const end = (v) => { answered = true; resolve(v); };
      const dlg = dialog(`Reject ${plural(fresh.length, "other frame")}?`, h("div", { class: "stack-v" },
        h("p", {}, `This stack is ${n(s.ids.length)} frames${bursts > 1 ? ` in ${bursts} bursts` : ""}, taken over ${span(s.span)}. `
          + `Keeping ${row(keepId)?.stem || "this frame"} rejects ${fresh.length === 1 ? "the other one" : `the other ${n(fresh.length)}`}.`),
        offer ? h("p", { class: "note" }, `Its own burst has ${plural(own, "other frame")}: you can reject only ${own === 1 ? "that one" : "those"}.`) : null), [
        { label: "Cancel", run: () => end(null) },
        offer ? { label: `Only its burst (${n(own)})`, run: () => end("burst") } : null,
        { label: `Reject ${n(fresh.length)}`, primary: true, run: () => end("all") },
      ].filter(Boolean));
      dlg.addEventListener("close", () => { if (!answered) resolve(null); });
    });
  }

  /** ⇧P: pick this frame, reject the others (picks stay). */
  async function keepOnly() {
    const s = stack(), id = focusId();
    if (!s || !id) return;
    if (s.ids.length < 2) return pickReject(1);
    let others = s.ids.filter((x) => x !== id && row(x)?.pick !== 1);
    const scope = await askBig(s, id, others);
    if (!scope || stack() !== s) return;
    if (scope === "burst") others = others.filter((x) => burstOf(s, x) === burstOf(s, id));
    const fresh = others.filter((x) => row(x)?.pick !== -1).length;
    const res = await decide([{ id, pick: 1 }, ...others.map((x) => ({ id: x, pick: -1 }))], "keep only this");
    if (!res) return;
    flash(stage, "Picked", "pick", row(id)?.stem);
    told(`Kept ${row(id)?.stem} · rejected ${n(fresh)}`, res.batch, s, id);
    if (stack() === s) decided({ advance: true });
  }

  /** A: finish the stack. The best frame is picked (unless one is picked already) and the other
   * undecided frames rejected; a single photo takes its suggestion. */
  async function acceptStack() {
    const s = stack();
    if (!s) return;
    if (s.ids.length < 2 || (store.settings && store.settings.show_suggestions === false)) {
      if (await acceptSuggestions(s.ids)) decided({ advance: true });
      return;
    }
    const rs = s.ids.map(row).filter(Boolean);
    const open = rs.filter(undecided);
    if (!open.length) { note("Every frame here is decided already"); return; }
    const hasPick = rs.some((r) => r.pick === 1);
    const keep = hasPick ? null : [...rs].sort((a, b) => (a.group_rank ?? 99) - (b.group_rank ?? 99))
      .find((r) => r.pick !== -1 && r.suggestion !== "reject") || null;
    // With nothing worth keeping (every frame suggested for rejection), only those suggestions are taken.
    let rej = open.filter((r) => r !== keep && (hasPick || keep || r.suggestion === "reject"));
    const anchor = keep?.id ?? focusId();
    const scope = await askBig(s, anchor, rej.map((r) => r.id));
    if (!scope || stack() !== s) return;
    if (scope === "burst") rej = rej.filter((r) => burstOf(s, r.id) === burstOf(s, anchor));
    const items = [...(keep && !keep.pick ? [{ id: keep.id, pick: 1 }] : []), ...rej.map((r) => ({ id: r.id, pick: -1 }))];
    if (!items.length) { note("Nothing to accept here"); return; }
    const res = await decide(items, "accept the best of a stack");
    if (!res) return;
    flash(stage, keep ? "Picked" : "Rejected", keep ? "pick" : "reject", keep?.stem);
    told(keep ? `Kept ${keep.stem} · rejected ${n(rej.length)}` : `Rejected ${n(rej.length)}`, res.batch, s, anchor);
    if (stack() !== s) return;
    if (keep) st.fi = s.ids.indexOf(keep.id);
    render();
    decided({ advance: true });
  }

  function toggleWhy() { st.why = !st.why; whyBox.dataset.id = ""; render(); }

  /** The shot list's items each frame is tagged as covering (for its pill); the button once there's a list. */
  function setShots(items) {
    st.covers = new Map();
    for (const it of items) for (const id of it.tagged) st.covers.set(id, [...(st.covers.get(id) || []), it.title]);
    shotBtn.hidden = !items.length;
    const cur = row(focusId());
    if (cur && !viewerEl.hidden) clear(tags).append(tagsFor(cur, stack()));
  }
  function shotList() {
    const id = focusId();
    if (id) coverDialog(id, { onChange: setShots });
  }

  function setZoom(level, at = st.at) {
    const r = row(focusId());
    if (!r || r.media === "video") return;
    const focus = st.survey ? st.cells.get(r.id)?.pic : pc;
    (focus || pc).setLevel(level, at);
    zc.paint(st.lock.level);
    stage.classList.toggle("zoomed", !!st.lock.level);
  }

  function openStackLoupe(zoom) {
    const ids = stack()?.ids;
    if (!ids) return;
    openLoupe({ ids, index: st.fi, zoom, lock: st.lock, onClose: (k) => { st.fi = k; render(); zc.paint(st.lock.level); } });
  }

  function options(anchor) {
    const autoBox = h("input", { type: "checkbox", checked: store.prefs.autoAdvance, onchange: (e) => { setPref("autoAdvance", e.target.checked); auto.checked = e.target.checked; } });
    const startSeg = h("div", { class: "seg", role: "group", "aria-label": "Start each stack at" });
    const paintStart = () => clear(startSeg).append([["best", "Its best frame"], ["first", "Its first frame"]].map(([v, t]) =>
      h("button", { type: "button", "aria-pressed": (store.prefs.cullStart || "best") === v ? "true" : "false",
        onclick: () => { setPref("cullStart", v); paintStart(); } }, t)));
    paintStart();
    popover(anchor, h("div", { class: "cull-options" },
      h("label", { class: "check" }, autoBox, h("span", {}, h("b", {}, "Auto-advance"), h("br"),
        h("span", { class: "muted" }, "Move to the next stack once every frame in this one is decided"))),
      h("div", { class: "stack-v" }, h("b", {}, "Start each stack at"), startSeg),
      h("div", { class: "menu-sep" }),
      h("div", { class: "grouping-pop" }, h("b", {}, "How strictly frames are stacked"),
        groupingControl(ctx.project, () => { closePopover(); load(); }))));
  }

  // ------------------------------------------------------------ the stage: zoom point, swipes, taps
  stage.addEventListener("pointermove", (e) => { if (e.pointerType === "mouse") st.at = e.target.closest(".pic") ? { x: e.clientX, y: e.clientY } : null; });
  stage.addEventListener("pointerleave", () => { st.at = null; });
  // A swipe has to be meant: the photo follows the finger, says what letting go will do, and only
  // a long, straight, steady stroke counts. A short or slanted one springs back.
  let sw = null;
  viewerEl.addEventListener("pointerdown", (e) => {
    if (e.pointerType === "mouse" || st.lock.level || e.target.closest("button")) { sw = null; return; }
    sw = { x: e.clientX, y: e.clientY, t: Date.now(), id: e.pointerId };
  });
  viewerEl.addEventListener("pointermove", (e) => {
    if (!sw || e.pointerId !== sw.id) return;
    const dx = e.clientX - sw.x, dy = e.clientY - sw.y;
    const vertical = Math.abs(dy) > Math.abs(dx);
    const reach = vertical ? Math.max(90, viewerEl.clientHeight * 0.2) : Math.max(70, viewerEl.clientWidth * 0.18);
    const armed = vertical ? Math.abs(dy) >= reach && Math.abs(dy) > 2 * Math.abs(dx) : Math.abs(dx) >= reach && Math.abs(dx) > 2 * Math.abs(dy);
    pc.el.style.transform = vertical ? `translateY(${dy * 0.5}px)` : `translateX(${dx * 0.5}px)`;
    pc.el.style.transition = "none";
    cue.className = `swipe-cue ${armed ? "armed" : ""} ${vertical ? (dy < 0 ? "pick" : "reject") : ""}`;
    cue.textContent = Math.abs(vertical ? dy : dx) < 24 ? "" : vertical ? (dy < 0 ? "Pick" : "Reject") : dx < 0 ? "Next frame" : "Previous frame";
    sw.armed = armed ? (vertical ? (dy < 0 ? "pick" : "reject") : dx < 0 ? "next" : "prev") : null;
    sw.moved = sw.moved || Math.abs(dx) + Math.abs(dy) > 10;
  });
  const endSwipe = (e, cancel) => {
    if (!sw || e.pointerId !== sw.id) return;
    const s = sw;
    sw = null;
    pc.el.style.transition = "";
    pc.el.style.transform = "";
    cue.className = "swipe-cue";
    cue.textContent = "";
    if (cancel) return;
    if (!s.moved && Date.now() - s.t < 350) { openStackLoupe(false); return; }     // a tap opens the viewer
    if (Date.now() - s.t > 1500) return;                                           // a slow drag isn't a swipe
    if (s.armed === "pick" || s.armed === "reject") { navigator.vibrate?.(12); pickReject(s.armed === "pick" ? 1 : -1); }
    else if (s.armed === "next") goFrame(st.fi + 1);
    else if (s.armed === "prev") goFrame(st.fi - 1);
  };
  viewerEl.addEventListener("pointerup", (e) => endSwipe(e, false));
  viewerEl.addEventListener("pointercancel", (e) => endSwipe(e, true));
  viewerEl.addEventListener("dblclick", (e) => { if (e.target.closest(".pic")) openStackLoupe(false); });

  const off = on((type, ids) => {
    if (type !== "rows" || !stack()) return;
    if (ids.some((id) => stack().ids.includes(id))) render();
    else paintHead();                         // another stack changed (an undo): the counts did too
  });
  // Waiting for the first scan: load the stacks once it stops (finished, or failed and said so).
  const offJob = on((type, j) => { if (type === "job" && st.waiting && j?.state !== "running") { st.waiting = false; load(); } });

  load();

  return {
    onKey(e) {
      if (e.metaKey || e.ctrlKey || e.altKey) return false;
      const k = e.key;
      if (st.si >= st.stacks.length) return false;
      if (k === "ArrowRight" || k === "l") { goFrame(st.fi + 1); return true; }
      if (k === "ArrowLeft" || k === "h") { goFrame(st.fi - 1); return true; }
      if (k === "ArrowDown" || k === "n" || k === "Enter" || k === "j") { goStack(st.si + 1); return true; }
      if (k === "ArrowUp" || k === "b" || k === "k") { goStack(st.si - 1); return true; }
      if (k === "0") { clearFocus(); return true; }
      if (/^[1-5]$/.test(k)) { decideFocus({ rating: +k }); return true; }
      if (LABEL_KEYS[k]) { setLabel([focusId()], LABEL_KEYS[k]); return true; }
      if (k === "P" || (k === "p" && e.shiftKey)) { keepOnly(); return true; }
      if (k === "p") { pickReject(1); return true; }
      if (k === "x") { pickReject(-1); return true; }
      if (k === "a") { acceptStack(); return true; }
      if (k === "c") { toggleSurvey(); return true; }
      if (k === "g") { toggleFaces(); return true; }
      if (k === "w") { toggleWhy(); return true; }
      if (k === "s") { shotList(); return true; }
      if (k === "f") { const r = row(focusId()); toggleFav([focusId()], !(r && r.fav)); return true; }
      if (k === "t") { keywordDialog([focusId()]); return true; }
      if (k === "z") { setZoom(st.lock.level ? 0 : 1); return true; }
      if (k === "Z") { setZoom(st.lock.level === 0.5 ? 0 : 0.5); return true; }
      if (k === "Escape" && st.lock.level) { setZoom(0); return true; }
      if (k === "Escape" && st.survey) { toggleSurvey(); return true; }
      if (k === " ") { openStackLoupe(false); return true; }
      return false;
    },
    keys: KEYS,
    refresh: load,
    waiting: () => !!st.waiting,
    unmount() { off(); offJob(); headSize.disconnect(); narrowBar.removeEventListener("change", onNarrow); dropCells(); pc.destroy(); if (store.show === goToPhoto) store.show = null; },
  };
}
