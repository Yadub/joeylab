// Places: where photos were taken (offline reverse geocoding), as a map and a list by country, and
// your homes, which decide what counts as a trip (Settings › Places & trips, also from here).
// The map is drawn on this Mac from Natural Earth's outlines (public domain, static/vendor/naturalearth)
// with Leaflet 1.9.4 (BSD-2-Clause, static/vendor/leaflet). OpenStreetMap's street map loads only if
// you turn it on: it then sees which areas you look at, never your photos. Google Maps is only ever
// a link (maps.js).

import { api, img, P } from "../api.js";
import { openLoupe } from "../loupe.js";
import { MAP_APPS, mapsApp, mapsLink, setMapsApp } from "../maps.js";
import { remember, row, store } from "../store.js";
import { choiceSeg, clear, closePopover, cssColors, dateRange, focusOn, h, icon, n, plural, popover, roving, toast } from "../ui.js";
import { waitNote } from "./waiting.js";

const LEAFLET = "/static/vendor/leaflet";
let leafletLoading = null;
function loadLeaflet() {
  if (window.L) return Promise.resolve(window.L);
  if (leafletLoading) return leafletLoading;
  leafletLoading = new Promise((resolve, reject) => {
    const css = document.createElement("link");
    css.rel = "stylesheet";
    css.href = `${LEAFLET}/leaflet.css`;
    document.head.append(css);
    const s = document.createElement("script");
    s.src = `${LEAFLET}/leaflet.js`;
    s.onload = () => resolve(window.L);
    s.onerror = () => { leafletLoading = null; s.remove(); reject(new Error("Couldn't load the map library")); };
    document.head.append(s);
  });
  return leafletLoading;
}

let worldLoading = null;
/** Natural Earth's land and borders, as Leaflet's [lat, lon] lists (read once). */
function loadWorld() {
  worldLoading ||= fetch("/static/vendor/naturalearth/world.json")
    .then((r) => { if (!r.ok) throw new Error("Couldn't load the map's outlines"); return r.json(); })
    .then((d) => {
      const line = (flat) => {
        const out = [];
        let x = 0, y = 0;
        for (let i = 0; i < flat.length; i += 2) { x += flat[i]; y += flat[i + 1]; out.push([y / d.scale, x / d.scale]); }
        return out;
      };
      return { land: d.land.map((poly) => poly.map(line)), borders: d.borders.map(line) };
    })
    .catch((e) => { worldLoading = null; throw e; });
  return worldLoading;
}

// ------------------------------------------------------------------ shared with Events and Settings
/** "Phuket, Thailand"; a country is just its name. */
export const placeLabel = (pl) => (pl.kind === "country" || !pl.country || pl.country === pl.name ? pl.name : `${pl.name}, ${pl.country}`);
const utc = (m) => `UTC${m < 0 ? "−" : "+"}${String(Math.floor(Math.abs(m) / 60)).padStart(2, "0")}:${String(Math.abs(m) % 60).padStart(2, "0")}`;

/** Where an event or trip was and why it's away: "Phuket, Thailand · 1,630 km from Delhi", with how
 * the place is known when it's a guess. */
export function placeWhy(pl) {
  if (!pl) return "";
  const a = pl.away;
  if (pl.src === "clock") {
    const home = a?.homes > 1 ? ", not the time at any of your homes"
      : a?.home && a.home_offset !== null && a.home_offset !== undefined ? `, not ${a.home}'s ${utc(a.home_offset)}` : "";
    return `Camera clocks on ${utc(pl.offset)}${home} (a guess)`;
  }
  const bits = [placeLabel(pl)];
  if (a?.km !== null && a?.km !== undefined && a.home) bits.push(`${pl.kind === "region" ? "about " : ""}${n(a.km)} km from ${a.home}`);
  else if (a?.abroad) bits.push("another country");
  if (pl.src === "folder") bits.push("guessed from the folder name");
  else if (pl.src === "manual") bits.push("set by you");
  return bits.join(" · ");
}

