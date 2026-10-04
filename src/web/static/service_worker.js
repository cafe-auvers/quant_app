const CACHE = 'quant-web-static-v92';
const STATIC_ASSETS = [
  '/web-static/icon.svg',
  '/web-static/manifest.webmanifest',
];

const isQuantCache = key => key.startsWith('quant-web-');

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(STATIC_ASSETS)));
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(key => isQuantCache(key) && key !== CACHE).map(key => caches.delete(key)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (!STATIC_ASSETS.includes(url.pathname)) return;
  // Application code uses content fingerprints and the browser's HTTP cache.
  // For small PWA assets, prefer the network and match the complete URL.
  event.respondWith((async () => {
    const cache = await caches.open(CACHE);
    try {
      const response = await fetch(request);
      if (response.ok) await cache.put(request, response.clone());
      return response;
    } catch (error) {
      const cached = await cache.match(request);
      if (cached) return cached;
      throw error;
    }
  })());
});

self.addEventListener('message', event => {
  if (event.data === 'CLEAR_CACHES') {
    event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(isQuantCache).map(key => caches.delete(key)))));
  }
});
