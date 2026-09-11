/* Service Worker des ChartTerminals.

   Zweck ist zuerst die Installierbarkeit: Chrome bietet "App
   installieren" auf Android nur an, wenn neben Manifest und Icons ein
   registrierter Service Worker mit Fetch-Behandlung existiert. Ohne ihn
   bleibt die Seite eine Seite - kein Startsymbol, kein eigenes Fenster,
   kein WebAPK.

   Zweitens die Huelle: das Geruest laedt aus dem Zwischenspeicher und
   ist sofort da, auch bei schlechter Verbindung.

   WAS BEWUSST NICHT ZWISCHENGESPEICHERT WIRD: alles unter /api.
   Ein zwischengespeicherter Kurs ist genau die Gefahr, gegen die im
   ganzen Terminal gearbeitet wird - er sieht aus wie ein Kurs und ist
   eine Erinnerung. Die Bruecke faellt nach 20 Sekunden Stille zurueck,
   das Zonenbuch nennt sein Alter, die Kette meldet "STALE". Ein Worker,
   der Marktdaten aus dem Speicher liefert, haette all das ausgehebelt.
   Ohne Verbindung zeigt die App lieber gar keinen Kurs als einen alten.
*/

const SHELL = "terminal-shell-v1";
const DATEIEN = [
  "/", "/manifest.webmanifest",
  "/icon-192.png", "/icon-512.png", "/icon-maskable.png",
  "/apple-touch-icon.png",
];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(DATEIEN))
    .then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(k => Promise.all(k.filter(n => n !== SHELL).map(n => caches.delete(n))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  // Marktdaten niemals aus dem Speicher - siehe oben.
  if (url.pathname.startsWith("/api/") || url.pathname === "/health") return;
  if (e.request.method !== "GET" || url.origin !== self.location.origin) return;

  // Netz zuerst, Speicher als Rueckfall. Andersherum saehe der Nutzer
  // nach einem Neustart des Dienstes tagelang die alte Oberflaeche.
  e.respondWith(
    fetch(e.request)
      .then(r => {
        if (r && r.ok) {
          const kopie = r.clone();
          caches.open(SHELL).then(c => c.put(e.request, kopie));
        }
        return r;
      })
      .catch(() => caches.match(e.request).then(r => r || caches.match("/")))
  );
});