/** What decides trips, in a line: "Trips: events more than 100 km from your homes (Delhi, Dubai), or in another country". */
export function ruleText(r) {
  const km = `${n(r.km)} km`;
  if (r.homes.length) return `Trips: events more than ${km} from your ${r.homes.length > 1 ? "homes" : "home"} (${r.homes.map((x) => x.name).join(", ")}), or in another country`;
  if (r.guess) return `Trips: events more than ${km} from home, or in another country. JoeyLab thinks home is ${r.guess.name}`;
  return "Trips: runs of events with a place, lasting a day or more. Set your home and trips are the times you were away";
}

export const placesSettingsHref = (p) => `#/p/${encodeURIComponent(p)}/settings?s=places`;

/** Ask again until trips have been found again (after a change of homes, distance or place), then done(). */
export function whenTripsFound(p, done, tries = 120) {
  setTimeout(async () => {
    let r;
    try { r = await api(P(p, "/places/prefs")); } catch { return; }
    if (r.busy && tries > 0) whenTripsFound(p, done, tries - 1);
    else done(r);
  }, 1000);
}

/** A box to find a town in the offline town list; onPick(town) with the one chosen. ↑ and ↓ move
 * through the towns found, Enter takes the one highlighted (the first, until you move). */
let townBoxes = 0;
export function townSearch(onPick, { placeholder = "Search for a town", label = "Town" } = {}) {
  const id = `town-${++townBoxes}`;
  const input = h("input", { class: "input", id, type: "search", placeholder, autocomplete: "off", spellcheck: "false",
    role: "combobox", "aria-autocomplete": "list", "aria-expanded": "false", "aria-controls": `${id}-list` });
  const list = h("div", { class: "town-results", id: `${id}-list`, role: "listbox", "aria-label": "Towns" });
  let timer = null, seq = 0, found = [], sel = 0;
  const mark = () => {
    [...list.querySelectorAll(".town-result")].forEach((b, i) => b.setAttribute("aria-selected", i === sel ? "true" : "false"));
    input.setAttribute("aria-expanded", found.length ? "true" : "false");
    if (found.length) input.setAttribute("aria-activedescendant", `${id}-opt-${sel}`); else input.removeAttribute("aria-activedescendant");
  };
  async function look() {
    const q = input.value.trim();
    const mine = ++seq;
    if (q.length < 2) { found = []; clear(list); mark(); return; }
    let d;
    try { d = await api("/api/places/search", { params: { q, limit: 8 } }); } catch (e) { clear(list).append(h("div", { class: "muted" }, e.message)); return; }
    if (mine !== seq) return;
    found = d.towns;
    sel = 0;
    if (!d.available) { found = []; mark(); clear(list).append(h("div", { class: "muted" }, "The offline town list isn't downloaded yet: press Update places on the Places page (about 5 MB, once).")); return; }
    clear(list).append(found.length ? found.map((t, i) => h("button", { type: "button", class: "town-result", role: "option", id: `${id}-opt-${i}`, tabindex: "-1",
      onclick: () => onPick(t) },
    h("b", {}, t.name), h("span", { class: "muted" }, [t.admin1 !== t.name ? t.admin1 : null, t.country].filter(Boolean).join(", "))))
      : h("div", { class: "muted", role: "status" }, "No town by that name in the town list (it has every place of 5,000 people or more)."));
    mark();
  }
  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(look, 150); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); if (found[sel]) onPick(found[sel]); }
    else if ((e.key === "ArrowDown" || e.key === "ArrowUp") && found.length) {
      e.preventDefault(); e.stopPropagation();
      sel = Math.max(0, Math.min(found.length - 1, sel + (e.key === "ArrowDown" ? 1 : -1)));
      mark();
    }
  });
  return { el: h("div", { class: "town-search stack-v" }, h("label", { class: "sr-only", for: id }, label), input, list), input,
    reset: () => { input.value = ""; found = []; clear(list); mark(); } };
}

async function savePrefs(p, patch) {
  return api(P(p, "/places/prefs"), { method: "POST", body: patch });
}

