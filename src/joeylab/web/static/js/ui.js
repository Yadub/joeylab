// Small DOM helpers, icons, toasts, dialogs and formatters shared by every view.

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") for (const [sk, sv] of Object.entries(v)) sk.startsWith("--") ? el.style.setProperty(sk, sv) : (el.style[sk] = sv);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function clear(el) { el.replaceChildren(); return el; }

// Views build content as `el.append(a, cond ? b : null, list.map(...))`. Native append would print
// "null" and stringify arrays, so within this app append skips empty values and flattens lists.
const nativeAppend = Element.prototype.append;
Element.prototype.append = function (...children) {
  const flat = children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false);
  return nativeAppend.apply(this, flat);
};
const nativeFragAppend = DocumentFragment.prototype.append;
DocumentFragment.prototype.append = function (...children) {
  return nativeFragAppend.apply(this, children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
};

const P = (d, extra = "") =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" ${extra}>${d}</svg>`;

export const ICONS = {
  star: `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 3.2l2.63 5.52 6.07.78-4.46 4.2 1.13 6.02L12 16.8l-5.37 2.92 1.13-6.02-4.46-4.2 6.07-.78z"/></svg>`,
  pick: P(`<path d="M5 21V4"/><path d="M5 4h11l-2 4 2 4H5"/>`),
  reject: P(`<path d="M6 6l12 12M18 6L6 18"/>`),
  check: P(`<path d="M5 12.5l4.2 4.2L19 7"/>`),
  search: P(`<circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/>`),
  close: P(`<path d="M6 6l12 12M18 6L6 18"/>`),
  left: P(`<path d="M15 5l-7 7 7 7"/>`),
  right: P(`<path d="M9 5l7 7-7 7"/>`),
  up: P(`<path d="M5 15l7-7 7 7"/>`),
  down: P(`<path d="M5 9l7 7 7-7"/>`),
  info: P(`<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.01"/>`),
  zoom: P(`<circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2M11 8v6M8 11h6"/>`),
  undo: P(`<path d="M9 14L4 9l5-5"/><path d="M4 9h10a6 6 0 010 12h-3"/>`),
  stack: P(`<rect x="7" y="7" width="13" height="13" rx="2"/><path d="M4 16V6a2 2 0 012-2h10"/>`),
  folder: P(`<path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z"/>`),
  download: P(`<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>`),
  sun: P(`<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>`),
  moon: P(`<path d="M20 14.5A8 8 0 019.5 4a8 8 0 1010.5 10.5z"/>`),
  auto: P(`<circle cx="12" cy="12" r="8.5"/><path d="M12 3.5v17" /><path d="M12 3.5a8.5 8.5 0 010 17z" fill="currentColor"/>`),
  filter: P(`<path d="M4 5h16l-6 8v6l-4-2v-4z"/>`),
  eyeOff: P(`<path d="M3 3l18 18"/><path d="M10.6 5.1A10 10 0 0112 5c5 0 8.5 4.5 9.5 7a13 13 0 01-2.7 3.9M6.6 6.6C4.6 8 3.2 10 2.5 12c1 2.5 4.5 7 9.5 7 1.6 0 3-.4 4.3-1.1"/>`),
  blur: P(`<circle cx="12" cy="12" r="3"/><circle cx="12" cy="12" r="7" stroke-dasharray="2 3"/>`),
  compare: P(`<rect x="3" y="5" width="8" height="14" rx="1.5"/><rect x="13" y="5" width="8" height="14" rx="1.5"/>`),
  refresh: P(`<path d="M20 11a8 8 0 10-2.3 5.7"/><path d="M20 4v7h-7"/>`),
  keep: P(`<path d="M12 3l2.4 5 5.6.6-4.2 3.8 1.2 5.6L12 15.2 7 18l1.2-5.6L4 8.6 9.6 8z"/>`),
  sparkle: P(`<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M6 18l2.5-2.5M15.5 8.5L18 6"/>`),
  menu: P(`<path d="M4 7h16M4 12h16M4 17h16"/>`),
  similar: P(`<rect x="3" y="3" width="8" height="8" rx="1.5"/><rect x="13" y="13" width="8" height="8" rx="1.5"/><path d="M13 7h4a2 2 0 012 2v2M11 17H7a2 2 0 01-2-2v-2"/>`),
  mark: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3.5" y="5" width="17" height="14" rx="2.5"/><path d="M3.5 15l4.5-4.5 4 4 3-3 5.5 5.5"/><circle cx="16.5" cy="9" r="1.5" fill="currentColor"/></svg>`,
  heart: P(`<path d="M12 20s-7-4.4-7-10a4 4 0 017-2.6A4 4 0 0119 10c0 5.6-7 10-7 10z"/>`),
  heartOn: `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 20s-7-4.4-7-10a4 4 0 017-2.6A4 4 0 0119 10c0 5.6-7 10-7 10z"/></svg>`,
  pin: P(`<path d="M12 21s-6-5.4-6-10.5a6 6 0 0112 0C18 15.6 12 21 12 21z"/><circle cx="12" cy="10.5" r="2.2"/>`),
  tag: P(`<path d="M3.5 12.2V4.5a1 1 0 011-1h7.7a1 1 0 01.7.3l8 8a1 1 0 010 1.4l-7.7 7.7a1 1 0 01-1.4 0l-8-8a1 1 0 01-.3-.7z"/><circle cx="8" cy="8" r="1.4"/>`),
  chart: P(`<path d="M4 20h16"/><rect x="6" y="11" width="3" height="7" rx="1"/><rect x="11" y="6" width="3" height="12" rx="1"/><rect x="16" y="9" width="3" height="9" rx="1"/>`),
  calendar: P(`<rect x="3.5" y="5" width="17" height="15" rx="2"/><path d="M3.5 10h17M8 3v4M16 3v4"/>`),
  people: P(`<circle cx="9" cy="8.5" r="3.2"/><path d="M3.5 19a5.5 5.5 0 0111 0"/><circle cx="17" cy="9.5" r="2.5"/><path d="M15.5 14.2A4.5 4.5 0 0121 18.5"/>`),
  broom: P(`<path d="M14 4l6 6M12.5 5.5l6 6-6.5 6.5H6v-6z"/><path d="M6 18l-2 2"/>`),
  album: P(`<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M4 15l4-4 4 4 3-3 5 5"/><path d="M8 4v-1h12a1 1 0 011 1v12"/>`),
  gear: P(`<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1.1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.5-1.1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z"/>`),
  history: P(`<path d="M3.5 12a8.5 8.5 0 102.5-6"/><path d="M3.5 4v4h4"/><path d="M12 8v4l3 2"/>`),
  play: `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.5v13l11-6.5z"/></svg>`,
  share: P(`<circle cx="6" cy="12" r="2.5"/><circle cx="18" cy="6" r="2.5"/><circle cx="18" cy="18" r="2.5"/><path d="M8.2 10.8l7.6-3.6M8.2 13.2l7.6 3.6"/>`),
  link: P(`<path d="M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 00-5.7 0l-3 3a4 4 0 005.7 5.7l1-1"/>`),
  wand: P(`<path d="M4 20L15 9M14 4v3M18.5 5.5l-2 2M20 10h-3"/>`),
  layers: P(`<path d="M12 4l9 5-9 5-9-5z"/><path d="M3 14l9 5 9-5"/>`),
  map: P(`<path d="M9 4L3 6v14l6-2 6 2 6-2V4l-6 2z"/><path d="M9 4v14M15 6v14"/>`),
  explore: P(`<circle cx="12" cy="12" r="8.5"/><path d="M15.5 8.5l-2 5-5 2 2-5z"/>`),
  home: P(`<path d="M4 11l8-7 8 7"/><path d="M6 9.5V20h12V9.5"/>`),
  grid: P(`<rect x="4" y="4" width="7" height="7" rx="1"/><rect x="13" y="4" width="7" height="7" rx="1"/><rect x="4" y="13" width="7" height="7" rx="1"/><rect x="13" y="13" width="7" height="7" rx="1"/>`),
  plus: P(`<path d="M12 5v14M5 12h14"/>`),
  cmd: P(`<path d="M9 6a3 3 0 10-3 3h12a3 3 0 10-3-3v12a3 3 0 103-3H6a3 3 0 103 3z"/>`),
  shield: P(`<path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z"/><path d="M9 12l2 2 4-4"/>`),
  clock: P(`<circle cx="12" cy="12" r="8.5"/><path d="M12 7v5l3.5 2"/>`),
  faces: P(`<circle cx="8" cy="10" r="3"/><circle cx="16" cy="10" r="3"/><path d="M3 19c.8-2.5 2.8-4 5-4M21 19c-.8-2.5-2.8-4-5-4"/>`),
  overlay: P(`<rect x="3.5" y="5" width="17" height="14" rx="2"/><path d="M3.5 15l5-5 4 4 3-3 5 5"/><path d="M14 5v14" stroke-dasharray="2 2"/>`),
  check2: P(`<circle cx="12" cy="12" r="8.5"/><path d="M8 12.5l2.7 2.7L16 10"/>`),
};

export const LABEL_COLORS = { Red: "#C8564B", Yellow: "#D4A72C", Green: "#5E9A61", Blue: "#4E7FB8", Purple: "#8A65B5" };
export const LABELS = Object.keys(LABEL_COLORS);
export function labelDot(label) {
  return label && LABEL_COLORS[label] ? h("span", { class: "label-dot", title: `${label} label`, style: { background: LABEL_COLORS[label] } }) : null;
}

/** Colours from the stylesheet for drawing on a canvas: `color("--pick")` as the browser shows it
 * inside `el` (a token is a light-dark() pair that only an element can resolve). */
export function cssColors(el = document.body) {
  const seen = new Map();
  return (name) => {
    if (!seen.has(name)) {
      const probe = el.appendChild(h("i", { hidden: true, style: { color: `var(${name})` } }));
      seen.set(name, getComputedStyle(probe).color);
      probe.remove();
    }
    return seen.get(name);
  };
}

export function icon(name, cls = "") {
  return h("span", { class: `icon ${cls}`, "aria-hidden": "true", html: ICONS[name] || "" });
}

/** Five stars to click: one Tab stop, the arrows move along them (a click on the lit one clears it). */
export function starsEl(rating, onSet, { size, label = "Stars" } = {}) {
  const wrap = h("span", { class: "stars", role: "group", "aria-label": label });
  for (let i = 1; i <= 5; i++) {
    const b = h("button", {
      type: "button", class: i <= (rating || 0) ? "on" : "", title: `${i} star${i > 1 ? "s" : ""} (${i})`,
      "aria-pressed": i === (rating || 0) ? "true" : "false", tabindex: i === (rating || 1) ? "0" : "-1",
      "aria-label": `${i} star${i > 1 ? "s" : ""}`,
      onclick: (e) => { e.stopPropagation(); onSet && onSet(i === rating ? 0 : i); },
    }, icon("star"));
    if (size) b.firstChild.style.width = b.firstChild.style.height = size;
    wrap.append(b);
  }
  return roving(wrap, { choose: false });
}

/** Put the keyboard on an element: a control, or the start of a part of the page (a skip link's target). */
export function focusOn(el) {
  if (!el) return;
  if (!el.matches('a[href], button, input, select, textarea, [tabindex]')) el.tabIndex = -1;
  el.focus();
}

/** A photo in words, for a screen reader and a button's name: "DSCF0103, 14 Jun 2025". */
export function photoName(r) {
  if (!r) return "Photo";
  return r.capture_str && r.time_src !== "file" ? `${r.stem}, ${when(r.capture_str).split(" · ")[0]}` : r.stem;
}

// ------------------------------------------------------------------ groups of buttons
// A row of buttons that is one thing (a choice of one, page tabs, stars) is one Tab stop: the
// arrows move along it, Home and End go to its ends. `choose`: moving also presses the button
// (as radio buttons do); otherwise Enter or Space does.
const ROVE = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
export function roving(group, { choose = true, sel = "button" } = {}) {
  const items = () => [...group.querySelectorAll(sel)].filter((b) => !b.disabled && !b.hidden);
  if (!items().some((b) => b.tabIndex === 0)) { const first = items()[0]; if (first) first.tabIndex = 0; }
  group.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const list = items();
    const i = list.indexOf(document.activeElement);
    if (i < 0) return;
    const to = e.key in ROVE ? i + ROVE[e.key] : e.key === "Home" ? 0 : e.key === "End" ? list.length - 1 : null;
    if (to === null) return;
    e.preventDefault();
    e.stopPropagation();                       // the page's own arrow keys (the grid's) stay out of it
    const b = list[(to + list.length) % list.length];
    list.forEach((x) => { x.tabIndex = x === b ? 0 : -1; });
    b.focus();
    if (choose && b.getAttribute("aria-checked") !== "true") b.click();
  });
  return group;
}

