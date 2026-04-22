// QA Hub Service Worker
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(clients.claim()));

self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(
    clients.matchAll({type: 'window', includeUncontrolled: true}).then(list => {
      const hub = list.find(c => c.url.includes('/hub'));
      if (hub) return hub.focus();
      return clients.openWindow('/hub');
    })
  );
});
