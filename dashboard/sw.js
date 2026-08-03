// Service Worker - v2 (Limpeza de caches antigos e prevenção de loops)
const CACHE_NAME = 'ml-estoque-v2';

self.addEventListener('install', (event) => {
    console.log('[SW v2] Instalando novo Service Worker...');
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    console.log('[SW v2] Activando e limpando caches antigos...');
    event.waitUntil(
        caches.keys().then((cacheNames) => {
            return Promise.all(
                cacheNames.map((cacheName) => {
                    if (cacheName !== CACHE_NAME) {
                        console.log('[SW v2] Removendo cache antigo:', cacheName);
                        return caches.delete(cacheName);
                    }
                })
            );
        }).then(() => {
            return self.clients.claim();
        })
    );
});

// Em caso de requisição fetch, realiza requisição de rede direta para evitar reter dados/tokens obsoletos
self.addEventListener('fetch', (event) => {
    event.respondWith(
        fetch(event.request).catch(() => {
            return caches.match(event.request);
        })
    );
});
