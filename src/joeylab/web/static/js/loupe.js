// Full-screen viewer: large preview (or the video itself), 1:1 zoom, overlays (histogram,
// clipping, where it's sharp), filmstrip, and an info panel with why JoeyLab suggests what it
// does, place, event, people, versions, comments and files.

import { api, img, P } from "./api.js";
import { mapsLink } from "./maps.js";
import { clearDecision, decide, on, remember, row, setLabel, setPick, setPref, store, toggleFav } from "./store.js";
import { addToAlbum } from "./views/albumpick.js";
import { keywordBlock, keywordDialog } from "./views/keywords.js";
import { rawTag, toggleRaw } from "./views/raws.js";
import { whereEl } from "./views/backups.js";
import { announce, bytes, clear, dialog, duration, flagLabel, h, holdFocus, icon, LABEL_COLORS, labelDot, offsetLabel, photoName, plural, starsEl, sugLabel, toast, when } from "./ui.js";

// How a photo's place is known, when it isn't its own GPS.
const PLACE_HOW = { inferred: " (from nearby photos)", folder: " (guessed from the folder name)", manual: " (set by you)" };

// ------------------------------------------------------------ a photo on the stage
// Shared by the viewer and Cull. It fits its box (portrait or landscape, with or without the info
// panel), keeps the picture before it until the next has decoded (never a blank stage), marks a
// thumbnail standing in for the full preview, and zooms to 50% or 100% from tiles of the full size
// rather than the whole original. Pictures that share a lock keep one zoom level and centre: the
// frames of a stack side by side, or one after another.

const decoded = new Map();          // url -> decoded Image, most recent last: showing one is instant
const decoding = new Map();         // url -> Promise
const DECODED_MAX = 24;

/** Fetch and decode a picture off screen; resolves once it can be shown without a blank frame. */
export function preload(url) {
  const have = decoded.get(url);
  if (have) { decoded.delete(url); decoded.set(url, have); return Promise.resolve(have); }
  if (decoding.has(url)) return decoding.get(url);
  const im = new Image();
  im.fetchPriority = "high";
  im.src = url;
  const p = im.decode().then(() => {
    decoding.delete(url);
    decoded.set(url, im);
    while (decoded.size > DECODED_MAX) decoded.delete(decoded.keys().next().value);
    pump();
    return im;
  }, (e) => { decoding.delete(url); pump(); throw e; });
  decoding.set(url, p);
  return p;
}

let queue = [], fetching = 0;
/** Bring pictures into the browser's cache two at a time, in the order given, while nothing more
 * urgent is loading (a slow link goes to what's next first); a newer list replaces what's waiting. */
export function prefetch(urls) {
  queue = urls.filter((u) => u && !decoded.has(u));
  pump();
}
function pump() {
  while (fetching < 2 && decoding.size < 2 && queue.length) {
    const u = queue.shift();
    if (decoded.has(u) || decoding.has(u)) continue;
    fetching++;
    const im = new Image();
    im.fetchPriority = "low";
    im.onload = im.onerror = () => { fetching--; pump(); };
    im.src = u;
  }
}

const tileInfo = new Map();
function tilesOf(p, r) {
  const k = `${p}/${r.id}/${r.v}`;
  if (!tileInfo.has(k)) {
    tileInfo.set(k, api(`/img/${encodeURIComponent(p)}/tiles/${r.id}`, { params: { v: r.v } })
      .catch((e) => { tileInfo.delete(k); throw e; }));
  }
  return tileInfo.get(k);
}

export const ZOOMS = [[0, "Fit"], [0.5, "50%"], [1, "100%"]];
/** One zoom level and centre (0-1 across the photo) for the pictures that share it. */
export function zoomLock() { return { level: 0, cx: 0.5, cy: 0.5, pics: new Set() }; }
const clamp01 = (x) => Math.max(0, Math.min(1, x));

