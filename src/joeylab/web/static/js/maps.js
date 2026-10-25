// Links to a place in Google Maps (or Apple Maps, if you'd rather, on this device). A link only: no
// map from Google is shown here and nothing is sent to it until you follow the link, which opens
// the Maps app on a phone and the website on a Mac.

import { setPref, store } from "./store.js";
import { h } from "./ui.js";

export const MAP_APPS = { google: "Google Maps", apple: "Apple Maps" };
export const mapsApp = () => (store.prefs.maps === "apple" ? "apple" : "google");
export const setMapsApp = (app) => setPref("maps", app === "apple" ? "apple" : "google");

export function mapsUrl(lat, lon, name = "") {
  const ll = `${(+lat).toFixed(5)},${(+lon).toFixed(5)}`;
  if (mapsApp() === "apple") return `https://maps.apple.com/?ll=${ll}&q=${encodeURIComponent(name || ll)}`;
  return `https://www.google.com/maps/search/?api=1&query=${ll}`;
}

/** "Open in Google Maps", as a link that leaves JoeyLab in a new tab. `named`: its name says
 * which place, for a list of them ("Open in Google Maps: Lisbon"). */
export function mapsLink(lat, lon, name, { cls = "linkish", label, named = false } = {}) {
  if (lat === null || lat === undefined || lon === null || lon === undefined) return null;
  const text = label || `Open in ${MAP_APPS[mapsApp()]}`;
  return h("a", { class: cls, href: mapsUrl(lat, lon, name), target: "_blank", rel: "noopener noreferrer",
    "aria-label": named && name ? `${text}: ${name}` : null,
    title: `Opens ${MAP_APPS[mapsApp()]}. Only the place is sent, when you follow the link.` }, text);
}
