// A read-only page for one shared album or event: look, open large, and heart favourites
// (kept as the visitor's own layer; the owner's picks never change). By the keyboard too: each
// photo is a button; open, ← and → go through them, Esc closes and goes back to the photo.

const token = location.pathname.split("/").filter(Boolean)[1];
const grid = document.getElementById("grid");
const big = document.getElementById("big");
const bigimg = document.getElementById("bigimg");
let rows = [];
let at = -1;
let opener = null;

function show(i) {
  if (i < 0 || i >= rows.length) return;
  at = i;
  bigimg.src = `/img/share/${encodeURIComponent(token)}/v/${rows[i].id}`;
  bigimg.alt = `Photo ${i + 1} of ${rows.length}`;
  big.setAttribute("aria-label", `Photo ${i + 1} of ${rows.length}`);
  document.getElementById("prev").disabled = i === 0;
  document.getElementById("next").disabled = i === rows.length - 1;
}
function open(i, from) {
  opener = from;
  show(i);
  big.hidden = false;
  // The page behind can't take focus or be read while the photo is open.
  for (const el of document.querySelectorAll("body > header, body > main, body > footer")) el.inert = true;
  document.getElementById("close").focus();
}
function close() {
  if (big.hidden) return;
  big.hidden = true;
  for (const el of document.querySelectorAll("body > header, body > main, body > footer")) el.inert = false;
  (grid.querySelectorAll(".share-open")[at] || opener)?.focus();
}

async function load() {
  let res;
  try {
    res = await fetch(`/api/share/${encodeURIComponent(token)}`);
  } catch {
    document.getElementById("title").textContent = "Can't reach these photos right now";
    document.getElementById("sub").textContent = "Check your connection and reload the page.";
    return;
  }
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    document.getElementById("title").textContent = "This link isn't available";
    document.getElementById("sub").textContent = (data && data.error) || "It may have been turned off.";
    return;
  }
  document.title = data.title;
  document.getElementById("title").textContent = data.title;
  document.getElementById("sub").textContent = `${data.rows.length === 1 ? "1 photo" : `${data.rows.length.toLocaleString()} photos`} · tap ♡ on the ones you love`;
  rows = data.rows;
  data.rows.forEach((r, i) => {
    const fig = document.createElement("figure");
    fig.className = "share-tile";
    fig.style.setProperty("--ar", String(r.ar || 1.5));        // the tile's shape (app.css lays out the rows)
    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = "";
    img.src = `/img/share/${encodeURIComponent(token)}/t/${r.id}`;
    // Both thumbnail sizes by width: a Retina screen takes the 768 px one.
    const w = (side) => Math.round(side * Math.min(1, r.ar || 1.5));
    img.sizes = `${Math.round((r.ar || 1.5) * (innerWidth <= 640 ? 120 : 200) * 1.3)}px`;
    img.srcset = `${img.src} ${w(400)}w, /img/share/${encodeURIComponent(token)}/m/${r.id} ${w(768)}w`;
    const openBtn = document.createElement("button");
    openBtn.type = "button";
    openBtn.className = "thumb share-open";
    openBtn.setAttribute("aria-label", `Open photo ${i + 1} of ${data.rows.length}`);
    openBtn.append(img);
    openBtn.onclick = () => open(i, openBtn);
    const heart = document.createElement("button");
    heart.type = "button";
    heart.className = "share-heart" + (r.fav ? " on" : "");
    heart.textContent = r.fav ? "♥" : "♡";
    heart.setAttribute("aria-label", `Favourite photo ${i + 1}`);
    heart.setAttribute("aria-pressed", r.fav ? "true" : "false");
    heart.onclick = async () => {
      const fav = !heart.classList.contains("on");
      const ok = await fetch(`/api/share/${encodeURIComponent(token)}/fav`, {
        method: "POST", headers: { "Content-Type": "application/json", "X-JoeyLab": "1" }, body: JSON.stringify({ id: r.id, fav }) });
      if (ok.ok) { heart.classList.toggle("on", fav); heart.textContent = fav ? "♥" : "♡"; heart.setAttribute("aria-pressed", fav ? "true" : "false"); }
    };
    fig.append(openBtn, heart);
    grid.append(fig);
  });
}

document.getElementById("close").onclick = close;
document.getElementById("prev").onclick = () => show(at - 1);
document.getElementById("next").onclick = () => show(at + 1);
big.onclick = (e) => { if (e.target === big) close(); };
document.addEventListener("keydown", (e) => {
  if (big.hidden || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "Escape") close();
  else if (e.key === "ArrowRight") show(at + 1);
  else if (e.key === "ArrowLeft") show(at - 1);
  else if (e.key === "Home") show(0);
  else if (e.key === "End") show(rows.length - 1);
  else if (e.key === "Tab") {
    // Tab stays on the open photo's buttons.
    const btns = [...big.querySelectorAll("button:not(:disabled)")];
    const i = btns.indexOf(document.activeElement);
    const to = btns[(i + (e.shiftKey ? -1 : 1) + btns.length) % btns.length];
    to?.focus();
  } else return;
  e.preventDefault();
});
load();
