// Moving rejected photos (or empty folders) to the Trash, for the Mac's owner. The server checks
// every file first (read-only) and says what stays behind for each; nothing moves until the count
// is typed back, and each file is checked again just before it goes. Put Back in Finder undoes it.

import { api, P } from "../api.js";
import { forgetDetails } from "../loupe.js";
import { emit, store } from "../store.js";
import { agree, bytes, clear, dialog, h, icon, n, plural } from "../ui.js";
import { trashNote, trashSummary } from "./backups.js";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function follow(p, job, onProgress) {
  while (job.state === "checking" || job.state === "moving") {
    onProgress(job);
    await sleep(job.state === "moving" ? 500 : 700);
    job = await api(P(p, `/trash/${job.token}`));
  }
  return job;
}

/** A folder by its last two names (the whole path on hover). */
function folderName(path) {
  const parts = path.split("/").filter(Boolean);
  return h("span", { class: "mono dup-path", title: path },
    parts.length > 1 ? h("span", { class: "faint" }, `${parts.at(-2)}/`) : null, h("b", {}, parts.at(-1) || path));
}

/** Each folder's loss: [{path, loses, keeps, total}]. A folder left with nothing is marked. */
export function folderLosses(rows, { max = 12 } = {}) {
  if (!rows.length) return null;
  const shown = rows.slice(0, max);
  return h("div", { class: "folder-losses" },
    h("div", { class: "fl-row fl-head" }, h("span", {}, "Folder"), h("span", {}, "Loses"), h("span", {}, "Keeps")),
    shown.map((r) => h("div", { class: "fl-row" }, folderName(r.path),
      h("b", {}, n(r.loses)),
      r.keeps ? h("span", { class: "muted" }, `${n(r.keeps)} of ${n(r.total)}`)
        : h("span", { class: "pill reject", title: "Nothing of this library's would be left in this folder" }, "emptied"))),
    rows.length > shown.length ? h("div", { class: "muted small" }, `…and ${plural(rows.length - shown.length, "more folder")}`) : null);
}

/** Per file: a checked backup on another drive, or the only copy anywhere (information only). */
export function backupsPart(b, noun = "file") {
  if (!b?.summary) return "";
  const base = (path) => path.split("/").pop();
  return h("div", { class: "stack-v" }, trashSummary(b.summary, noun),
    b.files.length ? h("details", { class: "bk-files", open: b.files.length <= 6 }, h("summary", { class: "label" }, `Each ${noun} and its backups`),
      h("div", { class: "stack-v trash-list" }, b.files.map((f) => h("div", { class: "bk-file", title: f.path },
        h("span", { class: "mono" }, base(f.path)), trashNote(f))))) : null);
}

/** what: "rejects" (the rejected photos matching `filter`, or just `ids`, or the rejected extra copies
 * of the duplicate sets `set_ids`) or "folders" (empty folders). `intro` goes above the check's result. */
