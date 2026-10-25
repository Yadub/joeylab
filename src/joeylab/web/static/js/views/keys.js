// Shortcut help: the keys of every page, grouped by page with the one you're on first. On a phone
// or tablet (no keyboard, a finger), the gestures instead.

import { loupeOpen } from "../loupe.js";
import { dialog, h } from "../ui.js";

// Kept in step with each page's own key handling. Cull, Duplicates and Events keep their own lists
// (their KEYS, which a page hands over as `keys`); these are the same, for when another page is open.
const CULL = [
  ["P", "Pick, then on to the next undecided frame (pressing it again changes nothing)"],
  ["X", "Reject, then on to the next undecided frame"],
  ["0", "Clear the pick or reject and the stars (stays on the frame)"],
  ["1 – 5", "Stars"], ["6 7 8 9", "Red, yellow, green, blue label"],
  ["⇧ P", "Keep this frame, reject the rest of the stack (asks first above 10 frames or a minute)"],
  ["A", "Accept: keep the best frame, reject the rest of the stack (asks the same way)"],
  ["→ / L", "Next frame (at the last one, again for the next stack)"], ["← / H", "Previous frame"],
  ["↓ / J / N / Enter", "Next stack"], ["↑ / K / B", "Previous stack"],
  ["C", "Compare 2–6 frames side by side, with one zoom"], ["G", "Everyone's faces across the stack"],
  ["W", "Why: how this frame ranks in its stack"], ["S", "Shot list: tick the moments this frame covers (1–9 tick the first nine)"],
  ["Z / ⇧ Z", "Zoom to 100% / 50% (again for the whole photo)"], ["Space", "Open in the viewer"],
  ["F", "Favourite"], ["T", "Keywords"],
];
const VIEWER = [
  ["← → / H L", "Previous / next photo"], ["Home / End", "First / last photo"],
  ["P / X", "Pick / reject (pressing it again changes nothing)"], ["0", "Clear the pick or reject and the stars"],
  ["1 – 5", "Stars"], ["6 7 8 9", "Red, yellow, green, blue label"],
  ["F", "Favourite (yours, apart from picks)"], ["T", "Keywords"], ["B", "Add to an album"],
  ["R", "A RAW + JPEG: reject its RAW (the JPEG stays), or keep a rejected one"], ["⇧ R", "Let the RAW + JPEG rules decide again"],
  ["Z / ⇧ Z", "Zoom to 100% / 50% (again for the whole photo)"],
  ["O", "Show the histogram, clipped highlights or where it's sharp"], ["I", "Info panel"],
  ["Esc", "Back to the whole photo, then close the viewer"],
];
const GRID = [
  ["← → ↑ ↓", "Move between photos (with ⇧, select as you go)"],
  ["Home / End", "First / last photo"], ["Page Up / Down", "A screen up / down"],
  ["Enter / Space", "Open the photo in the viewer"],
  ["P / X", "Pick / reject the selection (pressing it again changes nothing; the tile's own buttons switch it on and off)"],
  ["0", "Clear the pick or reject and the stars (labels stay)"],
  ["1 – 5", "Stars"], ["6 7 8 9", "Red, yellow, green, blue label (again to take it off)"],
  ["F", "Favourite"], ["T", "Keywords"], ["B", "Add to an album"], ["A", "Accept the suggestions"],
  ["C", "Compare the selection side by side"],
  ["⇧ click / ⌘ click", "Select a range / one more photo"],
  ["⌘ A", "Select everything loaded; again for everything matching"], ["⌘ ⇧ I", "Invert the selection"],
  ["Esc", "Clear the selection"],
  ["Move…", "In the bar over a selection: move its files to another folder, or rename them"],
];
// (Said under the grid's list: what its keys reach, and how a change is confirmed.)
const GRID_NOTE = "With Stacks on, the keys reach every frame of the stacks selected. A message with Undo says what changed.";
const GROUPS = [["J / K", "Next / previous folder group (or set, or selection)"],
  ["Enter", "Open it: a group's sets, a set's copies in the viewer, a selection's album"]];
const EVENTS = [["J / K", "Next / previous event or trip"], ["Enter", "Open it"]];
const HISTORY = [["J / K or ↓ ↑", "Next / previous step"], ["Enter", "Show the photos that step changed"]];
const PEOPLE = [
  ["Y / N", "“Is this…?”: yes / no (a no is remembered)"], ["S", "“Is this…?”: someone else"], ["→", "Skip that question"],
  ["Esc, then S / X", "On a face group's card: hide it as strangers / not a face"], ["Enter", "On a face group's card: name it"],
];
const COMPARE = [
  ["1 – 6 / ← →", "Highlight a photo"], ["P / X", "Pick / reject the highlighted photo"],
  ["Space", "Open the highlighted photo"], ["Enter", "Keep the highlighted one, reject the others"], ["Esc", "Close"],
];
const MAPS = [
  ["Tab", "Places: reach the places on the map"],
  ["← → ↑ ↓", "Move the pointer (Library › Explore; with ⇧, one photo at a time), or go from place to place (Places)"],
  ["Enter", "Open the photo at the pointer, or the place's card"], ["Esc", "Places: back to the map; Explore: clear the selection"],
  ["+ / −", "Explore: zoom in and out (or pinch, ⌃ and the wheel); Home shows everything"],
  ["Space", "Explore: select the photo at the pointer"], ["⇧ drag", "Explore: select the photos in a box"],
];
const EVERYWHERE = [
  ["U / ⌘ Z", "Undo (also after a reload; anything changed since is kept)"], ["⇧ U / ⌘ ⇧ Z", "Redo what you just undid"],
  ["/", "Search"], ["⌘ K", "Go anywhere or do anything: pages, people, keywords, albums, events, folders, years"],
  ["Esc", "Close, or clear the selection"], ["?", "This list"],
  ["← →", "Inside a group of buttons (tabs, choices, menus): from one to the next"],
  ["Tab", "The first stop on every page is Skip to the photos"],
];

