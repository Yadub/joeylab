// "Still working" notes. While an update runs, a page that needs one of its steps says what it's
// waiting for and how far along that is ("Search will be ready when indexing finishes · 40%"),
// and once the step is done offers to show the result.

import { on, store } from "../store.js";
import { h } from "../ui.js";

// The update's steps in order (pipeline.STEPS), and how each reads while it's running.
const STEPS = ["scan", "analyse", "group", "faces", "ai", "search", "places", "check"];
const DOING = {
  scan: "looking for photos", analyse: "analysing photos", group: "stacking", faces: "finding faces",
  ai: "comparing photos", search: "indexing for search", places: "finding places and events", check: "checking your folders",
};

/** Is the running update (job) past `step`, or is no update running? */
export function stepDone(job, step) {
  if (job?.state !== "running") return true;
  return (job.ready || []).includes(step) || STEPS.indexOf(job.step) > STEPS.indexOf(step);
}

/** How far the update is: "40%" on the step a page waits for, or what it's doing first. */
export function progressText(job, step) {
  if (job?.state !== "running") return "";
  const pct = job.total ? `${Math.round((100 * job.done) / job.total)}%` : "";
  if (job.step === step) return pct;
  const doing = DOING[job.step];
  return doing ? `first, ${doing}${pct ? ` (${pct})` : ""}` : pct;
}

/** A note for a page waiting on `step` of the update. `text` (a string, or a function of the job)
 * says what for; `progressOf` is the step whose progress to show (the one being waited for unless
 * the text names another, say "when analysis finishes"); `when` says whether this page needs it
 * at all (it has nothing yet). Once the step is done, `ready` and a Show button (`onReady`) take
 * its place. It hides itself whenever there's nothing to wait for, and keeps itself current. */
export function waitNote({ step, text, progressOf = step, when = () => true, ready = "", onReady = null }) {
  const el = h("div", { class: "result wait-note", role: "status", hidden: true });
  let waited = false, shown = false;
  const paint = (j) => {
    if (shown && !el.isConnected) { off(); return; }
    shown = shown || el.isConnected;
    if (!stepDone(j, step) && when(j)) {
      waited = true;
      const at = progressText(j, progressOf);
      el.hidden = false;
      el.replaceChildren(h("span", { class: "spinner", "aria-hidden": "true" }),
        h("span", {}, `${typeof text === "function" ? text(j) : text}${at ? ` · ${at}` : ""}`));
    } else if (waited && ready) {
      waited = false;
      el.hidden = false;
      el.replaceChildren(h("span", {}, ready), onReady ? h("button", { class: "btn small", type: "button",
        onclick: () => { el.hidden = true; onReady(); } }, "Show") : null);
    } else if (!el.querySelector("button")) el.hidden = true;
  };
  const off = on((type, j) => { if (type === "job") paint(j); });
  setTimeout(() => paint(store.job), 0);           // once it's on the page
  return el;
}
