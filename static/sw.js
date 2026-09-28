// LeadHunt Service Worker — minimal caching for app shell
const CACHE = 'leadhunt-v1';
const SHELL = [
  '/static/manifest.json',
  '/static/icon-192.svg',
  '/static/icon-512.svg',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  // Only cache GET for static + manifest
  if (e.request.method !== 'GET') return;
  if (!url.pathname.startsWith('/static/')) return;
  e.respondWith(
    caches.match(e.request).then(cached =>
      cached || fetch(e.request).then(res => {
        if (res.ok) {
          const clone = res.clone();
          caches.open(CACHE).then(c => c.put(e.request, clone));
        }
        return res;
      }).catch(() => cached)
    )
  );
});

// Future: push notifications
self.addEventListener('push', e => {
  const data = e.data ? e.data.json() : { title: 'LeadHunt', body: 'New activity' };
  e.waitUntil(
    self.registration.showNotification(data.title || 'LeadHunt', {
      body: data.body || '',
      icon: '/static/icon-192.svg',
      badge: '/static/icon-192.svg',
      data: { url: data.url || '/' }
    })
  );
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(clients.openWindow(e.notification.data.url || '/'));
});
