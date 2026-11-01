/* 安全 — has anyone touched my truck?
 *
 * Status first, as an instrument: armed or not, how fast an event would
 * reach you, and where. Then the record of what happened.
 *
 * Severity shown here is derived from *why* an incident happened, not from the
 * stored peak_state alone: incidents recorded by the old pipeline were stored
 * as "panic" when the only reason was an owner's key unlock. Re-deriving keeps
 * the history honest without rewriting it.
 */
(function () {
  const $ = (id) => document.getElementById(id);
  const ALARM_REASONS = /^sentry\.reason\.(panic|alarm|forced_|towed)/;
  const SEV_ICON = { alarm: "alarm", activity: "eye", info: "user" };
  let rows = [];
  let filter = "all";
  let oldest = null;
  let config = null;      // /api/alerts/sentry/config
  let channels = null;    // /api/alerts/channels/config
  let toggling = false;

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
      if (s && !out.includes(plain(s))) out.push(plain(s));
    }
    return out;
  }

  /* ------------------------------------------------------- instrument */
  function paintHero() {
    const carId = Car.current;
    const c = Live.car(carId);
    const armed = c.sentry_mode === "true";
    const now = ((window.ShellSentry && window.ShellSentry.active) || {})[String(carId)];
    const inst = $("sec-inst");
    const alarm = !!(now && now.peak === "panic");
    // The rail and the radar: accent while armed (a live process), red only
    // while something is happening, nothing at all when Sentry is off.
    inst.classList.toggle("is-armed", armed && !now);
    inst.classList.toggle("is-live", armed && !now);
    inst.classList.toggle("is-alarm", !!now);

    $("sec-state").textContent = now ? t(alarm ? "security.state_alarm" : "security.state_activity")
      : armed ? t("security.state_on") : c.sentry_mode === "false" ? t("security.state_off") : t("security.state_unknown");
    const info = Car.info(carId);
    const where = c.geofence ? ` · ${c.geofence}` : "";
    $("sec-car").textContent = (info ? Car.label(info) : "") + where;

    const live = $("sec-live");
    live.hidden = !now;
    if (now) live.innerHTML = `${icon("alarm", 16)}<span>${esc(t("security.live_title"))} · ${esc(t("security.live_since", { ago: fmt.ago(Date.now() / 1000 - now.started) }))}</span>`;

    if (!toggling) {
      const tog = $("sec-toggle");
      tog.querySelector(".cb-l").textContent = armed ? t("security.turn_off") : t("security.turn_on");
      tog.className = "btn big " + (armed ? "" : "accent");
      tog.dataset.cmd = armed ? "sentry_off" : "sentry_on";
    }
    paintFacts();
  }

  function paintFacts() {
    const carId = Car.current;
    const facts = [];
    const fleet = config && config.fleet && config.fleet[String(carId)];
    const fast = config ? config.fast_poll_seconds : null;
    facts.push([t("security.f_speed"), fleet ? t("security.detect_fleet") : fast ? t("security.detect_fast", { n: fast }) : t("security.detect_slow"), ""]);
    if (channels) {
      const ready = channels.channels.filter((x) => x.ready).map((x) => t("ch." + x.name));
      if (channels.webpush_devices) ready.push(t("ch.webpush"));
      facts.push([t("security.f_where"), ready.length ? ready.join("、")
        : `<a href="/settings#notifications">${esc(t("security.no_channel"))}</a>`, ready.length ? "" : "is-warn", !ready.length]);
    }
    if (config) facts.push([t("security.f_home"), config.home ? t("security.home_policy." + ((config.policies || {}).__home__ || "escalated")) : t("security.no_home"), ""]);
    $("sec-facts").innerHTML = facts.map(([l, v, cls, raw]) =>
      `<div class="sf"><div class="sf-l">${esc(l)}</div><div class="sf-v ${cls}">${raw ? v : esc(v)}</div></div>`).join("");
  }

  /* --------------------------------------------------------- the record */
  function dayKey(ts) {
    const d = new Date(ts * 1000);
    const today = new Date(), y = new Date(Date.now() - 86400000);
    if (d.toDateString() === today.toDateString()) return t("security.today");
    if (d.toDateString() === y.toDateString()) return t("security.yesterday");
    return d.toLocaleDateString(localeTag(), { weekday: "long", month: "long", day: "numeric", ...(d.getFullYear() !== today.getFullYear() ? { year: "numeric" } : {}) });
  }

  function paintList() {
    const list = $("sec-list");
    list.removeAttribute("aria-busy");
    const shown = rows.filter((r) => filter === "all" || severity(r) === filter);
    if (!shown.length) {
      list.innerHTML = `<div class="empty">${esc(t(rows.length ? "security.none_filter" : "security.none"))}</div>`;
      return;
    }
    let html = "", day = "";
    for (const r of shown) {
      const k = dayKey(r.started);
      if (k !== day) { html += `<div class="inc-day">${esc(k)}</div>`; day = k; }
      const sev = severity(r);
      const reasons = reasonText(r);
      const time = new Date(r.started * 1000).toLocaleTimeString(localeTag(), { hour: "2-digit", minute: "2-digit" });
      const dur = r.duration >= 60 ? t("security.lasted_min", { n: Math.round(r.duration / 60) }) : r.duration > 1 ? t("security.lasted_s", { n: Math.round(r.duration) }) : "";
      const clip = r.clip;
      const thumb = clip && clip.thumb
        ? `<img class="inc-th" loading="lazy" width="128" height="72" src="/media/${encodeURI(clip.thumb)}" alt="">`
        : `<div class="inc-th">${icon("film", 20)}</div>`;
      const href = clip ? `/dashcam/play?id=${encodeURIComponent(clip.event_id)}&${clip.at ? `at=${encodeURIComponent(clip.at)}` : `t=${clip.seek || 0}`}` : "";
      html += `<article class="inc ${sev}" id="inc-${r.id}">
        ${thumb}
        <div class="inc-top"><span class="inc-sev">${icon(SEV_ICON[sev], 15)}<span>${esc(t("security.lvl_" + sev))}</span></span>
          <time datetime="${new Date(r.started * 1000).toISOString()}">${esc(time)}</time>${r.geofence ? `<span>· ${esc(r.geofence)}</span>` : ""}</div>
        <div class="inc-what">${esc(reasons[0] || t("security.lvl_" + sev))}</div>
        <div class="inc-meta">${esc([...reasons.slice(1), dur, r.soc ? fmt.pct(Number(r.soc)) : "", t("sentry.source." + (r.source || "polled"))].filter(Boolean).join(" · "))}</div>
        ${href ? `<a class="inc-go" href="${href}">${esc(t("security.watch"))}${icon("arrow", 14)}</a>` : ""}
      </article>`;
    }
    list.innerHTML = html;
  }

  async function loadRows(more) {
    const q = new URLSearchParams({ limit: "40", car_id: String(Car.current) });
    if (more && oldest) q.set("before", String(oldest));
    let got = [];
    try { got = await apiGet(`/api/alerts/sentry/sessions?${q}`); } catch (e) {
      // The error stays inside the block that failed, with a way to try again.
      if (!more) {
        $("sec-list").innerHTML = `<div class="empty">${esc(t("common.load_failed", { msg: e.message }))}
          <div class="mt"><button type="button" class="btn sm" id="sec-retry">${esc(t("common.retry"))}</button></div></div>`;
        $("sec-retry").addEventListener("click", () => loadRows(false));
      }
      return;
    }
    rows = more ? rows.concat(got) : got;
    oldest = rows.length ? rows[rows.length - 1].started : null;
    $("sec-more").hidden = got.length < 40;
    paintList();
  }

  /* ----------------------------------------------------- folded settings */
  async function paintRules() {
    let list = [];
    try { list = await apiGet("/api/alerts/rules"); } catch (e) { return; }
    const loc = i18n.state.locale;
    const order = { push: 0, app: 1, off: 2 };
    list.sort((a, b) => order[a.default_mode] - order[b.default_mode]);
    const pushing = list.filter((r) => r.mode === "push").length;
    $("v-rules").textContent = t("security.rules_push", { n: pushing });
    $("rules-body").innerHTML = `<p class="body-sm prose" style="margin-bottom:12px">${esc(t("security.modes_intro"))}</p>`
      + list.map((r) => {
        const title = i18n.has("rule." + r.id) ? t("rule." + r.id) : (loc === "zh" ? r.title_zh : r.title_en);
        return `<div class="rule"><span class="nm">${esc(plain(title))}</span>
          <div class="segmented" role="group" data-rule="${r.id}">${["push", "app", "off"].map((m) =>
            `<button type="button" data-m="${m}" aria-pressed="${r.mode === m ? "true" : "false"}">${esc(t("mode." + m))}</button>`).join("")}</div>
          <span class="inline-status" role="status" aria-live="polite"></span></div>`;
      }).join("");
    $("rules-body").querySelectorAll(".segmented[data-rule]").forEach((g) => g.querySelectorAll("button").forEach((b) =>
      b.addEventListener("click", async () => {
        const st = g.parentElement.querySelector(".inline-status");
        g.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b ? "true" : "false"));
        try {
          await apiPost(`/api/alerts/rules/${g.dataset.rule}`, { mode: b.dataset.m });
          st.className = "inline-status is-done"; st.innerHTML = `${icon("check", 14)}<span>${esc(t("ch.saved"))}</span>`;
          const n = $("rules-body").querySelectorAll('[data-m="push"][aria-pressed="true"]').length;
          $("v-rules").textContent = t("security.rules_push", { n });
          setTimeout(() => { st.innerHTML = ""; }, 3000);
        } catch (e) {
          st.className = "inline-status is-fail"; st.innerHTML = `${icon("warn", 14)}<span>${esc(e.message)}</span>`;
          paintRules();
        }
      })));
  }

  async function paintSnooze() {
    try {
      const s = await apiGet("/api/alerts/snooze");
      $("v-snooze").textContent = s.active ? t("security.snoozed_for", { t: fmt.dur(s.seconds_left) }) : t("security.not_snoozed");
    } catch (e) { /* optional */ }
  }

  async function paintOther() {
    let list = [];
    try { list = await apiGet("/api/alerts/history?limit=30"); } catch (e) { return; }
    const other = list.filter((a) => !String(a.rule).startsWith("sentry"));
    $("v-other").textContent = other.length ? String(other.length) : "";
    $("other-body").innerHTML = other.length ? other.map((a) => `<div class="rule"><span class="nm">${esc(plain(a.title))}
        <span class="lv">${esc(new Date(a.ts * 1000).toLocaleString(localeTag()))}${a.delivered ? "" : " · " + esc(t("security.not_delivered"))}</span></span></div>`).join("")
      : `<div class="body-sm">${esc(t("security.none_other"))}</div>`;
  }

  async function loadConfig() {
    [config, channels] = await Promise.all([
      apiGet("/api/alerts/sentry/config").catch(() => null),
      apiGet("/api/alerts/channels/config").catch(() => null),
    ]);
    paintFacts();
  }

  /* --------------------------------------------------------------- boot */
  async function boot() {
    await i18nReady.catch(() => {});
    await Car.load(); Car.paint();
    paintHero();
    await Promise.all([loadConfig(), loadRows(false), paintRules(), paintSnooze(), paintOther()]);
    paintHero();

    $("sec-filter").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
      filter = b.dataset.f;
      $("sec-filter").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b ? "true" : "false"));
      paintList();
    }));
    $("sec-more").addEventListener("click", () => loadRows(true));

    const tog = $("sec-toggle");
    tog.addEventListener("click", async () => {
      toggling = true;
      const cmd = tog.dataset.cmd;
      await Cmd.run(cmd, null, { onState: Cmd.bindStatus(tog, $("sec-status"), {
        onState: (phase) => { if (phase !== "pending") { toggling = false; if (phase !== "sent") paintHero(); } },
      }) });
      toggling = false;
    });

    $("sec-test").addEventListener("click", async (e) => {
      const b = e.currentTarget, st = $("sec-status");
      b.disabled = true; b.setAttribute("aria-busy", "true");
      st.className = "inline-status is-pending"; st.innerHTML = `${icon("clock", 14)}<span>${esc(t("security.testing"))}</span>`;
      try {
        const r = await apiPost("/api/alerts/test");
        st.className = "inline-status " + (r.ok ? "is-done" : "is-fail");
        st.innerHTML = `${icon(r.ok ? "check" : "warn", 14)}<span>${esc(r.ok ? t("security.test_sent", { list: (r.channels || []).join("、") }) : t("security.test_none"))}</span>`;
      } catch (err) {
        st.className = "inline-status is-fail"; st.innerHTML = `${icon("warn", 14)}<span>${esc(err.message)}</span>`;
      }
      b.disabled = false; b.removeAttribute("aria-busy");
    });

    document.querySelectorAll("[data-snooze]").forEach((b) => b.addEventListener("click", async () => {
      const st = $("snooze-status");
      try {
        await apiPost("/api/alerts/snooze", { minutes: Number(b.dataset.snooze), scope: $("snooze-all").checked ? "all" : "noncritical" });
        st.className = "inline-status is-done"; st.innerHTML = `${icon("check", 14)}<span>${esc(t("ch.saved"))}</span>`;
      } catch (e) {
        st.className = "inline-status is-fail"; st.innerHTML = `${icon("warn", 14)}<span>${esc(e.message)}</span>`;
      }
      paintSnooze();
    }));

    let tm = null;
    Live.subscribe(() => { clearTimeout(tm); tm = setTimeout(paintHero, 300); });
    let activeSig = "";
    window.addEventListener("omt:sentry", (e) => {
      paintHero();
      const sig = JSON.stringify(Object.keys(e.detail || {}).sort());
      if (sig !== activeSig) { activeSig = sig; loadRows(false); }
    });
    window.addEventListener("omt:locale", () => { paintHero(); paintList(); paintRules(); paintSnooze(); paintOther(); });
    Car.onChange(() => { paintHero(); loadRows(false); });

    // ?incident=ID: bring that event into view and mark it.
    const want = new URLSearchParams(location.search).get("incident");
    if (want) {
      const el = document.getElementById("inc-" + want);
      if (el) { el.classList.add("is-target"); el.scrollIntoView({ block: "center" }); }
    }
  }
  boot();
})();