/** One of several, as a segmented control: [[value, text, attrs?]]. */
export function choiceSeg(options, current, onChoose, { label, cls = "seg" } = {}) {
  const has = options.some(([v]) => v === current);
  const group = h("div", { class: cls, role: "radiogroup", "aria-label": label },
    options.map(([v, t, attrs], i) => h("button", { type: "button", role: "radio", ...(attrs || {}),
      "aria-checked": v === current ? "true" : "false", tabindex: v === current || (!has && !i) ? "0" : "-1",
      onclick: () => onChoose(v) }, t)));
  return roving(group);
}

/** Page tabs: [[key, text, attrs?]]. ← → move between them, Enter or Space opens one. `id` names
 * them (`${id}-tab-${key}`), so the keyboard stays on the tab after the page redraws; `panel` is
 * the element the selected tab shows. */
export function tabBar(tabs, current, onSelect, { label, id, panel } = {}) {
  const tid = (k) => (id ? `${id}-tab-${k}` : null);
  if (panel && id) {
    panel.id ||= `${id}-panel`;
    panel.setAttribute("role", "tabpanel");
    panel.setAttribute("aria-labelledby", tid(current));
  }
  const bar = h("div", { class: "seg", role: "tablist", "aria-label": label },
    tabs.map(([k, t, attrs]) => h("button", { type: "button", role: "tab", id: tid(k), ...(attrs || {}),
      "aria-selected": k === current ? "true" : "false", tabindex: k === current ? "0" : "-1",
      "aria-controls": k === current && panel ? panel.id : null, onclick: () => onSelect(k) }, t)));
  return roving(bar, { choose: false });
}

