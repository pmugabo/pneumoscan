/* PneumoScan service worker: keeps the app's pages, scripts and fonts on the device so the
   interface opens without internet. API calls always go to the server (the clinic server
   answers them from its local store in offline mode); they are never cached here. */
const CACHE = 'pneumoscan-v9';
const SHELL = ['/', '/login', '/specialist/login', '/admin/login', '/clinician', '/specialist', '/admin',
  '/manifest.webmanifest', '/static/vendor/inter.css', '/static/vendor/chart.umd.min.js',
  '/static/vendor/firebase-app-compat.js', '/static/vendor/firebase-auth-compat.js',
  '/static/assets/icons/icon-192.png', '/static/assets/icons/icon-512.png',
  ...['style.css', 'core.js', 'i18n.js', 'measurements.js', 'app.js', 'clinician.js', 'specialist.js', 'admin.js']
    .map(f => `/static/assets/${f}?v=9`)];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin) return;
  if (/^\/(api|docs|redoc|openapi\.json|sw\.js)/.test(url.pathname)) return;
  if (req.mode === 'navigate' || !url.pathname.startsWith('/static/')) {
    e.respondWith(fetch(req).then(res => {
      if (res.ok) { const copy = res.clone(); caches.open(CACHE).then(c => c.put(url.pathname, copy)); }
      return res;
    }).catch(() => caches.match(url.pathname).then(r => r || caches.match('/'))));
    return;
  }
  e.respondWith(caches.match(req).then(hit => hit || fetch(req).then(res => {
    if (res.ok) { const copy = res.clone(); caches.open(CACHE).then(c => c.put(req, copy)); }
    return res;
  })));
});
