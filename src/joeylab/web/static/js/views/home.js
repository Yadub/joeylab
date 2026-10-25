// Home: what this library is for decides the front page.
//   Shoot   — progress toward a delivery, what to do next, the shot list, close calls.
//   Archive — on this day, years and their best photos, trips and events, people, library health.

import { api, img, P } from "../api.js";
import { thumbButton } from "../grid.js";
import { openLoupe } from "../loupe.js";
import { decide, on, remember, row, saveSettings, settings, store, undo } from "../store.js";
import { agree, bytes, clear, closePopover, eventSub, flagLabel, h, icon, n, plural, popover, roving, toast, when } from "../ui.js";
import { shotState } from "./shotlist.js";
import { stepDone, waitNote } from "./waiting.js";

// The first update's steps, in the order it does them (the job's step, pipeline.STEPS). This page
// waits for the first three; the rest carry on while you cull.
const STEPS = [
  [["scan"], "Finding your photos"],
  [["analyse"], "Checking focus, exposure and eyes"],
  [["group"], "Stacking bursts, finding duplicates, suggesting picks"],
  [["faces"], "Finding faces"],
  [["ai", "search"], "Learning what's in each photo, for search"],
  [["places", "check"], "Places and events, and a last check of your folders"],
];
const stepOf = (step) => STEPS.findIndex(([keys]) => keys.includes(step));

const SVGNS = "http://www.w3.org/2000/svg";
const s = (tag, attrs = {}) => {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
};

