// Events and trips, found from time, place and folders. Name them, merge them, hide them, say where
// they were, share them, open them. Which events are trips follows your homes (places.js).

import { api, img, P } from "../api.js";
import { thumbButton } from "../grid.js";
import { openLoupe } from "../loupe.js";
import { mapsLink } from "../maps.js";
import { remember, row, store } from "../store.js";
import { ask, choiceSeg, clear, closePopover, dateRange, dialog, dismissToast, eventSub, h, icon, n, plural, popover, toast } from "../ui.js";
import { groupKeys } from "./common.js";
import { groupRawsDialog } from "./raws.js";
import { placeLabel, placesSettingsHref, placeWhy, ruleText, townSearch, whenTripsFound } from "./places.js";
import { waitNote } from "./waiting.js";

/** The keys, for the shortcuts list. */
export const KEYS = { name: "Events", rows: [["J / K", "Next / previous event or trip"], ["Enter", "Open it"]] };

const names = new Map();       // event id -> name, for the Library's filter chip
export const eventName = (id) => names.get(String(id));
const noteNames = (evs) => evs.forEach((e) => names.set(String(e.id), e.name));

/** Its photos' RAWs: keep or reject them all, and move the rejected ones to the Trash. */
export const eventRaws = (e, onChange) => groupRawsDialog({ filter: { [e.kind === "trip" ? "trip" : "event"]: e.id, all: 1 }, title: e.name, onChange });

/** Rename, Merge with…, RAWs… and Hide (or Show again) for one event or trip, as a menu. */
export function eventMenu(anchor, e, onChange) {
  const item = (label, fn) => h("button", { type: "button", class: "menu-item", role: "menuitem", onclick: () => { closePopover(); fn(); } }, label);
  popover(anchor, [
    item("Rename…", () => renameEvent(e, onChange)),
    item("Merge with…", () => mergeEvent(e, onChange)),
    item("Set place…", () => setPlace(e, onChange)),
    item("RAWs…", () => eventRaws(e, onChange)),
    item(e.hidden ? "Show again" : "Hide", () => hideEvent(e, !e.hidden, onChange)),
  ]);
}

/** Say where an event or trip was: a town for its photos without GPS, which beats any guess and
 * stays after every update. Trips are found again afterwards; Undo puts back what was there. */
export function setPlace(e, onChange) {
  const p = store.project;
  const cur = e.place;
  const apply = async (pid, name) => {
    try {
      const r = await api(P(p, `/events/${e.id}/place`), { method: "POST", body: { place_id: pid } });
      const done = () => whenTripsFound(p, () => onChange?.());
      toast(pid ? `${name} is where ${plural(r.n, "photo")} ${r.n === 1 ? "was" : "were"} taken${r.gps ? ` (${n(r.gps)} with GPS keep their own place)` : ""}. Finding trips again…`
        : "Your place is taken away. Finding trips again…",
      { action: async () => { await api(P(p, `/events/${e.id}/place`), { method: "POST", body: { place_id: r.previous } }); done(); }, actionLabel: "Undo", ms: 8000 });
      done();
    } catch (err) { toast(err.message, { kind: "error" }); }
  };
  const search = townSearch((t) => { dlg.close(); apply(t.id, t.name); }, { placeholder: "Search for a town", label: "Town" });
  const dlg = dialog(`Where was “${e.name}”?`, h("div", { class: "stack-v" },
    cur ? h("p", {}, h("span", { class: "muted" }, "Now: "), cur.src === "clock" ? placeWhy(cur) : placeWhy({ ...cur, away: null })) : null,
    h("p", { class: "note" }, `Its photos without GPS get the town you choose. That replaces any guess and stays after every update; photos with GPS keep their own place.`),
    search.el), [
    ...(cur?.src === "manual" ? [{ label: "Take my place away", run: () => apply(null) }] : []),
    { label: "Cancel" },
  ]);
  setTimeout(() => search.input.focus(), 0);
}

/** An event's other names, beside its own: what its photos show ("Dance performance in Delhi") and the
 * folder they're in ("SD Card 20251110"), each only when its name doesn't already say it. */
