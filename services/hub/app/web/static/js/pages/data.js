/* 数据 hub: plumbing shared by the six chapters.
 *
 *  · ONE period for the whole hub (?days=7|30|90|365|all), kept in the URL,
 *    remembered per viewer, carried by every chapter link.
 *  · The chapter index: peers swap instantly, only the 2px indicator moves,
 *    and it slides from the chapter you came from.
 *  · Blocks load on their own and fail on their own: skeleton at final size,
 *    errors inside the block with Retry, honest empty states.
 *  · Formatting in the hub's time zone and the owner's units (U), numbers
 *    through Intl with fixed decimals, deltas coloured by good/bad.
 *  · Motion budget: one gentle reveal on the first hub visit of a session;
 *    charts morph when the period changes; nothing else moves.
 */
(function () {
  "use strict";
  const root = document.getElementById("dh");
  if (!root) return;

  const PERIODS = ["7", "30", "90", "365", "all"];
  const K_PERIOD = "omt.data.period";
  const K_SEEN = "omt.data.seen";
  const K_IND = "omt.data.ind";
  const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
  const ls = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
  };
  const ss = {
    get(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { sessionStorage.setItem(k, v); } catch (e) { /* blocked */ } },
    del(k) { try { sessionStorage.removeItem(k); } catch (e) { /* blocked */ } },
  };

  const DH = {
    root, chapter: root.dataset.chapter, car: null, carInfo: null, tz: undefined,
    period: "90", firstVisit: false,
  };

  /* -------------------------------------------------------------- period */

  function readPeriod() {
    let p = new URLSearchParams(location.search).get("days");
    if (p === "0") p = "all";
    if (!PERIODS.includes(p)) p = ls.get(K_PERIOD);
    return PERIODS.includes(p) ? p : "90";
  }
  DH.period = readPeriod();
  DH.days = () => (DH.period === "all" ? 0 : +DH.period);
  /* Bucket for "over time" charts: enough bars to read a trend, few enough
     to stay square. "All" switches to years once there are four of them. */
  DH.bucket = (spanDays) => {
    if (DH.period === "7" || DH.period === "30") return "day";
    if (DH.period === "90") return "week";
    if (DH.period === "all" && spanDays > 4 * 365) return "year";
    return "month";
  };

  function writeUrl() {
    const u = new URL(location.href);
    u.searchParams.set("days", DH.period);
    history.replaceState(history.state, "", u);
    root.querySelectorAll("a.dh-ch").forEach((a) => {
      const h = new URL(a.getAttribute("href"), location.href);
      h.searchParams.set("days", DH.period);
      a.setAttribute("href", h.pathname + h.search);
    });
  }

  function paintPeriod() {
    root.querySelectorAll(".dh-per button").forEach((b) =>
      b.setAttribute("aria-pressed", String(b.dataset.days === DH.period)));
    const lab = root.querySelector(".dh-per-btn .lbl");
    if (lab) lab.textContent = t("data.period.short_" + DH.period);
  }

  function setPeriod(p) {
    if (!PERIODS.includes(p) || p === DH.period) return;
    DH.period = p;
    ls.set(K_PERIOD, p);
    writeUrl();
    paintPeriod();
    paintMeta();
    reloadAll(true);
  }

  /* ------------------------------------------------------- chapter index */

  function indTarget() {
    const cur = root.querySelector('.dh-ch[aria-current="page"] > span');
    return cur ? { x: cur.offsetLeft, w: cur.offsetWidth } : null;
  }
  function placeInd(animateFrom) {
    const ind = root.querySelector(".dh-ind");
    const to = indTarget();
    if (!ind || !to) return;
    ind.classList.remove("go");
    if (animateFrom && !reduced()) {
      ind.style.width = animateFrom.w + "px";
      ind.style.transform = `translateX(${animateFrom.x}px)`;
      void ind.offsetWidth;
      ind.classList.add("go");
    }
    ind.style.width = to.w + "px";
    ind.style.transform = `translateX(${to.x}px)`;
  }
  function initNav() {
    const chs = root.querySelector(".dh-chs");
    if (!chs) return;
    let from = null;
    try { from = JSON.parse(ss.get(K_IND) || "null"); } catch (e) { from = null; }
    ss.del(K_IND);
    if (from && typeof from.sl === "number") chs.scrollLeft = from.sl;
    placeInd(from && from.ch !== DH.chapter ? from : null);
    // keep the current chapter visible on a narrow screen
    const cur = root.querySelector('.dh-ch[aria-current="page"]');
    if (cur) {
      const l = cur.offsetLeft, r = l + cur.offsetWidth;
      if (l < chs.scrollLeft || r > chs.scrollLeft + chs.clientWidth) {
        chs.scrollTo({ left: Math.max(0, l - 16), behavior: reduced() || !from ? "auto" : "smooth" });
      }
    }
    chs.addEventListener("click", (e) => {
      const a = e.target.closest("a.dh-ch");
      if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button) return;
      const to = indTarget();
      if (to) ss.set(K_IND, JSON.stringify({ ...to, sl: chs.scrollLeft, ch: DH.chapter }));
    });
    window.addEventListener("resize", () => placeInd(null), { passive: true });
    // labels change width when the language does
    window.addEventListener("omt:locale", () => requestAnimationFrame(() => placeInd(null)));
  }

  function initPeriod() {
    paintPeriod();
    root.querySelectorAll(".dh-per button").forEach((b) => b.addEventListener("click", () => setPeriod(b.dataset.days)));
    const btn = root.querySelector(".dh-per-btn");
    if (btn) btn.addEventListener("click", () => {
      const html = `<div class="dh-per-sheet" role="group">${PERIODS.map((p) =>
        `<button type="button" data-days="${p}" aria-pressed="${p === DH.period}">${esc(t("data.period.long_" + p))}</button>`).join("")}</div>`;
      Sheet.open({ title: t("data.period.label"), html });
      document.querySelectorAll(".dh-per-sheet button").forEach((b) => b.addEventListener("click", () => {
        Sheet.close();
        setPeriod(b.dataset.days);
      }));
    });
  }

  /* The shell's chrome may be a fixed top bar (phone, and the new desktop
     bar). Measure whatever is pinned to the top so the index sticks below
     it instead of sliding underneath. */
  function measureTop() {
    let bottom = 0;
    document.querySelectorAll("body > header, body > nav, body > div > header, .app-topbar, .app-top, .topbar").forEach((n) => {
      if (root.contains(n)) return;
      const cs = getComputedStyle(n);
      if (cs.display === "none" || (cs.position !== "fixed" && cs.position !== "sticky")) return;
      const r = n.getBoundingClientRect();
      if (r.top <= 1 && r.height > 0 && r.height < 160 && r.width > innerWidth * 0.5) bottom = Math.max(bottom, r.bottom);
    });
    root.style.setProperty("--dh-top", Math.round(bottom) + "px");
  }

  /* --------------------------------------------------------------- fetch */

  const cache = new Map();
  DH.url = (path, params) => {
    const u = new URL(path, location.origin);
    const p = params || {};
    if (p.car !== false && DH.car != null) u.searchParams.set("car_id", DH.car);
    if (p.days === true) u.searchParams.set("days", DH.days());
    else if (p.days != null && p.days !== false) u.searchParams.set("days", p.days);
    for (const [k, v] of Object.entries(p)) {
      if (["car", "days"].includes(k) || v == null) continue;
      u.searchParams.set(k, v);
    }
    return u.pathname + u.search;
  };
  DH.get = (url, fresh) => {
    if (!fresh && cache.has(url)) return cache.get(url);
    const p = fetch(url, { headers: { Accept: "application/json" } }).then(async (r) => {
      if (!r.ok) {
        let detail = r.statusText;
        try { const j = await r.json(); if (typeof j.detail === "string") detail = j.detail; } catch (e) { /* not JSON */ }
        const err = new Error(detail || String(r.status));
        err.code = r.headers.get("X-Error-Code");
        err.status = r.status;
        throw err;
      }
      const j = await r.json();
      const tz = j && ((j.period && j.period.tz) || j.tz);
      if (tz && !DH.tz) DH.tz = tz;
      return j;
    });
    cache.set(url, p);
    p.catch(() => cache.delete(url));
    return p;
  };
  DH.errText = (e) => {
    const key = e && e.code ? "analytics.error." + e.code : null;
    if (key && t(key) !== key) return t(key);
    if (e && e.status === 404) return t("data.c.err_404");
    return (e && e.message) || t("data.c.err_generic");
  };

  /* -------------------------------------------------------------- blocks
     DH.block(el, {load, render}) — load() resolves the data, render(data,
     body, ctx) paints it; ctx.reload is true when the period or car changed
     (charts morph). Errors stay inside the block with Retry. */

  const blocks = [];
  DH.block = (el, spec) => {
    const sec = typeof el === "string" ? document.getElementById(el) : el;
    if (!sec) return null;
    const body = sec.querySelector(".dh-body") || sec;
    const skel = body.innerHTML;
    const b = { sec, body, spec, gen: 0, data: null, shown: false };
    b.run = async (reload) => {
      const gen = ++b.gen;
      sec.setAttribute("aria-busy", "true");
      if (!b.shown && body.dataset.state === "err") { body.innerHTML = skel; body.dataset.state = ""; }
      let data;
      try {
        data = await spec.load();
      } catch (e) {
        if (gen !== b.gen) return;
        sec.removeAttribute("aria-busy");
        b.shown = false;
        body.dataset.state = "err";
        body.innerHTML = `<div class="dh-err" role="alert"><span class="ic">${icon("warn", 18)}</span>
          <span class="msg"><b>${esc(t(spec.errTitle || "data.c.err_title"))}</b>${esc(DH.errText(e))}</span>
          <button type="button" class="dh-btn">${esc(t("common.retry"))}</button></div>`;
        body.querySelector(".dh-btn").addEventListener("click", () => {
          body.innerHTML = skel; body.dataset.state = "";
          b.run(false);
        });
        return;
      }
      if (gen !== b.gen) return;
      b.data = data;
      sec.removeAttribute("aria-busy");
      body.dataset.state = "ok";
      try {
        spec.render(data, body, { reload: !!reload && b.shown, block: b });
        b.shown = true;
      } catch (e) {
        console.error(e);
        body.dataset.state = "err";
        body.innerHTML = `<div class="dh-err" role="alert"><span class="ic">${icon("warn", 18)}</span>
          <span class="msg"><b>${esc(t("data.c.err_title"))}</b>${esc(String(e && e.message || e))}</span></div>`;
      }
    };
    b.rerender = () => {
      if (b.data != null && b.shown) {
        try { spec.render(b.data, body, { reload: false, block: b, rerender: true }); } catch (e) { console.error(e); }
      }
    };
    blocks.push(b);
    if (DH._started) b.run(false);
    return b;
  };
  /* Build a block's structure once; later renders only refill it, so chart
     elements survive and can morph. A different key rebuilds. */
  DH.shell = (body, key, html) => {
    // the marker lives in the DOM: an empty state or an error that replaced
    // the block's content takes the marker with it, and the next render
    // rebuilds instead of looking for elements that are gone
    const cur = body.firstElementChild;
    if (body.children.length === 1 && cur && cur.dataset.shell === key) return false;
    body.innerHTML = `<div data-shell="${esc(key)}">${html}</div>`;
    return true;
  };

  const reloadHooks = [];
  DH.onReload = (fn) => reloadHooks.push(fn);
  function reloadAll(morph) {
    cache.clear();
    blocks.forEach((b) => b.run(morph));
    reloadHooks.forEach((fn) => { try { fn(morph); } catch (e) { console.error(e); } });
  }
  DH.reloadAll = reloadAll;

  /* ---------------------------------------------------------- formatting */

  const isMi = () => typeof U !== "undefined" && U.distLabel() === "mi";
  const isF = () => typeof U !== "undefined" && U.tempLabel() === "°F";
  DH.cv = {
    dist: (km) => (km == null || !isFinite(km) ? null : isMi() ? km * 0.621371 : +km),
    speed: (kmh) => (kmh == null || !isFinite(kmh) ? null : isMi() ? kmh * 0.621371 : +kmh),
    cons: (whkm) => (whkm == null || !isFinite(whkm) ? null : isMi() ? whkm / 0.621371 : +whkm),
    temp: (c) => (c == null || !isFinite(c) ? null : isF() ? c * 9 / 5 + 32 : +c),
    area: (km2) => (km2 == null || !isFinite(km2) ? null : isMi() ? km2 * 0.386102 : +km2),
    perDist: (perKm) => (perKm == null || !isFinite(perKm) ? null : isMi() ? perKm / 0.621371 : +perKm),
  };
  DH.u = {
    dist: () => (isMi() ? "mi" : "km"),
    speed: () => (isMi() ? "mph" : "km/h"),
    cons: () => (isMi() ? "Wh/mi" : "Wh/km"),
    temp: () => (isF() ? "°F" : "°C"),
    area: () => (isMi() ? "mi²" : "km²"),
  };
  const nfs = new Map();
  DH.nf = (v, d) => {
    if (v == null || !isFinite(v)) return null;
    const dd = d == null ? 0 : d;
    const k = localeTag() + "|" + dd;
    let f = nfs.get(k);
    if (!f) { f = new Intl.NumberFormat(localeTag(), { minimumFractionDigits: dd, maximumFractionDigits: dd }); nfs.set(k, f); }
    return f.format(v);
  };
  /* Number with fixed decimals; a grey dash when missing (never "null"). */
  DH.n = (v, d) => { const s = DH.nf(v, d); return s == null ? '<span class="dash">–</span>' : esc(s); };
  /* Value + grey unit. */
  DH.vu = (v, unit, d) => {
    const s = DH.nf(v, d);
    if (s == null) return '<span class="dash">–</span>';
    return `<span class="n">${esc(s)}</span>${unit ? `<span class="u">${esc(unit)}</span>` : ""}`;
  };
  DH.money = (v, d) => (v == null || !isFinite(v) ? null : fmt.money(v, d == null ? 2 : d));
  DH.currencySymbol = () => {
    try {
      const cur = (window.i18n && i18n.state && i18n.state.currency) || "USD";
      const p = new Intl.NumberFormat(localeTag(), { style: "currency", currency: cur }).formatToParts(0).find((x) => x.type === "currency");
      return p ? p.value : cur;
    } catch (e) { return ""; }
  };
  DH.mv = (v, d) => { const s = DH.money(v, d); return s == null ? '<span class="dash">–</span>' : `<span class="n">${esc(s)}</span>`; };
  /* Decimals that suit the size of a distance in the user's unit. */
  DH.dd = (v) => (v == null ? 0 : Math.abs(v) < 10 ? 1 : 0);

  DH.dur = (min) => {
    if (min == null || !isFinite(min)) return "–";
    const m = Math.round(min);
    if (m < 60) return t("timeline.dur_m", { m });
    if (m < 60 * 24) return t("timeline.dur_hm", { h: Math.floor(m / 60), m: m % 60 });
    return t("timeline.dur_dh", { d: Math.floor(m / 1440), h: Math.floor((m % 1440) / 60) });
  };
  DH.hours = (min) => {
    if (min == null || !isFinite(min)) return "–";
    const h = min / 60;
    return t("data.c.hours", { n: DH.nf(h, h < 10 ? 1 : 0) });
  };

  const dtfs = new Map();
  function dtf(opts) {
    const k = localeTag() + "|" + (DH.tz || "") + "|" + JSON.stringify(opts);
    let f = dtfs.get(k);
    if (!f) {
      try { f = new Intl.DateTimeFormat(localeTag(), { ...opts, timeZone: DH.tz }); }
      catch (e) { f = new Intl.DateTimeFormat(localeTag(), opts); }
      dtfs.set(k, f);
    }
    return f;
  }
  const toDate = (v) => (v instanceof Date ? v : typeof v === "number" ? new Date(v) : new Date(v));
  const DATE_ONLY = /^(\d{4})-(\d{2})-(\d{2})$/;
  /* "YYYY-MM-DD" strings are local calendar days already: format them as
     such, never through UTC midnight (that shifts a day west of Greenwich). */
  const dayDate = (s) => { const m = DATE_ONLY.exec(s); return m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], 12)) : null; };
  DH.fdate = (v, opts) => {
    if (v == null || v === "") return "–";
    const dd = typeof v === "string" ? dayDate(v) : null;
    if (dd) return new Intl.DateTimeFormat(localeTag(), { ...opts, timeZone: "UTC" }).format(dd);
    const d = toDate(v);
    return isNaN(d) ? "–" : dtf(opts).format(d);
  };
  DH.date = (v) => DH.fdate(v, { year: "numeric", month: "short", day: "numeric" });
  DH.dateShort = (v) => DH.fdate(v, { month: "short", day: "numeric" });
  DH.time = (v) => DH.fdate(v, { hour: "numeric", minute: "2-digit" });
  DH.dt = (v) => DH.fdate(v, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  DH.when = (v) => (DH.parts(v).key === DH.parts(Date.now()).key ? DH.time(v) : DH.dt(v));
  DH.monthLabel = (key, withYear) => DH.fdate(key, withYear ? { month: "short", year: "numeric" } : { month: "short" });

  /* Calendar parts of an instant in the hub's time zone (buckets, the
     weekday × hour grid). One formatter per zone: this runs per drive. */
  const WD = { Mon: 0, Tue: 1, Wed: 2, Thu: 3, Fri: 4, Sat: 5, Sun: 6 };
  const partsF = new Map();
  DH.parts = (v) => {
    const d = toDate(v);
    const zone = DH.tz || "";
    let f = partsF.get(zone);
    if (f === undefined) {
      try {
        f = new Intl.DateTimeFormat("en-US", {
          timeZone: DH.tz, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", hourCycle: "h23", weekday: "short",
        });
      } catch (e) { f = null; }
      partsF.set(zone, f);
    }
    let p;
    if (f) p = Object.fromEntries(f.formatToParts(d).map((x) => [x.type, x.value]));
    else {
      p = { year: String(d.getFullYear()), month: String(d.getMonth() + 1).padStart(2, "0"), day: String(d.getDate()).padStart(2, "0"),
            hour: String(d.getHours()), weekday: ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d.getDay()] };
    }
    return { y: +p.year, m: +p.month, d: +p.day, h: +p.hour % 24, wd: WD[p.weekday] ?? 0, key: `${p.year}-${p.month}-${p.day}` };
  };
  const pad2 = (n) => String(n).padStart(2, "0");
  const keyOf = (y, m, d) => `${y}-${pad2(m)}-${pad2(d)}`;
  const addDays = (key, n) => {
    const [y, m, d] = key.split("-").map(Number);
    const x = new Date(Date.UTC(y, m - 1, d + n));
    return keyOf(x.getUTCFullYear(), x.getUTCMonth() + 1, x.getUTCDate());
  };
  DH.addDays = addDays;
  DH.bucketKey = (dayKey, bucket) => {
    const [y, m, d] = dayKey.split("-").map(Number);
    if (bucket === "year") return keyOf(y, 1, 1);
    if (bucket === "month") return keyOf(y, m, 1);
    if (bucket === "week") {
      const wd = (new Date(Date.UTC(y, m - 1, d)).getUTCDay() + 6) % 7;
      return addDays(dayKey, -wd);
    }
    return dayKey;
  };
  /* Every bucket from the start of the period to today, so empty days and
     months show as honest zeros instead of disappearing. */
  DH.buckets = (startKey, bucket, endKey) => {
    const end = DH.bucketKey(endKey || DH.parts(Date.now()).key, bucket);
    let k = DH.bucketKey(startKey, bucket);
    const out = [];
    for (let guard = 0; k <= end && guard < 2000; guard++) {
      out.push(k);
      const [y, m] = k.split("-").map(Number);
      k = bucket === "day" ? addDays(k, 1) : bucket === "week" ? addDays(k, 7)
        : bucket === "month" ? (m === 12 ? keyOf(y + 1, 1, 1) : keyOf(y, m + 1, 1)) : keyOf(y + 1, 1, 1);
    }
    return out;
  };
  DH.bucketLabel = (key, bucket, multiYear) => {
    if (bucket === "year") return key.slice(0, 4);
    if (bucket === "month") return DH.monthLabel(key, multiYear && (key.slice(5, 7) === "01"));
    return DH.dateShort(key);
  };
  /* Start of the selected period as a local day key (needs the first record
     for "all"). */
  DH.periodStartKey = (firstIso) => {
    const today = DH.parts(Date.now()).key;
    if (DH.period === "all") return firstIso ? DH.parts(firstIso).key : addDays(today, -89);
    return addDays(today, -(DH.days() - 1));
  };

  /* Odometer distance per bucket from /api/analytics/mileage. Rows exist only
     where the car reported positions, so after a logging outage the whole
     gap lands on the bucket where data resumed. Spread the unlogged part
     evenly over the buckets of the gap instead of drawing one false spike;
     `spread[i]` says how many buckets shared it (tooltips say so). km out. */
  DH.mileage = (rows, keys) => {
    const idx = new Map(keys.map((k, i) => [k, i]));
    const logged = keys.map(() => 0), un = keys.map(() => 0), spread = keys.map(() => 0);
    let prev = -1;
    for (const r of rows || []) {
      const i = idx.get(r.date);
      if (i == null) continue;
      logged[i] += r.logged_km || 0;
      const u = Math.max(0, r.unlogged_km || 0);
      // only between two buckets that both have readings: before the first
      // one there is nothing to say when the distance happened
      const from = prev >= 0 ? prev + 1 : i;
      const n = i - from + 1;
      if (u > 0 && n > 1) {
        for (let j = from; j <= i; j++) { un[j] += u / n; spread[j] = n; }
      } else {
        un[i] += u;
      }
      prev = i;
    }
    return { logged, un, spread };
  };

  /* Change vs the previous period: sign carries direction, colour carries
     good/bad (when the metric has one), zero is grey. */
  DH.delta = (cur, prev, better) => {
    if (cur == null || prev == null || !isFinite(cur) || !isFinite(prev)) {
      return { html: `<span class="flat" title="${esc(t("data.c.delta_na_t"))}">${esc(t("data.c.delta_na"))}</span>`, cls: "na" };
    }
    if (prev === 0) {
      if (cur === 0) return { html: `<span class="flat">0%</span>`, cls: "flat" };
      return { html: `<span class="flat" title="${esc(t("data.c.delta_from0_t"))}">${esc(t("data.c.delta_from0"))}</span>`, cls: "na" };
    }
    const pct = ((cur - prev) / Math.abs(prev)) * 100;
    const r = Math.round(pct);
    if (r === 0) return { html: `<span class="flat">0%</span>`, cls: "flat" };
    const good = better === "down" ? r < 0 : better === "up" ? r > 0 : null;
    const cls = good == null ? "flat" : good ? "good" : "bad";
    const sign = r > 0 ? "+" : "−";
    const sr = good == null ? "" : `<span class="sr"> ${esc(t(good ? "data.c.better" : "data.c.worse"))}</span>`;
    return { html: `<span class="${cls}">${sign}${esc(DH.nf(Math.abs(r), 0))}%</span>${sr}`, cls };
  };

  /* KPI strip item. */
  DH.kpi = (i, label, valueHtml, deltaHtml, title) =>
    `<div class="dh-kpi" data-rv style="--i:${i}"${title ? ` title="${esc(title)}"` : ""}><div class="dh-kpi-l">${esc(label)}</div>
      <div class="dh-kpi-v">${valueHtml}</div>${deltaHtml != null ? `<div class="dh-kpi-d">${deltaHtml}</div>` : ""}</div>`;
  DH.fig = (label, valueHtml, sub) =>
    `<div class="dh-fig"><div class="dh-fig-l">${esc(label)}</div><div class="dh-fig-v">${valueHtml}</div>${sub ? `<div class="dh-fig-s">${sub}</div>` : ""}</div>`;
  DH.empty = (text, linkHtml) => `<div class="dh-empty">${esc(text)}${linkHtml ? " " + linkHtml : ""}</div>`;

  /* A token as "rgb(r, g, b)" for MapLibre and canvas, which can't parse
     color-mix() or every colour space a token may use. */
  let probe = null, pctx = null;
  DH.color = (expr, alpha) => {
    if (!probe) {
      probe = document.createElement("span");
      probe.style.cssText = "position:absolute;width:0;height:0;overflow:hidden;visibility:hidden";
      root.appendChild(probe);
      pctx = document.createElement("canvas").getContext("2d", { willReadFrequently: true });
      pctx.canvas.width = pctx.canvas.height = 1;
    }
    probe.style.color = "";
    probe.style.color = expr;
    const c = getComputedStyle(probe).color;
    pctx.clearRect(0, 0, 1, 1);
    pctx.fillStyle = "#000";
    pctx.fillStyle = c;
    pctx.fillRect(0, 0, 1, 1);
    const [r, g, b2] = pctx.getImageData(0, 0, 1, 1).data;
    return alpha == null ? `rgb(${r}, ${g}, ${b2})` : `rgba(${r}, ${g}, ${b2}, ${alpha})`;
  };
  DH.locale = () => ((window.i18n && i18n.state && i18n.state.locale) || "en");
  DH.isDark = () => {
    const forced = document.documentElement.dataset.theme;
    if (forced) return forced === "dark";
    return !matchMedia("(prefers-color-scheme: light)").matches;
  };

  /* Odometer digits: reuse the digit nodes; a real change rolls them. */
  DH.odo = (el, text, still) => {
    const s = String(text);
    el.classList.add("odo");
    el.setAttribute("aria-label", s);
    const kids = [...el.children];
    const sameShape = kids.length === s.length && kids.every((k, i) => (/\d/.test(s[i]) ? k.classList.contains("od") : k.classList.contains("os")));
    if (!sameShape) {
      el.classList.add("still");
      el.innerHTML = [...s].map((ch) => (/\d/.test(ch)
        ? `<span class="od" aria-hidden="true"><span>${"0123456789".split("").map((d) => `<i>${d}</i>`).join("")}</span></span>`
        : `<span class="os" aria-hidden="true">${esc(ch)}</span>`)).join("");
      void el.offsetWidth;
    } else if (still) {
      el.classList.add("still");
    }
    [...el.children].forEach((k, i) => {
      if (k.classList.contains("od")) k.firstElementChild.style.transform = `translateY(${-(+s[i]) * 10}%)`;
    });
    if (!sameShape || still) { void el.offsetWidth; requestAnimationFrame(() => el.classList.remove("still")); }
  };

  /* ---------------------------------------------------------- header meta */

  function paintMeta() {
    const el = document.getElementById("dh-meta");
    if (!el) return;
    const bits = [];
    const c = DH.carInfo;
    if (c) bits.push(esc(Car.label(c)));
    bits.push(esc(t("data.period.long_" + DH.period)));
    if (c && c.last_seen) {
      const age = (Date.now() - new Date(c.last_seen).getTime()) / 1000;
      bits.push(esc(age < 600 ? t("data.c.fresh_live") : t("data.c.fresh", { when: age < 86400 ? DH.time(c.last_seen) : DH.dt(c.last_seen) })));
    }
    el.innerHTML = bits.map((b) => `<span>${b}</span>`).join("");
  }

  async function paintNotes() {
    const box = document.getElementById("dh-notes");
    if (!box) return;
    const out = [];
    const c = DH.carInfo;
    if (c && c.archived) {
      out.push(`<div class="dh-notice"><span><b>${esc(t("analytics.car.archived"))}</b> · ${esc(t("analytics.car.archived_hint", { days: Math.round(c.days_since_seen || 0) }))}</span></div>`);
    }
    try {
      const h = await DH.get("/api/health/teslamate");
      if (h && h.warning) {
        const body = h.status === "incompatible" ? t("analytics.schema.incompatible", { columns: (h.missing_columns || []).join(", ") })
          : h.status === "not_ready" ? t("analytics.schema.not_ready") : t("analytics.schema.newer", { version: h.version || "?" });
        out.push(`<div class="dh-notice warn"><span>${esc(body)}</span></div>`);
      }
    } catch (e) { /* the guard is optional */ }
    box.innerHTML = out.join("");
  }

  /* --------------------------------------------------------------- start */

  let readyResolve;
  DH.ready = new Promise((r) => { readyResolve = r; });

  async function start() {
    try { await i18nReady; } catch (e) { /* catalogues fall back to keys */ }
    // first hub visit of the session: one gentle reveal, then never again
    DH.firstVisit = !ss.get(K_SEEN);
    ss.set(K_SEEN, "1");
    if (DH.firstVisit) {
      root.classList.add("rv");
      DC.revealUntil = performance.now() + 3500;
      setTimeout(() => root.classList.remove("rv"), 3500);
    }
    writeUrl();
    initNav();
    initPeriod();
    measureTop();
    setTimeout(measureTop, 400);
    window.addEventListener("resize", () => { clearTimeout(measureTop.t); measureTop.t = setTimeout(measureTop, 150); }, { passive: true });

    if (!Car.list.length) await Car.load();
    DH.car = Car.current;
    try {
      const a = await DH.get("/api/analytics/cars");
      DH.carInfo = (a.cars || []).find((c) => c.id === DH.car) || null;
      if (DH.car == null && a.default_car_id != null) DH.car = a.default_car_id;
    } catch (e) { DH.carInfo = Car.info(DH.car) || null; }
    paintMeta();
    paintNotes();

    if (DH.car == null) {
      const main = document.getElementById("dh-main");
      if (main) main.innerHTML = DH.empty(t("data.c.no_car"));
      readyResolve(DH);
      return;
    }
    Car.onChange(async (id) => {
      DH.car = Number(id);
      cache.clear();
      try {
        const a = await DH.get("/api/analytics/cars");
        DH.carInfo = (a.cars || []).find((c) => c.id === DH.car) || null;
      } catch (e) { DH.carInfo = Car.info(DH.car) || null; }
      paintMeta();
      paintNotes();
      reloadAll(false);
    });
    window.addEventListener("omt:locale", () => {
      dtfs.clear(); nfs.clear();
      document.title = t("data.title." + DH.chapter);
      paintPeriod();
      paintMeta();
      blocks.forEach((b) => b.rerender());
    });
    DH._started = true;
    blocks.forEach((b) => b.run(false));
    readyResolve(DH);
  }

  window.DH = DH;
  start();
})();
