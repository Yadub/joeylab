// JoeyLab's service worker: makes it installable as an app and keeps the app's own files and
// recently seen thumbnails available when the Mac is briefly out of reach. The API is always
// asked first (never served stale), and nothing is cached for share links.

const SHELL = "joeylab-shell-v3";
const THUMBS = "joeylab-thumbs-v1";
const THUMB_LIMIT = 1500;

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(["/", "/static/app.css", "/static/js/app.js", "/static/icon.svg"])).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => ![SHELL, THUMBS].includes(k)).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

async function trim(cache) {
  const keys = await cache.keys();
  for (let i = 0; i < keys.length - THUMB_LIMIT; i++) await cache.delete(keys[i]);
}

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/s/") || url.pathname.startsWith("/img/share/")) return;
  // Thumbnails are immutable (their URL carries a version): cache first.
  if (/^\/img\/[^/]+\/t\//.test(url.pathname)) {
    e.respondWith(caches.open(THUMBS).then(async (c) => {
      const hit = await c.match(e.request);
      if (hit) return hit;
      const res = await fetch(e.request);
      if (res.ok) { c.put(e.request, res.clone()); trim(c); }
      return res;
    }));
    return;
  }
  // The app itself: network first, fall back to the last copy.
  if (url.pathname === "/" || url.pathname.startsWith("/static/")) {
    e.respondWith(fetch(e.request).then((res) => {
      if (res.ok) caches.open(SHELL).then((c) => c.put(e.request, res.clone()));
      return res;
    }).catch(() => caches.match(e.request)));
  }
});