// ------------------------------------------------------------------ toasts
/** A short message at the bottom of the page. `key` keeps one message of a kind (a newer one replaces
 * it); `persist` keeps it until its button is pressed or `dismissToast(key)` is called. */
export function toast(msg, { kind = "", action, actionLabel, actions = [], ms = 4200, key, persist = false } = {}) {
  // A modal dialog sits above everything else on the page, so while one is open its toasts (and
  // their Undo buttons) go inside it.
  const dlg = [...document.querySelectorAll("dialog[open]")].pop();
  const host = dlg ? (dlg.querySelector(".dlg-toasts") || dlg.appendChild(h("div", { class: "dlg-toasts" })))
    : document.getElementById("toasts");
  if (key) dismissToast(key);
  announce(typeof msg === "string" ? msg : msg?.textContent || "", { urgent: kind === "error" });
  const el = h("div", { class: `toast ${kind}`, dataset: key ? { key } : {} }, h("span", {}, msg));
  if (action) {
    el.append(h("button", { type: "button", onclick: () => { action(); el.remove(); } }, actionLabel || "Undo"));
  }
  for (const a of actions) el.append(h("button", { type: "button", onclick: () => { a.run(); el.remove(); } }, a.label));
  if (persist) el.append(h("button", { type: "button", class: "toast-x", "aria-label": "Dismiss", onclick: () => el.remove() }, icon("close")));
  host.append(el);
  if (!persist) setTimeout(() => el.remove(), kind === "error" ? ms * 1.6 : ms);
  return el;
}
export function dismissToast(key) {
  document.querySelectorAll(`.toast[data-key="${key}"]`).forEach((t) => t.remove());
}