export function otherNames(e) {
  const name = String(e.name || "").toLowerCase();
  const core = (s) => String(s || "").replace(/^\s*\d{4}([-_. ]\d{1,2}){0,2}\s*[-_.]?\s*/, "").toLowerCase().trim();
  const alt = e.alt_name && !name.includes(e.alt_name.toLowerCase()) ? e.alt_name : null;
  const folder = e.folder && core(e.folder) && !name.includes(core(e.folder).slice(0, 24)) ? e.folder : null;
  return [
    alt ? h("span", { class: "event-alt", title: "What its photos show" }, alt) : null,
    folder ? h("span", { class: "event-folder faint", title: "The folder its photos are in" }, icon("folder"), folder) : null,
  ].filter(Boolean);
}

export async function renameEvent(e, onChange) {
  const folderName = e.folder && !/^\s*\d{4}/.test(e.folder) ? e.folder : e.folder?.replace(/^\s*\d{4}([-_. ]\d{1,2}){0,2}\s*[-_.]?\s*/, "");
  const name = await ask(`Name this ${e.kind}`, { value: e.user_named ? e.name : "", placeholder: e.auto_name,
    note: "Leave empty to go back to the automatic name.",
    suggestions: [...new Set([e.alt_name, folderName].filter((s) => s && s !== e.name))] });
  if (name === null) return;
  try {
    await api(P(store.project, `/events/${e.id}/name`), { method: "POST", body: { name } });
    names.set(String(e.id), name || e.auto_name);
    onChange?.();
  } catch (err) { toast(err.message, { kind: "error" }); }
}

export async function hideEvent(e, hidden, onChange) {
  const p = store.project;
  try {
    await api(P(p, `/events/${e.id}/hidden`), { method: "POST", body: { hidden } });
    toast(hidden ? `Hid “${e.name}” from Events and Home. Its photos stay in the Library.` : `“${e.name}” is shown again`,
      { action: async () => { await api(P(p, `/events/${e.id}/hidden`), { method: "POST", body: { hidden: !hidden } }); onChange?.(); }, actionLabel: "Undo" });
    onChange?.();
  } catch (err) { toast(err.message, { kind: "error" }); }
}

export async function mergeEvent(e, onChange) {
  const p = store.project;
  let d;
  try { d = await api(P(p, `/events/${e.id}/neighbours`)); } catch (err) { toast(err.message, { kind: "error" }); return; }
  const word = e.kind === "trip" ? ["trip", "trips"] : ["event", "events"];
  if (!d.events.length) { toast(`There's no other ${word[0]} to merge it with`); return; }
  const others = d.events;
  const at = others.findIndex((o) => o.start_ts > e.start_ts);
  const options = others.map((o, i) => {
    const between = at === -1 ? others.length - 1 - i : i < at ? at - 1 - i : i - at;
    return h("label", { class: "check merge-option" },
      h("input", { type: "radio", name: "merge-with", value: o.id, checked: i === (at === -1 ? others.length - 1 : Math.max(0, at - 1)) }),
      h("span", {}, h("b", {}, o.name), h("span", { class: "muted" }, ` · ${eventSub(o)}`
        + (between ? ` · and the ${plural(between, word[0], word[1])} between` : "") + (o.hidden ? " · hidden" : ""))));
  });
  const box = h("div", { class: "stack-v merge-list" }, options);
  dialog(`Merge “${e.name}” with…`, h("div", { class: "stack-v" },
    h("p", { class: "note" }, `The ${word[1]} just before and after it. They become one, ${e.user_named ? `called “${e.name}”` : "named from all their photos (rename it afterwards if you like)"}, and stay one after every update. Any ${word[1]} between the two join too. You can undo it straight after.`),
    box), [
    { label: "Cancel" },
    { label: "Merge", primary: true, submit: true, run: async () => {
      const other = +box.querySelector("input:checked")?.value;
      if (!other) return false;
      try {
        const r = await api(P(p, `/events/${e.id}/merge`), { method: "POST", body: { other } });
        // Undo takes the merge back: each comes back with its own name, and events are found again.
        const unmerge = async () => {
          try {
            await api(P(p, "/events/unmerge"), { method: "POST", body: { undo: r.undo } });
            toast(`They're apart again. Finding ${word[1]} again…`, { key: "unmerge" });
            onChange?.();
            whenTripsFound(p, () => { dismissToast("unmerge"); onChange?.(); });
          } catch (err) { toast(err.message, { kind: "error" }); }
        };
        toast(`Merged ${plural(r.merged, word[0], word[1])} into “${e.name}”`, { action: unmerge, actionLabel: "Undo", ms: 10000 });
        onChange?.();
      } catch (err) { toast(err.message, { kind: "error" }); return false; }
    } },
  ]);
}

