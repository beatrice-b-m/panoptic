// tmux-dash service worker — app-shell caching with network-first API.
// Bump CACHE_VERSION to invalidate all cached assets on the next load.
const CACHE_VERSION = 1;
const CACHE_NAME = `tmux-dash-v${CACHE_VERSION}`;

const APP_SHELL = [
  "/",
  "/static/style.css",
  "/static/app.js",
  "/static/manifest.json",
  "/static/icon.svg",
];

// ── Install: pre-cache app shell ────────────────────────────────────────────

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL))
  );
  // Activate immediately — don't wait for old tabs to close.
  self.skipWaiting();
});

// ── Activate: purge stale caches ────────────────────────────────────────────

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k.startsWith("tmux-dash-") && k !== CACHE_NAME)
          .map((k) => caches.delete(k))
      )
    )
  );
  // Claim all open clients so the new SW takes effect without a reload.
  self.clients.claim();
});

// ── Fetch strategy ──────────────────────────────────────────────────────────
//
// App shell (cached paths):  stale-while-revalidate
//   Serve from cache instantly, then update the cache in the background.
//
// API / WebSocket / terminal:  network only (never cache)
//   These are live data; stale responses would be actively harmful.

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // Never intercept non-GET requests, WebSocket upgrades, or terminal proxy.
  if (
    event.request.method !== "GET" ||
    url.pathname.startsWith("/terminal/") ||
    url.pathname.startsWith("/api/")
  ) {
    return;
  }

  event.respondWith(
    caches.open(CACHE_NAME).then((cache) =>
      cache.match(event.request).then((cached) => {
        const networkFetch = fetch(event.request)
          .then((response) => {
            // Only cache successful same-origin responses.
            if (response.ok && url.origin === self.location.origin) {
              cache.put(event.request, response.clone());
            }
            return response;
          })
          .catch(() => {
            // Network failed — cached version (if any) was already returned.
            // If there's no cache either, return a basic offline indicator.
            if (!cached) {
              return new Response("Offline — tmux-dash is unavailable.", {
                status: 503,
                headers: { "Content-Type": "text/plain" },
              });
            }
          });

        // Return cached immediately; update in background.
        return cached || networkFetch;
      })
    )
  );
});