/** Make a town one of your homes, or not (with Undo); then trips are found again. */
export async function toggleHome(p, rule, town, onChange) {
  const before = rule.homes.map((x) => x.id);
  const was = before.includes(town.id);
  const next = was ? before.filter((x) => x !== town.id) : [...before, town.id];
  try {
    await savePrefs(p, { homes: next });
    const instead = !before.length && rule.guess && rule.guess.id !== town.id ? ` instead of JoeyLab's guess, ${rule.guess.name}` : "";
    toast(was ? `${town.name} isn't one of your homes now. Finding trips again…`
      : `${town.name} is ${before.length ? "one of your homes" : "your home"} now${instead}. Finding trips again…`,
    { action: async () => { await savePrefs(p, { homes: before }); onChange?.(); }, actionLabel: "Undo" });
    onChange?.();
  } catch (e) { toast(e.message, { kind: "error" }); }
}

// ------------------------------------------------------------------ the page
export function mount(root, ctx) {
  const page = h("div", { class: "page" });
  root.append(page);
  const p = ctx.project;
  const lib = (q) => `#/p/${encodeURIComponent(p)}/library?${q}`;
  let shown = null;                 // the map on the page: {map, focus(id)}
  let d = null;

  async function render() {
    try { d = await api(P(p, "/places")); } catch (e) { clear(page).append(h("div", { class: "result err" }, e.message)); return; }
    remember(d.rows);
    const owner = store.meta?.owner !== false;
    const homeIds = new Set(d.homes.map((x) => x.id));
    const mapHost = h("div", { class: "map-host" });
    const scanning = store.job?.state === "running";
    const placed = d.gps + d.inferred + d.folder + d.manual;
    shown?.map.remove();
    shown = null;
    clear(page).append(
      h("div", { class: "page-head" },
        h("div", {}, h("h1", {}, "Places"),
          h("div", { class: "sub" }, !d.total ? (scanning ? "JoeyLab is still reading this library's photos." : "No photos in this library yet.")
            : placed ? [`${plural(d.gps, "photo")} with GPS`, d.inferred ? `${n(d.inferred)} placed by the photos around them` : "",
              d.folder ? `${n(d.folder)} by their folder's name (a guess)` : "", d.manual ? `${n(d.manual)} by you` : "", `${n(d.unknown)} unknown`].filter(Boolean).join(" · ")
              : `${plural(d.total, "photo")}, none with a GPS position.`),
          homeLine()),
        d.total ? h("div", { class: "actions" },
          h("button", { class: "btn", title: "Name every photo's place again, and find events and trips", onclick: async (e) => {
            e.currentTarget.disabled = true;
            try { const r = await api(P(p, "/places/refresh"), { method: "POST", body: {} }); toast(r.available ? `Placed ${plural(r.gps + r.inferred, "photo")}` : (r.log[0] || "Town list unavailable")); render(); }
            catch (err) { toast(err.message, { kind: "error" }); }
          } }, icon("refresh"), "Update places")) : null),
      waitNote({ step: "places", text: "Place names, and photos placed by the ones around them, come at the end of the update",
        when: () => !d.countries.length, ready: "Places are found", onReady: render }),
      d.total && !d.available ? h("div", { class: "result" }, "Naming places needs a one-time download of the offline town list (about 5 MB, from GeoNames). Press Update places. Your locations never leave the Mac.") : null,
      placed || d.homes.length ? h("section", { class: "section" },
        h("a", { class: "skip-link skip-inline", href: "#", onclick: (e) => { e.preventDefault(); focusOn(page.querySelector(".place-list h2")); } },
          "Skip the map: the same places as a list"), mapHost)
        : h("div", { class: "empty" }, h("h3", {}, scanning && !d.total ? "Places appear as photos are read" : "No GPS in these photos"),
          h("p", {}, "Phones record where photos are taken; most cameras only do when paired with a phone. Photos taken near a phone photo still get a place, and so do photos in a folder named after one (\"2023-Cyprus\").")),
      d.countries.map((c) => h("section", { class: "section place-list" },
        h("h2", {}, `${c.country || c.cc || "Unknown"} · ${plural(c.n, "photo")}`),
        h("div", { class: "place-grid" }, c.places.slice(0, 60).map((pl) => card(pl, homeIds.has(pl.id), owner))))));
    if (placed || d.homes.length) drawMap(mapHost);

    function homeLine() {
      const set = h("a", { class: "linkish", href: placesSettingsHref(p) }, d.homes.length || d.guess ? "Change" : "Set your home");
      const text = d.homes.length ? `${d.homes.length > 1 ? "Homes" : "Home"}: ${d.homes.map((x) => x.name).join(", ")} · trips start ${n(d.km)} km away`
        : d.guess ? `JoeyLab thinks home is ${d.guess.name}` : "No home set, so trips can't tell when you were away";
      return h("div", { class: "home-line" }, icon("home"), h("span", {}, text), " · ", set);
    }
  }

  function card(pl, isHome, owner) {
    const r = row(d.covers[pl.id]);
    const guessed = pl.folder && pl.folder === pl.n;
    return h("div", { class: "place-card-wrap" },
      h("a", { class: "place-card", href: lib(`place=${pl.id}`) },
        h("div", { class: "cover", style: r ? { backgroundImage: `url(${img.thumb(p, r, "m")})` } : {} }),
        h("b", {}, pl.name, isHome ? h("span", { class: "pill accent home-pill" }, "Home") : null),
        h("span", { class: "muted" }, `${pl.admin1 && pl.admin1 !== pl.name ? pl.admin1 + " · " : ""}${plural(pl.n, "photo")}`),
        h("span", { class: "faint" }, dateRange(pl.first, pl.last), guessed ? " · from folder names" : "")),
      h("div", { class: "place-card-links" }, mapsLink(pl.lat, pl.lon, pl.name, { named: true })),
      h("button", { type: "button", class: "btn small icon-only event-more", title: "More", "aria-label": `More for ${pl.name}`, "aria-haspopup": "menu",
        onclick: (e) => {
          const btn = e.currentTarget;
          const item = (label, fn) => h("button", { type: "button", class: "menu-item", role: "menuitem", onclick: () => { closePopover(); fn(); } }, label);
          popover(btn, [
            item("Show on the map", () => { shown?.focus(pl.id, btn); shown?.map.getContainer().scrollIntoView({ block: "center", behavior: "smooth" }); }),
            owner ? item(isHome ? "Not a home" : "Set as home", () => toggleHome(p, d, pl, render)) : null,
          ]);
        } }, h("span", { "aria-hidden": "true" }, "⋯")));
  }

  // ---------------------------------------------------------------- the map
  async function drawMap(host, view = null) {
    let L, world, pts;
    shown?.map.remove();
    shown = null;
    clear(host).append(h("div", { class: "leaflet-map place-map loading" }));
    try { [L, world, pts] = await Promise.all([loadLeaflet(), loadWorld(), api(P(p, "/places/points")).then((r) => r.points)]); }
    catch (e) { clear(host).append(h("div", { class: "result err" }, e.message)); return; }
    if (!host.isConnected) return;
    const tiles = d.map_tiles;
    const owner = store.meta?.owner !== false;
    const homeIds = new Set(d.homes.map((x) => x.id));
    const places = d.countries.flatMap((c) => c.places);
    const hint = h("span", { class: "sr-only", id: "place-map-keys" },
      "Arrow keys move the map, plus and minus zoom. Tab goes to the places on it, one after another with the arrow keys; Enter shows one. Every place is also listed below the map.");
    const el = h("div", { class: `leaflet-map place-map${tiles ? " tiles" : ""}`, role: "region", "aria-label": "Map of where photos were taken",
      "aria-describedby": "place-map-keys" });
    const note = h("p", { class: "note" }, tiles
      ? "Street map from OpenStreetMap, which sees which areas you look at (never your photos). Hide street map to draw the map on this Mac again."
      : "Drawn on this Mac from Natural Earth's outlines. Show street map loads map images from OpenStreetMap: it then sees which areas you look at, never your photos.",
    " Circles are places, sized by their photos; small dots are photos with GPS (click one to see the photos taken there).");
    clear(host).append(hint, el, note);
    const c = cssColors(el);
    const map = L.map(el, { preferCanvas: true, zoomSnap: 0.5, minZoom: 1, maxZoom: tiles ? 18 : 10, worldCopyJump: false,
      maxBounds: [[-85, -220], [85, 220]], maxBoundsViscosity: 0.7 });
    const renderer = L.canvas({ padding: 0.4 });
    if (tiles) {
      L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 18, attribution: "© OpenStreetMap contributors" }).addTo(map);
    } else {
      L.polygon(world.land, { renderer, color: c("--map-coast"), weight: 0.7, fillColor: c("--map-land"), fillOpacity: 1, interactive: false }).addTo(map);
      L.polyline(world.borders, { renderer, color: c("--map-border"), weight: 0.8, dashArray: "4 3", interactive: false }).addTo(map);
      map.attributionControl.addAttribution("Natural Earth");
    }
    // Photos with GPS, under the places.
    for (const [lat, lon] of pts) {
      L.circleMarker([lat, lon], { renderer, radius: 3, stroke: false, fillColor: c("--map-dot"), fillOpacity: 0.6 })
        .on("click", () => openNear(pts, lat, lon)).addTo(map);
    }
    const markers = new Map();
    const radius = (k) => Math.min(22, 5 + Math.sqrt(k) * 1.3);
    const guessed = !d.homes.length && d.guess ? d.guess.id : null;        // JoeyLab's guess, until you set a home
    for (const pl of [...places].sort((a, b) => b.n - a.n)) {
      const home = homeIds.has(pl.id);
      const ring = home || pl.id === guessed;
      const m = L.circleMarker([pl.lat, pl.lon], { renderer, radius: radius(pl.n), color: ring ? c("--map-home") : c("--map-ring"), weight: ring ? 2.5 : 1.5,
        dashArray: pl.id === guessed ? "4 3" : null, fillColor: c("--map-place"), fillOpacity: 0.85 })
        .bindPopup(() => popup(pl, home, owner, pl.id === guessed), { className: "place-pop", maxWidth: Math.min(300, window.innerWidth - 56), autoPanPaddingTopLeft: [16, 56], autoPanPaddingBottomRight: [16, 24] })
        .bindTooltip(home ? `${pl.name} (home)` : pl.id === guessed ? `${pl.name} (home?)` : pl.name,
          { permanent: true, direction: "right", offset: [radius(pl.n), 0], className: "map-label", interactive: false })
        .addTo(map);
      markers.set(pl.id, m);
      // The same place for the keyboard and a screen reader, over its circle: Tab reaches the
      // biggest; the arrows go on to the next; Enter (or a click) shows it.
      L.marker([pl.lat, pl.lon], { keyboard: true, riseOnHover: false, autoPanOnFocus: true,
        icon: L.divIcon({ className: "place-focus", iconSize: [radius(pl.n) * 2, radius(pl.n) * 2],
          html: h("span", { class: "sr-only" }, `${home ? `${pl.name}, home` : pl.name}, ${plural(pl.n, "photo")}`) }) })
        .bindPopup(() => popup(pl, home, owner, pl.id === guessed), { className: "place-pop", maxWidth: Math.min(300, window.innerWidth - 56), autoPanPaddingTopLeft: [16, 56], autoPanPaddingBottomRight: [16, 24] })
        .on("click", () => { popupFrom = document.activeElement; })
        .addTo(map);
    }
    const pane = map.getPane("markerPane");
    pane.querySelectorAll(".place-focus").forEach((x, i) => { x.tabIndex = i ? -1 : 0; });
    roving(pane, { sel: ".place-focus", choose: false });
    // A popup opened from the keyboard takes it; Esc closes it and goes back.
    let popupFrom = null;
    map.on("popupopen", (e) => {
      const pop = e.popup.getElement();
      const from = popupFrom || (pane.contains(document.activeElement) ? document.activeElement : null);
      if (!from || !pop) return;
      pop.addEventListener("keydown", (ev) => {
        if (ev.key !== "Escape") return;
        ev.preventDefault(); ev.stopPropagation();
        map.closePopup();
        if (from.isConnected) from.focus({ preventScroll: true });
      });
      if (!pop.contains(document.activeElement) && from.matches(":focus-visible")) pop.querySelector(".map-pop a, .map-pop button")?.focus({ preventScroll: true });
    });
    map.on("popupclose", () => { popupFrom = null; });
    // Homes with no photos here yet.
    const lone = [];
    for (const x of d.homes.filter((x) => !markers.has(x.id))) {
      lone.push(L.circleMarker([x.lat, x.lon], { renderer, radius: 6, color: c("--map-home"), weight: 2, dashArray: "3 2", fillColor: c("--map-land"), fillOpacity: 1 })
        .bindTooltip(`${x.name} (home)`, { permanent: true, direction: "right", offset: [7, 0], className: "map-label", interactive: false }).addTo(map));
    }
    // The street map switch, on the map itself.
    const toggle = L.control({ position: "topright" });
    toggle.onAdd = () => {
      const b = h("button", { type: "button", class: "map-toggle", "aria-pressed": tiles ? "true" : "false", disabled: !owner,
        title: tiles ? "Draw the map on this Mac again" : "Loads map images from OpenStreetMap: it sees which areas you look at, never your photos",
        onclick: async (e) => {
          L.DomEvent.stop(e);
          try {
            d = { ...d, ...(await savePrefs(p, { map_tiles: !tiles })) };
            drawMap(host, { center: map.getCenter(), zoom: Math.min(map.getZoom(), tiles ? 10 : 18) });
          } catch (err) { toast(err.message, { kind: "error" }); }
        } }, icon("map"), tiles ? "Hide street map" : "Show street map");
      L.DomEvent.disableClickPropagation(b);
      return b;
    };
    toggle.addTo(map);
    // Fit what there is to see.
    const spots = [...places.map((pl) => [pl.lat, pl.lon]), ...pts.map((q) => [q[0], q[1]]), ...d.homes.map((x) => [x.lat, x.lon])];
    if (view) map.setView(view.center, view.zoom, { animate: false });
    else if (spots.length === 1) map.setView(spots[0], 8);
    else if (spots.length) map.fitBounds(L.latLngBounds(spots), { padding: [36, 36], maxZoom: 9 });
    else map.setView([20, 10], 2);
    // Names that would sit on top of one another: the place with more photos keeps its name.
    const ranked = [...lone, ...[...places].sort((a, b) => b.n - a.n).map((pl) => markers.get(pl.id))];       // homes first
    const declutter = () => {
      const taken = [];
      for (const m of ranked) {
        const t = m.getTooltip()?.getElement();
        if (!t) continue;
        t.style.visibility = "";
        const b = t.getBoundingClientRect();
        if (taken.some((o) => !(b.right < o.left || b.left > o.right || b.bottom < o.top || b.top > o.bottom))) t.style.visibility = "hidden";
        else taken.push(b);
      }
    };
    map.on("zoomend moveend", declutter);
    setTimeout(declutter, 0);
    shown = { map, focus: (id, from = null) => {
      const m = markers.get(id);
      if (!m) return;
      popupFrom = from;
      map.setView(m.getLatLng(), Math.max(map.getZoom(), 7), { animate: false });
      setTimeout(() => m.openPopup(), 50);                    // once there, so it pans to show all of it
    } };
  }

  function popup(pl, home, owner, guess = false) {
    const r = row(d.covers[pl.id]);
    const how = [pl.gps ? `${n(pl.gps)} with GPS` : "", pl.n - pl.gps - (pl.folder || 0) - (pl.manual || 0) > 0 ? `${n(pl.n - pl.gps - (pl.folder || 0) - (pl.manual || 0))} placed by the photos around them` : "",
      pl.folder ? `${n(pl.folder)} by folder names (a guess)` : "", pl.manual ? `${n(pl.manual)} by you` : ""].filter(Boolean);
    return h("div", { class: "map-pop" },
      r ? h("a", { class: "cover", href: lib(`place=${pl.id}`), style: { backgroundImage: `url(${img.thumb(p, r, "m")})` }, "aria-label": `Photos taken in ${pl.name}` }) : null,
      h("b", {}, pl.name, home || guess ? h("span", { class: "pill accent home-pill", title: guess ? "JoeyLab's guess: the place in the most months of photos" : null }, guess ? "Home?" : "Home") : null),
      h("span", { class: "muted" }, [pl.admin1 !== pl.name ? pl.admin1 : null, pl.country].filter(Boolean).join(", ")),
      h("span", {}, `${plural(pl.n, "photo")} · ${dateRange(pl.first, pl.last)}`),
      how.length ? h("span", { class: "faint" }, how.join(" · ")) : null,
      h("div", { class: "row" },
        h("a", { class: "btn small primary", href: lib(`place=${pl.id}`) }, "See photos"),
        mapsLink(pl.lat, pl.lon, pl.name, { cls: "btn small" }),
        owner ? h("button", { type: "button", class: "btn small", onclick: () => toggleHome(p, d, pl, render) }, home ? "Not a home" : guess ? "Yes, it's home" : "Set as home") : null));
  }

  async function openNear(pts, lat, lon) {
    const near = [...pts].sort((a, b) => ((a[0] - lat) ** 2 + (a[1] - lon) ** 2) - ((b[0] - lat) ** 2 + (b[1] - lon) ** 2)).slice(0, 80).map((q) => q[2]);
    remember((await api(P(p, "/rows"), { params: { ids: near.join(",") } })).rows);
    openLoupe({ ids: near, index: 0 });
  }

  // The map takes the theme's colours: draw it again when light or dark changes.
  const redraw = () => { const host = shown?.map.getContainer().parentElement; if (host && d) drawMap(host, { center: shown.map.getCenter(), zoom: shown.map.getZoom() }); };
  const dark = window.matchMedia("(prefers-color-scheme: dark)");
  dark.addEventListener("change", redraw);
  const themeWatch = new MutationObserver(redraw);
  themeWatch.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

  render();
  return { refresh: render, unmount: () => { dark.removeEventListener("change", redraw); themeWatch.disconnect(); shown?.map.remove(); } };
}

