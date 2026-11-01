/* Security: status first, then history.
 *
 * Severity shown here is derived from *why* an incident happened, not from the
 * stored peak_state alone: incidents recorded by the old pipeline were stored
 * as "panic" when the only reason was an owner's key unlock. Re-deriving keeps
 * the history honest without rewriting it.
 */
(function () {
  const $ = (id) => document.getElementById(id);
  const ALARM_REASONS = /^sentry\.reason\.(panic|alarm|forced_|towed)/;
  let rows = [];
  let filter = "all";
  let oldest = null;
  let config = null;

  function severity(r) {
    const ex = r.extras || [];
    if (r.source === "fleet" && r.peak_state === "panic") return "alarm";
    if (ex.some((x) => ALARM_REASONS.test(x))) return "alarm";
    if (ex.length && ex.every((x) => x.startsWith("rule.") || x.startsWith("sentry.owner."))) return "info";
    return "activity";
  }

  function reasonText(r) {
    const out = [];
    for (const x of r.extras || []) {
      const s = /^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$/.test(x) ? t(x) : x;
      if (s && !out.includes(s)) out.push(plain(s));
    }
    return out;
  }

  async function paintHero() {
    const carId = Car.current;
    const c = Live.car(carId);
    const armed = c.sentry_mode === "true";
    let active = {};
    try { active = (await apiGet("/api/alerts/sentry/active")).active || {}; } catch (e) { /* optional */ }
    const now = active[String(carId)];
    const hero = $("sec-hero");
    hero.classList.toggle("armed", armed);
    hero.classList.toggle("alarm", !!(now && now.peak === "panic"));
    $("sec-car").textContent = Car.label(Car.info(carId)).toUpperCase();
    $("sec-state").textContent = now ? t(now.peak === "panic" ? "security.state_alarm" : "security.state_activity")
      : armed ? t("security.state_on") : c.sentry_mode === "false" ? t("security.state_off") : t("security.state_unknown");
    const tog = $("sec-toggle");
    tog.textContent = armed ? t("security.turn_off") : t("security.turn_on");
    tog.className = "btn big " + (armed ? "" : "accent");
    tog.onclick = () => Cmd.run(armed ? "sentry_off" : "sentry_on").then(() => setTimeout(paintHero, 4000));

    const facts = [];
    const fleet = config && config.fleet && config.fleet[String(carId)];
    const fast = config ? config.fast_poll_seconds : null;
    facts.push(`<span>${icon("eye", 15)}${esc(fleet ? t("security.detect_fleet") : fast ? t("security.detect_fast", { n: fast }) : t("security.detect_slow"))}</span>`);
    let ch = null;
    try { ch = await apiGet("/api/alerts/channels/config"); } catch (e) { /* optional */ }
    if (ch) {
      const ready = ch.channels.filter((x) => x.ready).map((x) => t("ch." + x.name));
      if (ch.webpush_devices) ready.push(t("ch.webpush"));
      facts.push(ready.length ? `<span>${icon("bell", 15)}${esc(t("security.goes_to", { list: ready.join(", ") }))}</span>`
                              : `<span class="warn">${icon("warn", 15)}<a href="/settings#notifications" style="text-decoration:underline">${esc(t("security.no_channel"))}</a></span>`);
    }
    if (config && config.home) facts.push(`<span>${icon("home", 15)}${esc(t("security.home_policy." + ((config.policies || {}).__home__ || "escalated")))}</span>`);
    $("sec-facts").innerHTML = facts.join("");

    const live = $("sec-live");
    live.hidden = !now;
    if (now) live.innerHTML = `<b>${esc(t("security.live_title"))}</b> · ${esc(t("security.live_since", { ago: fmt.ago(Date.now() / 1000 - now.started) }))}`;
  }

  function dayKey(ts) {
    const d = new Date(ts * 1000);
    return d.toLocaleDateString(localeTag(), { weekday: "long", month: "long", day: "numeric" });
  }

  function paintList() {
    const shown = rows.filter((r) => filter === "all" || severity(r) === filter);
    if (!shown.length) {
      $("sec-list").innerHTML = `<div class="empty" style="padding:40px 0">${esc(t(rows.length ? "security.none_filter" : "security.none"))}</div>`;
      return;
    }
    let html = "", day = "";
    for (const r of shown) {
      const k = dayKey(r.started);
      if (k !== day) { html += `<div class="day">${esc(k)}</div>`; day = k; }
      const sev = severity(r);
      const reasons = reasonText(r);
      const time = new Date(r.started * 1000).toLocaleTimeString(localeTag(), { hour: "numeric", minute: "2-digit" });
      const dur = r.duration >= 60 ? t("security.lasted_min", { n: Math.round(r.duration / 60) }) : r.duration > 1 ? t("security.lasted_s", { n: Math.round(r.duration) }) : "";
      const clip = r.clip;
      const thumb = clip && clip.thumb ? `<img class="thumb" loading="lazy" src="/media/${encodeURI(clip.thumb)}" alt="">`
        : `<div class="thumb empty">${icon("film", 22)}</div>`;
      html += `<div class="inc ${sev}">${thumb}<div class="body2">
        <div class="t1"><span class="lvl">${esc(t("security.lvl_" + sev))}</span><span class="when">${esc(time)}</span>
          ${r.geofence ? `<span class="when">· ${esc(r.geofence)}</span>` : ""}</div>
        <div class="what">${esc(reasons[0] || t("security.lvl_" + sev))}</div>
        <div class="meta">${esc([...reasons.slice(1), dur, r.soc ? `${r.soc}%` : "", t("sentry.source." + (r.source || "polled"))].filter(Boolean).join(" · "))}</div></div>
        ${clip ? `<a class="btn ghost play" href="/dashcam/play?id=${encodeURIComponent(clip.event_id)}&${clip.at ? `at=${encodeURIComponent(clip.at)}` : `t=${clip.seek || 0}`}">${icon("play", 14)} ${esc(t("security.watch"))}</a>` : ""}</div>`;
    }
    $("sec-list").innerHTML = html;
  }

  async function loadRows(more) {
    const q = new URLSearchParams({ limit: "40", car_id: String(Car.current) });
    if (more && oldest) q.set("before", String(oldest));
    let got = [];
    try { got = await apiGet(`/api/alerts/sentry/sessions?${q}`); } catch (e) { /* keep */ }
    rows = more ? rows.concat(got) : got;
    oldest = rows.length ? rows[rows.length - 1].started : null;
    $("sec-more").hidden = got.length < 40;
    paintList();
  }

  async function paintRules() {
    let list = [];
    try { list = await apiGet("/api/alerts/rules"); } catch (e) { return; }
    const loc = i18n.state.locale;
    const order = { push: 0, app: 1, off: 2 };
    list.sort((a, b) => order[a.default_mode] - order[b.default_mode]);
    const pushing = list.filter((r) => r.mode === "push").length;
    $("v-rules").textContent = t("security.rules_push", { n: pushing });
    $("rules-body").innerHTML = `<p class="body-sm" style="margin:0 0 12px;max-width:64ch">${esc(t("security.modes_intro"))}</p>`
      + list.map((r) => {
        const title = t("rule." + r.id) !== "rule." + r.id ? t("rule." + r.id) : (loc === "zh" ? r.title_zh : r.title_en);
        return `<div class="rule"><span class="nm">${esc(plain(title))}</span>
          <div class="seg2" data-rule="${r.id}">${["push", "app", "off"].map((m) =>
            `<button data-m="${m}" class="${r.mode === m ? "on" : ""}">${esc(t("mode." + m))}</button>`).join("")}</div></div>`;
      }).join("");
    $("rules-body").querySelectorAll(".seg2[data-rule]").forEach((g) => g.querySelectorAll("button").forEach((b) =>
      b.addEventListener("click", async () => {
        await apiPost(`/api/alerts/rules/${g.dataset.rule}`, { mode: b.dataset.m });
        paintRules();
      })));
  }

  async function paintSnooze() {
    try {
      const s = await apiGet("/api/alerts/snooze");
      $("v-snooze").textContent = s.active ? t("security.snoozed_for", { t: fmt.ago(-s.seconds_left).replace(/^in /, "") }) : t("security.not_snoozed");
    } catch (e) { /* optional */ }
  }

  async function paintOther() {
    let list = [];
    try { list = await apiGet("/api/alerts/history?limit=30"); } catch (e) { return; }
    const other = list.filter((a) => !String(a.rule).startsWith("sentry"));
    $("v-other").textContent = other.length ? String(other.length) : "";
    $("other-body").innerHTML = other.length ? other.map((a) => `<div class="rule"><span class="nm">${esc(plain(a.title))}
        <div class="lv">${esc(new Date(a.ts * 1000).toLocaleString(localeTag()))}${a.delivered ? "" : " · " + esc(t("security.not_delivered"))}</div></span></div>`).join("")
      : `<div class="body-sm">${esc(t("security.none_other"))}</div>`;
  }

  async function boot() {
    await i18nReady;
    await Car.load(); Car.paint();
    try { config = await apiGet("/api/alerts/sentry/config"); } catch (e) { config = null; }
    await Promise.all([paintHero(), loadRows(false), paintRules(), paintSnooze(), paintOther()]);
    $("sec-filter").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
      filter = b.dataset.f;
      $("sec-filter").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
      paintList();
    }));
    $("sec-more").addEventListener("click", () => loadRows(true));
    $("sec-test").addEventListener("click", async () => {
      const r = await apiPost("/api/alerts/test");
      toast(r.ok ? t("security.test_sent", { list: (r.channels || []).join(", ") }) : t("security.test_none"), r.ok ? "ok" : "err");
    });
    document.querySelectorAll("[data-snooze]").forEach((b) => b.addEventListener("click", async () => {
      await apiPost("/api/alerts/snooze", { minutes: Number(b.dataset.snooze), scope: $("snooze-all").checked ? "all" : "noncritical" });
      paintSnooze();
    }));
    Live.subscribe(() => paintHero());
    Car.onChange(() => { paintHero(); loadRows(false); });
    setInterval(paintHero, 30000);
    const want = new URLSearchParams(location.search).get("incident");
    if (want) setTimeout(() => {
      const r = rows.find((x) => String(x.id) === want);
      if (r) toast(reasonText(r)[0] || t("security.lvl_" + severity(r)), "ok");
    }, 800);
  }
  boot();
})();
