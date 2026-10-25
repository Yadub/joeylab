// Library › Explore: every photo on one surface, laid out by a rule you can read off the page:
// what's in them, colour, light, time, quality or camera (explore.py makes the layouts). It zooms
// and pans like a map. Far out, each photo is a square of its main colour; closer, a small
// thumbnail from an atlas (a thousand to an image); closer still, its own thumbnail. Choosing
// another arrangement glides every photo to its new place, which shows what the new rule does.
// The Library's filters choose which photos show; the others keep their places.

import { api, img, P } from "../api.js";
import { openLoupe } from "../loupe.js";
import { on, remember, row } from "../store.js";
import { announce, choiceSeg, cssColors, h, n, photoName, plural, toast, when } from "../ui.js";
import { selectionBar } from "./common.js";

export const ARRANGEMENTS = [["content", "What's in them"], ["colour", "Colour"], ["light", "Light"], ["time", "Time"],
  ["quality", "Quality"], ["camera", "Camera"]];
const CAPTIONS = {
  content: "Arranged by what's in them: photos that look alike sit together, and each region is named by what most of it shows. Zoom in for smaller regions.",
  colour: "Arranged by colour: the main hue around the circle, more colourful towards the edge. Black-and-white photos fill the middle, darker at the bottom.",
  light: "Arranged by light: darker to brighter from left to right, flatter to punchier from bottom to top, each axis shared out evenly among your photos.",
  time: "Arranged by time: the date from left to right, a column for each day with quiet stretches squeezed, and the time of day from top to bottom. A long timeline wraps onto more lines.",
  quality: "Arranged by quality: softer to sharper from left to right, a weaker to a stronger quality score from bottom to top, each axis shared out evenly. The strongest frames gather top right.",
  camera: "Arranged by camera: an island for each camera, its photos in rows by lens and then by date.",
};
// What each arrangement leaves room for around it (css px): axes, hours, the colour names, and on
// the right the map's buttons.
const PAD = { content: [16, 56, 16, 16], colour: [34, 56, 34, 34], light: [16, 56, 52, 56], quality: [16, 56, 52, 56],
  time: [8, 56, 12, 56], camera: [12, 56, 12, 16] };     // top, right, bottom, left
const pad = (by, W) => { const p = PAD[by] || PAD.content; return W < 500 ? [p[0], 12, p[2], p[3]] : p; };

const GAP = 0.12;              // the gap between photos, as a share of a cell, once they're thumbnails
const ATLAS_AT = 14;           // device pixels a cell from which photos are small thumbnails
const THUMB_AT = 64;           // ... from which they're their own thumbnails (the atlas has 48 pixels a photo)
const OPEN_AT = 18;            // css px: a click on a photo this big opens it; on a smaller one it zooms in
const MAX_S = 420;             // css px a cell, at most
const SHEET = 1024, SHEET_SIDE = 32, CELL = 48;                    // explore.py's atlas
const GLIDE = 900, STAGGER = 450;                                   // ms
const TILE = 16;               // cells a tile is, each way (photos drawn once at a few sizes, then reused)
const SHOW_MAX = 1500;         // photos "Show in Library" can list (they go in the address)
const MARK_HIGHLIGHTS = 1, MARK_SHADOWS = 2;
const reduceMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

/** The Explore view. `onBy(k)` keeps the arrangement chosen (the address); `onCount(k)` says how
 * many photos show; `changed()` after the selection bar changed which photos match. `asked`: the
 * arrangement was asked for (a link), so it's shown even when it can't be made here. */