/** A link to view (only) this event or trip, copied. */
export async function shareEvent(e) {
  try {
    const r = await api(P(store.project, "/shares"), { method: "POST", body: { kind: "event", ref: e.id, label: e.name } });
    const url = `${(location.origin)}${r.path}`;
    try { await navigator.clipboard.writeText(url); toast("Share link copied. Anyone on your tailnet with it can view (only) this."); }
    catch { toast(url, { ms: 12000 }); }
  } catch (err) { toast(err.message, { kind: "error" }); }
}

/** The event's best photos, to save as an album (Albums › Best of). */
export const bestOfHref = (p, e) => `#/p/${encodeURIComponent(p)}/albums?tab=bestof&event=${e.id}`;

/** The Library's heading when it shows one event or trip: its name, dates and the same actions. */
export function eventHeader(p, id, { onChange } = {}) {
  const host = h("div", { class: "event-head" });
  async function paint() {
    let d;
    try { d = await api(P(p, `/events/${id}`)); } catch (e) { clear(host).append(h("span", { class: "muted" }, e.message)); return; }
    const e = d.event;
    names.set(String(e.id), e.name);
    const again = () => { paint(); onChange?.(); };
    clear(host).append(
      h("div", { class: "grow" },
        h("h2", {}, e.name),
        otherNames(e).length ? h("div", { class: "event-names" }, otherNames(e)) : null,
        h("div", { class: "muted" }, [eventSub(e), e.hidden ? "hidden from Events and Home" : ""].filter(Boolean).join(" · ")),
        e.place ? h("div", { class: "faint trip-why" }, placeWhy(e.place)) : null),
      h("div", { class: "row" },
        h("button", { class: "btn small", type: "button", onclick: () => renameEvent(e, again) }, "Rename…"),
        h("button", { class: "btn small", type: "button", onclick: () => mergeEvent(e, again) }, "Merge with…"),
        h("button", { class: "btn small", type: "button", onclick: () => setPlace(e, again) }, "Set place…"),
        h("button", { class: "btn small", type: "button", title: "Keep or reject this event's RAWs (the JPEGs stay)", onclick: () => eventRaws(e, again) }, "RAWs…"),
        h("button", { class: "btn small", type: "button", onclick: () => hideEvent(e, !e.hidden, again) }, e.hidden ? "Show again" : "Hide"),
        h("button", { class: "btn small", type: "button", title: "Copy a link that lets someone view (only) these photos", onclick: () => shareEvent(e) }, icon("share"), "Share"),
        h("a", { class: "btn small", href: bestOfHref(p, e), title: "Its best photos, to save as an album" }, icon("star"), "Best of"),
        e.kind === "trip" ? h("a", { class: "btn small ghost", href: `#/p/${encodeURIComponent(p)}/events?open=${e.id}` }, "Trip page") : null));
    onChange?.("loaded");
  }
  paint();
  return host;
}

