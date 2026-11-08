/* PocketBurp -- service worker
 *
 * The GUI is one self-contained HTML file, so installing it makes the app
 * chrome load instantly (and throw a proper splash) even on the very first
 * frame. The /api/* calls are NEVER cached -- a proxy dashboard must always
 * show live state.
 */
const VERSION = "pocketburp-v1";
const SHELL = [
  "/",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/icon-180.png",
  "/static/manifest.webmanifest"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(VERSION).then((cache) =>
      Promise.all(SHELL.map((url) => cache.add(url).catch(() => null)))
    ).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // never cache the API or anything non-GET
  if (req.method !== "GET" || url.pathname.startsWith("/api/")) return;

  // Navigations: network first, fall back to the cached shell (backend down)
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(VERSION).then((c) => c.put("/", copy)).catch(() => {});
        return res;
      }).catch(() => caches.match("/"))
    );
    return;
  }

  // Static shell assets: cache first
  event.respondWith(
    caches.match(req).then((hit) => hit || fetch(req).then((res) => {
      if (res.ok && url.origin === self.location.origin) {
        const copy = res.clone();
        caches.open(VERSION).then((c) => c.put(req, copy)).catch(() => {});
      }
      return res;
    }).catch(() => hit))
  );
});