export function picture({ lock = zoomLock(), onPicture } = {}) {
  const pic = h("img", { alt: "", draggable: "false" });
  const inner = h("div", { class: "pic-inner" }, pic);
  const plane = h("div", { class: "pic-plane", hidden: true });
  const mark = h("span", { class: "pic-mark", hidden: true }, h("span", { class: "spinner" }), "Loading full size");
  const el = h("div", { class: "pic" }, inner, plane, mark);
  let row_ = null, p = null, token = 0, staleTimer = 0, full = false, z = null, syncing = false, queued = false, moved = false;

  function put(url, r, isFull, t) {
    if (t !== token) return;
    inner.style.setProperty("--ar", r.ar || 1.5);
    pic.src = url;
    pic.alt = r.stem || "";
    clearTimeout(staleTimer);
    el.classList.remove("stale");
    full = isFull;
    mark.hidden = isFull;
    if (z) plane.style.backgroundImage = `url("${url}")`;
    onPicture?.(isFull);
  }

  /** Show a photo: at once if its preview is ready, else its thumbnail and then the preview, with
   * the previous picture kept (dimmed) until one of them has decoded. */
  function show(project, r) {
    if (row_ && row_.id === r.id && row_.v === r.v && p === project) { layout(); return; }
    const t = ++token;
    p = project; row_ = r; full = false;
    const big = img.preview(p, r), small = img.thumb(p, r, "m");
    clearTimeout(staleTimer);
    if (decoded.has(big)) put(big, r, true, t);
    else {
      staleTimer = setTimeout(() => { if (t === token) { el.classList.add("stale"); mark.hidden = false; } }, 150);
      let got = false;
      preload(small).then(() => { if (!got) put(small, r, false, t); }).catch(() => {});
      preload(big).then(() => { got = true; put(big, r, true, t); }).catch(() => { if (!got) put(small, r, true, t); });
    }
    layout();
  }

  // ---- zoom
  async function layout() {
    const level = lock.level, r = row_, t = token;
    if (!level || !r || r.media === "video") { unzoom(); return; }
    let info;
    try { info = await tilesOf(p, r); } catch (e) {
      if (t === token && lock.level === level) { lock.level = 0; unzoom(); toast(e.message, { kind: "error" }); }
      return;
    }
    if (t !== token || lock.level !== level) return;
    const d = level >= 1 ? 1 : 2;
    if (!(z && z.id === r.id && z.d === d)) {
      const W = Math.round(info.w / d), H = Math.round(info.h / d);
      z = { id: r.id, v: r.v, d, W, H, T: info.tile, cols: Math.ceil(W / info.tile), rows: Math.ceil(H / info.tile), have: new Set() };
      clear(plane);
      Object.assign(plane.style, { width: `${W}px`, height: `${H}px`, backgroundImage: `url("${pic.src}")` });
      plane.hidden = false;
      inner.hidden = true;
      el.classList.add("zoomed");
    }
    follow();
  }
  function unzoom() {
    z = null;
    el.classList.remove("zoomed");
    plane.hidden = true;
    clear(plane);
    inner.hidden = false;
  }
  /** Scroll to the lock's centre and draw the tiles in view. */
  function follow() {
    if (!z) return;
    syncing = true;
    el.scrollLeft = lock.cx * z.W - el.clientWidth / 2;
    el.scrollTop = lock.cy * z.H - el.clientHeight / 2;
    requestAnimationFrame(() => { syncing = false; });
    tiles();
  }
  function tiles() {
    if (!z) return;
    const x0 = Math.max(0, el.scrollLeft - plane.offsetLeft), y0 = Math.max(0, el.scrollTop - plane.offsetTop);
    const i1 = Math.min(z.cols - 1, Math.floor((x0 + el.clientWidth) / z.T)), j1 = Math.min(z.rows - 1, Math.floor((y0 + el.clientHeight) / z.T));
    for (let j = Math.floor(y0 / z.T); j <= j1; j++) {
      for (let i = Math.floor(x0 / z.T); i <= i1; i++) {
        if (z.have.has(`${i}_${j}`)) continue;
        z.have.add(`${i}_${j}`);
        plane.append(h("img", { class: "tile", alt: "", draggable: "false",
          src: `/img/${encodeURIComponent(p)}/tile/${z.id}/${z.d}/${i}/${j}?v=${z.v}`,
          style: { left: `${i * z.T}px`, top: `${j * z.T}px`, width: `${Math.min(z.T, z.W - i * z.T)}px`, height: `${Math.min(z.T, z.H - j * z.T)}px` } }));
      }
    }
  }
  el.addEventListener("scroll", () => {
    if (!z) return;
    if (!queued) { queued = true; requestAnimationFrame(() => { queued = false; tiles(); }); }
    if (syncing) return;
    lock.cx = clamp01((el.scrollLeft + el.clientWidth / 2 - plane.offsetLeft) / z.W);
    lock.cy = clamp01((el.scrollTop + el.clientHeight / 2 - plane.offsetTop) / z.H);
    for (const o of lock.pics) if (o !== me) o.follow();
  });
  new ResizeObserver(() => tiles()).observe(el);
  // Drag to look around at 50% and 100% (a finger scrolls).
  let drag = null;
  el.addEventListener("pointerdown", (e) => {
    moved = false;
    if (!z || e.pointerType !== "mouse" || e.button) return;
    drag = { x: e.clientX, y: e.clientY, sl: el.scrollLeft, st: el.scrollTop };
    el.setPointerCapture(e.pointerId);
    el.classList.add("grabbing");
  });
  el.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 4) moved = true;
    el.scrollLeft = drag.sl - dx;
    el.scrollTop = drag.st - dy;
  });
  const endDrag = () => { drag = null; el.classList.remove("grabbing"); };
  el.addEventListener("pointerup", endDrag);
  el.addEventListener("pointercancel", endDrag);

  /** Zoom every picture sharing the lock; `at` ({x, y} on screen) becomes the centre. */
  function setLevel(level, at) {
    if (at) {
      const b = (z ? plane : inner).getBoundingClientRect();
      if (b.width) { lock.cx = clamp01((at.x - b.left) / b.width); lock.cy = clamp01((at.y - b.top) / b.height); }
    } else if (!lock.level) { lock.cx = 0.5; lock.cy = 0.5; }
    lock.level = level;
    for (const o of lock.pics) o.layout();
  }

  const me = { el, img: pic, inner, show, layout, follow, setLevel, row: () => row_, full: () => full, dragged: () => moved,
    destroy() { lock.pics.delete(me); token++; } };
  lock.pics.add(me);
  return me;
}

/** A large, brief word on the stage after a decision ("Picked", "Rejected"), with the photo it was
 * about (Cull has moved on by the time it fades), also read out. */
export function flash(host, text, kind = "", about = "") {
  let el = host.querySelector(":scope > .stage-flash");
  if (!el) host.append(el = h("div", { class: "stage-flash", role: "status" }));
  clear(el).append(kind === "pick" || kind === "reject" ? icon(kind) : null,
    h("span", {}, text, about ? h("small", {}, about) : null));
  el.className = `stage-flash ${kind}`;
  void el.offsetWidth;                          // restart the animation
  el.classList.add("on");
  clearTimeout(el.off);
  el.off = setTimeout(() => el.classList.remove("on"), 900);
}

/** Fit / 50% / 100%, for a bar. */
export function zoomControl(onSet) {
  const seg = h("div", { class: "seg zoom-seg", role: "group", "aria-label": "Zoom" });
  const paint = (level) => clear(seg).append(ZOOMS.map(([v, t]) => h("button", { type: "button", "aria-pressed": v === level ? "true" : "false",
    title: v === 0 ? "Fit the photo (Z toggles 100%)" : v === 1 ? "100%: every pixel (Z)" : "50% (⇧Z)", onclick: () => onSet(v) }, t)));
  return { el: seg, paint };
}

let active = null;

export const loupeOpen = () => !!active;
export function loupeKey(e) { return active ? active.onKey(e) : false; }
export function closeLoupe() { active?.close(); }
/** Go to another page (a hash link). From the viewer, its history entry becomes that page: Back then
 * returns to where the viewer was opened, and closing it doesn't undo the step. */