export function mount(root, ctx) {
  const page = h("div", { class: "page" });
  root.append(page);
  const p = ctx.project;
  let kind = ctx.params.get("kind") || "";
  let year = ctx.params.get("year") || "";
  let hidden = ctx.params.get("hidden") === "1";
  let waiting = false;
  const lib = (q) => `#/p/${encodeURIComponent(p)}/library?${q}`;
  const setParams = () => ctx.setParams({ kind, year, hidden: hidden ? "1" : "" });

  async function render() {
    const open = ctx.params.get("open");
    if (open) return renderOne(+open);
    let d;
    try { d = await api(P(p, "/events"), { params: { kind, year, hidden: hidden ? 1 : "" } }); } catch (e) { clear(page).append(h("div", { class: "result err" }, e.message)); return; }
    remember(d.rows);
    noteNames(d.events);
    const trips = d.events.filter((e) => e.kind === "trip");
    const evs = d.events.filter((e) => e.kind === "event");
    // What decides trips, and a way to change it; while trips are being found again, say so and
    // show them once they are.
    if (d.busy && !waiting) { waiting = true; whenTripsFound(p, () => { waiting = false; render(); }); }
    const rule = hidden ? null : h("div", { class: "trip-rule" }, icon("home"), h("span", {}, ruleText(d.rule)), " · ",
      h("a", { class: "linkish", href: placesSettingsHref(p) }, d.rule.homes.length || d.rule.guess ? "Change" : "Set your home"),
      d.busy ? h("span", { class: "muted" }, " · Finding trips again…") : null);
    clear(page).append(
      h("div", { class: "page-head" },
        h("div", {}, h("h1", {}, hidden ? "Hidden events" : "Events"),
          h("div", { class: "sub" }, hidden ? "Events and trips you hid. Their photos are still in the Library."
            : "Found from when and where photos were taken, across every camera. Names you give stay, even as photos are added."),
          rule),
        h("div", { class: "actions" },
          choiceSeg([["", "All"], ["trip", "Trips"], ["event", "Events"]], kind, (k) => { kind = k; setParams(); render(); }, { label: "Show" }),
          h("select", { class: "input", style: { width: "auto" }, "aria-label": "Year", onchange: (e) => { year = e.target.value; setParams(); render(); } },
            h("option", { value: "" }, "Every year"), d.years.map(([y, k]) => h("option", { value: y, selected: String(y) === year }, `${y} (${k})`))),
          hidden ? h("button", { class: "btn", type: "button", onclick: () => { hidden = false; setParams(); render(); } }, "Back to events")
            : d.hidden ? h("button", { class: "btn ghost", type: "button", title: "Events and trips you hid", onclick: () => { hidden = true; setParams(); render(); } }, `${n(d.hidden)} hidden`) : null)),
      hidden ? null : waitNote({ step: "places", text: "Events will appear when the update has found them, after stacks, faces and search",
        when: () => !d.events.length, ready: "Events are found", onReady: render }),
      !d.events.length ? h("div", { class: "empty" }, hidden ? h("h3", {}, "Nothing hidden") : [h("h3", {}, "No events yet"),
        h("p", {}, "Events are made from when and where photos were taken, so they need photos with a capture date. They're found at the end of each update.")]) : null,
      trips.length ? h("section", { class: "section" }, h("h2", {}, `Trips · ${trips.length}`), h("div", { class: "event-cards big" }, trips.map(card))) : null,
      evs.length ? groupByYear(evs) : null);
  }

  function groupByYear(evs) {
    const by = new Map();
    for (const e of evs) {
      const y = new Date(e.start_ts * 1000).getUTCFullYear();
      if (!by.has(y)) by.set(y, []);
      by.get(y).push(e);
    }
    // Years that hold an event or two each would take a mostly empty row apiece: then they're one
    // list (each card already says its dates).
    if (by.size > 2 && evs.length < by.size * 3) {
      return h("section", { class: "section" }, h("h2", {}, `Events · ${n(evs.length)}`), h("div", { class: "event-cards" }, evs.map(card)));
    }
    return [...by.entries()].map(([y, list]) => h("section", { class: "section" },
      h("h2", {}, `${y} · ${plural(list.length, "event")}`),
      h("div", { class: "event-cards" }, list.map(card))));
  }

  function card(e) {
    const r = row(e.cover_shot_id);
    const people = (e.people || []).slice(0, 3).map((x) => x[0]).filter(Boolean);
    const writer = store.meta?.writer !== false;
    return h("div", { class: "event-card-wrap" },
      h("a", { class: "event-card", href: e.kind === "trip" ? `#/p/${encodeURIComponent(p)}/events?open=${e.id}` : lib(`event=${e.id}`) },
        h("div", { class: "cover", style: r ? { backgroundImage: `url(${img.thumb(p, r, "m")})` } : {} }),
        h("b", {}, e.name),
        ...otherNames(e),
        h("span", { class: "muted" }, eventSub(e)),
        // Why a trip is one; where an event was, when that's a guess (or yours).
        e.place && (e.kind === "trip" || ["folder", "clock", "manual"].includes(e.place.src))
          ? h("span", { class: "trip-why" }, placeWhy(e.kind === "trip" ? e.place : { ...e.place, away: null })) : null,
        people.length ? h("span", { class: "faint" }, people.join(", ")) : null),
      writer ? h("button", { type: "button", class: "btn small icon-only event-more", title: "Rename, merge, set its place or hide",
        "aria-label": `Rename, merge, set the place of or hide ${e.name}`, "aria-haspopup": "menu", onclick: (ev) => eventMenu(ev.currentTarget, e, render) },
      h("span", { "aria-hidden": "true" }, "⋯")) : null);
  }

  async function renderOne(id) {
    let d;
    try { d = await api(P(p, `/events/${id}`)); } catch (e) { clear(page).append(h("div", { class: "result err" }, e.message)); return; }
    remember(d.best);
    remember(d.rows);
    const e = d.event;
    noteNames([e]);
    const places = JSON.parse(e.places || "[]");
    const at = e.place?.kind === "town" ? e.place : null;          // a country or region has no one spot to show
    clear(page).append(
      h("a", { class: "linkish", href: `#/p/${encodeURIComponent(p)}/events` }, "← All events"),
      h("div", { class: "page-head" },
        h("div", {}, h("h1", {}, e.name),
          h("div", { class: "sub" }, [dateRange(e.start_ts, e.end_ts), plural(e.n, "photo"), places.slice(0, 3).map((x) => x[0]).join(" · "),
            e.hidden ? "hidden" : ""].filter(Boolean).join(" · ")),
          e.place ? h("div", { class: "trip-why" }, e.kind === "trip" ? "A trip: " : "", placeWhy(e.place), " ",
            at ? mapsLink(at.lat, at.lon, placeLabel(at)) : null) : null),
        h("div", { class: "actions" },
          h("button", { class: "btn", onclick: () => renameEvent(e, render) }, "Rename"),
          h("button", { class: "btn", onclick: () => mergeEvent(e, render) }, "Merge with…"),
          h("button", { class: "btn", onclick: () => setPlace(e, render) }, icon("pin"), "Set place…"),
          h("button", { class: "btn", title: "Keep or reject this trip's RAWs (the JPEGs stay)", onclick: () => eventRaws(e, render) }, "RAWs…"),
          h("button", { class: "btn", onclick: () => hideEvent(e, !e.hidden, render) }, e.hidden ? "Show again" : "Hide"),
          h("button", { class: "btn", onclick: () => shareEvent(e) }, icon("share"), "Share"),
          h("a", { class: "btn", href: bestOfHref(p, e) }, icon("star"), "Best of"),
          h("a", { class: "btn primary", href: e.kind === "trip" ? lib(`trip=${e.id}`) : lib(`event=${e.id}`) }, "Open in Library"))),
      h("section", { class: "section" }, h("h2", {}, "Best photos"),
        h("div", { class: "strip", style: { "--strip-h": "180px" } }, d.best.map((r, i) => thumbButton(r, () => openLoupe({ ids: d.best.map((x) => x.id), index: i }), { size: "m" })))),
      d.children.length ? h("section", { class: "section" }, h("h2", {}, `Days and moments · ${d.children.length}`),
        h("div", { class: "event-cards" }, d.children.map((c) => card({ ...c, kind: "event" })))) : null);
  }

  render();
  // J and K: from one event or trip card to the next; Enter opens it (the card is a link).
  const onKey = groupKeys(() => [...page.querySelectorAll(".event-card-wrap")], { focusOf: (el) => el.querySelector(".event-card") });
  return { refresh: render, onKey, keys: KEYS };
}