const TOUCH = {
  cull: ["Cull", [["Swipe up", "Pick"], ["Swipe down", "Reject"], ["Swipe left / right", "Next / previous frame"],
    ["Tap", "Open the frame in the viewer"], ["Next", "The next stack (the button under the photo)"]]],
  viewer: ["The viewer", [["Swipe left / right", "Next / previous photo"], ["Double-tap", "Zoom to 100% there, and back"],
    ["Pick, Reject, stars", "The buttons under the photo"], ["Back", "Close the viewer"]]],
  grid: ["Library, Search and albums", [["Tap", "Open a photo"], ["Press and hold", "Select a photo; then tap to add or take off more"],
    ["The bar over a selection", "Pick, reject, stars, keywords, albums and Move… for everything selected"],
    ["Pinch, drag", "Library › Explore: zoom and move around; a tap on photos too small to see zooms in"],
    ["Select, then drag", "Library › Explore: select the photos in a box"]]],
  everywhere: ["Everywhere", [["Undo", "The Undo button on the message after a change, or History (top right)"],
    ["Search", "The magnifying glass at the top"]]],
};

/** Which page's keys come first: the viewer when it's open, else the page in view. */
function current() {
  if (loupeOpen()) return "viewer";
  const view = (location.hash.match(/^#\/p\/[^/]+\/([a-z-]+)/) || [])[1] || "home";
  const tab = new URLSearchParams(location.hash.split("?")[1] || "").get("tab");
  if (view === "tidy" || view === "dupes") return !tab || tab === "dupes" ? "dupes" : "other";
  if (view === "places" || (view === "library" && /[?&](view|lens)=(map|explore)\b/.test(location.hash))) return "maps";
  return { library: "grid", search: "grid", albums: "grid", cull: "cull", events: "events", history: "history",
    people: "people" }[view] || "other";
}

// A page's own list (its `keys`, by name) takes the place of the copy here.
const OWN = { Cull: "cull", Duplicates: "dupes", Events: "events" };

/** The shortcuts, by page. `own` ({name, rows}) is the open page's own list (Cull, Duplicates and
 * Events hand theirs over). */
export function keysDialog(own) {
  const here = current();
  const touch = matchMedia("(hover: none) and (pointer: coarse)").matches;
  // (A gesture isn't a key: it's said in words, not drawn as one.)
  const list = (rows) => h("div", { class: "keys-help" }, rows.flatMap(([k, t]) => [touch ? h("b", { class: "gesture" }, k) : h("kbd", {}, k), h("span", {}, t)]));
  const section = ([title, rows, note]) => h("section", { class: "keys-group" }, h("h4", {}, title), list(rows),
    note ? h("p", { class: "note", style: { margin: "8px 0 0" } }, note) : null);
  if (touch) {
    const order = [here === "cull" ? "cull" : here === "viewer" ? "viewer" : here === "grid" ? "grid" : null, "cull", "viewer", "grid", "everywhere"]
      .filter((k, i, a) => k && a.indexOf(k) === i);
    atTop(dialog("Gestures", h("div", { class: "stack-v keys-dialog" }, order.map((k) => section(TOUCH[k])),
      h("p", { class: "note" }, "With a keyboard attached, press ? for the keyboard shortcuts.")), [{ label: "Close", primary: true }]));
    return;
  }
  const groups = {
    cull: ["Cull", CULL],
    viewer: ["The viewer", VIEWER],
    grid: ["Library, Search and albums", GRID, GRID_NOTE],
    dupes: ["Tidy › Duplicates", GROUPS],
    events: ["Events", EVENTS],
    history: ["History", HISTORY],
    people: ["People", PEOPLE],
    compare: ["Compare", COMPARE],
    maps: ["Maps", MAPS],
  };
  const mine = OWN[own?.name];
  if (mine && own.rows?.length) groups[mine][1] = own.rows.filter(([k]) => !/^(U\b|\?)/.test(k));   // (undo and ? are under Everywhere)
  const order = [here, "cull", "viewer", "grid", "compare", "dupes", "events", "people", "maps", "history"]
    .filter((k, i, a) => groups[k] && a.indexOf(k) === i);
  atTop(dialog("Keyboard shortcuts", h("div", { class: "stack-v keys-dialog" },
    order.map((k) => section(groups[k])), section(["Everywhere", EVERYWHERE])), [{ label: "Close", primary: true }]));
}

/** Open on the page you're on (the first group), not scrolled down to the Close button. */
function atTop(dlg) {
  requestAnimationFrame(() => { dlg.scrollTop = 0; });
}