export function goTo(hash) {
  if (active) active.close({ to: hash });
  else location.hash = hash;
}

const detailCache = new Map();
function detail(id) {
  if (!detailCache.has(id)) {
    detailCache.set(id, api(P(store.project, `/shot/${id}`)).then((d) => { remember([d.row]); return d; })
      .catch((e) => { detailCache.delete(id); throw e; }));
  }
  return detailCache.get(id);
}
export function forgetDetails() { detailCache.clear(); }

const ramp = (x, lo, hi) => (x > 0 ? Math.max(0, Math.min(1, (Math.log10(x) - lo) / (hi - lo))) : 0);
const OVERLAYS = ["", "histogram", "clipping", "sharp"];
const OVERLAY_NAMES = { "": "No overlay", histogram: "Histogram", clipping: "Clipped highlights and shadows", sharp: "Where it's sharp" };
const LABEL_KEYS = { 6: "Red", 7: "Yellow", 8: "Green", 9: "Blue" };

export function openLoupe({ ids, index = 0, onClose, onIndex, loadMore, total, zoom = false, lock = zoomLock() }) {
  // Another photo opened from the viewer (a burst frame, another version) takes over its history
  // entry and its hold on focus, so Back and Esc still lead back to the page.
  const replacing = active && history.state?.joeylabLoupe;
  const heldBefore = active?.release;
  active?.close({ keepHistory: true, keepFocus: true });
  const host = document.getElementById("loupe");
  host.hidden = false;
  const p = store.project;
  const st = { i: index, dragged: false, overlay: store.prefs.overlay || "", shown: null, tap: 0 };

  const titleB = h("b"), titleS = h("span"), counter = h("span", { class: "mono faint" });
  const pc = picture({ lock, onPicture: () => { if (st.overlay) paintOverlay(); } });
  const picEl = pc.img;
  const videoEl = h("video", { controls: true, playsinline: true, preload: "metadata" });
  const videoBox = h("div", { class: "pic pic-video", hidden: true }, videoEl);
  const overlayCanvas = h("canvas", { class: "ov-canvas", hidden: true });
  const sharpGrid = h("div", { class: "ov-sharp", hidden: true });
  pc.inner.append(overlayCanvas, sharpGrid);
  const hist = h("canvas", { class: "ov-hist", width: 256, height: 90, hidden: true });
  const tags = h("div", { class: "overlay-tags" });
  const viewer = h("div", { class: "viewer" }, pc.el, videoBox, tags, hist,
    h("button", { class: "nav-btn prev", "aria-label": "Previous photo", onclick: () => go(st.i - 1) }, icon("left")),
    h("button", { class: "nav-btn next", "aria-label": "Next photo", onclick: () => go(st.i + 1) }, icon("right")));
  // On a phone the panel covers the photo, so it starts closed there whatever the desktop preference.
  const info = h("aside", { class: "info", hidden: !store.prefs.info || matchMedia("(max-width: 900px)").matches });
  const barStars = h("span");
  const pickBtn = h("button", { class: "btn", title: "Pick (P)", onclick: () => pick(1) }, icon("pick"), "Pick", h("kbd", { class: "hide-sm" }, "P"));
  const rejBtn = h("button", { class: "btn", title: "Reject (X)", onclick: () => pick(-1) }, icon("reject"), "Reject", h("kbd", { class: "hide-sm" }, "X"));
  const zc = zoomControl((v) => setZoom(v));
  const favBtn = h("button", { class: "btn ghost icon-only", title: "Favourite (F)", "aria-label": "Favourite", onclick: () => fav() }, icon("heart"));
  const labelBtn = h("button", { class: "btn ghost hide-sm", title: "Colour label (6–9)", "aria-haspopup": "menu", "aria-expanded": "false",
    onclick: (e) => labelMenu(e.currentTarget) }, h("span", { class: "label-dot empty" }), "Label");
  const overlayBtn = h("button", { class: "btn ghost hide-sm", title: "Overlays (O)", onclick: () => cycleOverlay() }, icon("overlay"), h("span", {}, "Overlay"));
  const albumBtn = h("button", { class: "btn ghost", title: "Add to an album (B)", "aria-label": "Add to album",
    onclick: (e) => addToAlbum({ ids: [cur()], anchor: e.currentTarget }) }, icon("album"), h("span", { class: "hide-sm" }, "Album"));
  // (The filmstrip is for the mouse: ← → Home End do the same from the keyboard, and each photo is read out.)
  const film = h("div", { class: "film", tabindex: "-1", "aria-hidden": "true" });

  const root = h("div", { class: "loupe stage-ui", role: "dialog", "aria-modal": "true", "aria-label": "Photo viewer", tabindex: "-1" },
    h("div", { class: "loupe-top" },
      // The viewer's heading: the photo's name, when it was taken, the camera.
      h("div", { class: "loupe-title", role: "heading", "aria-level": "1" }, titleB, titleS),
      h("div", { class: "right" }, counter,
        h("button", { class: "btn ghost icon-only", title: "Info (I)", "aria-label": "Toggle info", onclick: toggleInfo }, icon("info")),
        h("button", { class: "btn ghost icon-only", title: "Close (Esc)", "aria-label": "Close", onclick: () => close() }, icon("close")))),
    h("div", { class: "loupe-body" }, viewer, info),
    h("div", { class: "loupe-bar" }, barStars, pickBtn, rejBtn, favBtn, labelBtn, h("span", { class: "spacer" }),
      overlayBtn, albumBtn,
      h("button", { class: "btn ghost hide-sm", title: "Find similar photos", onclick: () => {
        close({ to: `#/p/${encodeURIComponent(p)}/search?similar=${cur()}` });
      } }, icon("similar"), "Similar"),
      zc.el),
    film);
  clear(host).append(root);
  const release = heldBefore || holdFocus(root);

  const cur = () => ids[st.i];
  // Links in the panel (place, event, trip, people) leave the viewer for that page. A ⌘- or
  // ⇧-click still opens a new tab or window and keeps the viewer open.
  const leave = (href) => ({ href, onclick: (e) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button) return;
    e.preventDefault();
    close({ to: href });
  } });

  function toggleInfo() {
    info.hidden = !info.hidden;
    setPref("info", !info.hidden);
    if (!info.hidden) paintInfo();
  }

  function fav() {
    const r = row(cur());
    toggleFav([cur()], !(r && r.fav));
    detailCache.delete(cur());
  }

  /** P and X set the pick or reject (pressing again changes nothing); 0 clears it. */
  function pick(v) {
    setPick([cur()], v);
    flash(viewer, v === 1 ? "Picked" : "Rejected", v === 1 ? "pick" : "reject");
  }

  function labelMenu(anchor) {
    const r = row(cur()) || {};
    // A menu: the arrows move through it, Esc closes it, and the keyboard goes back to Label.
    const done = (refocus = true) => {
      menu.remove();
      anchor.setAttribute("aria-expanded", "false");
      document.removeEventListener("pointerdown", off);
      if (refocus) anchor.focus({ preventScroll: true });
    };
    const off = (e) => { if (!menu.contains(e.target)) done(false); };
    const menu = h("div", { class: "label-menu", role: "menu", "aria-label": "Colour label", onkeydown: (e) => {
      const items = [...menu.querySelectorAll("button")];
      const i = items.indexOf(document.activeElement);
      if (e.key === "Escape" || e.key === "Tab") { e.preventDefault(); e.stopPropagation(); done(); return; }
      if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
      e.preventDefault(); e.stopPropagation();
      const to = e.key === "Home" ? 0 : e.key === "End" ? items.length - 1 : i + (e.key === "ArrowDown" ? 1 : -1);
      items[(to + items.length) % items.length].focus();
    } }, Object.entries(LABEL_COLORS).map(([name, color], k) =>
      h("button", { type: "button", role: "menuitemradio", "aria-checked": r.label === name ? "true" : "false", class: `menu-item ${r.label === name ? "on" : ""}`,
        onclick: () => { setLabel([cur()], name); done(); } },
        h("span", { class: "label-dot", style: { background: color } }), name, k < 4 ? h("kbd", {}, String(k + 6)) : null)),
    h("button", { type: "button", role: "menuitemradio", "aria-checked": r.label ? "false" : "true", class: "menu-item", onclick: () => { setLabel([cur()], ""); done(); } }, "No label"));
    const rect = anchor.getBoundingClientRect();
    Object.assign(menu.style, { left: `${rect.left}px`, bottom: `${window.innerHeight - rect.top + 6}px` });
    root.append(menu);
    anchor.setAttribute("aria-expanded", "true");
    (menu.querySelector('[aria-checked="true"]') || menu.firstChild).focus({ preventScroll: true });
    setTimeout(() => document.addEventListener("pointerdown", off), 0);
  }

  function paintBar() {
    const r = row(cur()) || {};
    clear(barStars).append(starsEl(r.rating || 0, (s) => decide([{ id: cur(), rating: s }], s ? `${s}★` : "clear stars")));
    pickBtn.classList.toggle("on-pick", r.pick === 1);
    rejBtn.classList.toggle("on-reject", r.pick === -1);
    clear(favBtn).append(icon(r.fav ? "heartOn" : "heart"));
    favBtn.classList.toggle("on-fav", !!r.fav);
    clear(labelBtn).append(r.label ? labelDot(r.label) : h("span", { class: "label-dot empty" }), h("span", {}, r.label || "Label"));
    clear(tags);
    if (r.pick === 1) tags.append(h("span", { class: "pill pick" }, "Picked"));
    if (r.pick === -1) tags.append(h("span", { class: "pill reject" }, "Rejected"));
    if (!r.pick && !r.rating && r.suggestion) tags.append(h("span", { class: `pill ${r.suggestion === "check" ? "warn" : "accent"}` }, sugLabel(r.suggestion)));
    for (const f of r.flags || []) if (f !== "best-in-group") tags.append(h("span", { class: "pill neutral" }, flagLabel(f)));
    tags.append(rawTag(cur()));            // Keep RAW / Reject RAW, for a RAW + JPEG
    if (st.overlay) tags.append(h("span", { class: "pill accent" }, OVERLAY_NAMES[st.overlay]));
  }

  function paintFilm() {
    clear(film);
    const lo = Math.max(0, st.i - 40), hi = Math.min(ids.length, st.i + 41);
    for (let k = lo; k < hi; k++) {
      const r = row(ids[k]);
      if (!r) continue;
      const f = h("div", {
        class: `f ${k === st.i ? "cur" : ""} ${r.pick === 1 ? "pick" : r.pick === -1 ? "reject" : ""}`,
        onclick: () => go(k), title: r.stem,
      }, h("img", { src: img.thumb(p, r), loading: "lazy", alt: "" }));
      film.append(f);
      if (k === st.i) requestAnimationFrame(() => f.scrollIntoView({ inline: "center", block: "nearest" }));
    }
  }

  /** Say who a face is, from the viewer. With a clear group behind it, the whole group can take the name. */
  async function nameFace(f) {
    let names = store.peopleList?.project === p ? store.peopleList.people : null;
    if (!names) {
      try { const d = await api(P(p, "/people"), { params: { limit: 0 } }); names = d.people.concat(d.pets || []); }
      catch { names = []; }
    }
    const listId = "loupe-people-names";
    const input = h("input", { class: "input", list: listId, placeholder: "Name", "aria-label": "Name", autocomplete: "off" });
    const others = (f.group_faces || 1) - 1;
    const whole = f.cluster !== null && f.cluster !== undefined && others > 0 && f.group_clear
      ? h("input", { type: "checkbox", checked: true }) : null;
    dialog("Who is this?", h("div", { class: "stack-v" },
      h("img", { class: "face-big", src: `/img/${encodeURIComponent(p)}/f/${f.id}`, alt: "" }),
      input, h("datalist", { id: listId }, names.flatMap((x) => [x.name, ...(x.aliases || [])]       // "Papa → Dad" too
        .map((nm) => h("option", { value: nm, label: nm === x.name ? null : `→ ${x.name}` })))),
      whole ? h("label", { class: "check" }, whole, h("span", {}, `Also the ${plural(others, "other face")} JoeyLab grouped with this one`)) : null,
      h("p", { class: "note" }, "An existing name adds this face to that person.")), [
      { label: "Cancel" },
      { label: "Save", primary: true, submit: true, run: async () => {
        let name = input.value.trim();
        if (!name) return false;
        try {
          name = (await api(P(p, "/people/name"), { method: "POST", body: whole?.checked ? { name, cluster: f.cluster } : { name, faces: [f.id] } })).name || name;
        } catch (e) { toast(e.message, { kind: "error" }); return false; }
        store.peopleList = null;
        detailCache.clear();              // the group's other photos changed too
        toast(whole?.checked ? `Named ${name} on ${(others + 1).toLocaleString()} faces` : `Named ${name}`);
        paintInfo();
      } },
    ]);
    setTimeout(() => input.focus(), 0);
  }

  async function paintInfo() {
    if (info.hidden) return;
    const id = cur();
    clear(info).append(h("div", { class: "muted" }, "Loading…"));
    let d;
    try { d = await detail(id); } catch (e) { clear(info).append(h("div", { class: "muted" }, e.message)); return; }
    if (id !== cur()) return;
    const m = d.metrics || {};
    const r = d.row;
    // Scores read 0-100 like the "why" text; the raw measurement stays in the tooltip.
    const meter = (label, frac, val, raw) => h("div", { class: "meter", title: raw ?? null }, h("span", {}, label),
      h("span", { class: "track" }, h("i", { style: { width: `${Math.round((frac || 0) * 100)}%` } })), h("span", { class: "val" }, val ?? Math.round((frac || 0) * 100)));
    const ex = d.exif;
    const lib = (q) => `#/p/${encodeURIComponent(p)}/library?${q}`;
    const commentIn = h("input", { class: "input", placeholder: "Add a comment (who is this? print this one…)", "aria-label": "Comment" });
    clear(info).append(
      d.why?.length ? h("section", {},
        h("h4", {}, "Why"),
        h("ul", { class: "why" }, d.why.map((w) => h("li", { class: `w-${w.kind}` }, w.text)))) : null,
      h("section", {},
        h("h4", {}, "When and where"),
        h("div", { class: "kv" },
          h("span", {}, "Taken"), h("span", { class: "v" }, ex.capture_str ? when(ex.capture_str) : "No capture date",
            ex.tz_offset !== null && ex.tz_offset !== undefined ? h("span", { class: "faint" }, ` (UTC${ex.tz_offset >= 0 ? "+" : "−"}${Math.floor(Math.abs(ex.tz_offset) / 60)}:${String(Math.abs(ex.tz_offset) % 60).padStart(2, "0")})`) : null),
          d.clock_offset ? h("span", {}, "Clock") : null, d.clock_offset ? h("span", { class: "v" }, `corrected ${offsetLabel(d.clock_offset)}`) : null,
          ex.time_src === "file" ? h("span", {}, "") : null, ex.time_src === "file" ? h("span", { class: "v faint" }, "dated by when the file was saved") : null,
          d.place ? h("span", {}, "Place") : null,
          d.place ? h("a", { class: "v", ...leave(lib(`place=${d.place.id}`)) },
            `${d.place.name}${d.place.country ? ", " + d.place.country : ""}`, PLACE_HOW[d.place_src] ? h("span", { class: "faint" }, PLACE_HOW[d.place_src]) : null) : null,
          ex.gps_lat !== null && ex.gps_lat !== undefined ? h("span", {}, "") : null,
          ex.gps_lat !== null && ex.gps_lat !== undefined ? h("span", { class: "v" }, mapsLink(ex.gps_lat, ex.gps_lon, d.place?.name)) : null,
          d.event ? h("span", {}, "Event") : null,
          d.event ? h("a", { class: "v", ...leave(lib(`event=${d.event.id}`)) }, d.event.name) : null,
          d.trip ? h("span", {}, "Trip") : null,
          d.trip ? h("a", { class: "v", ...leave(`#/p/${encodeURIComponent(p)}/events?open=${d.trip.id}`) }, d.trip.name) : null)),
      h("section", {},
        h("h4", {}, "Capture"),
        h("div", { class: "kv" },
          h("span", {}, "Camera"), h("span", { class: "v" }, ex.camera || "—"),
          h("span", {}, "Lens"), h("span", { class: "v" }, ex.lens || "—"),
          h("span", {}, "Exposure"), h("span", { class: "v mono" }, d.exposure || "—"),
          h("span", {}, "Size"), h("span", { class: "v mono" }, ex.width ? `${ex.width} × ${ex.height}` : "—"),
          ex.duration ? h("span", {}, "Length") : null, ex.duration ? h("span", { class: "v mono" }, duration(ex.duration)) : null,
          ex.cam_rating ? h("span", {}, "In camera") : null, ex.cam_rating ? h("span", { class: "v" }, ex.cam_rating > 0 ? "★".repeat(ex.cam_rating) : "rejected") : null,
          d.software ? h("span", {}, "Saved by") : null, d.software ? h("span", { class: "v" }, d.software) : null,
          ex.c2pa ? h("span", {}, "Credentials") : null, ex.c2pa ? h("span", { class: "v" }, h("span", { class: "pill accent", title: "This file carries C2PA Content Credentials (signed provenance)" }, "Content Credentials")) : null)),
      m.sharp_subject !== undefined && r.media !== "video" ? h("section", {},
        h("h4", {}, "Assessment"),
        h("div", { class: "stack-v" },
          meter("Overall", (m.quality || 0) / 100, Math.round(m.quality ?? 0)),
          meter("Subject focus", ramp(m.sharp_subject, 1.8, 3.7), null, `Sharpness measure ${Math.round(m.sharp_subject)}`),
          m.face_sharp ? meter("Face focus", ramp(m.face_sharp, 1.3, 2.3), null, `Sharpness measure ${Math.round(m.face_sharp)}`) : null,
          meter("Brightness", (m.brightness || 0) / 255, null, `Average level ${Math.round(m.brightness)} of 255`),
          meter("Clipped highlights", Math.min(1, (m.clip_high || 0) * 5), `${((m.clip_high || 0) * 100).toFixed(1)}%`),
          m.taste !== null && m.taste !== undefined ? meter("Your taste", m.taste, `${Math.round(m.taste * 100)}%`) : null,
          m.aesthetic ? meter("Looks", Math.max(0, Math.min(1, (m.aesthetic - 4.6) / 1.6)), null, `How good it looks to the picture model: ${m.aesthetic.toFixed(2)} of 10`) : null)) : null,
      h("section", {}, h("h4", {}, "Keywords"), keywordBlock(cur(), d.keywords || [], () => paintInfo())),
      d.faces?.length ? h("section", {},
        h("h4", {}, `People · ${d.faces.length}`),
        h("div", { class: "row", style: { gap: "6px" } }, d.faces.map((f) => f.person_id > 0 ? h("a", {
          class: `face-chip ${f.closed ? "closed" : ""}`, title: f.name + (f.closed ? " · eyes closed" : ""),
          ...leave(lib(`person=${f.person_id}`)),
        }, h("img", { src: `/img/${encodeURIComponent(p)}/f/${f.id}`, alt: "", loading: "lazy" }),
          h("span", {}, f.name), f.closed ? h("span", { class: "faint" }, "eyes closed") : null)
          : h("button", { type: "button", class: `face-chip unnamed ${f.closed ? "closed" : ""}`, title: "Not named yet: say who this is",
            onclick: () => nameFace(f) },
          h("img", { src: `/img/${encodeURIComponent(p)}/f/${f.id}`, alt: "", loading: "lazy" }),
          h("span", { class: "faint" }, "Name…"))))) : null,
      d.versions?.length ? h("section", {},
        h("h4", {}, "Other versions"),
        h("div", { class: "stack-v" }, d.versions.map((v) => h("button", { type: "button", class: "version", onclick: () => openOther(v.shot_id) },
          h("img", { src: `/img/${encodeURIComponent(p)}/t/${v.shot_id}?s=s`, alt: "" }),
          h("span", {}, h("b", {}, v.stem), h("br"), h("span", { class: "faint" }, v.kind === "pair" ? "its RAW or JPEG, in another folder" : v.edited ? "an edited version" : "an export or renamed version")))))) : null,
      d.group.members.length > 1 ? h("section", {},
        h("h4", {}, `Stack · ${d.group.members.length} frames`),
        h("div", { class: "members" }, d.group.members.map((mid, k) => {
          const mr = row(mid);
          return h("button", { type: "button", class: "thumb", "aria-current": mid === id ? "true" : null,
            "aria-label": mid === id ? `Frame ${k + 1} of ${d.group.members.length}, this one` : `Open frame ${k + 1} of ${d.group.members.length}${mr ? `, ${mr.stem}` : ""}`,
            onclick: () => { const j = ids.indexOf(mid); if (j >= 0) go(j); else openOther(mid, d.group.members); } },
          h("img", { class: mid === id ? "cur" : "", alt: "", title: mr ? mr.stem : `Photo ${mid}`,
            src: mr ? img.thumb(p, mr) : `/img/${encodeURIComponent(p)}/t/${mid}?s=s` }));
        }))) : null,
      d.dup ? h("section", {},
        h("h4", {}, d.dup.kind === "exact" ? "Identical copies on this drive" : "Re-saved copies on this drive"),
        d.dup.members.map((dm) => h("div", { class: "path" }, `${dm.keep ? "kept · " : "extra copy · "}${dm.path}`))) : null,
      d.backups ? h("section", { class: "bk-info" }, h("h4", {}, "Backups"), backupsBlock(d.backups)) : null,
      h("section", {},
        h("h4", {}, `Comments${d.comments.length ? " · " + d.comments.length : ""}`),
        d.comments.map((c) => h("div", { class: "comment" }, h("b", {}, c.viewer === "owner" ? "You" : c.viewer), " ", c.text)),
        d.favs.length ? h("div", { class: "faint small" }, `♥ ${d.favs.map((v) => v === "owner" ? "you" : v).join(", ")}`) : null,
        h("form", { onsubmit: async (e) => {
          e.preventDefault();
          if (!commentIn.value.trim()) return;
          try { await api(P(p, "/comments"), { method: "POST", body: { shot_id: id, text: commentIn.value } }); detailCache.delete(id); paintInfo(); }
          catch (err) { toast(err.message, { kind: "error" }); }
        } }, commentIn)),
      h("section", {},
        h("h4", {}, "Files"),
        h("div", { class: "stack-v" }, d.files.map((f) => h("div", {},
          h("div", { class: "row", style: { gap: "8px" } },
            h("span", { class: "pill neutral" }, f.kind.toUpperCase()), h("span", { class: "muted small" }, bytes(f.size)),
            f.status === "present" ? h("a", { href: img.original(p, f.id), target: "_blank", rel: "noopener", class: "btn small ghost" }, "Open") : h("span", { class: "pill warn" }, "missing")),
          h("div", { class: "path", style: { marginTop: "4px" } }, f.path))))),
    );
  }

  /** Where else this photo's files are: a backup on another drive (and the library it's in), or the
   * only copy. One line per file when the photo has several (a RAW and its JPEG). */
  function backupsBlock(b) {
    if (!b.known) return h("div", { class: "muted small" }, "Not looked at yet: this library's files are compared with every other library's at its next update.");
    const many = b.files.length > 1;
    return h("div", { class: "stack-v" }, b.files.map((f) => h("div", { class: "bk-file-info" },
      many ? h("span", { class: "pill neutral" }, f.kind.toUpperCase()) : null,
      f.backups.length === 1 ? whereEl(f.backups[0], p, leave, h("span", { class: "bk-head-line" }, icon("shield"), "Backed up on "))
        : f.backups.length ? h("div", { class: "stack-v" },
          h("span", { class: "bk-head-line" }, icon("shield"), `Backed up in ${f.backups.length} places`),
          f.backups.slice(0, 3).map((c) => whereEl(c, p, leave)),
          f.backups.length > 3 ? h("span", { class: "faint small" }, `and ${f.backups.length - 3} more`) : null)
        : h("span", { class: "bk-note risk" }, icon("info"), f.same_drive.length
          ? "Only on this drive: not on any other drive JoeyLab knows (another copy is on this drive)"
          : "Only copy: not on any other drive JoeyLab knows"))));
  }

  function openOther(id, list) {
    const rows = list || [id];
    api(P(p, "/rows"), { params: { ids: rows.join(",") } }).then((res) => {
      remember(res.rows);
      openLoupe({ ids: rows, index: rows.indexOf(id) });
    }).catch(() => {});
  }

  // ------------------------------------------------------------ overlays (analysis only, pixels untouched)
  function cycleOverlay() {
    st.overlay = OVERLAYS[(OVERLAYS.indexOf(st.overlay) + 1) % OVERLAYS.length];
    setPref("overlay", st.overlay);
    paintOverlay();
    paintBar();
  }

  async function paintOverlay() {
    overlayCanvas.hidden = st.overlay !== "clipping";
    sharpGrid.hidden = st.overlay !== "sharp";
    hist.hidden = st.overlay !== "histogram";
    const r = row(cur());
    if (!r || r.media === "video" || !st.overlay) return;
    if (st.overlay === "sharp") {
      const want = cur();
      try {
        const s = await api(`/img/${encodeURIComponent(p)}/sharp/${want}`);
        if (want !== cur()) return;
        clear(sharpGrid).append(...s.grid.flatMap((rowv) => rowv.map((v) => h("i", { style: { opacity: (Math.max(0, v - 0.55) * 1.6).toFixed(2) } }))));
        sharpGrid.style.gridTemplateColumns = `repeat(${s.n}, 1fr)`;
      } catch { /* not analysed */ }
      return;
    }
    if (!picEl.complete || !picEl.naturalWidth) { picEl.addEventListener("load", paintOverlay, { once: true }); return; }
    const w = Math.min(800, picEl.naturalWidth), hgt = Math.round(w * picEl.naturalHeight / picEl.naturalWidth);
    const c = document.createElement("canvas");
    c.width = w; c.height = hgt;
    const ctx = c.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(picEl, 0, 0, w, hgt);
    let data;
    try { data = ctx.getImageData(0, 0, w, hgt); } catch { return; }
    const px = data.data;
    if (st.overlay === "histogram") {
      const bins = [new Uint32Array(256), new Uint32Array(256), new Uint32Array(256), new Uint32Array(256)];
      for (let k = 0; k < px.length; k += 4) {
        bins[0][px[k]]++; bins[1][px[k + 1]]++; bins[2][px[k + 2]]++;
        bins[3][Math.round(0.299 * px[k] + 0.587 * px[k + 1] + 0.114 * px[k + 2])]++;
      }
      const hc = hist.getContext("2d");
      hc.clearRect(0, 0, 256, 90);
      const max = Math.max(...bins[3]) || 1;
      const draw = (b, color) => {
        hc.fillStyle = color;
        for (let x = 0; x < 256; x++) { const v = Math.min(90, (b[x] / max) * 85); hc.fillRect(x, 90 - v, 1, v); }
      };
      hc.globalCompositeOperation = "lighter";
      draw(bins[0], "rgba(220,70,60,.55)"); draw(bins[1], "rgba(70,190,90,.55)"); draw(bins[2], "rgba(70,110,230,.55)");
      hc.globalCompositeOperation = "source-over";
      draw(bins[3], "rgba(235,235,230,.35)");
      return;
    }
    const out = ctx.createImageData(w, hgt);
    for (let k = 0; k < px.length; k += 4) {
      const l = 0.299 * px[k] + 0.587 * px[k + 1] + 0.114 * px[k + 2];
      if (l >= 250 || px[k] >= 254 || px[k + 1] >= 254 || px[k + 2] >= 254) { out.data.set([255, 40, 40, 200], k); }
      else if (l <= 5) { out.data.set([40, 90, 255, 190], k); }
    }
    overlayCanvas.width = w; overlayCanvas.height = hgt;
    overlayCanvas.getContext("2d").putImageData(out, 0, 0);
  }

  function show() {
    const r = row(cur());
    if (!r) return;
    titleB.textContent = r.stem;
    titleS.textContent = [when(r.capture_str), r.camera].filter(Boolean).join(" · ");
    // Said, as the photo changes: which it is, and what's been decided about it.
    announce([photoName(r), `${(st.i + 1).toLocaleString()} of ${(total?.() ?? ids.length).toLocaleString()}`,
      r.pick === 1 ? "picked" : r.pick === -1 ? "rejected" : "", r.rating ? plural(r.rating, "star") : ""].filter(Boolean).join(", "));
    counter.textContent = `${(st.i + 1).toLocaleString()} / ${(total?.() ?? ids.length).toLocaleString()}`;
    // A zoomed look carries on across the frames of one stack; another photo starts fitted.
    if (lock.level && st.shown && !(st.shown.group_id != null && st.shown.group_id === r.group_id)) lock.level = 0;
    st.shown = r;
    const isVideo = r.media === "video";
    videoBox.hidden = !isVideo;
    pc.el.hidden = isVideo;
    if (isVideo) {
      videoEl.poster = img.thumb(p, r, "m");
      api(P(p, `/shot/${r.id}`)).then((d) => {
        const f = d.files.find((x) => x.kind === "video" && x.status === "present");
        if (f && cur() === r.id) videoEl.src = img.original(p, f.id);
      }).catch(() => {});
    } else {
      videoEl.removeAttribute("src");
      videoEl.load?.();
      pc.show(p, r);
    }
    // The next photo decoded and ready, the ones around it fetched.
    const next = row(ids[st.i + 1]);
    if (next && next.media !== "video") preload(img.preview(p, next)).catch(() => {});
    prefetch([st.i - 1, st.i + 2, st.i + 3].map((k) => row(ids[k])).filter((x) => x && x.media !== "video").map((x) => img.preview(p, x)));
    paintZoom();
    paintBar();
    paintFilm();
    paintInfo();
    paintOverlay();
    onIndex?.(st.i);
    if (loadMore && st.i > ids.length - 10) loadMore();
  }

  function go(k) {
    if (k < 0 || k >= ids.length || k === st.i) return;
    st.i = k;
    show();
  }

  // ------------------------------------------------------------ zoom (Fit, 50%, 100%) and swipes
  function setZoom(level, at) {
    const r = row(cur());
    if (!r || r.media === "video") return;
    pc.setLevel(level, at);
    paintZoom();
  }
  function paintZoom() {
    zc.paint(lock.level);
    viewer.classList.toggle("zoomed", !!lock.level);
  }
  function toggleZoom(ev) { setZoom(lock.level ? 0 : 1, ev ? { x: ev.clientX, y: ev.clientY } : null); }

  let drag = null;
  viewer.addEventListener("pointerdown", (e) => {
    if (e.target.closest("button") || e.target === videoEl) return;
    drag = { x: e.clientX, y: e.clientY, t: Date.now(), type: e.pointerType };
    st.dragged = false;
    st.pointer = e.pointerType;
  });
  viewer.addEventListener("pointermove", (e) => {
    if (drag && Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) > 5) st.dragged = true;
  });
  viewer.addEventListener("pointerup", (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    const wasTouch = drag.type !== "mouse", quick = Date.now() - drag.t < 300;
    drag = null;
    if (!lock.level && wasTouch && Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.5) {
      go(st.i + (dx < 0 ? 1 : -1));
      st.dragged = true;
    } else if (wasTouch && quick && !st.dragged && e.target.closest(".pic")) {
      // A double tap zooms to 100% there (and back).
      if (Date.now() - st.tap < 320) { st.tap = 0; toggleZoom(e); } else st.tap = Date.now();
    }
  });
  pc.el.addEventListener("click", (e) => { if (!st.dragged && !pc.dragged() && st.pointer === "mouse") toggleZoom(e); });

  const off = on((type, changed) => {
    if (type === "keywords") {             // tagged from the panel, the dialog, or undone: show it now
      detailCache.clear();
      if (!info.hidden) paintInfo();
      return;
    }
    if (type === "gone") {                 // moved to the Trash while open here: go on to the next photo
      const gone = new Set(changed);
      if (!ids.some((id) => gone.has(id))) return;
      const was = cur(), before = ids.slice(0, st.i).filter((id) => !gone.has(id)).length;
      ids = ids.filter((id) => !gone.has(id));
      total = loadMore = null;               // a list of its own from now on: the page's has moved on
      if (!ids.length) { close(); return; }
      st.i = gone.has(was) ? Math.min(before, ids.length - 1) : ids.indexOf(was);
      show();
      return;
    }
    if (type !== "rows") return;
    if (changed.includes(cur())) { paintBar(); detailCache.delete(cur()); }
    paintFilm();
  });

  /** fromHistory: Back closed it. keepHistory/keepFocus: another photo takes over the viewer.
   * to: leave for that page (a hash), which takes the viewer's history entry. */
  function close({ fromHistory = false, keepHistory = false, keepFocus = false, to = null } = {}) {
    if (active?.close !== close) return;
    off();
    pc.destroy();
    window.removeEventListener("popstate", onPop);
    videoEl.pause?.();
    host.hidden = true;
    clear(host);
    active = null;
    document.body.style.overflow = "";
    if (!keepFocus) release({ restore: to === null });
    if (to !== null) {
      onClose?.(st.i);
      if (history.state?.joeylabLoupe) {
        // Replace the viewer's entry with the page, then route to it (replaceState fires no event).
        history.replaceState(null, "", to);
        window.dispatchEvent(new HashChangeEvent("hashchange"));
      } else location.hash = to;
      return;
    }
    // Opening pushed a history entry (so Back on a phone closes the viewer); take it off again.
    if (!fromHistory && !keepHistory && history.state?.joeylabLoupe) history.back();
    onClose?.(st.i);
  }
  function onPop() { if (active) close({ fromHistory: true }); }

  function onKey(e) {
    const k = e.key;
    if (e.metaKey || e.ctrlKey || e.altKey) return false;
    if (k === "Escape") { lock.level ? setZoom(0) : close(); return true; }
    if (k === "ArrowRight" || k === "l") { go(st.i + 1); return true; }
    if (k === "ArrowLeft" || k === "h") { go(st.i - 1); return true; }
    if (k === "Home") { go(0); return true; }
    if (k === "End") { go(ids.length - 1); return true; }
    if (k === "0") { clearDecision([cur()]); flash(viewer, "Cleared"); return true; }
    if (/^[1-5]$/.test(k)) { decide([{ id: cur(), rating: +k }], `${k}★`); return true; }
    if (LABEL_KEYS[k]) { setLabel([cur()], LABEL_KEYS[k]); return true; }
    if (k === "p") { pick(1); return true; }
    if (k === "x") { pick(-1); return true; }
    if (k === "f") { fav(); return true; }
    if (k === "t") { keywordDialog([cur()], () => paintInfo()); return true; }
    if (k === "b") { addToAlbum({ ids: [cur()], anchor: albumBtn }); return true; }
    if (k === "r" || k === "R") { toggleRaw(cur(), { back: k === "R" }); return true; }
    if (k === "o") { cycleOverlay(); return true; }
    if (k === "z") { toggleZoom(); return true; }
    if (k === "Z") { setZoom(lock.level === 0.5 ? 0 : 0.5); return true; }
    if (k === "i") { toggleInfo(); return true; }
    return false;
  }

  document.body.style.overflow = "hidden";
  active = { onKey, close, release };
  // Back (a phone's back gesture) closes the viewer instead of leaving the page you were on.
  if (!replacing) history.pushState({ joeylabLoupe: true }, "", location.href);
  window.addEventListener("popstate", onPop);
  if (!root.contains(document.activeElement)) root.focus({ preventScroll: true });
  show();
  if (zoom) setZoom(1);
  return active;
}