export function exploreView({ project, by: first, asked = false, filter: firstFilter = {}, onBy, onCount, changed }) {
  let by = ARRANGEMENTS.some(([k]) => k === first) ? first : "content";
  let filter = firstFilter;
  const layouts = new Map();             // arrangement -> layout from the server
  let available = {};
  let shownSet = null;                   // ids the filters show (null: not known yet)
  let alive = true;

  // ------------------------------------------------------------ the page
  const canvas = h("canvas", { class: "explore-canvas", tabindex: "0", role: "application", "aria-roledescription": "map",
    "aria-label": "Your photos, arranged. Arrow keys move a pointer (with ⇧, one photo at a time), + and − zoom, Home shows everything, Enter opens the photo at the pointer, Space selects it." });
  const inset = h("canvas", { class: "explore-inset", "aria-hidden": "true", hidden: true });
  const peek = h("div", { class: "explore-peek", hidden: true, "aria-hidden": "true" });
  const boxEl = h("div", { class: "explore-box", hidden: true });
  const pointerEl = h("div", { class: "libmap-pointer", hidden: true, "aria-hidden": "true" });
  const wait = h("div", { class: "explore-wait", role: "status", hidden: true });
  const map = h("div", { class: "explore-map", role: "region", "aria-label": "Explore" }, canvas, inset, peek, boxEl, pointerEl, wait);
  const caption = h("p", { class: "explore-caption" });
  const legend = h("p", { class: "explore-legend", hidden: true });
  const note = h("p", { class: "explore-note", role: "status", hidden: true });
  const arrange = h("div", { class: "explore-arrange" });
  let marking = false, selecting = false;
  const markBtn = h("button", { type: "button", class: "btn ghost small", "aria-pressed": "false",
    title: "Mark picked and rejected photos", onclick: () => { marking = !marking; markBtn.setAttribute("aria-pressed", String(marking)); paintLegend(); schedule(); } },
  "Mark picks");
  const selectBtn = h("button", { type: "button", class: "btn small", "aria-pressed": "false",
    title: "Drag across photos to select them (with a mouse, ⇧ and drag does it too)", onclick: () => {
      selecting = !selecting; selectBtn.setAttribute("aria-pressed", String(selecting)); map.classList.toggle("selecting", selecting); } },
  "Select");
  const tool = (label, text, title, f) => h("button", { type: "button", class: "btn small", "aria-label": label, title, onclick: f }, text);
  map.append(h("div", { class: "explore-tools", role: "toolbar", "aria-label": "Map", "aria-orientation": "vertical" },
    tool("Zoom in", "+", "Zoom in (+)", () => zoomBy(2)), tool("Zoom out", "−", "Zoom out (−)", () => zoomBy(1 / 2)),
    tool("Show everything", "Fit", "Show everything (Home)", () => fly(fitView())), selectBtn));
  const el = h("div", { class: "explore" },
    h("div", { class: "explore-bar" }, arrange, markBtn),
    caption, legend, note, map);
  const ctx = canvas.getContext("2d");
  const ictx = inset.getContext("2d");
  let color = cssColors(el);
  let dpr = window.devicePixelRatio || 1;
  let W = 0, H = 0;

  // ------------------------------------------------------------ the selection, and the bar over it
  const sel = new Set();
  const selListeners = new Set();
  let hover = -1;                        // scene index under the mouse
  const grid = {                         // what the selection bar needs of a grid
    sel, matchAll: false, done: true, focus: -1, stackN: new Map(),
    get ids() { return inMapOrder(shownIndices()).map((i) => S.ids[i]); },
    get total() { return shownCount(); },
    targetIds: () => (sel.size ? [...sel] : target() >= 0 ? [S.ids[target()]] : []),
    onSelection: (fn) => selListeners.add(fn),
    clearSelection: () => { sel.clear(); paintSel(); },
    clearSel: () => { sel.clear(); paintSel(); },
    selectAll: () => { for (const i of shownIndices()) sel.add(S.ids[i]); paintSel(); },
  };
  const bar = selectionBar(grid, { changed: () => changed?.() });
  bar.insertBefore(h("button", { type: "button", class: "btn ghost small", title: "List these photos in the Library's grid",
    onclick: showInLibrary }, "Show in Library"), bar.lastElementChild);
  el.append(bar);
  function paintSel() { for (const fn of selListeners) fn(); schedule(); }

  function showInLibrary() {
    const ids = [...sel];
    if (ids.length > SHOW_MAX) {
      toast(`That's more than ${n(SHOW_MAX)} photos to list by hand. Add them to an album instead (B), and open the album.`);
      return;
    }
    location.hash = `#/p/${encodeURIComponent(project)}/library?ids=${ids.join(",")}`;
  }

  // ------------------------------------------------------------ the photos on the surface
  // A photo is an index into these arrays: where it's coming from and going to (in cells), its
  // colour and marks, and whether the filters show it. At rest, from and to are the same.
  let S = scene(0);
  let anim = null;                       // {start, dur} while photos glide
  let cell = new Int32Array(1).fill(-1); // cols x rows -> photo at rest there
  let colours = document.createElement("canvas");     // each photo's colour (paintColours)
  let mosaic = document.createElement("canvas");      // every shown photo, k pixels a cell
  let mosaicK = 1;

  function scene(N) {
    return { n: N, ids: new Int32Array(N), at: new Map(), x0: new Float32Array(N), y0: new Float32Array(N), x1: new Float32Array(N),
      y1: new Float32Array(N), a0: new Uint8Array(N), a1: new Uint8Array(N), delay: new Float32Array(N), col: new Uint32Array(N),
      marks: new Uint8Array(N), pick: new Int8Array(N), region: new Int32Array(N).fill(-1), shown: new Uint8Array(N),
      cols: 1, rows: 1, L: null };
  }
  const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2);
  function progressOf(i, now) {
    if (!anim) return 1;
    const t = (now - anim.start - S.delay[i]) / GLIDE;
    return t <= 0 ? 0 : t >= 1 ? 1 : ease(t);
  }
  const posX = (i, p) => S.x0[i] + (S.x1[i] - S.x0[i]) * p;
  const posY = (i, p) => S.y0[i] + (S.y1[i] - S.y0[i]) * p;

  /** Put a layout on the surface; with glide, each photo moves there from where it is now. */
  function apply(L, glide) {
    const now = performance.now();
    const old = S;
    const m = L.ids.length;
    const inL = new Set(L.ids);
    const leaving = [];
    if (glide) for (let i = 0; i < old.n; i++) if (old.a1[i] && !inL.has(old.ids[i])) leaving.push(i);
    const T = scene(m + leaving.length);
    for (let j = 0; j < m; j++) {
      const id = L.ids[j];
      T.ids[j] = id;
      T.at.set(id, j);
      T.x1[j] = L.cells[2 * j];
      T.y1[j] = L.cells[2 * j + 1];
      const o = old.at.get(id);
      if (glide && o !== undefined && old.a1[o]) {
        const p = progressOfIn(old, o, now);
        T.x0[j] = old.x0[o] + (old.x1[o] - old.x0[o]) * p;
        T.y0[j] = old.y0[o] + (old.y1[o] - old.y0[o]) * p;
        T.a0[j] = 1;
      } else {
        T.x0[j] = T.x1[j]; T.y0[j] = T.y1[j]; T.a0[j] = glide ? 0 : 1;
      }
      T.a1[j] = 1;
      T.col[j] = L.main[j];
      T.marks[j] = L.marks[j];
      const r = row(id);
      T.pick[j] = r ? r.pick || 0 : L.pick[j];
      if (L.region) T.region[j] = L.region[j];
      T.shown[j] = !shownSet || shownSet.has(id) ? 1 : 0;
    }
    leaving.forEach((o, k) => {
      const j = m + k;
      const p = progressOfIn(old, o, now);
      T.ids[j] = old.ids[o];
      T.x0[j] = T.x1[j] = old.x0[o] + (old.x1[o] - old.x0[o]) * p;
      T.y0[j] = T.y1[j] = old.y0[o] + (old.y1[o] - old.y0[o]) * p;
      T.a0[j] = 1; T.a1[j] = 0; T.col[j] = old.col[o]; T.shown[j] = old.shown[o];
    });
    T.cols = L.cols; T.rows = L.rows; T.L = L;
    if (glide) {
      // A sweep: photos set off one after another along their new places, so the eye can follow.
      const span = Math.max(1, L.cols + L.rows);
      for (let j = 0; j < T.n; j++) T.delay[j] = STAGGER * ((T.x1[j] + T.y1[j]) / span);
    }
    S = T;
    sheetImgs = [];
    dropTiles();
    fitted = null;
    regrow();
    hover = -1;
    peek.hidden = true;
    peekFor = -1;
    cell = new Int32Array(L.cols * L.rows).fill(-1);
    for (let j = 0; j < m; j++) cell[T.y1[j] * L.cols + T.x1[j]] = j;
    paintColours();
    paintMosaic();
    anim = glide ? { start: now, dur: GLIDE + STAGGER } : null;
    for (const id of [...sel]) if (!T.at.has(id)) sel.delete(id);
    paintSel();
  }
  function progressOfIn(sc, i, now) {
    if (!anim || sc !== S) return 1;
    return progressOf(i, now);
  }

  /** Each photo's colour as a 3 x 3 block (drawn from its middle pixel, so smoothing never blends in
   * a neighbour's), 256 photos a row. */
  function paintColours() {
    const w = 256 * 3, hh = Math.max(1, Math.ceil(S.n / 256)) * 3;
    colours.width = w; colours.height = hh;
    const c = colours.getContext("2d");
    const im = c.createImageData(w, hh);
    const d = new Uint32Array(im.data.buffer);
    for (let i = 0; i < S.n; i++) {
      const v = S.col[i];
      const px = 0xff000000 | ((v & 0xff) << 16) | (v & 0xff00) | ((v >> 16) & 0xff);   // RGBA, little-endian
      const o = (i >> 8) * 3 * w + (i & 255) * 3;
      for (let k = 0; k < 3; k++) d[o + k * w] = d[o + k * w + 1] = d[o + k * w + 2] = px;
    }
    c.putImageData(im, 0, 0);
  }

  /** The whole layout as one small image, k pixels a cell: what's drawn when photos are tiny. */
  function paintMosaic() {
    const cols = S.cols, rows = S.rows;
    mosaicK = [8, 4, 2, 1].find((k) => cols * rows * k * k <= 4e6) || 1;
    const k = mosaicK;
    mosaic.width = cols * k; mosaic.height = rows * k;
    const c = mosaic.getContext("2d");
    const im = c.createImageData(cols * k, rows * k);
    const d = new Uint32Array(im.data.buffer);
    const fill = k >= 8 ? k - 1 : k;
    for (let i = 0; i < S.n; i++) {
      if (!S.a1[i] || !S.shown[i]) continue;
      const v = S.col[i];
      const px = 0xff000000 | ((v & 0xff) << 16) | (v & 0xff00) | ((v >> 16) & 0xff);
      const x = S.x1[i] * k, y = S.y1[i] * k;
      for (let yy = 0; yy < fill; yy++) {
        const o = (y + yy) * cols * k + x;
        for (let xx = 0; xx < fill; xx++) d[o + xx] = px;
      }
    }
    c.putImageData(im, 0, 0);
  }

  // ------------------------------------------------------------ the view: css px = cell * s + t
  const view = { s: 4, tx: 0, ty: 0 };
  let flight = null;
  function bounds() {
    // What "show everything" shows: the photos the filters show (or all, if none), and the guides.
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    const any = shownCount() > 0;
    for (let i = 0; i < S.n; i++) {
      if (!S.a1[i] || (any && !S.shown[i])) continue;
      const x = S.x1[i], y = S.y1[i];
      if (x < x0) x0 = x; if (y < y0) y0 = y; if (x + 1 > x1) x1 = x + 1; if (y + 1 > y1) y1 = y + 1;
    }
    if (!isFinite(x0)) return { x0: 0, y0: 0, x1: S.cols, y1: S.rows };
    const wheel = S.L?.guides?.wheel;
    if (wheel && (!any || shownCount() === S.n)) {
      const r = wheel.r + 1;
      return { x0: wheel.cx - r, y0: wheel.cy - r, x1: wheel.cx + r, y1: wheel.cy + r };
    }
    if (S.L?.guides?.lines && (!any || shownCount() === S.n)) return { x0: 0, y0: 0, x1: S.cols, y1: S.rows };
    return { x0, y0, x1, y1 };
  }
  let fitted = null;                     // fitView(), until the layout, the filters or the size change
  function fitView() {
    if (fitted) return fitted;
    const b = bounds();
    const [pt, pr, pb, pl] = pad(by, W);
    const bw = Math.max(1, b.x1 - b.x0), bh = Math.max(1, b.y1 - b.y0);
    const s = Math.min(MAX_S, Math.max(0.05, Math.min((W - pl - pr) / bw, (H - pt - pb) / bh)));
    fitted = { s, tx: pl + (W - pl - pr - bw * s) / 2 - b.x0 * s, ty: pt + (H - pt - pb - bh * s) / 2 - b.y0 * s };
    return fitted;
  }
  const minS = () => Math.min(fitView().s * 0.6, 1);
  function setView(v) {
    view.s = Math.min(MAX_S, Math.max(minS(), v.s));
    view.tx = v.tx; view.ty = v.ty;
    schedule();
    settle();
  }
  function zoomAt(f, px, py) {
    const s = Math.min(MAX_S, Math.max(minS(), view.s * f));
    const k = s / view.s;
    setView({ s, tx: px - (px - view.tx) * k, ty: py - (py - view.ty) * k });
  }
  function zoomBy(f, at) {
    const [px, py] = at || (pointer.on ? [pointer.x * view.s + view.tx, pointer.y * view.s + view.ty] : [W / 2, H / 2]);
    const s = Math.min(MAX_S, Math.max(minS(), view.s * f)), k = s / view.s;
    fly({ s, tx: px - (px - view.tx) * k, ty: py - (py - view.ty) * k });
  }
  /** Move the view smoothly to v (zooming on a log scale, so it feels even). */
  function fly(v, dur = 420) {
    if (reduceMotion()) { setView(v); return; }
    const a = { ...view };
    // the world point at the centre, before and after
    const ca = [(W / 2 - a.tx) / a.s, (H / 2 - a.ty) / a.s], cb = [(W / 2 - v.tx) / v.s, (H / 2 - v.ty) / v.s];
    flight = { start: performance.now(), dur, from: { s: a.s, c: ca }, to: { s: v.s, c: cb } };
    schedule();
  }
  function flyStep(now) {
    if (!flight) return false;
    const t = Math.min(1, (now - flight.start) / flight.dur), e = ease(t);
    const s = Math.exp(Math.log(flight.from.s) + (Math.log(flight.to.s) - Math.log(flight.from.s)) * e);
    const cx = flight.from.c[0] + (flight.to.c[0] - flight.from.c[0]) * e, cy = flight.from.c[1] + (flight.to.c[1] - flight.from.c[1]) * e;
    view.s = s; view.tx = W / 2 - cx * s; view.ty = H / 2 - cy * s;
    if (t >= 1) { flight = null; settle(); }
    return true;
  }

  // ------------------------------------------------------------ drawing
  let raf = 0;
  function schedule() { if (!raf && alive) raf = requestAnimationFrame(frame); }
  function frame(now) {
    raf = 0;
    const flying = flyStep(now);
    if (anim && now - anim.start > anim.dur) {
      anim = null;
      if (S.L && S.n !== S.L.ids.length) apply(S.L, false);        // the photos that left are gone
    }
    draw(now);
    if (flying || anim) schedule();
  }

  const sheets = new Map();              // atlas sheet -> {img, ok, v}
  let sheetImgs = [];                    // sheet -> its image, once loaded, for the layout on screen
  const thumbs = new Map();              // id -> {img, ok, cx, cy, side}: its own thumbnail, cropped square
  function loadSheets() {
    for (const k in S.L.sheets) {
      const v = S.L.sheets[k];
      let sh = sheets.get(k);
      if (!sh || sh.v !== v) {
        const one = sh = { img: new Image(), ok: false, v };
        one.img.decoding = "async";
        one.img.onload = () => { one.ok = true; sheetLoads++; if (S.L?.sheets?.[k] === v) sheetImgs[+k] = one.img; schedule(); };
        one.img.src = `/img/${encodeURIComponent(project)}/atlas/${k}?v=${v}`;
        sheets.set(k, sh);
      }
      sheetImgs[+k] = sh.ok ? sh.img : null;
    }
  }
  // Tiles. Thousands of small thumbnails are too many to draw again for every frame of a pan or a
  // zoom, so at those sizes they're drawn once into tiles of TILE x TILE cells, at 16, 32 or 64
  // pixels a cell, and the tiles are reused; a tile not drawn yet shows the colour squares.
  const tiles = new Map();               // "R:x:y" -> {c: canvas, whole: drawn with every atlas sheet in, loads}
  let tilePixels = 0, sheetLoads = 0;
  function dropTiles() { tiles.clear(); tilePixels = 0; }
  const tileLevel = (cs) => (cs < 20 ? 16 : cs < 40 ? 32 : cs < THUMB_AT ? 64 : 0);
  function buildTile(R, a, b, old) {
    const c = old?.c || document.createElement("canvas");
    if (!old) { c.width = c.height = TILE * R; tilePixels += c.width * c.height; }
    const g = c.getContext("2d");
    g.clearRect(0, 0, c.width, c.height);
    g.imageSmoothingEnabled = true;
    g.imageSmoothingQuality = "low";
    const size = R * (1 - GAP), off = (R * GAP) / 2;
    let whole = true;
    for (let y = b * TILE; y < Math.min(S.rows, (b + 1) * TILE); y++) {
      for (let x = a * TILE; x < Math.min(S.cols, (a + 1) * TILE); x++) {
        const i = cell[y * S.cols + x];
        if (i < 0 || S.shown[i] !== 1) continue;
        const id = S.ids[i], sh = sheetImgs[(id / SHEET) | 0];
        const dx = (x - a * TILE) * R + off, dy = (y - b * TILE) * R + off;
        if (sh) {
          const k = id % SHEET;
          g.drawImage(sh, (k % SHEET_SIDE) * CELL, ((k / SHEET_SIDE) | 0) * CELL, CELL, CELL, dx, dy, size, size);
        } else {
          whole = false;
          g.drawImage(colours, (i & 255) * 3 + 1, (i >> 8) * 3 + 1, 1, 1, dx, dy, size, size);
        }
      }
    }
    const t = { c, whole, loads: sheetLoads };
    tiles.delete(`${R}:${a}:${b}`);
    tiles.set(`${R}:${a}:${b}`, t);
    // Keep about three screens of them, the least recently used going first.
    const budget = Math.max(4e6, 3 * W * H * dpr * dpr);
    for (const [k, old] of tiles) {
      if (tilePixels <= budget) break;
      if (old === t) continue;
      tiles.delete(k);
      tilePixels -= old.c.width * old.c.height;
    }
    return t;
  }
  function drawTiles(R) {
    const { s, tx, ty } = view;
    const tw = TILE * s, start = performance.now();
    const [cx0, cy0, cx1, cy1] = visibleCells();
    let waiting = false;
    for (let b = Math.floor(cy0 / TILE); b <= Math.floor((cy1 - 1) / TILE); b++) {
      for (let a = Math.floor(cx0 / TILE); a <= Math.floor((cx1 - 1) / TILE); a++) {
        const key = `${R}:${a}:${b}`;
        let t = tiles.get(key);
        if ((!t || (!t.whole && t.loads !== sheetLoads)) && performance.now() - start < 6) t = buildTile(R, a, b, t);
        else if (t) { tiles.delete(key); tiles.set(key, t); }
        const dx = a * tw + tx, dy = b * tw + ty;
        if (t) ctx.drawImage(t.c, dx, dy, tw, tw);
        else {
          const k = mosaicK * TILE;
          ctx.drawImage(mosaic, a * k, b * k, k, k, dx, dy, tw, tw);
        }
        if (!t || (!t.whole && t.loads !== sheetLoads)) waiting = true;
      }
    }
    if (waiting) schedule();
  }

  /** One photo: its own thumbnail, its atlas cell, or a square of its colour, whichever is there. */
  function drawOne(i, sx, sy, size, big, small) {
    const id = S.ids[i];
    if (big) {
      const th = thumbs.get(id);
      if (th !== undefined && th.ok) { ctx.drawImage(th.img, th.cx, th.cy, th.side, th.side, sx, sy, size, size); return; }
    }
    if (small) {
      const sh = sheetImgs[(id / SHEET) | 0];
      if (sh) {
        const k = id % SHEET;
        ctx.drawImage(sh, (k % SHEET_SIDE) * CELL, ((k / SHEET_SIDE) | 0) * CELL, CELL, CELL, sx, sy, size, size);
        return;
      }
    }
    ctx.drawImage(colours, (i & 255) * 3 + 1, (i >> 8) * 3 + 1, 1, 1, sx, sy, size, size);
  }

  function draw(now) {
    if (!W || !H) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.globalAlpha = 1;
    ctx.fillStyle = color("--surface");
    ctx.fillRect(0, 0, W, H);
    if (!S.L) return;
    underlay();
    const { s, tx, ty } = view;
    const cs = s * dpr;
    const { size, off } = geom();
    const level = tileLevel(cs);
    if (!anim && cs < ATLAS_AT && growNow() === 1) {
      ctx.imageSmoothingEnabled = cs < mosaicK;          // shrinking it: average; growing it: crisp squares
      ctx.drawImage(mosaic, tx, ty, S.cols * s, S.rows * s);
    } else if (!anim && growNow() === 1 && level) {
      loadSheets();
      ctx.imageSmoothingEnabled = true;
      ctx.imageSmoothingQuality = "low";
      drawTiles(level);
    } else {
      const big = size * dpr >= THUMB_AT, small = size * dpr >= ATLAS_AT * 0.9;
      if (small) loadSheets();
      ctx.imageSmoothingEnabled = true;
      ctx.imageSmoothingQuality = big ? "medium" : "low";
      if (anim) {
        for (let i = 0; i < S.n; i++) {
          if (!S.shown[i]) continue;
          const p = progressOf(i, now);
          const a = S.a0[i] + (S.a1[i] - S.a0[i]) * p;
          if (a <= 0.02) continue;
          const sx = posX(i, p) * s + tx + off, sy = posY(i, p) * s + ty + off;
          if (sx > W || sy > H || sx + size < 0 || sy + size < 0) continue;
          if (a < 1) ctx.globalAlpha = a;
          drawOne(i, sx, sy, size, big, small);
          if (a < 1) ctx.globalAlpha = 1;
        }
      } else {
        const [cx0, cy0, cx1, cy1] = visibleCells();
        for (let y = cy0; y < cy1; y++) {
          const o = y * S.cols, sy = y * s + ty + off;
          for (let x = cx0; x < cx1; x++) {
            const i = cell[o + x];
            if (i >= 0 && S.shown[i] === 1) drawOne(i, x * s + tx + off, sy, size, big, small);
          }
        }
      }
      if (big && !anim) wantThumbs();
    }
    if (!anim) marks();
    labels();
    paintInset();
  }

  /** The cells on screen: [x0, y0, x1, y1), clamped to the layout (and a margin for photos drawn bigger). */
  function visibleCells() {
    const { s, tx, ty } = view;
    const m = Math.ceil(growNow());
    return [Math.max(0, Math.floor(-tx / s) - m), Math.max(0, Math.floor(-ty / s) - m),
      Math.min(S.cols, Math.ceil((W - tx) / s) + m), Math.min(S.rows, Math.ceil((H - ty) / s) + m)];
  }
  /** While a filter leaves only a few photos, they're drawn bigger than their cells (centred on
   * them), less so as you zoom in. */
  let grow = 1;
  function growNow() {
    if (grow <= 1) return 1;
    return 1 + (grow - 1) * Math.min(1, Math.max(0, (60 - view.s) / 50));
  }
  /** A photo's size on screen and how far in from its cell's corner it starts. */
  function geom() {
    const s = view.s, cs = s * dpr;
    const size = Math.max(cs >= ATLAS_AT ? s * (1 - GAP) : s, 0.6) * growNow();
    return { size, off: (s - size) / 2 };
  }

  /** Picks and rejects (when marked), clipped highlights and shadows (light), the selection, the photo under the mouse. */
  function marks() {
    const { s, tx, ty } = view;
    const { size, off } = geom();
    const [cx0, cy0, cx1, cy1] = visibleCells();
    const inView = (i) => S.x1[i] >= cx0 && S.x1[i] < cx1 && S.y1[i] >= cy0 && S.y1[i] < cy1;
    const at = (i) => [S.x1[i] * s + tx + off, S.y1[i] * s + ty + off];
    if (marking || by === "light") {
      const pickC = color("--pick"), rejC = color("--reject");
      const tri = size >= 10;
      for (let y = cy0; y < cy1; y++) {
        for (let x = cx0; x < cx1; x++) {
          const i = cell[y * S.cols + x];
          if (i < 0 || !S.shown[i]) continue;
          const [sx, sy] = at(i);
          if (marking && S.pick[i]) {
            ctx.fillStyle = S.pick[i] > 0 ? pickC : rejC;
            if (tri) {
              const k = Math.max(5, size * 0.3);
              ctx.beginPath(); ctx.moveTo(sx + size - k, sy); ctx.lineTo(sx + size, sy); ctx.lineTo(sx + size, sy + k); ctx.fill();
            } else ctx.fillRect(sx, sy, size, size);
          }
          if (by === "light" && S.marks[i] && size >= 8) {
            const k = Math.max(4, size * 0.24);
            if (S.marks[i] & MARK_HIGHLIGHTS) corner(sx, sy, k, 1, "#ffffff", "#000000");
            if (S.marks[i] & MARK_SHADOWS) corner(sx, sy + size, k, -1, "#000000", "#ffffff");
          }
        }
      }
    }
    if (sel.size) {
      ctx.strokeStyle = color("--accent");
      ctx.fillStyle = color("--accent");
      ctx.lineWidth = Math.max(1.5, Math.min(3, size * 0.08));
      for (const id of sel) {
        const i = S.at.get(id);
        if (i === undefined || !S.shown[i] || !inView(i)) continue;
        const [sx, sy] = at(i);
        if (size >= 6) ctx.strokeRect(sx + ctx.lineWidth / 2, sy + ctx.lineWidth / 2, size - ctx.lineWidth, size - ctx.lineWidth);
        else { ctx.globalAlpha = 0.6; ctx.fillRect(sx - 0.5, sy - 0.5, size + 1, size + 1); ctx.globalAlpha = 1; }
      }
    }
    if (hover >= 0 && hover < S.n && S.shown[hover]) {
      const [sx, sy] = at(hover);
      ctx.strokeStyle = color("--ink");
      ctx.lineWidth = 2;
      const k = Math.max(size, 6);
      ctx.strokeRect(sx - (k - size) / 2 - 1, sy - (k - size) / 2 - 1, k + 2, k + 2);
    }
  }
  /** A small triangle in a photo's left corner (dir 1: top, -1: bottom), light on dark or dark on light. */
  function corner(x, y, k, dir, fill, edge) {
    ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x + k, y); ctx.lineTo(x, y + dir * k); ctx.closePath();
    ctx.fillStyle = fill; ctx.fill();
    ctx.strokeStyle = edge; ctx.lineWidth = 1; ctx.stroke();
  }

  // ------------------------------------------------------------ guides: what explains the arrangement
  const fonts = {};
  function font(size, weight = 400) {
    const key = `${size}-${weight}`;
    if (!fonts[key]) {
      const st = getComputedStyle(el);
      fonts[key] = `${weight} ${st.getPropertyValue(size).trim() || "12px"} ${st.getPropertyValue("--font-ui").trim() || "sans-serif"}`;
    }
    return fonts[key];
  }
  const X = (x) => x * view.s + view.tx;
  const Y = (y) => y * view.s + view.ty;

  function underlay() {
    const g = S.L.guides || {};
    if (g.lines) {                            // time: each line of the timeline, its hours and light
      const R = g.line_rows;
      for (const [top, w] of g.lines) {
        if (Y(top + R) < 0 || Y(top) > H) continue;
        ctx.fillStyle = color("--sunken");
        ctx.globalAlpha = 0.55;
        ctx.fillRect(X(0), Y(top), w * view.s, R * view.s);
        ctx.fillStyle = color("--star");
        ctx.globalAlpha = 0.16;
        for (const [a, b] of g.bands) ctx.fillRect(X(0), Y(top + a), w * view.s, (b - a) * view.s);
        ctx.globalAlpha = 1;
        ctx.strokeStyle = color("--line");
        ctx.lineWidth = 1;
        for (const [r] of g.hours) {
          const y = Math.round(Y(top + r)) + 0.5;
          ctx.beginPath(); ctx.moveTo(Math.max(0, X(0)), y); ctx.lineTo(Math.min(W, X(w)), y); ctx.stroke();
        }
      }
    }
    if (g.wheel?.stops) {                     // colour: the rim, in the hue at each angle
      const wh = g.wheel;
      const cx = X(wh.cx), cy = Y(wh.cy), r = wh.r * view.s + 3, thick = Math.max(5, Math.min(12, view.s * 1.2));
      const stops = wh.stops;
      for (let k = 0; k < stops.length; k++) {
        const a0 = stops[k][0], a1 = k + 1 < stops.length ? stops[k + 1][0] : 360;
        ctx.strokeStyle = stops[k][1];
        ctx.lineWidth = thick;
        ctx.beginPath();
        ctx.arc(cx, cy, r + thick / 2, (a0 - 90.4) * Math.PI / 180, (a1 - 89.6) * Math.PI / 180);
        ctx.stroke();
      }
    }
  }

  /** Text in a pill, or plain; returns its box. */
  function textBox(text, x, y, { size = "--fs-sm", weight = 400, pill = true, align = "center", base = "middle", ink = "--ink", measureOnly = false } = {}) {
    ctx.font = font(size, weight);
    const w = ctx.measureText(text).width, hh = parseFloat(ctx.font.split(" ")[1]) || 12;
    const padX = pill ? 7 : 0, padY = pill ? 4 : 0;
    const bx = align === "center" ? x - w / 2 - padX : align === "right" ? x - w - padX : x - padX;
    const by_ = base === "middle" ? y - hh / 2 - padY : base === "bottom" ? y - hh - padY : y - padY;
    const box = [bx, by_, w + 2 * padX, hh + 2 * padY];
    if (measureOnly) return box;
    if (pill) {
      ctx.globalAlpha = 0.92;
      ctx.fillStyle = color("--raised");
      roundRect(...box, 999);
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.strokeStyle = color("--line-strong");
      ctx.lineWidth = 1;
      ctx.stroke();
    } else {
      ctx.lineJoin = "round";
      ctx.strokeStyle = color("--surface");
      ctx.lineWidth = 3;
      ctx.textAlign = "left"; ctx.textBaseline = "top";
      ctx.strokeText(text, box[0], box[1] + 1);
    }
    ctx.fillStyle = color(ink);
    ctx.textAlign = "left"; ctx.textBaseline = "top";
    ctx.fillText(text, box[0] + padX, box[1] + padY + 1);
    return box;
  }
  function roundRect(x, y, w, hh, r) {
    r = Math.min(r, hh / 2, w / 2);
    ctx.beginPath();
    ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + hh, r); ctx.arcTo(x + w, y + hh, x, y + hh, r);
    ctx.arcTo(x, y + hh, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
  }
  const overlaps = (a, list) => list.some((b) => a[0] < b[0] + b[2] + 4 && b[0] < a[0] + a[2] + 4 && a[1] < b[1] + b[3] + 3 && b[1] < a[1] + a[3] + 3);
  /** Draw labels by priority, leaving out any that would cover one already drawn (or be off screen). */
  function placeAll(items, taken = [], { clip = true } = {}) {
    for (const it of items) {
      const box = textBox(it.text, it.x, it.y, { ...it.opts, measureOnly: true });
      if ((clip && (box[0] + box[2] < 0 || box[0] > W || box[1] + box[3] < 0 || box[1] > H)) || overlaps(box, taken)) continue;
      textBox(it.text, it.x, it.y, it.opts);
      taken.push(box);
    }
    return taken;
  }

  function labels() {
    const g = S.L.guides || {};
    const s = view.s;
    ctx.globalAlpha = anim ? 0.35 : 1;
    if (g.regions) {
      // Big regions while zoomed out; their parts once there's room.
      const coarse = g.regions.filter((r) => r.level === 0), fine = g.regions.filter((r) => r.level === 1);
      const items = [];
      const showFine = s >= 4.5 || !coarse.length, showCoarse = s < 9 || !fine.length;
      if (showCoarse) for (const r of [...coarse].sort((a, b) => b.n - a.n)) items.push({ text: r.name, x: X(r.x), y: Y(r.y), opts: { size: "--fs-ui", weight: 600 } });
      if (showFine) for (const r of [...fine].sort((a, b) => b.n - a.n)) items.push({ text: r.name, x: X(r.x), y: Y(r.y), opts: { size: "--fs-sm" } });
      const said = [];
      placeAll(items.filter((it) => {
        if (said.some((o) => o.text === it.text && Math.hypot(o.x - it.x, o.y - it.y) < 260)) return false;
        said.push(it);
        return true;
      }));
    }
    if (g.wheel) wheelLabels(g.wheel);
    if (g.x) axes(g);
    if (g.lines) timeLabels(g);
    if (g.islands) {
      const items = g.islands.slice().sort((a, b) => b[5] - a[5]).map(([x, y, w, hh, name, k]) =>
        ({ text: `${name} · ${n(k)}`, x: X(x), y: Y(y + 1.6), opts: { size: s >= 6 ? "--fs-ui" : "--fs-sm", weight: 600, pill: false, align: "left", base: "bottom" } }));
      const taken = placeAll(items);
      if (s >= 7) placeAll(g.lenses.slice().sort((a, b) => b[3] - a[3]).map(([x, y, name, k]) => ({ text: `${name} · ${n(k)}`, x: X(x) + 2, y: Y(y) + 2, opts: { size: "--fs-xs", align: "left", base: "top" } })), taken);
    }
    ctx.globalAlpha = 1;
  }

  function wheelLabels(wh) {
    const cx = X(wh.cx), cy = Y(wh.cy), r = wh.r * view.s + Math.max(5, Math.min(12, view.s * 1.2)) + 16;
    const items = (wh.names || []).map(([a, name]) => {
      const t = (a - 90) * Math.PI / 180;
      return { text: name, x: cx + Math.cos(t) * (r + 10), y: cy + Math.sin(t) * (r + 2), opts: { size: "--fs-sm", weight: 600, pill: false } };
    });
    if (wh.grey && view.s < 14) items.push({ text: "Black and white", x: cx, y: cy, opts: { size: "--fs-sm", weight: 600 } });
    placeAll(items);
  }

  function axes(g) {
    const [, , pb, pl] = pad(by, W);
    // Zoomed in, photos run under the axes: a band behind them keeps them readable.
    ctx.fillStyle = color("--surface");
    ctx.globalAlpha = 0.9;
    if (X(0) < pl - 4) ctx.fillRect(0, 0, pl - 4, H);
    if (Y(S.rows) > H - pb + 4) ctx.fillRect(0, H - pb + 4, W, pb);
    ctx.globalAlpha = anim ? 0.35 : 1;
    const x0 = Math.max(pl, X(0)), x1 = Math.min(W - 8, X(S.cols));
    const yb = Math.min(H - pb + 10, Y(S.rows) + 8);
    const y0 = Math.max(8, Y(0)), y1 = Math.min(H - pb + 4, Y(S.rows));
    const xl = Math.max(pl - 10, X(0) - 8);
    ctx.strokeStyle = color("--line-strong");
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x0, yb + 0.5); ctx.lineTo(x1, yb + 0.5); ctx.moveTo(xl + 0.5, y0); ctx.lineTo(xl + 0.5, y1); ctx.stroke();
    const taken = [];
    // x: the ends say which way is which; the ticks where actual values fall
    const ends = [{ text: `← ${g.x.low}`, x: x0, y: yb + 6, opts: { pill: false, align: "left", base: "top", ink: "--muted" } },
      { text: `${g.x.high} →`, x: x1, y: yb + 6, opts: { pill: false, align: "right", base: "top", ink: "--muted" } },
      { text: g.x.label, x: (x0 + x1) / 2, y: yb + 22, opts: { pill: false, base: "top", weight: 600 } }];
    placeAll(ends, taken);
    const ticks = g.x.ticks.map(([p, t]) => ({ text: t, x: X(p), y: yb + 6, opts: { pill: false, base: "top", size: "--fs-xs", ink: "--muted" } }));
    for (const [p] of g.x.ticks) {
      const x = Math.round(X(p)) + 0.5;
      if (x < x0 || x > x1) continue;
      ctx.beginPath(); ctx.moveTo(x, yb - 3); ctx.lineTo(x, yb + 3); ctx.stroke();
    }
    placeAll(ticks, taken);
    // y: written up the side
    ctx.save();
    ctx.translate(xl - 8, 0);
    ctx.rotate(-Math.PI / 2);
    const yItems = [{ text: `${g.y.high} →`, x: -y0, y: 0, opts: { pill: false, align: "right", base: "bottom", ink: "--muted" } },
      { text: `← ${g.y.low}`, x: -y1, y: 0, opts: { pill: false, align: "left", base: "bottom", ink: "--muted" } },
      { text: g.y.label, x: -(y0 + y1) / 2, y: -18, opts: { pill: false, base: "bottom", weight: 600 } },
      ...g.y.ticks.map(([p, t]) => ({ text: t, x: -Y(p), y: 0, opts: { pill: false, base: "bottom", size: "--fs-xs", ink: "--muted" } }))];
    placeAll(yItems.filter((it) => it.x <= -y0 + 1 && it.x >= -y1 - 1), [], { clip: false });
    ctx.restore();
  }

  function timeLabels(g) {
    const s = view.s;
    const [, , , pl] = pad("time", W);
    if (X(0) < pl - 4) {
      ctx.fillStyle = color("--surface");
      ctx.globalAlpha = 0.9;
      ctx.fillRect(0, 0, pl - 4, H);
      ctx.globalAlpha = anim ? 0.35 : 1;
    }
    const taken = [];
    const items = [];
    for (const [x, top, year] of g.years) items.push({ text: year, x: X(x), y: Y(top) - 3, opts: { pill: false, align: "left", base: "bottom", weight: 600 } });
    placeAll(items, taken);
    if (s >= 1.6) placeAll(g.months.map(([x, top, m]) => ({ text: m, x: X(x), y: Y(top) - 3, opts: { pill: false, align: "left", base: "bottom", size: "--fs-xs", ink: "--muted" } })), taken);
    // The hours down the left of each line, kept in view.
    const R = g.line_rows;
    for (const [top] of g.lines) {
      if (Y(top + R) < 0 || Y(top) > H) continue;
      const x = Math.max(pl - 6, X(0) - 6);
      for (const [r, t] of g.hours) {
        if (r >= R || (R * s < 60 && r !== 0 && r !== R / 2)) continue;
        textBox(t, x, Y(top + r), { pill: false, align: "right", size: "--fs-xs", ink: "--muted" });
      }
      if (R * s >= 110) for (const [a, b, t] of g.bands) textBox(t, Math.max(X(0), pl) + 4, Y(top + (a + b) / 2), { pill: false, align: "left", size: "--fs-xs", ink: "--muted" });
    }
    if (g.undated) {
      const [x, top, , k] = g.undated;
      textBox(`Undated · ${n(k)}`, X(x), Y(top) - 3, { pill: false, align: "left", base: "bottom", weight: 600 });
    }
  }

  // ------------------------------------------------------------ the overview in the corner
  function paintInset() {
    const b = { x0: 0, y0: 0, x1: S.cols, y1: S.rows };
    const shownAll = (W / view.s) * (H / view.s) >= S.cols * S.rows * 0.7 || (-view.tx / view.s <= 0 && -view.ty / view.s <= 0 && (W - view.tx) / view.s >= S.cols && (H - view.ty) / view.s >= S.rows);
    inset.hidden = shownAll || W < 360;
    if (inset.hidden) return;
    const iw = W < 600 ? 110 : 150;
    const k = Math.min(iw / (b.x1 - b.x0), iw / (b.y1 - b.y0));
    const w = Math.ceil((b.x1 - b.x0) * k), hh = Math.ceil((b.y1 - b.y0) * k);
    if (inset.width !== w * dpr || inset.height !== hh * dpr) {
      inset.width = w * dpr; inset.height = hh * dpr;
      inset.style.width = `${w}px`; inset.style.height = `${hh}px`;
    }
    ictx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ictx.fillStyle = color("--raised");
    ictx.fillRect(0, 0, w, hh);
    ictx.imageSmoothingEnabled = true;
    ictx.drawImage(mosaic, 0, 0, w, hh);
    ictx.strokeStyle = color("--accent");
    ictx.lineWidth = 2;
    const vx = (-view.tx / view.s) * k, vy = (-view.ty / view.s) * k;
    ictx.strokeRect(vx, vy, (W / view.s) * k, (H / view.s) * k);
    inset.k = k;
  }
  inset.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    e.stopPropagation();
    const go = (ev) => {
      const r = inset.getBoundingClientRect();
      const wx = (ev.clientX - r.left) / inset.k, wy = (ev.clientY - r.top) / inset.k;
      setView({ s: view.s, tx: W / 2 - wx * view.s, ty: H / 2 - wy * view.s });
    };
    go(e);
    inset.setPointerCapture(e.pointerId);
    inset.onpointermove = go;
    inset.onpointerup = () => { inset.onpointermove = null; };
  });

  // ------------------------------------------------------------ big thumbnails for what's on screen
  let wantTimer = 0;
  function wantThumbs() {
    clearTimeout(wantTimer);
    wantTimer = setTimeout(async () => {
      const [cx0, cy0, cx1, cy1] = visibleCells();
      const ids = [];
      for (let y = cy0; y < cy1 && ids.length < 240; y++) {
        for (let x = cx0; x < cx1; x++) {
          const i = cell[y * S.cols + x];
          if (i >= 0 && S.shown[i] && !thumbs.has(S.ids[i])) ids.push(S.ids[i]);
        }
      }
      if (!ids.length) return;
      const need = ids.filter((id) => !row(id));
      try { if (need.length) remember((await api(P(project, "/rows"), { params: { ids: need.join(",") } })).rows); } catch { return; }
      for (const id of ids) {
        const r = row(id);
        if (!r || thumbs.has(id)) continue;
        const th = { img: new Image(), ok: false, cx: 0, cy: 0, side: 1 };
        th.img.decoding = "async";
        th.img.onload = () => {
          const w = th.img.naturalWidth, hh = th.img.naturalHeight;
          th.side = Math.min(w, hh); th.cx = (w - th.side) / 2; th.cy = (hh - th.side) / 2; th.ok = true;
          schedule();
        };
        th.img.src = img.thumb(project, r);
        thumbs.set(id, th);
      }
      if (thumbs.size > 900) {                // keep the nearest ones
        const keep = new Set(ids);
        for (const id of thumbs.keys()) { if (thumbs.size <= 600) break; if (!keep.has(id)) thumbs.delete(id); }
      }
    }, 90);
  }

  // ------------------------------------------------------------ finding photos
  /** The photo at a point on the canvas (css px), or -1. */
  function photoAt(px, py) {
    if (!S.L || anim) return -1;
    const wx = (px - view.tx) / view.s, wy = (py - view.ty) / view.s;
    const gr = growNow();
    if (gr > 1.05) {                         // photos drawn bigger than their cells: the one drawn there
      const i = nearest(wx, wy, Math.ceil(gr / 2));
      return i >= 0 && Math.abs(S.x1[i] + 0.5 - wx) <= gr / 2 && Math.abs(S.y1[i] + 0.5 - wy) <= gr / 2 ? i : -1;
    }
    const x = Math.floor(wx), y = Math.floor(wy);
    if (x < 0 || y < 0 || x >= S.cols || y >= S.rows) return -1;
    const i = cell[y * S.cols + x];
    return i >= 0 && S.shown[i] ? i : -1;
  }
  /** The shown photo nearest a cell, within reach. */
  function nearest(cx, cy, reach = 3) {
    let best = -1, bd = Infinity;
    for (let y = Math.max(0, Math.floor(cy) - reach); y <= Math.min(S.rows - 1, Math.floor(cy) + reach); y++) {
      for (let x = Math.max(0, Math.floor(cx) - reach); x <= Math.min(S.cols - 1, Math.floor(cx) + reach); x++) {
        const i = cell[y * S.cols + x];
        if (i < 0 || !S.shown[i]) continue;
        const d = (x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2;
        if (d < bd) { bd = d; best = i; }
      }
    }
    return best;
  }
  function shownIndices() {
    const out = [];
    for (let i = 0; i < S.n; i++) if (S.a1[i] && S.shown[i]) out.push(i);
    return out;
  }
  function regrow() { grow = Math.max(1, Math.min(3, Math.sqrt(S.n / Math.max(1, shownCount())) * 0.55)); }
  function shownCount() {
    let k = 0;
    for (let i = 0; i < S.n; i++) k += S.a1[i] && S.shown[i] ? 1 : 0;
    return k;
  }
  /** Map order: row by row, left to right, as you'd read it. */
  const inMapOrder = (idx) => idx.sort((a, b) => S.y1[a] - S.y1[b] || S.x1[a] - S.x1[b]);
  /** The photo the keys act on when nothing is selected: the keyboard pointer's, else the mouse's. */
  function target() {
    if (pointer.on) return nearest(pointer.x, pointer.y, 0);
    return hover;
  }

  /** Open a photo in the viewer with the photos around it on the map, in map order. */
  async function openAt(i) {
    if (i < 0) return;
    const [cx0, cy0, cx1, cy1] = visibleCells();
    let idx = [];
    for (let y = cy0; y < cy1; y++) for (let x = cx0; x < cx1; x++) { const k = cell[y * S.cols + x]; if (k >= 0 && S.shown[k]) idx.push(k); }
    if (idx.length > 400 || idx.length < 2) {
      // too many on screen (or too few): the 200 nearest
      const all = shownIndices().map((k) => [k, (S.x1[k] - S.x1[i]) ** 2 + (S.y1[k] - S.y1[i]) ** 2]);
      all.sort((a, b) => a[1] - b[1]);
      idx = all.slice(0, 200).map(([k]) => k);
    }
    const ids = inMapOrder(idx).map((k) => S.ids[k]);
    const need = ids.filter((id) => !row(id));
    try { if (need.length) remember((await api(P(project, "/rows"), { params: { ids: need.join(",") } })).rows); }
    catch (e) { toast(e.message, { kind: "error" }); return; }
    const have = ids.filter((id) => row(id));
    openLoupe({ ids: have, index: Math.max(0, have.indexOf(S.ids[i])), onClose: () => canvas.focus({ preventScroll: true }) });
  }

  // ------------------------------------------------------------ the peek
  let peekFor = -1, peekTimer = 0;
  const factsCache = new Map();
  function showPeek(i, px, py) {
    if (i < 0) { peek.hidden = true; peekFor = -1; return; }
    placePeek(px, py);
    if (i === peekFor) return;
    peekFor = i;
    clearTimeout(peekTimer);
    peekTimer = setTimeout(async () => {
      const id = S.ids[i];
      try {
        if (!row(id)) remember((await api(P(project, "/rows"), { params: { ids: String(id) } })).rows);
        if (!factsCache.has(id)) factsCache.set(id, await api(P(project, `/explore/photo/${id}`)));
      } catch { return; }
      if (peekFor !== i) return;
      const r = row(id), f = factsCache.get(id) || {};
      peek.replaceChildren(h("img", { src: img.thumb(project, r), alt: "" }),
        h("div", { class: "explore-peek-text" }, h("b", {}, r.stem), h("span", {}, r.capture_str && r.time_src !== "file" ? when(r.capture_str) : "No date"),
          ...factLines(i, r, f).map((t) => (typeof t === "string" ? h("span", {}, t) : t))));
      peek.hidden = false;
      placePeek(lastPeekAt[0], lastPeekAt[1]);
    }, 80);
  }
  let lastPeekAt = [0, 0];
  function placePeek(px, py) {
    lastPeekAt = [px, py];
    const w = peek.offsetWidth || 200, hh = peek.offsetHeight || 220;
    const x = px + 16 + w > W ? px - 16 - w : px + 16;
    const y = Math.max(4, Math.min(H - hh - 4, py + 16));
    peek.style.left = `${Math.max(4, x)}px`;
    peek.style.top = `${y}px`;
  }
  function factLines(i, r, f) {
    const out = [];
    if (by === "content") {
      const reg = S.region[i] >= 0 ? S.L.guides.regions[S.region[i]] : null;
      if (reg) out.push(`In: ${reg.name}`);
    } else if (by === "colour") {
      const words = [f.hue, f.colourful].filter(Boolean).join(", ");
      if (words) out.push(words[0].toUpperCase() + words.slice(1));
      if (f.palette?.length) out.push(h("span", { class: "explore-swatches", "aria-hidden": "true" }, f.palette.map((c) => h("i", { style: { background: c } }))));
    } else if (by === "light") {
      if (f.brightness != null) out.push(`Brightness ${f.brightness}% · contrast ${f.contrast}%`);
      if (f.highlights) out.push("Highlights clipped");
      if (f.shadows) out.push("Shadows clipped");
    } else if (by === "quality") {
      if (f.quality != null) out.push(`Quality ${f.quality}${f.focus ? ` · ${f.focus}` : ""}`);
    } else if (by === "camera") {
      if (f.camera) out.push(f.camera);
      if (f.lens) out.push(f.lens);
    }
    if (r.pick === 1) out.push("Picked");
    else if (r.pick === -1) out.push("Rejected");
    return out;
  }

  // ------------------------------------------------------------ mouse, trackpad and touch
  const touches = new Map();
  let gesture = null;
  canvas.addEventListener("pointerdown", (e) => {
    if (e.button > 0) return;
    canvas.setPointerCapture(e.pointerId);
    touches.set(e.pointerId, [e.offsetX, e.offsetY]);
    hidePointer();
    if (touches.size === 2) {
      const [[ax, ay], [bx, by_]] = [...touches.values()];
      gesture = { kind: "pinch", d: Math.hypot(ax - bx, ay - by_), c: [(ax + bx) / 2, (ay + by_) / 2], v: { ...view } };
      boxEl.hidden = true;
      return;
    }
    flight = null;
    const box = e.shiftKey || selecting;
    gesture = { kind: box ? "box" : "pan", x: e.offsetX, y: e.offsetY, moved: false, v: { ...view }, touch: e.pointerType === "touch",
      meta: e.metaKey || e.ctrlKey, t: performance.now() };
    if (gesture.touch && !box) {
      gesture.hold = setTimeout(() => {          // press and hold: select the photo
        const i = photoAt(gesture.x, gesture.y);
        if (i >= 0 && !gesture.moved) { gesture.held = true; toggle(i); navigator.vibrate?.(10); }
      }, 450);
    }
  });
  canvas.addEventListener("pointermove", (e) => {
    if (touches.has(e.pointerId)) touches.set(e.pointerId, [e.offsetX, e.offsetY]);
    if (!gesture) {
      if (e.pointerType === "mouse" && !anim) {
        const i = photoAt(e.offsetX, e.offsetY);
        if (i !== hover) { hover = i; schedule(); }
        showPeek(i, e.offsetX, e.offsetY);
      }
      return;
    }
    if (gesture.kind === "pinch" && touches.size >= 2) {
      const [[ax, ay], [bx, by_]] = [...touches.values()];
      const d = Math.hypot(ax - bx, ay - by_), c = [(ax + bx) / 2, (ay + by_) / 2];
      const s = Math.min(MAX_S, Math.max(minS(), gesture.v.s * d / Math.max(1, gesture.d))), k = s / gesture.v.s;
      setView({ s, tx: c[0] - (gesture.c[0] - gesture.v.tx) * k, ty: c[1] - (gesture.c[1] - gesture.v.ty) * k });
      return;
    }
    const dx = e.offsetX - gesture.x, dy = e.offsetY - gesture.y;
    if (!gesture.moved && Math.hypot(dx, dy) > (gesture.touch ? 8 : 4)) {
      gesture.moved = true;
      clearTimeout(gesture.hold);
      peek.hidden = true; peekFor = -1;
      if (gesture.kind === "pan") canvas.classList.add("dragging");
    }
    if (!gesture.moved) return;
    if (gesture.kind === "pan") setView({ s: gesture.v.s, tx: gesture.v.tx + dx, ty: gesture.v.ty + dy });
    else {
      const x = Math.min(gesture.x, e.offsetX), y = Math.min(gesture.y, e.offsetY);
      Object.assign(boxEl.style, { left: `${x}px`, top: `${y}px`, width: `${Math.abs(dx)}px`, height: `${Math.abs(dy)}px` });
      boxEl.hidden = false;
    }
  });
  const endPointer = (e) => {
    touches.delete(e.pointerId);
    if (!gesture) return;
    if (gesture.kind === "pinch") { if (!touches.size) gesture = null; return; }
    clearTimeout(gesture.hold);
    canvas.classList.remove("dragging");
    const g = gesture;
    gesture = null;
    if (e.type === "pointercancel") { boxEl.hidden = true; return; }
    if (g.kind === "box" && g.moved) { boxSelect(g.x, g.y, e.offsetX, e.offsetY); boxEl.hidden = true; return; }
    if (g.moved || g.held) return;
    // A click or a tap.
    const i = photoAt(g.x, g.y);
    if (g.meta || (selecting && i >= 0) || (g.touch && sel.size && i >= 0)) { if (i >= 0) toggle(i); return; }
    if (i >= 0 && view.s >= OPEN_AT) openAt(i);
    else if (S.L) zoomBy(2.5, [g.x, g.y]);
  };
  canvas.addEventListener("pointerup", endPointer);
  canvas.addEventListener("pointercancel", endPointer);
  canvas.addEventListener("pointerleave", (e) => {
    if (e.pointerType !== "mouse" || gesture) return;
    hover = -1; peek.hidden = true; peekFor = -1; schedule();
  });
  canvas.addEventListener("dblclick", (e) => { e.preventDefault(); zoomBy(e.shiftKey ? 1 / 2 : 2, [e.offsetX, e.offsetY]); });
  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    flight = null;
    // A pinch on a trackpad (or ⌃ and the wheel) zooms; so does a mouse wheel. Two fingers on a trackpad pan.
    const mouseWheel = e.deltaMode !== 0 || (!e.deltaX && Math.abs(e.deltaY) >= 40 && Number.isInteger(e.deltaY));
    if (e.ctrlKey || mouseWheel) {
      const k = e.deltaMode === 1 ? 33 : 1;
      zoomAt(Math.exp(-e.deltaY * k * (e.ctrlKey ? 0.012 : 0.0025)), e.offsetX, e.offsetY);
    } else setView({ s: view.s, tx: view.tx - e.deltaX, ty: view.ty - e.deltaY });
    peek.hidden = true; peekFor = -1;
  }, { passive: false });

  function toggle(i) {
    const id = S.ids[i];
    sel.has(id) ? sel.delete(id) : sel.add(id);
    paintSel();
    announce(`${sel.has(id) ? "Selected" : "Deselected"} ${photoName(row(id))} · ${plural(sel.size, "photo")} selected`);
  }
  function boxSelect(ax, ay, bx, by_) {
    const x0 = Math.floor((Math.min(ax, bx) - view.tx) / view.s), x1 = Math.floor((Math.max(ax, bx) - view.tx) / view.s);
    const y0 = Math.floor((Math.min(ay, by_) - view.ty) / view.s), y1 = Math.floor((Math.max(ay, by_) - view.ty) / view.s);
    let k = 0;
    for (let y = Math.max(0, y0); y <= Math.min(S.rows - 1, y1); y++) {
      for (let x = Math.max(0, x0); x <= Math.min(S.cols - 1, x1); x++) {
        const i = cell[y * S.cols + x];
        if (i >= 0 && S.shown[i] && !sel.has(S.ids[i])) { sel.add(S.ids[i]); k++; }
      }
    }
    paintSel();
    announce(k ? `Selected ${plural(k, "more photo")} · ${plural(sel.size, "photo")} selected` : "No more photos there");
  }

  // ------------------------------------------------------------ the keyboard's pointer
  const pointer = { on: false, x: 0, y: 0 };
  function showPointer() {
    pointer.on = true;
    const px = pointer.x * view.s + view.tx, py = pointer.y * view.s + view.ty;
    // keep it in view
    const m = 40;
    let { tx, ty } = view;
    if (px < m) tx += m - px; else if (px > W - m) tx -= px - (W - m);
    if (py < m) ty += m - py; else if (py > H - m) ty -= py - (H - m);
    if (tx !== view.tx || ty !== view.ty) setView({ s: view.s, tx, ty });
    Object.assign(pointerEl.style, { left: `${pointer.x * view.s + view.tx}px`, top: `${pointer.y * view.s + view.ty}px` });
    pointerEl.hidden = false;
    const i = nearest(pointer.x, pointer.y, 0);
    hover = i;
    schedule();
    if (i >= 0) {
      showPeek(i, pointer.x * view.s + view.tx, pointer.y * view.s + view.ty);
      sayPointer(i);
    } else { peek.hidden = true; peekFor = -1; }
  }
  function hidePointer() { pointer.on = false; pointerEl.hidden = true; }
  let sayTimer = 0;
  function sayPointer(i) {
    clearTimeout(sayTimer);
    sayTimer = setTimeout(async () => {
      const id = S.ids[i];
      if (!row(id)) { try { remember((await api(P(project, "/rows"), { params: { ids: String(id) } })).rows); } catch { return; } }
      const reg = by === "content" && S.region[i] >= 0 ? ` · ${S.L.guides.regions[S.region[i]].name}` : "";
      announce(`${photoName(row(id))}${reg}${sel.has(id) ? " · selected" : ""}`);
    }, 250);
  }
  canvas.addEventListener("focus", () => {
    if (!canvas.matches(":focus-visible") || !S.L) return;
    // Back where it was if that's still on screen; otherwise in the middle of what's shown.
    const px = pointer.x * view.s + view.tx, py = pointer.y * view.s + view.ty;
    if (!(pointer.x || pointer.y) || px < 0 || py < 0 || px > W || py > H) {
      const b = bounds();
      pointer.x = Math.min(b.x1 - 0.5, Math.max(b.x0 + 0.5, (W / 2 - view.tx) / view.s));
      pointer.y = Math.min(b.y1 - 0.5, Math.max(b.y0 + 0.5, (H / 2 - view.ty) / view.s));
    }
    showPointer();
  });
  // Keys reach the pointer's photo only while the map has the focus.
  canvas.addEventListener("blur", () => {
    if (!pointer.on) return;
    hidePointer();
    hover = -1; peek.hidden = true; peekFor = -1;
    schedule();
  });

  /** Keys while the map has the focus. Returns true if handled. */
  function onKey(e) {
    if (document.activeElement !== canvas) {
      if (e.key === "Escape" && sel.size) { grid.clearSelection(); return true; }
      return false;
    }
    if (e.metaKey || e.ctrlKey) {
      if (e.key.toLowerCase() === "a") { grid.selectAll(); announce(`${plural(sel.size, "photo")} selected`); return true; }
      return false;
    }
    const k = e.key;
    const step = e.shiftKey ? 1 : Math.max(1, Math.round(28 / view.s));
    const move = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[k];
    if (move) {
      if (!pointer.on) { pointer.x = (W / 2 - view.tx) / view.s; pointer.y = (H / 2 - view.ty) / view.s; }
      pointer.x = Math.min(S.cols - 0.5, Math.max(0.5, Math.floor(pointer.x + move[0]) + 0.5));
      pointer.y = Math.min(S.rows - 0.5, Math.max(0.5, Math.floor(pointer.y + move[1]) + 0.5));
      showPointer();
      return true;
    }
    if (k === "+" || k === "=") { zoomBy(2); if (pointer.on) setTimeout(showPointer, 450); return true; }
    if (k === "-" || k === "_") { zoomBy(1 / 2); if (pointer.on) setTimeout(showPointer, 450); return true; }
    if (k === "Home") { fly(fitView()); return true; }
    if (k === "Enter") { const i = target(); if (i >= 0) openAt(i); else if (pointer.on) announce("No photo at the pointer"); return true; }
    if (k === " ") { const i = target(); if (i >= 0) toggle(i); return true; }
    if (k === "Escape" && sel.size) { grid.clearSelection(); return true; }
    return false;
  }

  // ------------------------------------------------------------ loading
  let poll = 0;
  function paintArrange() {
    const had = arrange.contains(document.activeElement);
    arrange.replaceChildren(choiceSeg(ARRANGEMENTS.map(([k, t]) => [k, t, available[k] === true || available[k] === undefined ? {}
      : { title: available[k] }]), by, (k) => choose(k), { label: "Arrange by" }));
    arrange.prepend(h("span", { class: "explore-arrange-label", "aria-hidden": "true" }, "Arrange by"));
    if (had) arrange.querySelector('[aria-checked="true"]')?.focus();
  }
  function paintLegend() {
    const bits = [];
    if (by === "light") bits.push("White corner: highlights clipped · black corner: shadows clipped");
    if (by === "colour") bits.push("The rim shows which hue is where: hues most of your photos have get more of it.");
    if (by === "time") bits.push("Shaded: around sunrise (05:30–08:00) and sunset (17:00–19:30)");
    if (by === "content" && S.L && S.L.guides && !S.L.guides.named) bits.push("Region names need the picture-search model on this Mac.");
    if (by === "content" && S.L?.guides?.unplaced) bits.push(`${plural(S.L.guides.unplaced, "photo")} not indexed yet wait in a row underneath.`);
    if (marking) bits.push("Green: picked · red: rejected");
    legend.hidden = !bits.length;
    legend.textContent = bits.join(" · ");
  }
  function choose(k) {
    if (k === by) return;
    by = k;
    onBy?.(k);
    if (measuring === null) note.hidden = true;            // (what the first arrangement needed said)
    paintArrange();
    caption.textContent = CAPTIONS[by];
    announce(CAPTIONS[by]);
    load(true);
  }

  async function load(glide) {
    clearTimeout(poll);
    const want = by;
    let L = layouts.get(want);
    if (!L) {
      let d;
      try { d = await api(P(project, `/explore/${want}`)); } catch (e) { if (want === by) showWait({ error: e.message }); return; }
      if (!alive || want !== by) return;
      if (!d.ready) {
        if (d.stale && !S.L) { prepare(d.stale); apply(d.stale, false); setView(fitView()); }
        showWait(d);
        if (d.progress || (!d.missing && !d.error)) poll = setTimeout(() => load(glide), 800);
        return;
      }
      prepare(d);
      layouts.set(want, d);
      L = d;
    }
    showWait(null);
    const first = !S.L;
    apply(L, glide && !first && !reduceMotion());
    paintLegend();
    const v = fitView();
    if (first || reduceMotion()) setView(v); else fly(v, GLIDE + STAGGER * 0.6);
    schedule();
  }
  function prepare(d) { d.ids = Int32Array.from(d.ids); d.cells = Int32Array.from(d.cells); }

  function showWait(d) {
    wait.hidden = !d;
    // An arrangement that can't be made here leaves nothing of another one showing behind it.
    const blank = !!(d && (d.missing || d.error));
    map.classList.toggle("blank", blank);
    if (blank) legend.hidden = true;
    if (!d) return;
    const label = d.missing || d.error ? null : d.progress;
    // (A total of 100 is a share done; anything else is photos.)
    const far = label?.total ? (label.total === 100 ? `${label.done}%` : `${n(label.done)} of ${n(label.total)}`) : "";
    wait.replaceChildren(...[
      label ? h("span", { class: "spinner", "aria-hidden": "true" }) : null,
      h("div", {}, h("b", {}, d.missing ? "Not here yet" : d.error ? "That didn't work" : label?.what || "Getting ready"),
        h("p", {}, d.missing || d.error || far),
        d.missing || d.error ? h("p", { class: "muted" }, "Choose another arrangement above.") : null),
      label?.total ? h("div", { class: "explore-progress", "aria-hidden": "true" }, h("i", { style: { width: `${Math.round((100 * label.done) / label.total)}%` } })) : null,
    ].filter(Boolean));
  }

  let measuring = null;
  async function loadShown() {
    let d;
    try { d = await api(P(project, "/explore"), { params: filter }); } catch (e) { toast(e.message, { kind: "error" }); return; }
    if (!alive) return;
    available = d.available || {};
    // A library without picture vectors opens on its timeline, saying why.
    if (!S.L && !asked && available[by] !== true && available.time === true) {
      by = "time";
      caption.textContent = CAPTIONS[by];
      note.hidden = false;
      note.textContent = `${available.content} Showing your photos by time meanwhile.`;
    }
    paintArrange();
    shownSet = new Set(d.shown);
    for (let i = 0; i < S.n; i++) S.shown[i] = shownSet.has(S.ids[i]) ? 1 : 0;
    fitted = null;
    dropTiles();
    regrow();
    for (const id of [...sel]) if (!shownSet.has(id)) sel.delete(id);
    paintMosaic();
    paintSel();
    onCount?.(d.shown.length);
    const m = d.measuring;
    if (m?.running) {
      note.hidden = false;
      note.textContent = `Measuring each photo's colours: ${n(m.done)} of ${n(m.total)}. Squares take their colours as it goes.`;
      measuring = setTimeout(loadShown, 2500);
    } else if (measuring !== null) {
      note.hidden = true;
      measuring = null;
      layouts.clear();                          // the colours are in: the layouts again, with them
      load(false);
    }
  }

  // ------------------------------------------------------------ size, theme, life
  function resize() {
    // A desktop's map fills the window below the controls; a phone's, the screen under the top bar
    // once the controls have scrolled away.
    const narrow = window.innerWidth < 640;
    const hh = narrow ? Math.max(320, window.innerHeight - (document.getElementById("topbar")?.offsetHeight || 0) - 16)
      : Math.max(420, Math.round(window.innerHeight - Math.max(0, map.getBoundingClientRect().top + window.scrollY) - 20));
    map.style.height = `${hh}px`;
    const w = map.clientWidth;
    const centre = W && H ? [(W / 2 - view.tx) / view.s, (H / 2 - view.ty) / view.s] : null;
    dpr = window.devicePixelRatio || 1;
    W = w; H = hh;
    fitted = null;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    canvas.style.width = `${W}px`; canvas.style.height = `${H}px`;
    if (centre) { view.tx = W / 2 - centre[0] * view.s; view.ty = H / 2 - centre[1] * view.s; }
    schedule();
  }
  const ro = new ResizeObserver(() => { if (map.clientWidth !== W) resize(); });
  const onWinResize = () => resize();
  window.addEventListener("resize", onWinResize);
  const retheme = () => { color = cssColors(el); for (const k in fonts) delete fonts[k]; schedule(); };
  const scheme = matchMedia("(prefers-color-scheme: dark)");
  scheme.addEventListener("change", retheme);
  const mo = new MutationObserver(retheme);
  mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "style", "class"] });
  const off = on((type, ids) => {
    if (type !== "rows" || !S.L) return;
    for (const id of ids) { const i = S.at.get(id); const r = row(id); if (i !== undefined && r) S.pick[i] = r.pick || 0; }
    schedule();
  });
  let settleTimer = 0;
  function settle() {
    clearTimeout(settleTimer);
    settleTimer = setTimeout(() => { if (pointer.on) Object.assign(pointerEl.style, { left: `${pointer.x * view.s + view.tx}px`, top: `${pointer.y * view.s + view.ty}px` }); }, 30);
  }

  caption.textContent = CAPTIONS[by];
  paintArrange();
  requestAnimationFrame(() => {
    if (!alive) return;
    resize();
    ro.observe(map);
    loadShown().then(() => load(false));
  });

  return {
    el, grid,
    onKey,
    /** The Library's filters changed. */
    setFilter(f) { filter = f; loadShown(); },
    /** Everything again (an update finished, or the page was asked to refresh). */
    refresh() { layouts.clear(); loadShown().then(() => load(false)); },
    destroy() {
      alive = false;
      clearTimeout(poll); clearTimeout(wantTimer); clearTimeout(peekTimer); clearTimeout(measuring); clearTimeout(sayTimer);
      if (raf) cancelAnimationFrame(raf);
      ro.disconnect(); mo.disconnect(); off();
      window.removeEventListener("resize", onWinResize);
      scheme.removeEventListener("change", retheme);
    },
  };
}