export async function trashDialog({ what, filter = {}, ids, set_ids, title, intro, onDone } = {}) {
  const p = store.project;
  const body = h("div", { class: "stack-v trash-dlg" });
  title = title || (what === "folders" ? "Move empty folders to the Trash" : set_ids ? "Move extra copies to the Trash" : "Move rejected photos to the Trash");
  const dlg = dialog(title, body, [{ label: "Close" }]);
  const progress = (j) => {
    const verb = j.state === "moving" ? "Moving to the Trash" : what === "folders" ? "Looking for empty folders" : "Checking each file and its copies (read-only)";
    clear(body).append(intro || "", h("div", { class: "muted" }, j.total ? `${verb}… ${n(j.done)} of ${n(j.total)}` : `${verb}…`),
      j.total ? h("div", { class: "bar" }, h("i", { style: { width: `${Math.round((100 * j.done) / j.total)}%` } })) : null);
  };
  let job;
  try {
    job = await api(P(p, "/trash/plan"), { method: "POST", body: { what, filter, ids, set_ids } });
    job = await follow(p, job, progress);
  } catch (e) { clear(body).append(h("div", { class: "result err" }, e.message)); return; }
  if (!dlg.open) return;
  if (job.state === "error") { clear(body).append(h("div", { class: "result err" }, job.error)); return; }
  paint(job);

  function paint(j) {
    let includeLast = false;
    const confirmBox = h("div", { class: "stack-v" });
    const lossBox = h("div");
    const goCount = () => (what === "folders" ? j.go.all : includeLast ? j.go.all : j.go.copies);
    const goBytes = () => (includeLast ? j.go.all_bytes : j.go.copies_bytes);
    const parts = [];
    if (what === "folders") {
      parts.push(j.folders.length
        ? [h("p", {}, `${plural(j.folders.length, "folder")} ${agree(j.folders.length, "holds", "hold")} nothing, or only leftovers like .DS_Store:`),
          h("div", { class: "stack-v trash-list" }, j.folders.slice(0, 200).map((f) => h("div", { class: "path" }, f.path,
            f.leftovers || f.inside ? h("span", { class: "muted" }, ` · ${[f.leftovers ? plural(f.leftovers, "leftover") : "", f.inside ? plural(f.inside, "empty folder") + " inside" : ""].filter(Boolean).join(", ")}`) : null)))]
        : h("p", { class: "muted" }, "No empty folders."));
    } else {
      if (intro) parts.push(intro);
      if (j.scope) {
        parts.push(h("p", {}, h("b", {}, plural(Object.values(j.summary).reduce((a, s) => a + s.n, 0), "file")),
          ` from ${plural(j.scope.photos, "rejected extra copy", "rejected extra copies")} in ${plural(j.scope.set_ids, "duplicate set")}.`,
          j.scope.not_rejected ? ` ${plural(j.scope.not_rejected, "extra copy", "extra copies")} that ${agree(j.scope.not_rejected, "isn't", "aren't")} rejected ${agree(j.scope.not_rejected, "stays", "stay")}.` : ""));
      }
      const order = ["identical", "resaved", "twin", "last", "skip"];
      parts.push(h("div", { class: "trash-table" }, order.filter((c) => j.summary[c]).map((c) => {
        const row = j.summary[c];
        const goes = c === "identical" || c === "resaved";
        return h("div", { class: `trash-row ${c}`, dataset: { cat: c } },
          h("b", {}, n(row.n)), h("span", { class: "muted" }, row.bytes ? bytes(row.bytes) : ""), h("span", { class: "grow" }, row.label),
          h("span", { class: `pill ${goes ? "pick" : "neutral"}`, dataset: { mark: c } }, goes ? "goes" : "stays"));
      })));
      if (j.skipped.length) parts.push(h("div", { class: "muted small" }, j.skipped.map((s) => h("div", {}, `${n(s.n)} skipped: ${s.why}`))));
      parts.push(backupsPart(j.backups));
      parts.push(lossBox);
      const lastN = j.go.all - j.go.copies;
      if (lastN) {
        const only = j.backups?.summary?.only_copy || 0;
        const warn = h("div", { class: "result err", hidden: true },
          h("b", {}, `${plural(lastN, "file")} with no other copy on ${agree(lastN, "its", "their")} drive will go. Once you empty the Trash, ${agree(lastN, "it's", "they're")} gone from there.`
            + (only ? ` ${n(only)} of them ${agree(only, "is the only copy", "are the only copies")} anywhere JoeyLab knows.` : "")),
          j.last_examples.map((x) => h("div", { class: "path" }, x)), lastN > j.last_examples.length ? h("div", {}, `…and ${n(lastN - j.last_examples.length)} more`) : null);
        const box = h("input", { type: "checkbox", onchange: (e) => {
          includeLast = e.target.checked;
          warn.hidden = !includeLast;
          body.querySelectorAll("[data-mark=twin], [data-mark=last]").forEach((m) => { m.textContent = includeLast ? "goes" : "stays"; m.classList.toggle("reject", includeLast); m.classList.toggle("neutral", !includeLast); });
          paintLosses();
          paintConfirm();
        } });
        parts.push(h("label", { class: "check" }, box, h("span", {}, `Also move the ${lastN === 1 ? "file that has" : `${n(lastN)} files that have`} no copy on ${agree(lastN, "its", "their")} drive (${bytes(j.go.all_bytes - j.go.copies_bytes)})`)), warn);
      }
    }
    clear(body).append(...parts, confirmBox);
    if (what !== "folders") paintLosses();
    paintConfirm();

    // What each folder loses and keeps, for the files that would go now.
    function paintLosses() {
      const rows = (j.by_folder || []).map((f) => {
        const loses = f.copies + (includeLast ? f.last : 0);
        return { path: f.path, loses, keeps: f.total - loses, total: f.total };
      }).filter((r) => r.loses);
      clear(lossBox).append(folderLosses(rows) || "");
    }

    function paintConfirm() {
      const count = goCount();
      if (!count) { clear(confirmBox).append(h("p", { class: "muted" }, "Nothing to move.")); return; }
      const label = what === "folders" ? plural(count, "folder") : `${plural(count, "file")} (${bytes(goBytes())})`;
      const input = h("input", { class: "input", inputmode: "numeric", autocomplete: "off", "aria-label": `Type ${count} to confirm`,
        oninput: () => { btn.disabled = input.value.replace(/[,\s]/g, "") !== String(count); },
        onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); if (!btn.disabled) run(count); } } });
      const btn = h("button", { type: "button", class: "btn primary danger-fill", disabled: true, onclick: () => run(count) }, icon("close"), "Move to Trash");
      clear(confirmBox).append(
        h("p", { class: "note" }, "They go to the Trash, not away for good: until you empty it, Put Back in Finder returns them. On an external drive the space comes back when you empty the Trash."),
        h("label", { class: "field" }, h("span", {}, `Type ${count} to move ${label} to the Trash`), h("div", { class: "row", style: { flexWrap: "nowrap" } }, input, btn)));
      setTimeout(() => input.focus(), 0);
    }

    async function run(count) {
      let r;
      try {
        r = await api(P(p, `/trash/${j.token}/run`), { method: "POST", body: { count, include_last: includeLast } });
        r = await follow(p, r, progress);
      } catch (e) { clear(body).append(h("div", { class: "result err" }, e.message)); return; }
      if (r.state === "error") { clear(body).append(h("div", { class: "result err" }, r.error)); return; }
      const res = r.result;
      // The catalog already shows them gone: every page, count and the viewer follow at once.
      store.overview = null;
      store.overviewStale = true;
      forgetDetails();
      if (res.gone?.length) emit("gone", res.gone);
      clear(body).append(
        h("div", { class: "result" }, h("b", {}, what === "folders" ? `Moved ${plural(res.moved, "folder")} to the Trash.`
          : `Moved ${plural(res.moved, "file")} (${bytes(res.bytes)}) to the Trash.`)),
        h("p", { class: "note" }, "To get the space back, empty the Trash in Finder. Until then, Put Back returns anything: ",
          h("a", { href: `#/p/${encodeURIComponent(p)}/history?tab=trash` }, "History › Moved to the Trash"),
          " puts it back where it was, and so does Finder's Put Back (JoeyLab finds it again on its next update)."),
        res.emptied ? h("p", {}, `${plural(res.emptied, "folder")} left empty: see `,
          h("a", { href: `#/p/${encodeURIComponent(p)}/tidy?tab=health&empty=1` }, "Tidy › Health › Empty folders"), ".") : null,
        res.left.length ? h("details", {}, h("summary", { class: "label" }, `${plural(res.left.length, "item")} left alone`),
          res.left.map((x) => h("div", { class: "path" }, `${x.path}: ${x.why}`))) : null,
        h("div", { class: "muted small mono" }, `Log: ${res.log}`));
      onDone?.(res);
    }
  }
}
