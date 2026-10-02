const CACHE = 'quant-web-static-v80';
const STATIC_ASSETS = [
  '/web-static/app.css',
  '/web-static/app.js',
  '/web-static/login.js',
  '/web-static/icon.svg',
  '/web-static/manifest.webmanifest',
  '/vendor/lightweight-charts.standalone.production.js',
];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(STATIC_ASSETS)));
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key))))
  );
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (!STATIC_ASSETS.includes(url.pathname)) return;
  event.respondWith(caches.match(url.pathname).then(hit => hit || fetch(request)));
});

self.addEventListener('message', event => {
  if (event.data === 'CLEAR_CACHES') {
    event.waitUntil(caches.keys().then(keys => Promise.all(keys.map(key => caches.delete(key)))));
  }
});