export function mount(root, ctx) {
  const page = h("div", { class: "page" });
  root.append(page);
  const p = ctx.project;
  const link = (view, q) => `#/p/${encodeURIComponent(p)}/${view}${q ? "?" + q : ""}`;
  const lib = (q) => link("library", q);

  let gen = 0;
  let waiting = false;                     // showing "Reading your photos": fills in by itself
  let dashboard = false;                   // showing the dashboard: newer numbers are drawn in place
  let lastHome = null;                     // the archive's years, events and people, for drawing in place
  let offJob = () => {};
  function render() { return draw(); }
  // `given`: newer numbers the top bar just fetched (the server counted again): drawn in place, keeping
  // the page where it was scrolled to, with the archive's sections as they were.
  async function draw(given = null) {
    const mine = ++gen;
    const quiet = !!given;
    let o, st, job;
    try {
      [o, st, job] = await Promise.all([given || api(P(p, "/overview")), settings(true),
        api(P(p, "/jobs/current")).then((r) => r.job).catch(() => null)]);
    } catch (e) { if (!quiet) clear(page).append(h("div", { class: "result err" }, e.message)); return; }
    if (mine !== gen) return;
    store.overview = o;
    remember(o.top);
    const c = o.counts;
    offJob(); offJob = () => {};
    waiting = false;
    dashboard = false;
    const y = window.scrollY;
    if (!o.roots.length) { clear(page).append(noFolders(o)); return; }
    // The first update of a new library: nothing is grouped or scored yet, so say what's happening
    // rather than show a dashboard of zeros.
    const first = c.photos === 0 || c.analysed + c.failed === 0 || (c.groups === 0 && c.undated < c.photos);
    if (first && job?.state === "running" && !(job.ready || []).includes("group")) { clear(page).append(reading(o, job)); return; }
    if (c.photos === 0) { clear(page).append(noPhotos(o, job)); return; }
    if (!st.purpose) { clear(page).append(chooser(o)); return; }
    const shoot = st.purpose === "shoot";
    clear(page).append(
      h("div", { class: "page-head" },
        h("div", {},
          h("h1", {}, o.name),
          h("div", { class: "sub" }, [
            plural(c.photos - c.videos, "photo"), c.videos ? plural(c.videos, "video") : null,
            o.span.from ? `${when(o.span.from).split(" · ")[0]} – ${when(o.span.to).split(" · ")[0]}` : null,
            c.events ? plural(c.events, "event") : null, c.places ? plural(c.places, "place") : null,
            c.pairs ? `${n(c.pairs)} RAW+JPEG pairs` : null,
          ].filter(Boolean).join(" · "))),
        h("div", { class: "actions" },
          h("button", { class: "btn ghost", type: "button", title: "What this library is for: changes this page",
            onclick: (e) => purposeMenu(e.currentTarget, st) }, shoot ? "A shoot" : "An archive", icon("down")),
          h("button", { class: "btn", title: "Look for new and changed photos", onclick: rescan }, icon("refresh"), "Update"),
          shoot ? h("a", { class: "btn primary", href: link("cull") }, "Start culling")
            : h("a", { class: "btn primary", href: link("library") }, "Browse the library"))),
      banners(o),
      ctx.params.get("learn") ? learnSection() : null,
      ...(shoot ? shootSections(o, st) : archiveSections(o, quiet)),
    );
    dashboard = o.seq ?? true;
    if (quiet) window.scrollTo({ top: y, behavior: "instant" });
  }

  // Newer numbers arrived: draw them in place, unless the keyboard is on something here or a menu is
  // open (drawing would lose your place; the next numbers are drawn instead).
  const offOverview = on((type, o) => {
    if (type !== "overview" || !dashboard || o?.seq === dashboard || o?.name !== p || !page.isConnected) return;
    if (document.querySelector(".popover") || page.contains(document.activeElement)) return;
    draw(o);
  });

  // ------------------------------------------------------------ first visit
  function chooser(o) {
    const pick = async (purpose, then) => {
      await saveSettings({ purpose, show_suggestions: purpose === "shoot" });
      if (then) ctx.go(then); else render();
    };
    const card = (title, text, purpose, then, ic) => h("button", { type: "button", class: "choice", onclick: () => pick(purpose, then) },
      icon(ic), h("b", {}, title), h("span", {}, text));
    return h("div", { class: "stack-v first-run" },
      h("div", { class: "page-head" }, h("div", {}, h("h1", {}, `What is “${o.name}” for?`),
        h("div", { class: "sub" }, `${plural(o.counts.photos, "photo")} so far. This decides what JoeyLab shows first; change it any time.`))),
      h("div", { class: "choices" },
        card("Cull a shoot", "A wedding, an event, a trip: pick the keepers and deliver. Bursts, suggestions, a delivery target and a shot list.", "shoot", null, "stack"),
        card("Explore and tidy an archive", "Years of photos: browse by time, people, places and events, and find what to tidy. No pressure to decide every photo.", "archive", null, "calendar"),
        card("Just find duplicates", "See which folders repeat each other and how much space that takes. Nothing is ever deleted for you.", "archive", "tidy", "broom")));
  }

  // ------------------------------------------------------------ before there's anything to show
  function noFolders(o) {
    return h("div", { class: "stack-v first-run", style: { gap: "20px" } },
      h("div", { class: "page-head" }, h("div", {}, h("h1", {}, o.name),
        h("div", { class: "sub" }, "This library has no folders yet, so there's nothing to show."))),
      h("div", { class: "panel stack-v" },
        h("b", {}, "Add the folder your photos are in"),
        h("p", { class: "note" }, "JoeyLab reads it (your files are never changed) and this page fills in: what to cull, the best frames, duplicates, people and places."),
        h("div", {}, h("a", { class: "btn primary", href: link("settings", "s=folders") }, icon("folder"), "Add a folder"))));
  }

  function noPhotos(o, job) {
    const failed = job?.state === "error";
    const unreadable = o.counts.failed;
    return h("div", { class: "stack-v first-run", style: { gap: "20px" } },
      h("div", { class: "page-head" }, h("div", {}, h("h1", {}, o.name),
        h("div", { class: "sub" }, failed ? "JoeyLab couldn't finish reading this library."
          : unreadable ? `JoeyLab found ${plural(unreadable, "file")} here but couldn't read ${agree(unreadable, "it", "any of them")} as a photo.`
            : "No photos found in this library's folders yet."))),
      failed ? h("div", { class: "result err" }, job.error || "The update stopped.") : null,
      h("div", { class: "panel stack-v" },
        h("p", { class: "note" }, failed ? "Check the folder is still there (and its drive connected), then try again."
          : unreadable ? h("span", {}, "They may be damaged, or in a format JoeyLab doesn't read. ", h("a", { href: link("tidy", "tab=health") }, "See which"))
            : "JoeyLab reads JPEG, HEIC, RAW and video files in these folders and their subfolders. Copied photos in since? Update to find them."),
        h("div", { class: "kv", style: { gridTemplateColumns: "minmax(0, 1fr)" } }, o.roots.map((r) => h("span", { class: "mono" }, r))),
        h("div", { class: "row" },
          h("button", { class: "btn primary", onclick: rescan }, icon("refresh"), failed ? "Try again" : "Update now"),
          h("a", { class: "btn", href: link("settings", "s=folders") }, icon("folder"), "Folders"))));
  }

  /** The first update of a new library, step by step, kept current until it's done. */
  function reading(o, job) {
    waiting = true;
    const c = o.counts;
    const stage = h("b");
    const bar = h("i");
    const counts = h("span", { class: "muted" });
    const found = h("span");
    const steps = h("ol", { class: "reading-steps" }, STEPS.map(([, label]) => h("li", {}, label)));
    let oc = c;                              // the counts, fetched again now and then
    const paint = (j) => {
      const k = stepOf(j.step);
      stage.textContent = k >= 0 ? STEPS[k][1] : j.stage;
      bar.style.width = j.total ? `${Math.round((100 * j.done) / j.total)}%` : "0%";
      bar.parentElement?.classList.toggle("busy", !j.total);
      counts.textContent = j.total ? `${n(j.done)} of ${n(j.total)}` : "";
      found.textContent = oc.photos ? `${plural(oc.photos, "photo")} found so far` : "Looking for photos…";
      [...steps.children].forEach((li, i) => { li.className = i < k ? "done" : i === k ? "now" : ""; });
    };
    let lastCounts = 0;
    offJob = on((type, j) => {
      if (type !== "job" || !page.isConnected) return;
      // Finished (or stopped), or the stacks and suggestions are in: show the page (the rest carries on).
      if (!j || j.state !== "running" || (j.ready || []).includes("group")) { offJob(); offJob = () => {}; render(); return; }
      paint(j);
      if (Date.now() - lastCounts > 5000) {                          // and, now and then, how many are in
        lastCounts = Date.now();
        api(P(p, "/overview")).then((oo) => { if (page.isConnected && waiting) { oc = oo.counts; paint(j); } }).catch(() => {});
      }
    });
    const el = h("div", { class: "stack-v first-run reading", style: { gap: "22px" } },
      h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Reading your photos"),
        h("div", { class: "sub" }, `JoeyLab is going through “${o.name}” for the first time. This page fills in by itself when it's done.`))),
      h("div", { class: "panel stack-v" },
        h("div", { class: "row" }, h("span", { class: "spinner" }), stage, h("span", { class: "grow" }), counts),
        h("div", { class: "reading-bar" }, bar),
        found),
      steps,
      h("p", { class: "note" }, "A few thousand photos take a few minutes; a whole drive can take an hour or more. Your files are only read, never changed. "
        + "You can close this page: JoeyLab carries on, and the Library already shows photos as they're found."),
      h("div", { class: "row" }, h("a", { class: "btn", href: link("library") }, icon("grid"), "Open the Library")));
    paint(job);
    return el;
  }

  /** A shoot or an archive: the two choices with what each changes, the current one ticked. */
  function purposeMenu(anchor, st) {
    const choice = (purpose, title, text) => h("button", { class: "menu-item purpose-item", type: "button", "aria-current": st.purpose === purpose ? "true" : null,
      onclick: async () => {
        closePopover();
        if (st.purpose === purpose) return;
        await saveSettings({ purpose, show_suggestions: purpose === "shoot" });
        render();
      } },
    h("span", { class: "grow" }, h("b", {}, title), h("span", { class: "muted" }, text)), st.purpose === purpose ? icon("check") : null);
    popover(anchor, h("div", { class: "purpose-pop" },
      h("div", { class: "label" }, "What this library is for"),
      choice("shoot", "A shoot to cull", "Opens on culling progress, with suggested picks and rejects on the photos, a delivery target and a shot list."),
      choice("archive", "An archive to explore and tidy", "Opens on years, events, people and tidying, without suggestions on the photos."),
      h("p", { class: "note" }, "Suggestions can be turned on or off on their own in Settings › This library.")));
  }

  function banners(o) {
    const c = o.counts;
    return [
      c.analysed < c.photos ? h("div", { class: "result" },
        `${plural(c.photos - c.analysed, "photo")} ${agree(c.photos - c.analysed, "is", "are")} still being analysed. Stacks, scores and events fill in as that finishes.`) : null,
      waitNote({ step: "places", progressOf: "places",
        text: (j) => `Still to come: ${[!stepDone(j, "faces") ? "people" : null,
          !stepDone(j, "search") ? "search" : null, "places and events"].filter(Boolean).join(", ")}`,
        when: (j) => (j.ready || []).includes("group") }),
      c.missing ? h("div", { class: "result" }, `${plural(c.missing, "photo")} ${agree(c.missing, "is", "are")} no longer on disk. `
        + `${agree(c.missing, "Its decisions are", "Their decisions are")} kept in case ${agree(c.missing, "it comes", "they come")} back. `,
        h("a", { href: link("settings", "s=folders") }, "Folders")) : null,
      c.failed ? h("div", { class: "result" }, `${plural(c.failed, "file")} couldn't be read. `, h("a", { href: link("tidy", "tab=health") }, "See which")) : null,
      c.cam_rated ? h("div", { class: "result row" }, h("span", {}, `${plural(c.cam_rated, "photo")} already ${agree(c.cam_rated, "has", "have")} stars from your camera, Lightroom or another app. Using them also puts an export's stars on its original.`),
        h("button", { class: "btn small", onclick: async () => {
          const r = await api(P(p, "/import/camera-ratings"), { method: "POST", body: {} });
          toast(`Used ${plural(r.n, "rating")}${r.originals ? `, ${n(r.originals)} of them on the originals of exports` : ""}`, { action: () => undo(r.batch).then(render) });
          render();
        } }, "Use them")) : null,
    ];
  }

  // ------------------------------------------------------------ shoot
  function shootSections(o, st) {
    const c = o.counts;
    const decided = c.photos - c.undecided;
    const pct = (x) => (c.photos ? (100 * x) / c.photos : 0);
    const ratedOnly = Math.max(0, decided - c.picks - c.rejects);
    const target = st.delivery_target;
    const blurry = (o.flags.blurry || 0) + (o.flags.soft || 0), closed = o.flags["eyes-closed"] || 0;
    // (`none`: what a card says in the "All clear" line when its count is 0.)
    const next = [
      { n: c.undecided_stacks, t: `${agree(c.undecided_stacks, "stack", "stacks")} to cull (${plural(c.undecided, "photo")})`, href: link("cull"), none: "every stack culled" },
      { n: c.suggest_reject, t: `${agree(c.suggest_reject, "suggested reject", "suggested rejects")} to review`, href: lib("status=undecided&suggested=reject"), none: "no suggested rejects to review" },
      { n: c.suggest_check, t: "worth checking: soft focus, blinks, exposure", href: link("cull", "suggested=check"), none: "nothing worth checking" },
      { n: blurry, t: agree(blurry, "blurry or soft photo", "blurry or soft photos"), href: lib("flag=blurry,soft"), none: "no blurry or soft photos" },
      { n: closed, t: agree(closed, "photo with closed eyes", "photos with closed eyes"), href: lib("flag=eyes-closed"), none: "no closed eyes" },
      { n: c.dup_sets, t: agree(c.dup_sets, "duplicate set", "duplicate sets"), href: link("tidy"), none: "no duplicates" },
    ];
    const clearNext = next.filter((x) => !x.n);
    const targetIn = h("input", { class: "input", id: "home-target", type: "number", min: 1, value: target || "", placeholder: "e.g. 300", style: { width: "110px" } });
    return [
      clockHint(),
      h("section", { class: "section" },
        h("h2", {}, "Progress"),
        h("div", { class: "stat-line" },
          stat(c.picks, target ? `picked of ${n(target)} to deliver` : "picked"), stat(c.rejects, "rejected"), stat(c.rated, "rated"),
          stat(c.undecided, "still to look at"), stat(c.multi_groups, agree(c.multi_groups, "stack of several frames", "stacks of several frames"))),
        target ? h("div", { class: "cullbar target", role: "img", "aria-label": `${c.picks} of ${target} picked` },
          h("i", { class: "p", style: { width: `${Math.min(100, (100 * c.picks) / target)}%` } })) :
          h("div", { class: "cullbar", role: "img", "aria-label": `${Math.round(pct(decided))}% decided` },
            h("i", { class: "p", style: { width: `${pct(c.picks)}%` } }),
            h("i", { class: "s", style: { width: `${pct(ratedOnly)}%` } }),
            h("i", { class: "r", style: { width: `${pct(c.rejects)}%` } })),
        h("div", { class: "row" },
          h("label", { class: "muted", for: "home-target" }, "Photos to deliver"), targetIn,
          h("button", { class: "btn small", onclick: async () => { await saveSettings({ delivery_target: +targetIn.value || null }); render(); } }, "Set"),
          target ? h("a", { class: "btn small", href: link("albums", "tab=balance") }, icon("keep"), "Suggest a balanced set") : null,
          o.today.photos ? h("span", { class: "muted", style: { marginLeft: "auto" } },
            `Today: ${[o.today.picked && `${n(o.today.picked)} picked`, o.today.rejected && `${n(o.today.rejected)} rejected`].filter(Boolean).join(", ") || "rated"}`) : null)),
      h("section", { class: "section" },
        h("h2", {}, "Next"),
        h("div", { class: "next-grid" },
          next.filter((x) => x.n).map((x) => h("a", { class: "next-card", href: x.href }, h("b", {}, n(x.n)), h("span", {}, x.t))),
          c.suggest_pick ? h("button", { class: "next-card", type: "button", onclick: acceptAll },
            h("b", {}, n(c.suggest_pick)), h("span", {}, agree(c.suggest_pick, "suggested pick · accept it", "suggested picks · accept them all"))) : null,
          tasteCard(o.taste),
          peopleCard(c)),
        clearNext.length ? h("p", { class: "note all-clear" }, icon("check"), `All clear: ${clearNext.map((x) => x.none).join(", ")}.`) : null),
      shotListSection(),
      o.timeline && o.timeline.buckets.length > 1 ? h("section", { class: "section timeline" },
        h("h2", {}, `Photos per ${o.timeline.unit}`), chart(o.timeline)) : null,
      strongest(o),
      noticed(o),
    ];
  }

  /** Next › people: named people, groups to name, or nothing at all when no face was found. */
  function peopleCard(c) {
    if (c.people) return h("a", { class: "next-card", href: link("people") }, h("b", {}, n(c.people)),
      h("span", {}, `${agree(c.people, "person", "people")} named · check coverage`));
    if (c.face_groups) return h("a", { class: "next-card", href: link("people", "tab=stream") }, h("b", {}, n(c.face_groups)),
      h("span", {}, `${agree(c.face_groups, "group", "groups")} of faces to name · nobody named yet`));
    return null;
  }

  function clockHint() {
    const host = h("div");
    api(P(p, "/clock")).then((d) => {
      if (!d.suggestions.length) return;
      const s = d.suggestions[0];
      const name = (k) => { const c = d.cameras.find((x) => x.key === k); return c ? `${c.camera}${c.serial ? " …" + String(c.serial).slice(-4) : ""}` : k; };
      const hrs = Math.abs(s.offset_s) / 3600;
      host.append(h("div", { class: "result row" },
        h("span", { class: "grow" }, `The clock of ${name(s.camera)} looks ${hrs >= 1 ? hrs.toFixed(1) + " hours" : Math.round(Math.abs(s.offset_s) / 60) + " minutes"} `
          + `${s.offset_s > 0 ? "behind" : "ahead of"} ${name(s.relative_to)} (${plural(s.pairs, "moment")} both cameras caught). Its photos are out of order until that's corrected.`),
        h("a", { class: "btn small", href: link("tidy", "tab=clock") }, "Review and fix")));
    }).catch(() => {});
    return host;
  }

  function shotListSection() {
    const host = h("section", { class: "section" }, h("h2", {}, "Shot list"), h("div", { class: "muted" }, "Loading…"));
    api(P(p, "/shotlist")).then((d) => {
      remember(d.rows);
      const title = h("input", { class: "input", placeholder: "A must-have moment, e.g. Cake cutting", "aria-label": "Add to the shot list", style: { flex: "1", width: "auto" } });
      const add = h("form", { class: "row", style: { flexWrap: "nowrap", maxWidth: "640px" }, onsubmit: async (e) => {
        e.preventDefault();
        if (!title.value.trim()) return;
        await api(P(p, "/shotlist"), { method: "POST", body: { title: title.value.trim() } });
        refreshShotList();
      } }, title, h("button", { class: "btn", type: "submit" }, icon("plus"), "Add"));
      const done = d.items.filter((i) => i.done).length;
      // The frames to show: the picks that cover it, else the ones tagged for it, else what search suggests.
      const shown = (it) => (it.covered.length ? it.covered : it.tagged.length ? it.tagged : it.candidates);
      clear(host).append(
        h("h2", {}, `Shot list${d.items.length ? ` · ${done} of ${d.items.length} covered by a pick` : ""}`),
        d.items.length && done < d.items.length ? h("p", { class: "note" }, `${plural(d.items.length - done, "moment")} still to cover. `
          + "In Cull, press S on a frame to say which of these it shows; it covers them once it's picked. ",
        h("a", { class: "linkish", href: link("cull") }, "Cull")) : null,
        d.items.length ? h("div", { class: "shotlist" }, d.items.map((it) => h("div", { class: `shot-item ${it.done ? "done" : ""}` },
          h("span", { class: "tick" }, icon(it.done ? "check2" : "info")),
          h("div", { class: "grow" }, h("b", {}, it.title), h("div", { class: "muted" }, shotState(it))),
          h("div", { class: "thumbs" }, shown(it).slice(0, 4).map((id, i, arr) => {
            const r = row(id);
            return r ? thumbButton(r, () => openLoupe({ ids: arr, index: i })) : null;
          })),
          h("button", { class: "btn ghost small icon-only", title: "Remove", "aria-label": `Remove ${it.title}`, onclick: async () => {
            await api(P(p, `/shotlist/${it.id}`), { method: "DELETE" }); refreshShotList();
          } }, icon("close"))))) :
          h("div", { class: "row" }, h("span", { class: "muted" }, "Start from a list:"),
            ["wedding", "birthday", "holiday", "portrait"].map((k) => h("button", { class: "btn small", onclick: async () => {
              await api(P(p, "/shotlist/preset"), { method: "POST", body: { kind: k } }); refreshShotList();
            } }, k[0].toUpperCase() + k.slice(1)))),
        add);
    }).catch((e) => { clear(host).append(h("h2", {}, "Shot list"), h("div", { class: "muted" }, e.message)); });
    function refreshShotList() { host.replaceWith(shotListSection()); }
    return host;
  }

  // ------------------------------------------------------------ archive
  function archiveSections(o, quiet = false) {
    const host = h("div", { class: "stack-v", style: { gap: "28px" } }, h("div", { class: "muted" }, "Loading…"));
    const fill = (d) => {
      lastHome = d;
      remember(d.rows);
      clear(host).append(
        d.on_this_day.length ? h("section", { class: "section" },
          h("h2", {}, `On this day · ${new Date().toLocaleDateString(undefined, { day: "numeric", month: "long" })}`),
          h("div", { class: "otd" }, d.on_this_day.map((y) => h("div", { class: "otd-year" },
            h("b", {}, `${plural(new Date().getFullYear() - +y.year, "year")} ago · ${y.year}`),
            strip(y.ids, 110))))) : null,
        d.trips.length ? h("section", { class: "section" }, h("h2", {}, "Trips"),
          h("div", { class: "event-cards" }, d.trips.map(eventCard)),
          h("a", { class: "linkish", href: link("events", "kind=trip") }, "All trips")) : null,
        d.events.length ? h("section", { class: "section" }, h("h2", {}, "Recent events"),
          h("div", { class: "event-cards" }, d.events.slice(0, 8).map(eventCard)),
          h("a", { class: "linkish", href: link("events") }, "All events")) : null,
        d.people.length ? h("section", { class: "section" }, h("h2", {}, "People"),
          h("div", { class: "avatars" }, d.people.map((x) => h("a", { class: "avatar-link", href: lib(`person=${x.id}`), title: `${x.name} · ${plural(x.photos, "photo")}` },
            h("img", { src: `/img/${encodeURIComponent(p)}/f/${x.face}`, alt: "" }), h("span", {}, x.name))))) :
          // Nobody named: offer the groups to name, if any faces were found at all.
          o.counts.face_groups ? h("section", { class: "section" }, h("h2", {}, "People"),
            h("a", { class: "next-card", href: link("people", "tab=stream") }, h("b", {}, n(o.counts.face_groups)),
              h("span", {}, `${agree(o.counts.face_groups, "group", "groups")} of faces to name. Naming the first few dozen takes a minute or two and makes everyone findable.`))) : null,
        h("section", { class: "section" }, h("h2", {}, "Years"),
          d.years.map((y) => h("div", { class: "year-row" },
            h("div", { class: "year-head" }, h("a", { href: lib(`from=${y.year}-01-01&to=${y.year}-12-31`) }, h("b", {}, y.year)),
              h("span", { class: "muted" }, plural(y.n, "photo")),
              h("a", { class: "linkish", href: link("albums", `tab=bestof&year=${y.year}`) }, "Best of " + y.year)),
            y.best.length ? strip(y.best, 130) : h("p", { class: "muted" }, "Nothing to show: this year's photos are rejected, thumbnails or extra copies.")))),
        healthSummary(o),
        noticed(o),
      );
    };
    if (quiet && lastHome) fill(lastHome);
    else api(P(p, "/home")).then(fill).catch((e) => { clear(host).append(h("div", { class: "result err" }, e.message)); });
    return [host];
  }

  function eventCard(e) {
    const r = row(e.cover_shot_id);
    return h("a", { class: "event-card", href: e.kind === "trip" ? link("events", `open=${e.id}`) : lib(`event=${e.id}`) },
      h("div", { class: "cover", style: r ? { backgroundImage: `url(${img.thumb(p, r, "m")})` } : {} }),
      h("b", {}, e.name), h("span", { class: "muted" }, eventSub(e)));
  }

  function healthSummary(o) {
    const c = o.counts;
    const items = [
      c.dup_sets ? { t: `${bytes(c.dup_bytes)} in duplicates`, href: link("tidy", "tab=dupes") } : null,
      c.undated ? { t: `${plural(c.undated, "photo")} without a capture date`, href: lib("undated=1") } : null,
      o.flags.tiny ? { t: `${plural(o.flags.tiny, "thumbnail")} or fragments (hidden while browsing)`, href: lib("flag=tiny") } : null,
      o.flags.utility ? { t: `${plural(o.flags.utility, "screenshot")} and documents`, href: lib("flag=utility") } : null,
      c.missing ? { t: `${plural(c.missing, "photo")} missing from disk`, href: link("settings") } : null,
    ].filter(Boolean);
    return h("section", { class: "section" }, h("h2", {}, "Library health"),
      items.length ? h("div", { class: "next-grid" }, items.map((x) => h("a", { class: "next-card", href: x.href }, h("span", {}, x.t)))) : h("div", { class: "muted" }, "Nothing to tidy."),
      h("a", { class: "linkish", href: link("tidy", "tab=health") }, "Full health check, including which photos aren't backed up"));
  }

  // ------------------------------------------------------------ shared pieces
  function strip(ids, height) {
    return h("div", { class: "strip", style: { "--strip-h": `${height}px` } }, ids.map((id, i) => {
      const r = row(id);
      return r ? thumbButton(r, () => openLoupe({ ids, index: i })) : null;
    }));
  }

  function strongest(o) {
    return o.top.length ? h("section", { class: "section" },
      h("h2", {}, "Strongest frames"), strip(o.top.map((r) => r.id), 120)) : null;
  }

  function noticed(o) {
    const flags = Object.entries(o.flags).filter(([f]) => f !== "best-in-group");
    const ratings = Object.values(o.ratings).reduce((a, b) => a + b, 0);
    return h("div", { class: "two-col" },
      h("section", { class: "section" },
        h("h2", {}, "What JoeyLab noticed"),
        flags.length ? h("div", { class: "row chips-row" },
          flags.map(([f, k]) => h("a", { class: "chip", href: lib(`flag=${f}`) }, flagLabel(f), h("span", { class: "n" }, n(k)))))
          : h("div", { class: "muted" }, "Nothing flagged."),
        !o.ai.available ? h("p", { class: "note" }, "Install the AI extra (uv sync --extra ai) for search, similar photos and stacking whole scenes.")
          : o.ai.embedded < o.counts.analysed ? h("p", { class: "note" }, `Similar photos and whole-scene stacks: ${n(o.ai.embedded)} of ${plural(o.counts.analysed, "photo")} compared so far.`) : null),
      h("section", { class: "section" },
        ratings ? h("h2", {}, "Ratings") : null,
        ratings ? h("div", { class: "stack-v" }, [5, 4, 3, 2, 1].map((r) => {
          const k = o.ratings[r] || 0;
          const max = Math.max(1, ...Object.values(o.ratings));
          return h("a", { class: "hbar", href: lib(`min_rating=${r}`), style: { textDecoration: "none" } },
            h("span", { style: { color: "var(--star-ink)" } }, "★".repeat(r)),
            h("span", { class: "track" }, h("i", { style: { width: `${(100 * k) / max}%` } })),
            h("span", { class: "num muted", style: { textAlign: "right" } }, n(k)));
        })) : null,
        h("h2", { style: { marginTop: ratings ? "12px" : 0 } }, "Cameras"),
        h("div", { class: "kv" }, o.cameras.flatMap((cam) => [
          h("a", { href: lib(`camera=${encodeURIComponent(cam.serial || cam.camera)}`), style: { textDecoration: "none" } },
            cam.camera, cam.serial ? h("span", { class: "faint mono" }, ` · …${String(cam.serial).slice(-4)}`) : null),
          h("span", { class: "v" }, n(cam.n))]))));
  }

  function tasteCard(t) {
    const learned = (t?.positives || 0) + (t?.negatives || 0);
    let big, text;
    if (!t || t.status === "learning" || t.auc === null || t.auc === undefined) {
      const need = Math.max(0, (t?.needed || 12) - (t?.positives || 0)) + Math.max(0, (t?.needed || 12) - (t?.negatives || 0));
      big = n(learned);
      text = `${agree(learned, "decision", "decisions")} learned from · about ${n(need)} more picks and rejects until JoeyLab learns your taste`;
    } else {
      big = `${Math.round(t.auc * 100)}%`;
      text = t.trusted ? `taste accuracy from ${plural(learned, "decision")} · now shaping suggestions`
        : `taste accuracy from ${plural(learned, "decision")} · keep deciding, it isn't reliable yet`;
    }
    return h("a", { class: "next-card", href: link("home", "learn=1"), title: "Help it learn: 10 quick choices" },
      h("b", {}, big), h("span", {}, text), h("span", { class: "linkish" }, "Teach it faster →"));
  }

  function learnSection() {
    const host = h("section", { class: "section learn" }, h("h2", {}, "Help JoeyLab learn your taste"), h("div", { class: "muted" }, "Loading…"));
    Promise.all([api(P(p, "/taste/questions"), { params: { n: 10 } }), api(P(p, "/taste"))]).then(([d, t]) => {
      remember(d.rows);
      let left = d.rows.length;
      const explain = (t.explain || []).filter((x) => x.weight !== undefined).slice(0, 3);
      clear(host).append(h("h2", {}, "Help JoeyLab learn your taste"),
        h("p", { class: "note" }, "These are the photos JoeyLab is least sure about. Keep or skip each one; every answer teaches it the most it can."),
        explain.length ? h("p", { class: "note" }, "So far you seem to care most about: ",
          explain.map((x) => `${x.signal} (${x.weight > 0 ? "more is better" : "less is better"})`).join(", "), ".") : null,
        h("div", { class: "learn-grid" }, d.rows.map((r) => {
          const card = h("div", { class: "learn-card" },
            thumbButton(r, () => openLoupe({ ids: d.rows.map((x) => x.id), index: d.rows.indexOf(r) }), { size: "m" }),
            h("div", { class: "row" },
              h("button", { class: "btn small", onclick: () => answer(r.id, 1, card) }, icon("pick"), "Keep"),
              h("button", { class: "btn small", onclick: () => answer(r.id, -1, card) }, icon("reject"), "Skip")));
          return card;
        })),
        h("div", { class: "row" }, h("a", { class: "btn", href: link("home") }, "Done")));
      async function answer(id, pick, card) {
        await decide([{ id, pick }], pick === 1 ? "keep (teaching taste)" : "skip (teaching taste)");
        card.classList.add("answered", pick === 1 ? "kept" : "skipped");
        if (--left === 0) {
          try { await api(P(p, "/taste/train"), { method: "POST", body: {} }); toast("Thanks: taste retrained"); } catch { /* fine */ }
          ctx.go("home", { learn: 1 });
        }
      }
    }).catch((e) => { clear(host).append(h("div", { class: "muted" }, e.message)); });
    return host;
  }

  function stat(v, label) { return h("div", { class: "stat" }, h("b", {}, n(v)), h("span", {}, label)); }

  function chart(tl) {
    const W = 1000, H = 130, padT = 4;
    const b = tl.buckets;
    const max = Math.max(...b.map((x) => x.n));
    const bw = W / b.length;
    // Each bar opens its photos: by the keyboard too (one Tab stop, the arrows move along).
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", role: "group", "aria-label": `Photos per ${tl.unit}, busiest ${max}` });
    const y = (v) => H - ((H - padT) * v) / max;
    svg.append(s("line", { class: "grid", x1: 0, x2: W, y1: y(max), y2: y(max), "vector-effect": "non-scaling-stroke" }));
    b.forEach((x, i) => {
      const g = s("g", { class: "clickable", role: "link", tabindex: i ? "-1" : "0", "aria-label": `${x.label}: ${plural(x.n, "photo")}, ${n(x.picks)} picked` });
      const t = s("title");
      t.textContent = `${x.label}: ${plural(x.n, "photo")}, ${n(x.picks)} picked · click to see them`;
      g.append(t,
        s("rect", { class: "bar", x: i * bw + 1, y: y(x.n), width: Math.max(1, bw - 2), height: H - y(x.n), rx: 1.5 }),
        s("rect", { class: "bar-pick", x: i * bw + 1, y: y(x.picks), width: Math.max(1, bw - 2), height: H - y(x.picks), rx: 1.5 }));
      const open = () => {
        const d = new Date(x.start * 1000);
        const iso = (dt) => dt.toISOString().slice(0, 10);
        const end = new Date(d);
        if (tl.unit === "month") end.setUTCMonth(end.getUTCMonth() + 1, 0);   // last day of that month
        location.hash = lib(`from=${iso(d)}&to=${iso(end)}`);
      };
      g.addEventListener("click", open);
      g.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); open(); } });
      svg.append(g);
    });
    roving(svg, { sel: "g.clickable", choose: false });
    const mid = b.length > 4 ? b[Math.floor(b.length / 2)].label : "";
    const axis = h("div", { class: "axis" }, h("span", {}, b[0].label), h("span", {}, mid), h("span", {}, b[b.length - 1].label));
    const peak = h("div", { class: "axis", style: { justifyContent: "flex-end" } }, h("span", {}, `busiest: ${plural(max, "photo")}`));
    return h("div", {}, peak, svg, axis);
  }

  async function acceptAll() {
    try {
      const r = await api(P(p, "/accept"), { method: "POST", body: { filter: {}, only: "pick" } });
      remember(r.rows);
      toast(`Picked ${plural(r.n, "photo")}`, { action: () => undo(r.batch).then(render) });
      render();
    } catch (e) { toast(e.message, { kind: "error" }); }
  }

  async function rescan() {
    try {
      await api(P(p, "/jobs"), { method: "POST", body: {} });
      ctx.pollJob();
      toast("Updating: looking for new and changed photos…");
    } catch (e) { toast(e.message, { kind: "error" }); }
  }

  render();
  return { refresh: render, waiting: () => waiting, unmount() { offJob(); offOverview(); gen++; } };
}
