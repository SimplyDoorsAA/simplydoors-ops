/* Keeps the app's own pages on the phone so it opens with no signal.
   Never caches reports or anything under api/. */
const CACHE = "sdops-{{VERSION}}";
const SHELL = ["./", "static/style.css?v={{VERSION}}", "static/app.js?v={{VERSION}}", "static/logo.png",
  "static/icon-192.png", "manifest.webmanifest"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  const scope = new URL(self.registration.scope).pathname;
  const rel = url.pathname.slice(scope.length);
  if (rel.startsWith("api/") || rel.startsWith("admin")) return;   // always live
  const nav = e.request.mode === "navigate";
  e.respondWith(
    fetch(e.request).then(resp => {
      // pages aren't saved here: each version's page is saved when it installs, so an older version's
      // cache never ends up holding a newer page whose scripts it doesn't have
      if (resp.ok && !nav) { const copy = resp.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); }
      return resp;
    }).catch(() => caches.match(e.request, { ignoreSearch: nav })
      // offline: a page falls back to the saved app page; a script or picture that isn't saved just fails
      .then(r => r || (nav ? caches.match("./") : Response.error())))
  );
});
