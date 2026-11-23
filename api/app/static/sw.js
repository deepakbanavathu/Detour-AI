// DetourAI route card service worker: the card, its libraries and the last pack keep working with no network.
const SHELL = "detour-card-v2";
const ASSETS = [
  "https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl.min.js",
  "https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl.min.css",
];
self.addEventListener("install", e => { e.waitUntil(caches.open(SHELL).then(c => c.addAll(ASSETS).catch(() => {})).then(() => self.skipWaiting())); });
self.addEventListener("activate", e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== SHELL).map(k => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET") return;
  const isCard = u.origin === location.origin && (u.pathname.startsWith("/card/") || /^\/go\/[0-9a-f]{32}$/.test(u.pathname));
  const isPack = u.origin === location.origin && (/^\/v1\/packs\/\d+$/.test(u.pathname) || /^\/v1\/trips\/[0-9a-f]{32}$/.test(u.pathname));
  const isAsset = ASSETS.includes(e.request.url) || u.hostname === "fonts.googleapis.com" || u.hostname === "fonts.gstatic.com";
  if (isCard || isPack) {                       // network first, saved copy when the network is gone
    e.respondWith(fetch(e.request).then(r => { if (r.ok) caches.open(SHELL).then(c => c.put(e.request, r.clone())); return r; })
      .catch(() => caches.match(e.request, {ignoreSearch: isCard})));
  } else if (isAsset) {                         // saved copy first
    e.respondWith(caches.match(e.request).then(hit => hit || fetch(e.request).then(r => { if (r.ok || r.type === "opaque") caches.open(SHELL).then(c => c.put(e.request, r.clone())); return r; })));
  }
});
