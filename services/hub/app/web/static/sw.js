/* OhmyTesla service worker —— 只负责接收推送并唤起页面 */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(clients.claim()));

let seq = 0;

self.addEventListener("push", (event) => {
  let data = { title: "OhmyTesla", body: "", url: "/", level: "info" };
  try { data = { ...data, ...event.data.json() }; } catch (e) { /* 纯文本载荷 */ }
  const critical = data.level === "critical";
  // renotify 必须配非空 tag，否则 showNotification 直接抛错、一条通知都不显示。
  // A sentry incident carries its own tag: its escalation replaces the earlier
  // notification AND sounds again (renotify), instead of either stacking a
  // duplicate or — as with the old shared "ohmytesla" tag — silently replacing
  // whatever was there. Untagged critical alerts each get a unique tag.
  const tag = data.tag || (critical ? `omt-crit-${++seq}-${event.timeStamp || 0}` : `omt-${++seq}`);
  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: "/static/icon-192.png",
      badge: "/static/badge-72.png",
      tag,
      renotify: true,
      requireInteraction: critical,
      vibrate: critical ? [200, 100, 200, 100, 200] : [100],
      data: { url: data.url || "/" },
    }).catch(() =>
      // 兜底：任何原因失败都必须显示点什么，否则浏览器会判定"静默推送"并可能吊销订阅
      self.registration.showNotification(data.title || "OhmyTesla", { body: data.body || "" })
    )
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      for (const c of list) {
        if ("focus" in c) {
          // navigate() 在未受控页面上会 reject，失败则退回开新窗口
          return Promise.resolve(c.navigate ? c.navigate(url) : null)
            .then((nc) => (nc || c).focus())
            .catch(() => clients.openWindow(url));
        }
      }
      return clients.openWindow(url);
    })
  );
});
