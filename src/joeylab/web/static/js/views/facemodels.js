// Face recognition's models: the choice between the sets (what each is good at, its licence and its
// download), made when face recognition is turned on (the welcome page, People, Settings), and
// switching from one set to the other.

import { api, P } from "../api.js";
import { h, n, toast } from "../ui.js";

let seq = 0;

const download = (m) => `A one-time download of about ${n(m.download_mb)} MB${m.kept_mb !== m.download_mb ? `, ${n(m.kept_mb)} MB kept` : ""}.`;
const useClass = (m) => (m.key === "insightface" ? "warn" : "pick");

/** What a set of models is good at, its licence (linked) and its download, under its name. */
export function aboutModels(m, id) {
  return h("div", { class: "model-about", id },
    h("p", {}, m.good),
    h("p", { class: "note" }, `${m.licence} `, m.links.map((l, i) => [i ? " · " : "",
      h("a", { href: l.url, target: "_blank", rel: "noopener" }, l.label)])),
    h("p", { class: "note" }, download(m), ` ${m.credit}.`));
}

/** The models' name and who may use them, as one line: "Most accurate · Personal, non-commercial use only". */
export const modelName = (m) => [h("b", {}, m.title), " ", h("span", { class: `pill ${useClass(m)}` }, m.use)];

/** The choice, with nothing chosen unless `current` is given; the button stays off until one is.
 * Turning on agrees to the chosen models' licence. `library` (when there is one) is updated
 * straight away so faces are found; `onDone(status)` runs after. */
export function chooseModels(status, { current = null, library = null, onDone = null, onCancel = null } = {}) {
  const name = `face-models-${++seq}`;
  let chosen = current;
  // Faces found with the other models are all found again: say so before it's agreed to.
  const switching = h("p", { class: "note", hidden: !(status.model && chosen && chosen !== status.model) }, status.switch_note);
  const go = h("button", { class: "btn primary", type: "button", disabled: !chosen, onclick: async () => {
    go.disabled = true;
    try {
      const st = await api("/api/faces", { method: "POST", body: { enabled: true, model: chosen, accept_licence: true } });
      if (library) await api(P(library, "/jobs"), { method: "POST", body: {} }).catch(() => null);
      toast(library ? "Face recognition is on. Finding faces in this library now." : "Face recognition is on. Faces are found as your photos are read.");
      onDone?.(st);
    } catch (e) { toast(e.message, { kind: "error" }); go.disabled = false; }
  } }, "Agree and turn on");
  const cards = status.choices.map((m) => {
    const about = `${name}-${m.key}`;
    const radio = h("input", { type: "radio", name, value: m.key, checked: m.key === current, "aria-describedby": about,
      onchange: () => { chosen = m.key; go.disabled = false; switching.hidden = !(status.model && chosen !== status.model); } });
    return h("div", { class: "model-choice", onclick: (e) => { if (!e.target.closest("a, label, input")) radio.click(); } },
      h("label", { class: "check" }, radio, h("span", { class: "model-name" }, modelName(m))),
      aboutModels(m, about));
  });
  return h("fieldset", { class: "model-choices" },
    h("legend", { class: "label" }, "Choose the face models"),
    h("div", { class: "model-grid" }, cards),
    switching,
    h("p", { class: "note" }, "Turning face recognition on means you agree to the licence of the models you chose. You can switch later."),
    h("div", { class: "row" }, go, onCancel ? h("button", { class: "btn ghost", type: "button", onclick: onCancel }, "Cancel") : null));
}

/** Face recognition is on: the models in use, and switching to the other set after saying what happens. */
export function currentModels(status, { library = null, onDone = null } = {}) {
  const m = status.choices.find((x) => x.key === status.model);
  const other = status.choices.find((x) => x.key !== status.model);
  const ask = h("div");
  const box = h("div", { class: "panel stack-v model-current" },
    h("div", { class: "label" }, "Face models"),
    h("div", { class: "model-name" }, modelName(m)),
    aboutModels(m),
    status.earlier ? h("p", { class: "note" }, "You had these models before there was a choice, so they stay in use until you switch.") : null,
    other ? h("div", { class: "row" }, h("button", { class: "btn", type: "button", "aria-expanded": "false", onclick: (e) => {
      const open = !ask.firstChild;
      e.currentTarget.setAttribute("aria-expanded", String(open));
      ask.replaceChildren(...(open ? [switchPanel()] : []));
    } }, `Switch to “${other.title}”…`)) : null,
    ask);
  function switchPanel() {
    const go = h("button", { class: "btn primary", type: "button", onclick: async () => {
      go.disabled = true;
      try {
        const st = await api("/api/faces", { method: "POST", body: { enabled: true, model: other.key, accept_licence: true } });
        if (library) await api(P(library, "/jobs"), { method: "POST", body: {} }).catch(() => null);
        toast(library ? "Switched. Finding every face in this library again; your other libraries follow at their next update."
          : "Switched. Every library's faces are found again at its next update.");
        onDone?.(st);
      } catch (e) { toast(e.message, { kind: "error" }); go.disabled = false; }
    } }, "Agree and switch");
    return h("div", { class: "stack-v model-switch" },
      h("div", { class: "label" }, "Switch to"),
      h("div", { class: "model-name" }, modelName(other)),
      aboutModels(other),
      h("p", {}, status.switch_note),
      h("div", { class: "row" }, go, h("button", { class: "btn ghost", type: "button", onclick: () => box.querySelector("[aria-expanded]")?.click() }, "Cancel")));
  }
  return box;
}

/** While a library's faces are found again after a switch: how far it is. */
export function switchNote(sw, status, { onStart = null, running = false } = {}) {
  if (!sw) return null;
  const to = status?.choices?.find((x) => x.key === sw.to);
  return h("div", { class: "result row", role: "status" },
    h("span", {}, `Finding every face again with the ${to ? `“${to.title}”` : "new"} models: ${n(sw.done)} of ${n(sw.total)} photos so far. `
      + "Names stay; suggestions fill in as it goes."),
    onStart && !running ? h("button", { class: "btn small", type: "button", onclick: onStart }, "Carry on now") : null);
}
