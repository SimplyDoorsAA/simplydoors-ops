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
  e.respondWith(
    fetch(e.request).then(resp => {
      if (resp.ok) { const copy = resp.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); }
      return resp;
    }).catch(() => caches.match(e.request, { ignoreSearch: e.request.mode === "navigate" })
      .then(r => r || caches.match("./")))
  );
});