/** Say something to a screen reader. Two regions that stay on the page (one waits its turn, one
 * interrupts) are read more reliably than a message that arrives in an element of its own. */
export function announce(text, { urgent = false } = {}) {
  if (!text) return;
  const id = urgent ? "announce-now" : "announce";
  let el = document.getElementById(id);
  if (!el) {
    el = h("div", { id, class: "sr-only", role: urgent ? "alert" : "status", "aria-live": urgent ? "assertive" : "polite" });
    document.body.append(el);
  }
  // Inside an open dialog the page behind it is hidden from a screen reader, so its region speaks.
  const dlg = [...document.querySelectorAll("dialog[open]")].pop();
  const host = dlg ? dlg.querySelector(`.${id}`) || dlg.appendChild(h("div", { class: `sr-only ${id}`, role: urgent ? "alert" : "status" })) : el;
  host.textContent = "";
  setTimeout(() => { host.textContent = text; }, 60);      // a change, even when it says the same again
}

// ------------------------------------------------------------------ dialogs
let dialogs = 0;
export function dialog(title, body, actions = []) {
  const titleId = `dlg-title-${++dialogs}`;
  const dlg = h("dialog", { class: "dlg", "aria-labelledby": titleId });
  const close = (v) => { dlg.close(v || ""); };
  const bar = h("div", { class: "dlg-actions" },
    ...actions.map((a) => h("button", {
      type: a.submit ? "submit" : "button", class: `btn ${a.primary ? "primary" : ""}`,
      onclick: a.submit ? null : async () => { if ((await a.run?.(close)) !== false) close(a.value); },
    }, a.label)));
  const form = h("form", {
    class: "dlg-body", method: "dialog",
    onsubmit: async (e) => {
      e.preventDefault();
      const a = actions.find((x) => x.submit);
      if (a && (await a.run?.(close)) !== false) close(a.value);
    },
  }, h("h3", { id: titleId }, title), body, bar);
  dlg.append(form);
  dlg.addEventListener("close", () => {
    // Messages raised while it was open (often "Saved · Undo" from the very step that closed it)
    // carry on outside it, Undo button and all.
    const page = document.getElementById("toasts");
    dlg.querySelectorAll(".dlg-toasts > .toast").forEach((t) => page?.append(t));
    dlg.remove();
  });
  document.body.append(dlg);
  dlg.showModal();
  return dlg;
}

