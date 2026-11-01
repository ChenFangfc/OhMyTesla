/* 数据 · 行车: how do I drive? How much, what the trips are like, how fast
   and at what cost, when; then the trips themselves. */
(function () {
  "use strict";
  const $ = (s, r) => (r || document).querySelector(s);
  const DRIVES = () => DH.url("/api/analytics/drives", { days: DH.days(), limit: 1000 });
  const SUMMARY = () => DH.url("/api/insights/summary");
  const EFF = () => DH.url("/api/analytics/efficiency", { days: DH.days() });
  const loadDrives = () => DH.get(DRIVES());

  /* Bucket keys for the period. Needs the hub's time zone, which arrives with
     the first analytics answer, so the drives list is loaded first. */
  async function bucketing() {
    await loadDrives();
    const s = await DH.get(SUMMARY());
    const span = s.first_record ? (Date.now() - new Date(s.first_record)) / 864e5 : 90;
    const bucket = DH.bucket(span);
    const keys = DH.buckets(DH.periodStartKey(s.first_record), bucket);
    const multi = keys.length > 0 && keys[0].slice(0, 4) !== keys[keys.length - 1].slice(0, 4);
    return { s, bucket, keys, labels: keys.map((k) => DH.bucketLabel(k, bucket, multi)) };
  }
  const routeOf = (d) => (d.from && d.to && d.from !== d.to ? `${d.from} → ${d.to}` : d.to || d.from || t("data.drv.unknown_route"));

  /* --------------------------------------------------------------- KPIs */
  DH.block("dv-sum", {
    load: loadDrives,
    render(d, body) {
      const s = d.summary || {};
      if (!s.drives) { body.innerHTML = DH.empty(t("data.drv.none"), `<a href="?days=all">${esc(t("data.c.try_all"))}</a>`); return; }
      const km = DH.cv.dist(s.distance_km);
      const rated = d.car && d.car.efficiency_kwh_per_km ? d.car.efficiency_kwh_per_km * 1000 : null;
      const avg = s.duration_min ? DH.cv.speed(s.distance_km / (s.duration_min / 60)) : null;
      const vsRated = rated && s.wh_per_km ? Math.round((rated / s.wh_per_km) * 100) : null;
      const items = [
        [t("data.k.distance"), DH.vu(km, DH.u.dist(), DH.dd(km)),
          s.median_distance_km != null ? esc(t("data.drv.median", { v: `${DH.nf(DH.cv.dist(s.median_distance_km), 1)} ${DH.u.dist()}` })) : ""],
        [t("data.k.drives"), DH.vu(s.drives, "", 0), s.open ? esc(t("data.drv.open_n", { n: s.open })) : esc(t("data.drv.per_week", { n: DH.nf(s.drives / Math.max(1, (DH.days() || spanDays(d)) / 7), 1) }))],
        [t("data.k.time_driving"), DH.vu(s.duration_min / 60, t("data.c.h_unit"), s.duration_min < 600 ? 1 : 0), ""],
        [t("data.k.avg_speed"), DH.vu(avg, DH.u.speed(), 0), s.speed_max_kmh ? esc(t("data.drv.top_speed", { v: `${DH.nf(DH.cv.speed(s.speed_max_kmh), 0)} ${DH.u.speed()}` })) : ""],
        [t("data.k.consumption"), DH.vu(DH.cv.cons(s.wh_per_km), DH.u.cons(), 0),
          vsRated ? esc(t("data.drv.vs_rated", { pct: vsRated })) : ""],
        [t("data.k.per_day"), DH.vu(DH.cv.dist(s.km_per_day), DH.u.dist(), 1),
          s.extrapolated_annual_km ? esc(t("data.drv.per_year", { v: `${DH.nf(DH.cv.dist(s.extrapolated_annual_km), 0)} ${DH.u.dist()}` })) : ""],
      ];
      body.innerHTML = `<div class="dh-kpis" style="--cols:6">${items.map(([l, v, sub], i) => DH.kpi(i, l, v, sub)).join("")}</div>`;
    },
  });
  function spanDays(d) {
    const ds = d.drives || [];
    if (!ds.length) return 7;
    return Math.max(1, (Date.now() - new Date(ds[ds.length - 1].start)) / 864e5);
  }

  /* ------------------------------------------------- distance over time */
  DH.block("dv-dist", {
    load: async () => {
      const bk = await bucketing();
      const mi = await DH.get(DH.url("/api/analytics/mileage", { days: DH.days(), bucket: bk.bucket }));
      return { ...bk, mi };
    },
    render(d, body, ctx) {
      $("#dv-trend-n").textContent = t("data.ov.by_" + d.bucket);
      const m = DH.mileage(d.mi.rows, d.keys);
      const logged = m.logged.map(DH.cv.dist);
      const un = m.un.map(DH.cv.dist);
      const anyUn = un.some((v) => v > 0.5);
      DH.shell(body, "c", `<p class="dh-subn"></p><div class="dh-chart"></div><div class="dh-legend"></div>`);
      const tl = logged.reduce((a, v) => a + v, 0), tu = un.reduce((a, v) => a + v, 0);
      $(".dh-subn", body).textContent = anyUn
        ? t("data.ov.dist_sub_gap", { logged: `${DH.nf(tl, 0)} ${DH.u.dist()}`, unlogged: `${DH.nf(tu, 0)} ${DH.u.dist()}` })
        : t("data.ov.dist_sub", { logged: `${DH.nf(tl, 0)} ${DH.u.dist()}` });
      const du = DH.u.dist();
      DC.bars($(".dh-chart", body), {
        keys: d.keys, labels: d.labels, values: logged, extra: anyUn ? un : null, unit: du, morph: ctx.reload,
        label: t("data.drv.dist"), empty: t("data.c.none_period"),
        tip: (i) => `<b>${esc(d.labels[i])}</b><div class="r"><span>${esc(t("data.ov.logged"))}</span><span class="v">${esc(DH.nf(logged[i], DH.dd(logged[i])))} ${esc(du)}</span></div>`
          + (anyUn ? `<div class="r"><span>${esc(t("data.ov.unlogged"))}</span><span class="v">${esc(DH.nf(un[i], DH.dd(un[i])))} ${esc(du)}</span></div>` : "")
          + (m.spread[i] > 1 ? `<div>${esc(t("data.ov.spread_" + d.bucket, { n: m.spread[i] }))}</div>` : ""),
      });
      DC.legend($(".dh-legend", body), anyUn ? [{ kind: "bar", label: t("data.ov.logged") }, { kind: "hollow", label: t("data.ov.unlogged_long") }] : []);
    },
  });

  /* ---------------------------------------------------- drives per bucket */
  DH.block("dv-count", {
    load: async () => ({ ...(await bucketing()), dv: await loadDrives() }),
    render(d, body, ctx) {
      const idx = new Map(d.keys.map((k, i) => [k, i]));
      const n = d.keys.map(() => 0);
      for (const x of d.dv.drives || []) {
        const i = idx.get(DH.bucketKey(DH.parts(x.start).key, d.bucket));
        if (i != null) n[i]++;
      }
      DH.shell(body, "c", `<p class="dh-subn"></p><div class="dh-chart"></div>`);
      const busiest = n.reduce((a, v, i) => (v > n[a] ? i : a), 0);
      $(".dh-subn", body).textContent = n[busiest] ? t("data.drv.count_sub", { label: d.labels[busiest], n: n[busiest] }) : " ";
      DC.bars($(".dh-chart", body), {
        keys: d.keys, labels: d.labels, values: n, integer: true, morph: ctx.reload, label: t("data.drv.count"),
        empty: t("data.c.none_period"),
        tip: (i) => `<b>${esc(d.labels[i])}</b>${esc(t(n[i] === 1 ? "data.c.n_drives_one" : "data.c.n_drives", { n: n[i] }))}`,
      });
    },
  });

  /* --------------------------------------------- length and duration */
  function histBlock(id, binsFor, valueOf, fmtSub) {
    DH.block(id, {
      load: loadDrives,
      render(d, body, ctx) {
        const ds = (d.drives || []).filter((x) => valueOf(x) != null);
        const bins = binsFor();
        const counts = bins.map(() => 0);
        ds.forEach((x) => {
          const v = valueOf(x);
          const i = bins.findIndex((b) => v >= b.lo && v < b.hi);
          if (i >= 0) counts[i]++;
        });
        DH.shell(body, "c", `<p class="dh-subn"></p><div class="dh-chart"></div>`);
        const sorted = ds.map(valueOf).sort((a, b) => a - b);
        const med = sorted.length ? sorted[Math.floor((sorted.length - 1) / 2)] : null;
        $(".dh-subn", body).textContent = med != null ? fmtSub(med, ds.length) : " ";
        DC.bars($(".dh-chart", body), {
          keys: bins.map((b) => b.label), labels: bins.map((b) => b.label), values: counts, integer: true, morph: ctx.reload,
          label: $("h3", body.parentElement).textContent, empty: t("data.c.none_period"), fill: 0.7,
          tip: (i) => `<b>${esc(bins[i].long || bins[i].label)}</b>${esc(t(counts[i] === 1 ? "data.c.n_drives_one" : "data.c.n_drives", { n: counts[i] }))}`
            + (ds.length ? `<div>${esc(t("data.drv.share", { pct: DH.nf((counts[i] / ds.length) * 100, 0) }))}</div>` : ""),
        });
      },
    });
  }
  const lenEdges = () => [0, 1, 2, 5, 10, 20, 50, 100, 200, Infinity];
  histBlock("dv-len", () => {
    const e = lenEdges(), u = DH.u.dist();
    return e.slice(0, -1).map((lo, i) => {
      const hi = e[i + 1];
      const label = hi === Infinity ? `${DH.nf(lo, 0)}+` : lo === 0 ? `<${DH.nf(hi, 0)}` : `${DH.nf(lo, 0)}–${DH.nf(hi, 0)}`;
      return { lo, hi, label, long: `${label} ${u}` };
    });
  }, (x) => DH.cv.dist(x.distance_km), (med, n) => t("data.drv.len_sub", { v: `${DH.nf(med, med < 10 ? 1 : 0)} ${DH.u.dist()}`, n: DH.nf(n, 0) }));
  histBlock("dv-dur", () => {
    const e = [0, 5, 10, 20, 30, 60, 120, 240, Infinity];
    const lab = (m) => (m >= 60 ? t("data.c.h_short", { n: DH.nf(m / 60, 0) }) : t("data.c.min_short", { n: DH.nf(m, 0) }));
    return e.slice(0, -1).map((lo, i) => {
      const hi = e[i + 1];
      const label = hi === Infinity ? `${lab(lo)}+` : lo === 0 ? `<${lab(hi)}` : `${lo >= 60 ? DH.nf(lo / 60, 0) : DH.nf(lo, 0)}–${lab(hi)}`;
      return { lo, hi, label };
    });
  }, (x) => x.duration_min, (med) => t("data.drv.dur_sub", { v: DH.dur(med) }));

  /* ------------------------------------------------------ longest drives */
  DH.block("dv-top", {
    load: loadDrives,
    render(d, body) {
      const top = (d.drives || []).filter((x) => x.distance_km > 0).sort((a, b) => b.distance_km - a.distance_km).slice(0, 5);
      if (!top.length) { body.innerHTML = `<p class="dh-subn">&nbsp;</p>${DH.empty(t("data.drv.none"))}`; return; }
      const du = DH.u.dist();
      body.innerHTML = `<p class="dh-subn">${esc(t("data.drv.top_sub"))}</p><ol class="dh-ledger">${top.map((x, i) =>
        `<li data-rv style="--i:${i}"><a class="row" href="#trip-${x.id}" data-drive="${x.id}" style="display:contents">
          <span class="k">${esc(routeOf(x))}</span><span class="v">${DH.vu(DH.cv.dist(x.distance_km), du, 0)}</span>
          <span class="s">${esc(DH.date(x.start))} · ${esc(DH.dur(x.duration_min))}${x.wh_per_km ? ` · ${esc(DH.nf(DH.cv.cons(x.wh_per_km), 0))} ${esc(DH.u.cons())}` : ""}</span></a></li>`).join("")}</ol>`;
      body.querySelectorAll("a[data-drive]").forEach((a) => a.addEventListener("click", (e) => {
        e.preventDefault();
        Trips.select(+a.dataset.drive, { scroll: true });
      }));
    },
  });

  /* ------------------------------------------------- speed and its cost */
  let speedMax = null, effMax = null;
  async function speedDomain() {
    const [h, ef] = await Promise.all([
      DH.get(DH.url("/api/analytics/speed-histogram", { days: DH.days() })),
      DH.get(EFF()),
    ]);
    const hi = Math.max(0, ...(h.buckets || []).map((b) => b.speed_kmh + 5), ...(ef.by_speed || []).map((b) => b.speed_max_kmh));
    speedMax = DH.cv.speed(Math.max(hi, 60));
    const ws = [...(ef.by_speed || []), ...(ef.by_temperature || [])].map((b) => b.wh_per_km).filter((v) => v != null);
    effMax = Math.max(DH.cv.cons(ef.rated_wh_per_km || 0), ...ws.map((v) => DH.cv.cons(v)));
    return { h, ef };
  }

  DH.block("dv-speed", {
    load: speedDomain,
    render({ h }, body, ctx) {
      const bk = (h.buckets || []).filter((b) => b.seconds > 0);
      const total = bk.reduce((a, b) => a + b.seconds, 0);
      DH.shell(body, "c", `<p class="dh-subn"></p><div class="dh-chart"></div>`);
      const bins = bk.map((b) => ({
        key: b.speed_kmh, x0: DH.cv.speed(Math.max(0, b.speed_kmh - 5)), x1: DH.cv.speed(b.speed_kmh + 5),
        v: total ? (b.seconds / total) * 100 : 0, secs: b.seconds,
      }));
      const top = bins.reduce((a, b) => (!a || b.v > a.v ? b : a), null);
      const su = DH.u.speed();
      $(".dh-subn", body).textContent = top
        ? t("data.drv.speed_sub", { range: `${DH.nf(top.x0, 0)}–${DH.nf(top.x1, 0)} ${su}`, pct: DH.nf(top.v, 0) }) : " ";
      DC.hist($(".dh-chart", body), {
        bins, xMin: 0, xMax: speedMax, unit: "%", morph: ctx.reload, label: t("data.drv.speed"), empty: t("data.c.none_period"),
        xFmt: (v) => DH.nf(v, 0), yFmt: (v) => DH.nf(v, 0),
        tip: (i, b) => `<b>${esc(DH.nf(b.x0, 0))}–${esc(DH.nf(b.x1, 0))} ${esc(su)}</b><div class="r"><span>${esc(t("data.drv.time_share"))}</span><span class="v">${esc(DH.nf(b.v, 1))}%</span></div><div class="r"><span>${esc(t("data.k.time_driving"))}</span><span class="v">${esc(DH.dur(b.secs / 60))}</span></div>`,
      });
    },
  });

  function effChart(id, rowsOf, edgesOf, minKm, axisFmt, tipHead) {
    DH.block(id, {
      load: speedDomain,
      render({ ef }, body, ctx) {
        const rated = DH.cv.cons(ef.rated_wh_per_km);
        const rows = rowsOf(ef).filter((r) => r.wh_per_km != null);
        const cu = DH.u.cons();
        DH.shell(body, "c", `<p class="dh-subn"></p><div class="dh-chart"></div><div class="dh-legend"></div>`);
        const bins = rows.map((r) => {
          const [a, z] = edgesOf(r);
          return { key: a, x0: a, x1: z, v: DH.cv.cons(r.wh_per_km), hollow: (r.km || 0) < minKm, r };
        });
        const best = bins.filter((b) => !b.hollow).reduce((a, b) => (!a || b.v < a.v ? b : a), null);
        $(".dh-subn", body).textContent = best ? t("data.drv.eff_best", { range: tipHead(best), v: `${DH.nf(best.v, 0)} ${cu}` }) : " ";
        if (id === "dv-eff-speed") {
          const bits = [];
          if (ef.net && ef.net.wh_per_km) bits.push(t("data.drv.eff_net", { v: `${DH.nf(DH.cv.cons(ef.net.wh_per_km), 0)} ${cu}` }));
          if (rated) bits.push(t("data.drv.rated_v", { v: `${DH.nf(rated, 0)} ${cu}` }));
          if (ef.overhead_pct != null) bits.push(t("data.drv.eff_overhead", { pct: DH.nf(ef.overhead_pct, 1) }));
          $("#dv-eff-n").textContent = bits.join(" · ") || " ";
        }
        DC.hist($(".dh-chart", body), {
          bins, unit: cu, morph: ctx.reload, yMax: effMax, label: $("h3", body.parentElement).textContent,
          xMin: id === "dv-eff-speed" ? 0 : undefined, xMax: id === "dv-eff-speed" ? speedMax : undefined,
          refs: rated ? [{ y: rated, label: t("data.drv.rated_v", { v: DH.nf(rated, 0) }) }] : [],
          xFmt: axisFmt, empty: t("data.c.none_period"),
          tip: (i, b) => `<b>${esc(tipHead(b))}</b><div class="r"><span>${esc(t("data.k.consumption"))}</span><span class="v">${esc(DH.nf(b.v, 0))} ${esc(cu)}</span></div>`
            + (rated ? `<div class="r"><span>${esc(t("data.drv.vs_rated_s"))}</span><span class="v">${esc(DH.nf((b.v / rated) * 100, 0))}%</span></div>` : "")
            + `<div class="r"><span>${esc(t("data.k.distance"))}</span><span class="v">${esc(DH.nf(DH.cv.dist(b.r.km), 0))} ${esc(DH.u.dist())}</span></div>`
            + (b.hollow ? `<div>${esc(t("data.drv.few_data"))}</div>` : ""),
        });
        DC.legend($(".dh-legend", body), [
          { kind: "bar", label: t("data.k.consumption") },
          bins.some((b) => b.hollow) ? { kind: "hollow", label: t("data.drv.few_data") } : null,
          rated ? { kind: "ref", label: t("data.drv.rated") } : null,
        ]);
      },
    });
  }
  effChart("dv-eff-speed", (ef) => ef.by_speed || [], (r) => [DH.cv.speed(r.speed_kmh), DH.cv.speed(r.speed_max_kmh)], 5,
    (v) => DH.nf(v, 0), (b) => `${DH.nf(b.x0, 0)}–${DH.nf(b.x1, 0)} ${DH.u.speed()}`);
  effChart("dv-eff-temp", (ef) => ef.by_temperature || [], (r) => [DH.cv.temp(r.temp_c - 2.5), DH.cv.temp(r.temp_c + 2.5)], 10,
    (v) => `${DH.nf(v, 0)}°`, (b) => `${DH.nf(b.x0, 0)}–${DH.nf(b.x1, 0)}${DH.u.temp()}`);

  /* ---------------------------------------------- weekday × hour of day */
  DH.block("dv-heat", {
    load: loadDrives,
    render(d, body) {
      const v = Array.from({ length: 7 }, () => Array(24).fill(0));
      const km = Array.from({ length: 7 }, () => Array(24).fill(0));
      for (const x of d.drives || []) {
        const p = DH.parts(x.start);
        v[p.wd][p.h]++;
        km[p.wd][p.h] += x.distance_km || 0;
      }
      DH.shell(body, "c", `<div class="dh-chart"></div>`);
      const wd = [0, 1, 2, 3, 4, 5, 6].map((i) => DH.fdate(`2026-01-${String(5 + i).padStart(2, "0")}`, { weekday: "short" }));
      DC.heat($(".dh-chart", body), {
        values: v, rowLabels: wd, colTicks: [0, 6, 12, 18],
        colFmt: (h) => new Intl.DateTimeFormat(localeTag(), { hour: "numeric", timeZone: "UTC" }).format(new Date(Date.UTC(2026, 0, 5, h))),
        label: t("data.drv.when"),
        tip: (r, c) => `<b>${esc(wd[r])} ${esc(new Intl.DateTimeFormat(localeTag(), { hour: "numeric", minute: "2-digit", timeZone: "UTC" }).format(new Date(Date.UTC(2026, 0, 5, c))))}</b>`
          + `<div class="r"><span>${esc(t("data.k.drives"))}</span><span class="v">${esc(DH.nf(v[r][c], 0))}</span></div>`
          + (v[r][c] ? `<div class="r"><span>${esc(t("data.k.distance"))}</span><span class="v">${esc(DH.nf(DH.cv.dist(km[r][c]), 0))} ${esc(DH.u.dist())}</span></div>` : ""),
      });
    },
  });

  /* ---------------------------------------------------- top destinations */
  DH.block("dv-dest", {
    load: loadDrives,
    render(d, body) {
      const top = ((d.summary || {}).top_destinations || []).slice(0, 6);
      body.innerHTML = `<p class="dh-subn">${esc(t("data.drv.dest_sub"))}</p><div></div>`;
      DC.rank(body.lastElementChild, top.map((x) => ({
        label: x.label || t("common.unknown"), value: x.arrivals,
        display: `<span class="n">${esc(DH.nf(x.arrivals, 0))}</span><span class="u">${esc(t("data.drv.arrivals"))}</span>`,
      })), { empty: t("data.c.none_period") });
    },
  });

  /* ================================================================ trips */

  const TRIP_CATS = ["business", "personal", "commute", "other"];
  const EMPTY = { type: "FeatureCollection", features: [] };
  /* A phone shows a short page of trips; a desk shows a scrolling column. */
  const PAGE = () => (matchMedia("(max-width: 900px)").matches ? 15 : 50);
  const Trips = {
    map: null, mapReady: null, list: [], shown: PAGE(), cur: null, gen: 0, tags: {}, hover: null, hoverOff: null, deep: null,
  };
  Trips.deep = +(new URLSearchParams(location.search).get("drive") || 0) || null;
  if (!Trips.deep && /^#trip-\d+$/.test(location.hash)) Trips.deep = +location.hash.slice(6);

  function initMap() {
    if (Trips.mapReady) return Trips.mapReady;
    Trips.mapReady = (async () => {
      const map = await OmtMap.create($("#tr-gl"), { center: [39, -98], zoom: 3 });
      map.dragRotate.disable();
      map.touchZoomRotate.disableRotation();
      await OmtMap.ready(map);
      const ink = DH.color("var(--dh-ink-2)");
      const data = DH.color("var(--dh-data)");
      const ground = DH.color("var(--dh-ground)");
      map.addSource("ghosts", { type: "geojson", data: EMPTY });
      map.addLayer({ id: "ghosts", type: "line", source: "ghosts", layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": ink, "line-width": 1.5, "line-opacity": 0.35 } });
      map.addSource("trip", { type: "geojson", data: EMPTY });
      map.addLayer({ id: "trip", type: "line", source: "trip", layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": data, "line-width": 3 } });
      map.addSource("ends", { type: "geojson", data: EMPTY });
      map.addLayer({ id: "ends", type: "circle", source: "ends",
        paint: { "circle-radius": 4, "circle-color": ground, "circle-stroke-color": data, "circle-stroke-width": 2 } });
      map.on("click", "ghosts", (e) => { const f = e.features && e.features[0]; if (f) Trips.select(f.properties.id, { scroll: false }); });
      map.on("mouseenter", "ghosts", () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", "ghosts", () => { map.getCanvas().style.cursor = ""; });
      Trips.map = map;
      return map;
    })().catch((e) => {
      console.warn("map unavailable", e);
      Trips.map = null;
      $("#tr-gl").hidden = true;
      $("#tr-idle").hidden = false;
      $("#tr-idle").textContent = t("data.c.map_unavailable");
      return null;
    });
    return Trips.mapReady;
  }

  async function drawGhosts() {
    let tracks = [];
    try { tracks = await DH.get(DH.url("/api/drives/tracks", { limit: 200 })); } catch (e) { return; }
    const map = await initMap();
    if (!map) return;
    const ids = new Set(Trips.list.map((d) => d.id));
    const features = tracks.filter((x) => ids.has(x.id) && x.points && x.points.length > 1).map((x) => ({
      type: "Feature", properties: { id: x.id },
      geometry: { type: "LineString", coordinates: OmtMap.line(map, x.points) },
    }));
    map.getSource("ghosts").setData({ type: "FeatureCollection", features });
    if (!Trips.cur && features.length) OmtMap.fit(map, features.flatMap((f) => f.geometry.coordinates), 30);
    paintGhosts();
  }
  function paintGhosts() {
    const map = Trips.map;
    if (!map || !map.getLayer("ghosts")) return;
    map.setPaintProperty("ghosts", "line-opacity", ["case", ["==", ["get", "id"], Trips.cur ?? -1], 0, 0.35]);
  }

  function listRow(x) {
    const tag = (Trips.tags[String(x.id)] || {}).category;
    return `<tr class="tap" data-id="${x.id}" aria-selected="${x.id === Trips.cur}">
      <td class="nowrap dim">${esc(DH.dateShort(x.start))}<span class="s">${esc(DH.time(x.start))}</span></td>
      <td>${esc(routeOf(x))}${tag ? `<span class="s">${esc(t("trips.cat_" + tag))}</span>` : ""}</td>
      <td class="num">${DH.n(DH.cv.dist(x.distance_km), 1)}</td>
      <td class="num dh-hide-sm dim">${esc(DH.dur(x.duration_min))}</td></tr>`;
  }
  function paintList() {
    const box = $("#tr-list");
    const n = Trips.list.length;
    if (!n) {
      box.innerHTML = DH.empty(t("data.drv.none"), `<a href="?days=all">${esc(t("data.c.try_all"))}</a>`);
      $("#tr-more").innerHTML = "";
      return;
    }
    const rows = Trips.list.slice(0, Trips.shown);
    box.innerHTML = `<table class="dh-table"><thead><tr><th>${esc(t("data.k.date"))}</th><th>${esc(t("data.k.route"))}</th>
      <th class="num">${esc(DH.u.dist())}</th><th class="num dh-hide-sm">${esc(t("data.k.duration"))}</th></tr></thead>
      <tbody>${rows.map(listRow).join("")}</tbody></table>`;
    box.querySelectorAll("tbody tr").forEach((tr) => tr.addEventListener("click", () => Trips.select(+tr.dataset.id, { scroll: false })));
    const more = $("#tr-more");
    more.innerHTML = `<span>${esc(t("data.drv.showing", { a: 1, b: DH.nf(rows.length, 0), n: DH.nf(n, 0) }))}</span>`
      + (Trips.total > n ? `<span>${esc(t("data.c.capped", { n: DH.nf(n, 0), total: DH.nf(Trips.total, 0) }))}</span>` : "")
      + (n > rows.length ? `<button type="button" class="dh-btn quiet">${esc(t("data.drv.show_more_n", { n: Math.min(PAGE(), n - rows.length) }))}</button>` : "");
    const b = more.querySelector("button");
    if (b) b.addEventListener("click", () => { Trips.shown += PAGE(); paintList(); });
  }

  /* One block owns the list + map; the period change refills both. */
  DH.block("tr-list-b", {
    load: async () => {
      const [d, tags] = await Promise.all([loadDrives(), DH.get("/api/trips/tags").catch(() => ({}))]);
      return { d, tags };
    },
    render({ d, tags }) {
      Trips.tags = tags || {};
      Trips.list = d.drives || [];
      Trips.total = d.total || Trips.list.length;
      Trips.shown = Math.max(PAGE(), Trips.list.findIndex((x) => x.id === Trips.cur) + 1);
      $("#tr-note").textContent = Trips.list.length
        ? t("data.drv.trips_n", { n: DH.nf(Trips.list.length, 0), dist: `${DH.nf(DH.cv.dist(Trips.list.reduce((a, x) => a + (x.distance_km || 0), 0)), 0)} ${DH.u.dist()}` })
        : " ";
      $("#tr-csv").href = DH.url("/api/trips/export.csv", { days: DH.days() });
      paintList();
      // a car with no trips in the period gets no world map, just the line
      // that says so (the list column carries it)
      $("#tr-map").hidden = !Trips.list.length && !Trips.deep;
      document.querySelector(".tr-wrap").classList.toggle("empty", !Trips.list.length && !Trips.deep);
      $("#tr-csv").hidden = !Trips.list.length;
      if (!Trips.list.length && !Trips.deep) { $("#tr-detail").hidden = true; loadCats(); return; }
      initMap();
      drawGhosts();
      const want = Trips.deep;
      if (want) {
        Trips.deep = null;
        Trips.select(want, { scroll: true, deep: true });
      } else if (Trips.cur && Trips.list.some((x) => x.id === Trips.cur)) {
        Trips.select(Trips.cur, { quiet: true });
      } else if (Trips.list.length) {
        Trips.select(Trips.list[0].id, { quiet: true });
      } else {
        $("#tr-detail").hidden = true;
        if (Trips.map) { Trips.map.getSource("trip").setData(EMPTY); Trips.map.getSource("ends").setData(EMPTY); }
        $("#tr-idle").hidden = false;
      }
      loadCats();
    },
  });

  Trips.select = async (id, opt) => {
    const o = opt || {};
    Trips.cur = id;
    const idx = Trips.list.findIndex((x) => x.id === id);
    if (idx >= Trips.shown) { Trips.shown = idx + 1; paintList(); }
    document.querySelectorAll("#tr-list tbody tr").forEach((tr) => tr.setAttribute("aria-selected", String(+tr.dataset.id === id)));
    const row = document.querySelector(`#tr-list tbody tr[data-id="${id}"]`);
    if (row && !o.quiet) row.scrollIntoView({ block: "nearest" });
    if (o.scroll) document.getElementById("dv-trips").scrollIntoView({ block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    paintGhosts();
    const gen = ++Trips.gen;
    const base = Trips.list.find((x) => x.id === id);
    const box = $("#tr-detail");
    box.hidden = false;
    if (base) paintDetailHead(base, null, !o.deep || idx >= 0 ? null : "out");
    let det;
    try { det = await DH.get(`/api/analytics/drives/${id}?max_points=1500`); }
    catch (e) {
      if (gen !== Trips.gen) return;
      box.innerHTML = `<div class="dh-err" role="alert"><span class="ic">${icon("warn", 18)}</span><span class="msg"><b>${esc(t("data.drv.trip_err"))}</b>${esc(DH.errText(e))}</span></div>`;
      return;
    }
    if (gen !== Trips.gen) return;
    paintDetail(det, base, idx < 0);
    drawTrip(det, gen);
  };

  function paintDetailHead(x, det, note) {
    $("#tr-detail").innerHTML = `<h3>${esc(routeOf(x))}</h3><div class="when">${esc(DH.date(x.start))} · ${esc(DH.time(x.start))}${x.end ? `–${esc(DH.time(x.end))}` : ""}</div>
      ${note === "out" ? `<p class="dh-note" style="margin-top:8px">${esc(t("data.drv.outside_period"))}</p>` : ""}`;
  }

  function paintDetail(det, base, outside) {
    const d = det.drive || {}, en = det.energy || {};
    const x = base || { id: d.id, start: d.start, end: d.end, from: d.from && d.from.label, to: d.to && d.to.label };
    const du = DH.u.dist(), cu = DH.u.cons();
    const km = DH.cv.dist(d.distance_km);
    const figs = [
      DH.fig(t("data.k.distance"), DH.vu(km, du, DH.dd(km))),
      DH.fig(t("data.k.duration"), `<span class="n">${esc(DH.dur(d.duration_min))}</span>`),
      DH.fig(t("data.k.avg_speed"), DH.vu(DH.cv.speed(d.speed_avg_moving_kmh || d.speed_avg_kmh), DH.u.speed(), 0),
        d.speed_max_kmh ? esc(t("data.drv.top_speed", { v: `${DH.nf(DH.cv.speed(d.speed_max_kmh), 0)} ${DH.u.speed()}` })) : ""),
      DH.fig(t("data.k.consumption"), DH.vu(DH.cv.cons(en.wh_per_km), cu, 0),
        en.efficiency_vs_rated_pct != null ? esc(t("data.drv.of_rated", { pct: DH.nf(en.efficiency_vs_rated_pct, 0) })) : ""),
      DH.fig(t("data.k.energy_used"), DH.vu(en.net_kwh, "kWh", 1),
        en.regen_kwh != null ? esc(t("data.drv.regen", { kwh: DH.nf(en.regen_kwh, 1), pct: DH.nf(en.regen_share_pct, 0) })) : ""),
      DH.fig(t("data.k.battery"), d.start_soc != null ? `<span class="n">${esc(DH.nf(d.start_soc, 0))}→${esc(DH.nf(d.end_soc, 0))}</span><span class="u">%</span>` : '<span class="dash">–</span>',
        d.outside_temp_avg_c != null ? esc(t("data.drv.outside", { v: `${DH.nf(DH.cv.temp(d.outside_temp_avg_c), 0)}${DH.u.temp()}` })) : ""),
    ];
    const cur = (Trips.tags[String(d.id)] || {}).category || "";
    $("#tr-detail").innerHTML = `<h3>${esc(routeOf(x))}</h3>
      <div class="when">${esc(DH.date(d.start))} · ${esc(DH.time(d.start))}${d.end ? `–${esc(DH.time(d.end))}` : ""}${d.open ? ` · ${esc(t("data.drv.open"))}` : ""}</div>
      ${outside ? `<p class="dh-note" style="margin-top:8px">${esc(t("data.drv.outside_period"))}</p>` : ""}
      <div class="dh-figs" style="margin-top:16px">${figs.join("")}</div>
      <div class="dh-grid" style="margin-top:20px">
        <div class="dh-sub c6"><p class="dh-subn">${esc(t("data.drv.profile_speed"))}</p><div class="dh-chart mini" id="tr-spd"></div></div>
        <div class="dh-sub c6"><p class="dh-subn">${esc(t("data.drv.profile_elev"))}</p><div class="dh-chart mini" id="tr-elev"></div></div>
      </div>
      <div class="tr-tags">
        <span>${esc(t("data.drv.category"))}</span>
        <div class="dh-seg" role="group" aria-label="${esc(t("data.drv.category"))}">${TRIP_CATS.map((c) =>
          `<button type="button" data-cat="${c}" aria-pressed="${c === cur}">${esc(t("trips.cat_" + c))}</button>`).join("")}</div>
        <span role="status" id="tr-tag-st"></span>
        <a class="dh-btn" href="/api/trips/${d.id}/export.gpx" download><span class="ico-slot" data-icon="download" data-size="15"></span>${esc(t("data.drv.gpx"))}</a>
      </div>
      <div id="tr-clips"></div>`;
    paintIcons($("#tr-detail"));
    const s = det.series || {};
    const ts = (s.t || []).map((v) => new Date(v).getTime());
    const t0 = ts[0] || 0;
    const mins = ts.map((v) => (v - t0) / 60000);
    DC.line($("#tr-spd"), {
      points: mins.map((m, i) => [m, DH.cv.speed(s.speed_kmh[i])]), zero: true, unit: DH.u.speed(), thin: true,
      xFmt: (v) => t("data.c.min_short", { n: DH.nf(v, 0) }), label: t("data.drv.profile_speed"),
      tip: (p, i) => `<b>${esc(DH.time(s.t[i]))}</b>${esc(DH.nf(p[1], 0))} ${esc(DH.u.speed())}`,
    });
    const elev = (s.elevation_m || []).map((v) => (v == null ? null : v));
    DC.line($("#tr-elev"), {
      points: mins.map((m, i) => [m, elev[i]]), unit: "m", thin: true, area: false,
      xFmt: (v) => t("data.c.min_short", { n: DH.nf(v, 0) }), label: t("data.drv.profile_elev"),
      tip: (p, i) => `<b>${esc(DH.time(s.t[i]))}</b>${esc(DH.nf(p[1], 0))} m`,
    });
    $("#tr-detail").querySelectorAll("[data-cat]").forEach((b) => b.addEventListener("click", () => saveTag(d.id, b.dataset.cat)));
    loadClips(d.id);
  }

  async function saveTag(id, cat) {
    const cur = (Trips.tags[String(id)] || {}).category || "";
    const next = cat === cur ? "" : cat;            // pressing the chosen one clears it
    const st = $("#tr-tag-st");
    st.textContent = t("data.drv.saving");
    try {
      await apiPost(`/api/trips/tags/${id}`, { category: next });
      if (next) Trips.tags[String(id)] = { category: next }; else delete Trips.tags[String(id)];
      document.querySelectorAll("#tr-detail [data-cat]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.cat === next)));
      st.textContent = next ? t("data.drv.saved") : t("data.drv.cleared");
      const row = document.querySelector(`#tr-list tbody tr[data-id="${id}"]`);
      const x = Trips.list.find((y) => y.id === id);
      if (row && x) { row.outerHTML = listRow(x); document.querySelector(`#tr-list tbody tr[data-id="${id}"]`).addEventListener("click", () => Trips.select(id, { scroll: false })); }
      loadCats(true);
    } catch (e) {
      st.textContent = t("data.drv.save_failed", { msg: e.message });
    }
  }

  async function loadClips(id) {
    const box = $("#tr-clips");
    let f;
    try { f = await DH.get(`/api/drives/${id}/footage`); } catch (e) { return; }
    if (Trips.cur !== id || !box) return;
    const list = f.footage || [];
    if (!list.length) { box.innerHTML = ""; return; }
    box.innerHTML = `<p class="dh-subn" style="margin-top:20px">${esc(t("data.drv.clips", { n: list.length }))}</p>
      <div class="tr-clips">${list.map((c) => {
        const q = c.at ? `at=${encodeURIComponent(c.at)}` : `t=${c.seek}`;
        return `<a class="tr-clip" href="/dashcam/play?id=${encodeURIComponent(c.event_id)}&${q}">
          <img loading="lazy" width="320" height="180" alt="" src="/api/dashcam/thumb?id=${encodeURIComponent(c.event_id)}">
          <div class="c">${esc(DH.time(c.event_start))} · ${esc(t("drives.footage_seek", { time: fmt.ts(c.seek) }))}</div></a>`;
      }).join("")}</div>`;
    box.querySelectorAll("img").forEach((img) => img.addEventListener("error", () => {
      const ph = document.createElement("div"); ph.className = "ph"; ph.innerHTML = icon("film", 22); img.replaceWith(ph);
    }, { once: true }));
  }

  async function drawTrip(det, gen) {
    const map = await initMap();
    if (!map || gen !== Trips.gen) return;
    const s = det.series || {};
    const pts = (s.lat || []).map((la, i) => [la, s.lon[i]]).filter((p) => p[0] != null && p[1] != null);
    const route = pts.length > 1 ? pts : ((det.route && det.route.coordinates) || []).map(([lo, la]) => [la, lo]);
    if (Trips.hover) { Trips.hover.remove(); Trips.hover = null; }
    if (route.length < 2) {
      map.getSource("trip").setData(EMPTY); map.getSource("ends").setData(EMPTY);
      $("#tr-idle").hidden = false; $("#tr-idle").textContent = t("drives.track_none");
      return;
    }
    $("#tr-idle").hidden = true;
    const coords = OmtMap.line(map, route);
    map.getSource("trip").setData({ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: coords } });
    map.getSource("ends").setData({ type: "FeatureCollection", features: [coords[0], coords[coords.length - 1]].map((c) => ({
      type: "Feature", properties: {}, geometry: { type: "Point", coordinates: c } })) });
    OmtMap.fit(map, coords, 48);
    trackHover(map, coords, pts.length > 1 ? s : null);
  }

  /* Hovering the route shows where you were then: time, speed, battery. */
  function trackHover(map, coords, s) {
    if (Trips.hoverOff) Trips.hoverOff();
    if (!s) return;
    const move = (e) => {
      let best = 0, bd = Infinity;
      for (let i = 0; i < coords.length; i += 1) {
        const p = map.project(coords[i]);
        const dd = (p.x - e.point.x) ** 2 + (p.y - e.point.y) ** 2;
        if (dd < bd) { bd = dd; best = i; }
      }
      const bits = [DH.time(s.t[best]), s.speed_kmh[best] != null ? `${DH.nf(DH.cv.speed(s.speed_kmh[best]), 0)} ${DH.u.speed()}` : "",
                    s.soc[best] != null ? `${DH.nf(s.soc[best], 0)}%` : ""].filter(Boolean).join(" · ");
      if (!Trips.hover) {
        const el = document.createElement("div");
        el.className = "tr-dot";
        el.innerHTML = "<i></i><span></span>";
        Trips.hover = new maplibregl.Marker({ element: el, anchor: "left" }).setLngLat(coords[best]).addTo(map);
      }
      Trips.hover.setLngLat(coords[best]);
      Trips.hover.getElement().querySelector("span").textContent = bits;
    };
    const leave = () => { if (Trips.hover) { Trips.hover.remove(); Trips.hover = null; } };
    map.on("mousemove", "trip", move);
    map.on("mouseleave", "trip", leave);
    Trips.hoverOff = () => { map.off("mousemove", "trip", move); map.off("mouseleave", "trip", leave); };
  }

  /* Mileage by category for the period (expense and tax reports). */
  async function loadCats(fresh) {
    const box = $("#tr-cats-b");
    let s;
    try { s = await DH.get(DH.url("/api/trips/summary", { days: DH.days() }), fresh); }
    catch (e) { box.innerHTML = `<div class="dh-err"><span class="msg">${esc(DH.errText(e))}</span></div>`; return; }
    $("#tr-cats-v").textContent = s.untagged ? t("trips.n_untagged", { n: s.untagged }) : s.total_drives ? t("trips.all_tagged") : "";
    const du = DH.u.dist();
    box.innerHTML = s.categories && s.categories.length ? `<table class="dh-table"><thead><tr><th>${esc(t("data.drv.category"))}</th>
      <th class="num">${esc(t("data.k.drives"))}</th><th class="num">${esc(du)}</th><th class="num">kWh</th></tr></thead><tbody>
      ${s.categories.map((c) => `<tr><td>${esc(c.category === "untagged" ? t("trips.untagged") : t("trips.cat_" + c.category))}</td>
        <td class="num">${DH.n(c.drives, 0)}</td><td class="num">${DH.n(DH.cv.dist(c.km), 0)}</td><td class="num">${DH.n(c.kwh, 1)}</td></tr>`).join("")}
      </tbody></table><p class="dh-note" style="margin-top:8px">${esc(t("data.drv.cats_note"))}</p>` : DH.empty(t("data.drv.none"));
  }

  window.addEventListener("hashchange", () => {
    if (/^#trip-\d+$/.test(location.hash)) Trips.select(+location.hash.slice(6), { scroll: true });
  });
})();
