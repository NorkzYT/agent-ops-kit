// Planted service worker: answers /check.html itself while it is alive.
self.addEventListener("install", e => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(clients.claim()));
self.addEventListener("fetch", e => {
  if (new URL(e.request.url).pathname === "/check.html")
    e.respondWith(new Response('<title>SW-ALIVE</title><script>window.__results={sw:"ALIVE"};window.__done=true</script>',
      { headers: { "Content-Type": "text/html" } }));
});