/** Ask for one line of text in the app's own dialog. Resolves to the text, or null if cancelled. */
export function ask(title, { label = "", value = "", placeholder = "", note = "", ok = "Save", suggestions = [] } = {}) {
  return new Promise((resolve) => {
    let done = false;
    const input = h("input", { class: "input", value, placeholder, "aria-label": label || title });
    // Ready-made answers ("Dance performance", the folder's name): one click fills the box.
    const picks = suggestions.filter(Boolean).length ? h("div", { class: "row ask-suggestions" }, h("span", { class: "muted small" }, "Use:"),
      suggestions.filter(Boolean).map((s) => h("button", { type: "button", class: "chip", onclick: () => { input.value = s; input.focus(); } }, s))) : null;
    const dlg = dialog(title, h("div", { class: "stack-v" }, note ? h("p", { class: "note" }, note) : null,
      label ? h("label", { class: "field" }, h("span", {}, label), input) : input, picks), [
      { label: "Cancel", run: () => { done = true; resolve(null); } },
      { label: ok, primary: true, submit: true, run: () => { done = true; resolve(input.value.trim()); } },
    ]);
    dlg.addEventListener("close", () => { if (!done) resolve(null); });
    setTimeout(() => { input.focus(); input.select(); }, 0);
  });
}

// ------------------------------------------------------------------ overlays
// The viewer, ⌘K and Compare cover the page: while one is open, the page behind it can't take focus
// (Tab stays inside the overlay) and, when it closes, focus goes back where it was.
export function holdFocus(overlay) {
  const opener = document.activeElement;
  // (What an outer overlay already holds stays held when an inner one closes.)
  const behind = ["skip", "topbar", "view", "loupe"].map((id) => document.getElementById(id))
    .filter((el) => el && !el.contains(overlay) && !el.inert);
  behind.forEach((el) => { el.inert = true; });
  return ({ restore = true } = {}) => {
    behind.forEach((el) => { el.inert = false; });
    if (restore && opener?.isConnected && opener !== document.body) opener.focus({ preventScroll: true });
  };
}

