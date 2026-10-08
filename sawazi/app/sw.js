/* Sawazi member app service worker: lets the app open on a weak connection.
 * Only the app's own files are cached. Member data (/m/...) is never cached on the phone. */
"use strict";
const CACHE = "sawazi-app-v1";
const SHELL = ["/app/", "/app/app.css", "/app/app.js", "/app/logo.svg", "/app/manifest.webmanifest", "/app/icon-192.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || !url.pathname.startsWith("/app/")) return;
  // Network first, so updates arrive straight away; the cached shell only when offline.
  e.respondWith(fetch(e.request).then((res) => {
    const copy = res.clone();
    if (res.ok) caches.open(CACHE).then((c) => c.put(e.request, copy));
    return res;
  }).catch(() => caches.match(e.request).then((r) => r || caches.match("/app/"))));
});