// ------------------------------------------------------------------ Settings › Places & trips
/** Your homes, the trip distance, the street map and which maps app links open: for every library. */
export function placesSettings(body, p) {
  const host = h("div", { class: "stack-v", style: { gap: "28px" } });
  body.append(host);
  let again = null;
  async function paint() {
    if (again && !host.isConnected) return;          // left the page while trips were being found
    let r;
    try { r = await api(P(p, "/places/prefs")); } catch (e) { clear(host).append(h("div", { class: "result err" }, e.message)); return; }
    const owner = store.meta?.owner !== false;
    const ids = r.homes.map((x) => x.id);
    const save = async (patch, message, undo) => {
      try {
        await savePrefs(p, patch);
        toast(message, undo ? { action: async () => { await savePrefs(p, undo); paint(); }, actionLabel: "Undo" } : {});
        paint();
      } catch (e) { toast(e.message, { kind: "error" }); }
    };
    const search = townSearch((t) => {
      search.reset();
      if (ids.includes(t.id)) { toast(`${t.name} is already one of your homes`); return; }
      save({ homes: [...ids, t.id] }, `${t.name} is ${ids.length ? "one of your homes" : "your home"} now. Finding trips again…`, { homes: ids });
    }, { placeholder: ids.length ? "Add another home: search for a town" : "Add your home: search for a town", label: "Add a home" });
    const kmIn = h("input", { class: "input", type: "number", min: 1, max: 20000, step: 1, value: r.km, style: { width: "6.5em" }, "aria-label": "Trip distance in kilometres", disabled: !owner });
    const download = async (e) => {
      e.currentTarget.disabled = true;
      try { const x = await api(P(p, "/places/refresh"), { method: "POST", body: {} }); toast(x.available ? "The town list is downloaded" : (x.log[0] || "Couldn't download the town list")); paint(); }
      catch (err) { toast(err.message, { kind: "error" }); }
    };
    clear(host).append(
      h("section", { class: "section" }, h("h2", {}, r.homes.length > 1 ? "Homes" : "Home"),
        h("p", { class: "note" }, "Where you live, or have lived. Events far from every home, or in another country, are trips. Your homes are the same in every library on this Mac."),
        r.homes.length ? h("div", { class: "stack-v" }, r.homes.map((x) => h("div", { class: "drive-row" }, icon("home"),
          h("span", { class: "grow" }, h("b", {}, x.name), h("span", { class: "muted" }, ` · ${[x.admin1 !== x.name ? x.admin1 : null, x.country].filter(Boolean).join(", ")}`)),
          mapsLink(x.lat, x.lon, x.name),
          owner ? h("button", { class: "btn small ghost", type: "button",
            onclick: () => save({ homes: ids.filter((i) => i !== x.id) }, `${x.name} isn't one of your homes now. Finding trips again…`, { homes: ids }) }, "Remove") : null)))
          : r.guess ? h("div", { class: "panel stack-v" },
            h("p", {}, `JoeyLab thinks home is ${r.guess.name}${r.guess.country ? `, ${r.guess.country}` : ""}: the place in the most months of this library's photos.`),
            owner ? h("div", { class: "row" },
              h("button", { class: "btn primary", type: "button", onclick: () => save({ homes: [r.guess.id] }, `${r.guess.name} is your home now`, { homes: [] }) }, `Yes, ${r.guess.name} is home`),
              h("span", { class: "muted" }, "or add your homes below.")) : null)
          : h("p", { class: "muted" }, "No home yet. Until you add one, any run of events with a place lasting a day or more counts as a trip."),
        owner ? (r.available ? search.el : h("div", { class: "row" }, h("span", { class: "muted" }, "Finding towns needs the offline town list (about 5 MB, from GeoNames, once)."),
          h("button", { class: "btn small", type: "button", onclick: download }, "Download it"))) : null),
      h("section", { class: "section" }, h("h2", {}, "Trips"),
        h("form", { class: "row trip-km", onsubmit: (e) => {
          e.preventDefault();
          const km = Math.round(+kmIn.value);
          if (!(km >= 1 && km <= 20000)) { toast("Choose a distance from 1 to 20,000 km", { kind: "error" }); return; }
          if (km !== r.km) save({ km }, `Trips start ${n(km)} km from home now. Finding trips again…`, { km: r.km });
        } }, h("span", {}, "Events more than"), kmIn, h("span", {}, "km from every home, or in another country, are trips."),
        owner ? h("button", { class: "btn", type: "submit" }, "Save") : null),
        h("p", { class: "note" }, "A trip is a run of such events with no more than two days between them. In your own country it lasts a day or more; in another country a single day counts."),
        h("p", { class: "note" }, "Events whose photos have no GPS take their place from photos taken around them, from a folder named after a place (\"2023-Cyprus\", \"Phuket Jan 2026\"), or from cameras' clocks set to another time zone than home's. The last two are guesses, and shown as such. Set place… on an event or trip replaces any guess, for good."),
        r.busy ? h("p", { class: "muted" }, "Finding trips again…") : null),
      h("section", { class: "section" }, h("h2", {}, "Maps"),
        h("label", { class: "check" },
          h("input", { type: "checkbox", checked: r.map_tiles, disabled: !owner,
            onchange: (e) => save({ map_tiles: e.target.checked }, e.target.checked ? "The street map is on" : "The street map is off", { map_tiles: !e.target.checked }) }),
          h("span", {}, h("b", {}, "Show the street map on the Places page"), h("br"),
            h("span", { class: "muted" }, "Loads map images from OpenStreetMap, which then sees which areas you look at (never your photos). Off, the map is drawn on this Mac."))),
        h("div", { class: "row" }, h("span", {}, "Open places in"),
          choiceSeg(Object.entries(MAP_APPS), mapsApp(), (k) => { setMapsApp(k); paint(); }, { label: "Open places in" }),
          h("span", { class: "muted" }, "on this device")),
        h("p", { class: "note" }, "Only a link: nothing is sent to Google or Apple until you follow it. It opens the Maps app on a phone and the website on a Mac.")));
    clearTimeout(again);
    if (r.busy) again = setTimeout(paint, 1500);
  }
  paint();
}