// ------------------------------------------------------------------ popovers
// A list of actions (menu items) is a menu: focus goes to its first item and the arrows move
// through them. Anything else (History, the job, the album picker) is a small dialog that takes
// focus. Either way Esc closes it, and focus goes back to the button that opened it; Tab past
// its end closes it and carries on from that button.
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select, textarea, [tabindex]:not([tabindex="-1"])';
let openPop = null;
export function popover(anchor, content, { label } = {}) {
  closePopover({ refocus: false });
  const r = anchor.getBoundingClientRect();
  const el = h("div", { class: "popover", tabindex: "-1" }, content);
  document.body.append(el);
  const w = el.offsetWidth, ht = el.offsetHeight;
  // Under its button, or above it when there's no room below (a bar at the foot of the screen).
  const above = r.bottom + 6 + ht > window.innerHeight - 8 && r.top - ht - 6 >= 8;
  el.style.top = `${above ? r.top - ht - 6 : r.bottom + 6}px`;
  el.style.left = `${Math.max(16, Math.min(r.left, window.innerWidth - w - 16))}px`;
  const menu = !!el.querySelector('[role="menuitem"]');
  el.setAttribute("role", menu ? "menu" : "dialog");
  const name = label || anchor.getAttribute("aria-label") || anchor.title || anchor.textContent.trim();
  if (name) el.setAttribute("aria-label", name);
  if (anchor.isConnected) {
    if (!anchor.hasAttribute("aria-haspopup")) anchor.setAttribute("aria-haspopup", menu ? "menu" : "dialog");
    anchor.setAttribute("aria-expanded", "true");
  }
  const items = () => [...el.querySelectorAll(menu ? '[role="menuitem"]' : FOCUSABLE)].filter((x) => !x.disabled && x.offsetParent !== null);
  const onDoc = (e) => { if (!el.contains(e.target) && !anchor.contains(e.target)) closePopover({ refocus: false }); };
  // Esc closes it even when the keyboard isn't in it (it was opened with the mouse).
  const onEsc = (e) => {
    if (e.key !== "Escape" || document.querySelector("dialog[open]")) return;
    e.preventDefault(); e.stopPropagation();
    closePopover();
  };
  el.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const list = items();
    const i = list.indexOf(document.activeElement);
    if (e.key === "Tab") {
      const out = e.shiftKey ? i <= 0 : i === list.length - 1;
      if (out || !list.length) { closePopover(); }       // Tab carries on from the button
      return;
    }
    if (!menu || !["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
    e.preventDefault(); e.stopPropagation();
    const to = e.key === "Home" ? 0 : e.key === "End" ? list.length - 1 : i + (e.key === "ArrowDown" ? 1 : -1);
    list[(to + list.length) % list.length]?.focus();
  });
  setTimeout(() => document.addEventListener("pointerdown", onDoc), 0);
  document.addEventListener("keydown", onEsc, true);
  openPop = { el, anchor, off: () => { document.removeEventListener("pointerdown", onDoc); document.removeEventListener("keydown", onEsc, true); } };
  // Into it, once its caller has filled it (a few fill it after a fetch, or focus a box themselves).
  setTimeout(() => {
    if (openPop?.el !== el || el.contains(document.activeElement)) return;
    (menu && items()[0] || el).focus({ preventScroll: true });
  }, 0);
  return el;
}
export function closePopover({ refocus = true } = {}) {
  if (!openPop) return;
  const { el, anchor, off } = openPop;
  openPop = null;
  off();
  const had = el.contains(document.activeElement);
  el.remove();
  if (anchor.isConnected) {
    anchor.setAttribute("aria-expanded", "false");
    if (refocus && had) anchor.focus({ preventScroll: true });
  }
}

