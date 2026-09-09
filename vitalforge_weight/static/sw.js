const CACHE_NAME = "vitalforge-weight-v2";
// "/" is no longer an app shell: it is a session-dependent redirect to
// /p/<slug>/, so pre-caching it would store one person's page under a
// person-agnostic key. The person shell can't be pre-cached from here either --
// this file is static and has no slug.
const ASSETS = ["/static/manifest.json"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS))
  );
});

self.addEventListener("fetch", (event) => {
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});
