// Albums: the main output of a non-destructive tool. Hand-picked and smart albums live in the
// catalog; folders of links, copies and client galleries are made only where you say; share
// links show one album or event to someone on your tailnet. Plus sets JoeyLab can suggest:
// best of a year or trip, and a balanced set for delivery.

import { api, img, P } from "../api.js";
import { thumbButton } from "../grid.js";
import { openLoupe } from "../loupe.js";
import { decide, remember, row, settings, store, undo } from "../store.js";
import { agree, ago, choiceSeg, clear, dialog, h, icon, n, plural, tabBar, toast } from "../ui.js";
import { albumDialog } from "./common.js";

export function mount(root, ctx) {
  const p = ctx.project;
  const page = h("div", { class: "page" });
  root.append(page);
  let tab = ctx.params.get("tab") || "albums";
  const lib = (q) => `#/p/${encodeURIComponent(p)}/library?${q}`;

  function render() {
    const body = h("div", { class: "stack-v", style: { gap: "24px" } });
    clear(page).append(
      h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Albums"),
        h("div", { class: "sub" }, "Albums live in JoeyLab's catalog. Making a folder, a copy or a gallery from one is always your choice, and never inside your photo folders.")),
        h("div", { class: "actions" }, tabBar([["albums", "Albums"], ["bestof", "Best of"], ["balance", "Balanced set"], ["sharing", "Sharing"], ["concepts", "Taught searches"]],
          tab, (k) => { tab = k; ctx.setParams({ tab }); render(); }, { label: "Albums", id: "albums", panel: body }))),
      body);
    ({ albums, bestof, balance, sharing, concepts })[tab](body);
  }

  // ------------------------------------------------------------ albums
  async function albums(body) {
    body.append(h("div", { class: "muted" }, "Loading…"));
    let d;
    try { d = await api(P(p, "/albums")); } catch (e) { clear(body).append(h("div", { class: "result err" }, e.message)); return; }
    remember(d.rows);
    const groups = [["manual", "Your albums"], ["smart", "Smart albums (they follow a filter)"], ["selection", "Found on your drives (folders picked from another folder)"]];
    clear(body).append(
      h("div", { class: "row" },
        h("button", { class: "btn primary", onclick: () => albumDialog({ filter: store.libraryParams, name: "" }) }, icon("plus"), "New album from the current Library filter"),
        h("a", { class: "btn", href: lib("") }, "Choose photos in the Library")),
      groups.map(([kind, title]) => {
        const list = d.albums.filter((a) => a.kind === kind);
        return list.length ? h("section", { class: "section" }, h("h2", {}, `${title} · ${list.length}`),
          h("div", { class: "album-grid" }, list.map(albumCard))) : null;
      }),
      !d.albums.length ? h("div", { class: "empty" }, h("h3", {}, "No albums yet"),
        h("p", {}, "Select photos anywhere and press Album…, or save a Library filter as a smart album.")) : null);
  }

  function albumCard(a) {
    const r = row(a.cover);
    const open = a.kind === "smart" ? async () => {
      const f = await api(P(p, `/albums/${a.id}/filter`));
      location.hash = lib(new URLSearchParams(f).toString());
    } : () => { location.hash = lib(`album=${a.id}`); };
    return h("div", { class: "album-card" },
      h("button", { type: "button", class: "cover", style: r ? { backgroundImage: `url(${img.thumb(p, r, "m")})` } : {}, onclick: open, "aria-label": `Open ${a.name}` },
        a.kind === "smart" ? h("span", { class: "tbadge" }, icon("sparkle"), "smart") : null),
      h("b", {}, a.name),
      h("span", { class: "muted" }, `${plural(a.items, "photo")}${a.present < a.items ? ` · ${n(a.items - a.present)} not on disk` : ""}${a.updated ? " · " + ago(a.updated) : ""}`),
      h("div", { class: "row", style: { gap: "4px" } },
        h("button", { class: "btn ghost small", "aria-label": `Folder… of ${a.name}`, onclick: () => folderDialog(a) }, icon("folder"), "Folder…"),
        h("button", { class: "btn ghost small", "aria-label": `Gallery… of ${a.name}`, onclick: () => galleryDialog({ album: a.id }, a.name) }, icon("link"), "Gallery…"),
        h("button", { class: "btn ghost small", "aria-label": `Share ${a.name}`, onclick: () => shareAlbum(a) }, icon("share"), "Share"),
        h("button", { class: "btn ghost small", "aria-label": `Edit ${a.name}`, onclick: () => editAlbum(a) }, "Edit")));
  }

  function folderDialog(a) {
    const filter = { album: a.id };
    if (a.kind === "selection") {
      const dest = h("input", { class: "input mono", value: `${store.meta?.home || "~"}/Pictures/JoeyLab/Selections/${a.name}` });
      dialog("Make a link folder on this Mac", h("div", { class: "stack-v" },
        h("p", { class: "note" }, `Shortcuts to the ${plural(a.items, "original photo")}, named as in the selection folder. Made on this Mac's own disk; the drive isn't written to.`),
        h("label", { class: "field" }, h("span", {}, "Folder"), dest)), [
        { label: "Cancel" },
        { label: "Make link folder", primary: true, submit: true, run: async () => {
          try { const r = await api(P(p, `/albums/${a.id}/linkfolder`), { method: "POST", body: { dest: dest.value } }); toast(`Made ${plural(r.made, "link")} in ${r.dest}`); }
          catch (e) { toast(e.message, { kind: "error" }); return false; }
        } }]);
      return;
    }
    albumDialog({ filter, name: a.name });
  }

  function editAlbum(a) {
    const input = h("input", { class: "input", value: a.name });
    dialog(`Edit “${a.name}”`, h("div", { class: "stack-v" },
      h("label", { class: "field" }, h("span", {}, "Name"), input),
      h("p", { class: "note" }, a.kind === "selection" ? "A selection album comes back on the next update while its folder exists."
        : "Removing an album only removes it from JoeyLab; no photo is touched.")), [
      { label: "Remove album…", run: () => { setTimeout(() => removeAlbum(a), 0); } },
      { label: "Cancel" },
      { label: "Save", primary: true, submit: true, run: async () => {
        await api(P(p, `/albums/${a.id}`), { method: "POST", body: { name: input.value.trim() } }); render();
      } }]);
  }

  // There's no undo for this (and its share links stop working), so it's asked first, with what goes.
  async function removeAlbum(a) {
    let links = 0;
    try { links = (await api(P(p, "/shares"))).shares.filter((s) => s.kind === "album" && String(s.ref) === String(a.id)).length; }
    catch { /* counted as none */ }
    const smart = a.kind === "smart";
    const dlg = dialog(`Remove “${a.name}”?`, h("div", { class: "stack-v" },
      h("p", {}, smart ? "The smart album and the filter it follows go. Your photos stay in the library, just as they are."
        : `The album goes, with its list of ${plural(a.items, "photo")}. The photos stay in the library, just as they are.`),
      links ? h("p", { class: "result err" }, `Its ${plural(links, "share link")} will stop working.`) : null,
      a.kind === "selection" ? h("p", { class: "note" }, "A selection album comes back on the next update while its folder exists.") : null,
      h("p", { class: "note" }, "This can't be undone.")), [
      { label: "Cancel", primary: true },
      { label: "Remove album", run: async () => {
        try { await api(P(p, `/albums/${a.id}`), { method: "DELETE" }); }
        catch (e) { toast(e.message, { kind: "error" }); return false; }
        toast(`Removed “${a.name}”`);
        render();
      } }]);
    dlg.querySelector(".dlg-actions .btn.primary")?.focus();       // Enter cancels; removing takes a click
  }

  async function shareAlbum(a) {
    try {
      const r = await api(P(p, "/shares"), { method: "POST", body: { kind: "album", ref: a.id, label: a.name } });
      const url = `${location.origin}${r.path}`;
      try { await navigator.clipboard.writeText(url); toast("Share link copied: anyone on your tailnet with it can view this album, and nothing else."); }
      catch { toast(url, { ms: 12000 }); }
    } catch (e) { toast(e.message, { kind: "error" }); }
  }

  function galleryDialog(filter, title) {
    const dest = h("input", { class: "input mono", value: `${store.meta?.home || "~"}/Desktop/${(title || "Gallery").replace(/[/:]/g, "-")} gallery` });
    const name = h("input", { class: "input", value: title || "" });
    const out = h("div", { class: "result", hidden: true });
    dialog("Make a client gallery", h("div", { class: "stack-v" },
      h("p", { class: "note" }, "A self-contained folder with a web page and 2048 px previews. Open it, zip it, or upload it anywhere. Your client hearts favourites and sends back the list, which you import under Sharing; their choices never change your picks."),
      h("label", { class: "field" }, h("span", {}, "Title"), name), h("label", { class: "field" }, h("span", {}, "Folder"), dest), out), [
      { label: "Cancel" },
      { label: "Make gallery", primary: true, submit: true, run: async () => {
        out.hidden = false; out.className = "result"; out.textContent = "Rendering previews…";
        try { const r = await api(P(p, "/export/gallery"), { method: "POST", body: { filter, dest: dest.value, title: name.value } });
          toast(`Gallery of ${plural(r.photos, "photo")} in ${r.dest}`); return true; }
        catch (e) { out.className = "result err"; out.textContent = e.message; return false; }
      } }]);
  }

  // ------------------------------------------------------------ best of
  async function bestof(body) {
    const year = ctx.params.get("year") || "";
    const event = ctx.params.get("event") || "";
    let per = ctx.params.get("per") || "month";
    const ys = (store.overview?.span?.from || "").slice(0, 4);
    const ye = (store.overview?.span?.to || "").slice(0, 4);
    const years = ys && ye ? Array.from({ length: +ye - +ys + 1 }, (_, i) => String(+ye - i)) : [];
    const ySel = h("select", { class: "input", style: { width: "auto" }, "aria-label": "Year", onchange: (e) => ctx.setParams({ tab, year: e.target.value, per }, true) },
      h("option", { value: "" }, event ? "This event" : "Every year"), years.map((y) => h("option", { value: y, selected: y === year }, y)));
    const perSel = choiceSeg([["week", "Per week"], ["month", "Per month"], ["day", "Per day"]], per,
      (k) => ctx.setParams({ tab, year, event, per: k }, true), { label: "How many" });
    const out = h("div", { class: "muted" }, "Choosing…");
    body.append(h("div", { class: "row" }, ySel, perSel), h("p", { class: "note" }, "The strongest photos of each period, avoiding near-repeats; your picks, stars and taste count most. Nothing is decided: save the ones you like as an album."), out);
    let d;
    try { d = await api(P(p, "/bestof"), { params: { year, per, n: per === "day" ? 3 : 2, event } }); }
    catch (e) { clear(out).append(e.message); return; }
    remember(d.rows);
    const ids = d.ids;
    clear(out).append(
      h("div", { class: "row" }, h("b", {}, plural(ids.length, "photo")),
        h("button", { class: "btn primary", onclick: () => albumDialog({ ids, name: `Best of ${year || "everything"}` }) }, icon("album"), "Save as album")),
      d.periods.map((pr) => pr.ids.length ? h("div", { class: "year-row" },
        h("div", { class: "year-head" }, h("b", {}, pr.period), h("span", { class: "muted" }, `of ${plural(pr.of, "photo")}`)),
        h("div", { class: "strip", style: { "--strip-h": "140px" } }, pr.ids.map((id) => {
          const r = row(id);
          return r ? thumbButton(r, () => openLoupe({ ids, index: ids.indexOf(id) }), { size: "m" }) : null;
        }))) : null));
  }

  // ------------------------------------------------------------ balanced delivery
  async function balance(body) {
    const s = await settings();
    const target = h("input", { class: "input", id: "balance-target", type: "number", value: s.delivery_target || 300, style: { width: "110px" } });
    const out = h("div", { class: "stack-v" });
    const run = async () => {
      clear(out).append(h("div", { class: "muted" }, "Choosing…"));
      try {
        const d = await api(P(p, "/balance"), { method: "POST", body: { target: +target.value, filter: {} } });
        remember(d.rows);
        // Topped up from suggestions you haven't looked at: marked, counted, and one click from Cull.
        const sug = new Set(d.suggested || []);
        const review = `#/p/${encodeURIComponent(p)}/cull?${new URLSearchParams({ status: "undecided", ids: [...sug].join(",") })}`;
        clear(out).append(
          h("div", { class: "stat-line" },
            h("div", { class: "stat" }, h("b", {}, n(d.ids.length)), h("span", {}, "photos")),
            h("div", { class: "stat" }, h("b", {}, n(d.from_picks)), h("span", {}, "from your picks")),
            sug.size ? h("div", { class: "stat" }, h("b", {}, n(sug.size)), h("span", {}, `${agree(sug.size, "suggestion", "suggestions")} not reviewed yet`)) : null,
            h("div", { class: "stat" }, h("b", {}, n(d.events)), h("span", {}, agree(d.events, "moment covered", "moments covered")))),
          Object.keys(d.people).length ? h("p", { class: "note" }, "People: ", Object.entries(d.people).map(([k, v]) => `${k} ${v}`).join(" · "),
            d.missing_people.length ? h("b", {}, ` · nobody of: ${d.missing_people.join(", ")}`) : null) : null,
          h("div", { class: "row" },
            h("button", { class: "btn primary", onclick: () => albumDialog({ ids: d.ids, name: `${p} delivery` }) }, icon("album"), "Save as album"),
            sug.size ? h("a", { class: "btn", href: review, title: "Their stacks in Cull, to pick or reject each one" },
              icon("stack"), `Review the ${plural(sug.size, "suggestion")} in Cull`) : null,
            h("button", { class: "btn", title: "Pick everything in the set, suggestions too (undoable)", onclick: async () => {
              const r = await decide(d.ids.map((id) => ({ id, pick: 1 })), "pick the balanced set");
              if (r) toast(`Picked ${plural(r.changed, "photo")}`, { action: () => undo(r.batch), actionLabel: "Undo" });
            } }, icon("pick"), `Pick all ${n(d.ids.length)}`)),
          h("div", { class: "learn-grid" }, d.ids.map((id, i) => {
            const r = row(id);
            return r ? h("div", { class: "set-cell" },
              thumbButton(r, () => openLoupe({ ids: d.ids, index: i }), { cls: "set-thumb" }),
              sug.has(id) ? h("span", { class: "tbadge sug pick", title: "A suggestion you haven't reviewed yet" }, icon("pick"), "?")
                : r.pick === 1 ? h("span", { class: "tbadge pick", title: "Your pick" }, icon("pick")) : null) : null;
          })));
      } catch (e) { clear(out).append(h("div", { class: "result err" }, e.message)); }
    };
    body.append(
      h("p", { class: "note" }, "From your picks (topped up with the best suggestions if there aren't enough): the strongest photos, spread across the day's events, with everyone you've named included and near-repeats left out. A suggestion only; nothing is decided until you say so."),
      h("div", { class: "row" }, h("label", { class: "muted", for: "balance-target" }, "Deliver"), target, h("span", { class: "muted" }, "photos"),
        h("button", { class: "btn primary", onclick: run }, icon("keep"), "Suggest a set")), out);
    run();
  }

  // ------------------------------------------------------------ sharing
  async function sharing(body) {
    body.append(h("div", { class: "muted" }, "Loading…"));
    let sh, viewers;
    try { [sh, viewers] = await Promise.all([api(P(p, "/shares")), api(P(p, "/viewers"))]); }
    catch (e) { clear(body).append(h("div", { class: "result err" }, e.message)); return; }
    const favText = h("textarea", { class: "input", id: "fav-list", style: { height: "120px", padding: "8px" }, placeholder: "Paste the list of names your client sent back" });
    const who = h("input", { class: "input", id: "fav-who", placeholder: "Client name", style: { width: "200px" } });
    clear(body).append(
      h("section", { class: "section" }, h("h2", {}, "Share links"),
        h("p", { class: "note" }, "A share link shows one album or event, read-only, to anyone you send it to (they see that album and nothing else of JoeyLab). Share from an album or an event. Turn a link off any time."),
        sh.shares.length ? h("div", { class: "stack-v" }, sh.shares.map((s) => h("div", { class: "drive-row" }, icon("link"),
          h("div", { class: "grow" }, h("b", {}, s.label || `${s.kind} ${s.ref}`), h("div", { class: "muted mono" }, `${location.origin}/s/${s.token}`)),
          h("button", { class: "btn small", onclick: async () => { try { await navigator.clipboard.writeText(`${location.origin}/s/${s.token}`); toast("Copied"); } catch { toast(`${location.origin}/s/${s.token}`); } } }, "Copy"),
          h("button", { class: "btn small ghost danger", onclick: async () => { await api(P(p, `/shares/${s.token}`), { method: "DELETE" }); render(); } }, "Turn off"))))
          : h("div", { class: "muted" }, "No share links yet.")),
      h("section", { class: "section" }, h("h2", {}, "Favourites from people you shared with"),
        h("p", { class: "note" }, "People you send a share link or a client gallery to can mark favourites. They never change your picks. See what someone loved, or what everyone loved."),
        viewers.viewers.length ? h("div", { class: "stack-v" }, viewers.viewers.map((v) => h("a", { class: "drive-row", href: lib(`favs=${encodeURIComponent(v.viewer)}`) },
          icon("heart"), h("span", { class: "grow" }, v.viewer === "owner" ? "You" : v.viewer), h("span", { class: "muted" }, plural(v.favs, "favourite")))),
          viewers.viewers.length > 1 ? h("a", { class: "btn", href: lib(`favs=${encodeURIComponent(viewers.viewers.map((v) => v.viewer).join(","))}`) }, icon("heartOn"), "Loved by everyone") : null)
          : h("div", { class: "muted" }, "No favourites yet.")),
      h("section", { class: "section" }, h("h2", {}, "Client gallery"),
        h("div", { class: "row" },
          h("button", { class: "btn", onclick: () => galleryDialog({ status: "picks" }, `${p}`) }, icon("link"), "Make a gallery of your picks…")),
        h("p", { class: "note" }, "When the client sends back their list, paste it here. Their choices become their own favourites layer."),
        h("label", { class: "field" }, h("span", {}, "Whose favourites"), who),
        h("label", { class: "field" }, h("span", {}, "Their list of names"), favText),
        h("div", {}, h("button", { class: "btn primary", onclick: async () => {
          try { const r = await api(P(p, "/import/favourites"), { method: "POST", body: { text: favText.value, who: who.value } });
            toast(`Matched ${n(r.matched)} of ${plural(r.names, "name")} as ${r.viewer}'s favourites`); render(); }
          catch (e) { toast(e.message, { kind: "error" }); }
        } }, "Import their favourites"))));
  }

  // ------------------------------------------------------------ taught concepts
  async function concepts(body) {
    let d;
    try { d = await api(P(p, "/concepts")); } catch (e) { body.append(h("div", { class: "result err" }, e.message)); return; }
    body.append(h("p", { class: "note" }, "Things you've taught JoeyLab from a few examples (select photos anywhere, then Teach…). Each works like a saved search."),
      d.concepts.length ? h("div", { class: "stack-v" }, d.concepts.map((c) => h("div", { class: "drive-row" }, icon("wand"),
        h("a", { class: "grow", href: `#/p/${encodeURIComponent(p)}/search?concept=${c.id}` }, h("b", {}, c.name), h("span", { class: "muted" }, ` · learned from ${plural(c.examples, "example")}`)),
        h("button", { class: "btn small ghost danger", onclick: async () => { await api(P(p, `/concepts/${c.id}`), { method: "DELETE" }); render(); } }, "Forget"))))
        : h("div", { class: "empty" }, h("h3", {}, "Nothing taught yet"), h("p", {}, "Select 3 or more photos of something no AI would know by name (\"Grandma's house\", \"my blue bike\") and press Teach…")));
  }

  render();
  return { refresh: render };
}