// ------------------------------------------------------------------ formatters
export const nf = new Intl.NumberFormat();
export const n = (x) => nf.format(x ?? 0);
/** "1 photo", "2 photos" (or the plural given: plural(k, "person", "people")). */
export const plural = (k, one, many) => `${n(k)} ${k === 1 ? one : (many || one + "s")}`;
/** The word that agrees with a count: `${plural(k, "photo")} ${agree(k, "is", "are")} missing`. */
export const agree = (k, one, many) => (k === 1 ? one : many);
export function bytes(b) {
  if (!b) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(u.length - 1, Math.floor(Math.log(b) / Math.log(1000)));
  return `${(b / 1000 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
}
export function ago(ts) {
  if (!ts) return "never";
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}
export function when(capture) {
  if (!capture) return "No date";
  const [d, t] = capture.split(" ");
  const dt = new Date(`${d}T${t || "00:00:00"}`);
  if (isNaN(dt)) return capture;
  return dt.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) +
    (t ? " · " + t.slice(0, 5) : "");
}

export const FLAG_LABELS = {
  "blurry": "Blurry", "soft": "Soft focus", "eyes-closed": "Eyes closed", "overexposed": "Overexposed",
  "underexposed": "Underexposed", "duplicate": "Duplicate", "backup-copy": "Backup copy on another drive", "best-in-group": "Best in its stack",
  "softer-than-twin": "Softer than a near-identical frame", "tiny": "Thumbnail-sized", "utility": "Screenshot or document",
  "rescuable": "Fixable in editing", "slightly-softer": "A bit softer than a near-identical frame", "damaged": "Damaged file",
  "darker-than-burst": "Darker than the rest of its burst", "brighter-than-burst": "Brighter than the rest of its burst",
};
/** How a suggestion reads on a pill or badge. */
export const sugLabel = (s) => ({ pick: "Suggested pick", reject: "Suggested reject", check: "Worth checking" }[s] || s);

export function dateRange(a, b) {
  if (!a) return "";
  const fa = new Date(a * 1000), fb = new Date((b || a) * 1000);
  const opt = { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" };
  if (fa.toDateString() === fb.toDateString()) return fa.toLocaleDateString(undefined, opt);
  if (fa.getUTCFullYear() === fb.getUTCFullYear())
    return `${fa.toLocaleDateString(undefined, { day: "numeric", month: "short", timeZone: "UTC" })} – ${fb.toLocaleDateString(undefined, opt)}`;
  return `${fa.toLocaleDateString(undefined, opt)} – ${fb.toLocaleDateString(undefined, opt)}`;
}
/** An event card's second line: the dates, unless the name already carries them. */
export function eventSub(e) {
  const dates = dateRange(e.start_ts, e.end_ts);
  const count = plural(e.n || 0, "photo");
  return /\b(19|20)\d\d\b/.test(e.name || "") ? count : `${dates} · ${count}`;
}
export function monthLabel(ym) {
  if (!ym || ym === "undated") return "No capture date";
  const [y, m] = ym.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, 1)).toLocaleDateString(undefined, { month: "long", year: "numeric", timeZone: "UTC" });
}
export function duration(s) {
  if (!s) return "";
  const m = Math.floor(s / 60), r = Math.round(s % 60);
  return `${m}:${String(r).padStart(2, "0")}`;
}
export function offsetLabel(s) {
  if (!s) return "no correction";
  const sign = s > 0 ? "+" : "−", a = Math.abs(s);
  const hh = Math.floor(a / 3600), mm = Math.floor((a % 3600) / 60), ss = Math.round(a % 60);
  return `${sign}${hh ? hh + "h " : ""}${mm ? mm + "m " : ""}${ss}s`.trim();
}
export const flagLabel = (f) => FLAG_LABELS[f] || f;

export function debounce(fn, ms = 250) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

// Only places that take text swallow shortcut keys: a checkbox, slider or button that has focus
// (because you just clicked it) must not switch P, X, 1-5 and the arrows off. A <select> keeps
// its arrow keys and letters (they choose an option) but lets the rest through.
const TEXT_TYPES = new Set(["text", "search", "email", "url", "tel", "password", "number", "date", "datetime-local", "month", "time", "week"]);
export function isTyping(e) {
  const t = e.target;
  if (!t) return false;
  if (t.tagName === "TEXTAREA" || t.isContentEditable) return true;
  if (t.tagName === "INPUT") return TEXT_TYPES.has((t.type || "text").toLowerCase());
  if (t.tagName === "SELECT") return e.key.length === 1 || e.key.startsWith("Arrow") || e.key === "Enter" || e.key === " ";
  return false;
}

/** A key the focused control answers itself: Enter or Space presses a button, link or checkbox, and
 * the arrows move a slider. When the keyboard brought focus there, the page's own shortcuts leave
 * those keys to it (Enter on "Select all" selects all, rather than opening the photo in focus).
 * After a click on a button, Enter and Space still do what they do on the page (next stack, open). */
export function controlKey(e) {
  const t = e.target;
  if (!t || !t.matches || e.metaKey || e.ctrlKey || e.altKey || !byKey.has(t)) return false;
  const k = e.key;
  if (t.tagName === "INPUT" && t.type === "range") return k.startsWith("Arrow") || ["Home", "End", "PageUp", "PageDown"].includes(k);
  if (k !== "Enter" && k !== " ") return false;
  return t.matches('button, a[href], summary, input, [role="button"], [role="tab"], [role="radio"], [role="menuitem"], [role="checkbox"], [role="switch"], [role="option"]');
}

// ------------------------------------------------------------------ keeping the keyboard's place
// Pages draw parts of themselves again after a change (the Library's filters, a list after Undo,
// tabs). The control the keyboard was on is then gone, and focus would fall back to the top of the
// page. Instead it goes to the control drawn in its place: the same id, or the same kind of control
// with the same words in the same part of the page. Only after a key, not a click.
let lastInput = "pointer";
const byKey = new WeakSet();        // elements the keyboard (not a click) last brought focus to
let held = null;                    // {el, key, nth, scope, lost}
const LOST_FOR = 1500;              // ms a lost place is looked for, once nothing else has focus
const words = (el) => (el.getAttribute("aria-label") || el.textContent || el.getAttribute("placeholder") || el.title || "")
  .replace(/[\d.,]+/g, "#").replace(/\s+/g, " ").trim().slice(0, 80);
function keyOf(el) {
  if (el.id) return `#${el.id}`;
  const group = el.parentElement?.closest('[role="radiogroup"], [role="tablist"], [role="group"], [role="toolbar"], [role="menu"]');
  return [el.tagName, el.getAttribute("role") || "", group?.getAttribute("aria-label") || "", words(el)].join("|");
}
function sameIn(scope, key, tag) {
  return [...scope.querySelectorAll(tag)].filter((x) => keyOf(x) === key);
}
/** Forget the place (a new page is opening). */
export function forgetFocus() { held = null; }
export function keepFocus() {
  document.addEventListener("keydown", (e) => { if (!["Shift", "Meta", "Control", "Alt"].includes(e.key)) lastInput = "key"; }, true);
  document.addEventListener("pointerdown", () => { lastInput = "pointer"; }, true);
  document.addEventListener("focusin", (e) => {
    const el = e.target;
    if (!(el instanceof Element)) return;
    if (lastInput === "key") byKey.add(el); else byKey.delete(el);
    if (!el.closest("#view, #topbar") || el.closest("dialog, .popover")) return;
    const scope = el.parentElement?.closest("[id]") || document.body;
    const key = keyOf(el);
    held = { el, key, tag: el.tagName, scope: scope.id, nth: Math.max(0, sameIn(scope, key, el.tagName).indexOf(el)), lost: 0 };
  }, true);
  new MutationObserver(() => {
    if (!held || held.el.isConnected) return;
    if (lastInput !== "key") { held = null; return; }
    const active = document.activeElement;
    if (active && active !== document.body) return;              // a dialog has it, or the page moved it on purpose
    held.lost ||= Date.now();
    if (Date.now() - held.lost > LOST_FOR) { held = null; return; }
    const scope = document.getElementById(held.scope) || document.body;
    const found = held.key.startsWith("#") ? [document.getElementById(held.key.slice(1))].filter(Boolean) : sameIn(scope, held.key, held.tag);
    const to = found[Math.min(held.nth, found.length - 1)];
    if (to) to.focus({ preventScroll: true });
  }).observe(document.body, { childList: true, subtree: true });
}
