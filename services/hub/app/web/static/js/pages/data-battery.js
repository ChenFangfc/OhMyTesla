/* 数据 · 电池: is the battery all right, and how is it used? */
(function () {
  "use strict";
  const $ = (s, r) => (r || document).querySelector(s);
  const loadBattery = (fresh) => DH.get(DH.url("/api/analytics/battery"), fresh);
  const loadUpdates = () => DH.get(DH.url("/api/analytics/updates")).catch(() => ({ updates: [] }));
  const rangeBucket = () => (DH.period === "all" ? "month" : DH.period === "365" ? "week" : "day");
  const loadProjected = () => DH.get(DH.url("/api/analytics/projected-range", { days: DH.days(), bucket: rangeBucket() }));
  const shortVer = (v) => String(v || "").split(".").slice(0, 2).join(".");

  /* Firmware installs belong on charts whose x axis is the odometer, so map
     each install date onto the odometer by interpolating the readings we
     have around it. Installs outside the readings are left off, not guessed. */
  function installMarks(updates, readings) {
    const pts = readings.filter((r) => r.odometer_km != null && r.date).map((r) => [new Date(r.date.length === 10 ? r.date + "T12:00:00" : r.date).getTime(), r.odometer_km])
      .sort((a, b) => a[0] - b[0]);
    if (pts.length < 2) return [];
    const out = [];
    for (const u of updates || []) {
      const tt = new Date(u.start).getTime();
      if (tt < pts[0][0] || tt > pts[pts.length - 1][0]) continue;
      let i = pts.findIndex((p) => p[0] >= tt);
      if (i <= 0) i = 1;
      const [t0, o0] = pts[i - 1], [t1, o1] = pts[i];
      const odo = t1 === t0 ? o1 : o0 + ((tt - t0) / (t1 - t0)) * (o1 - o0);
      out.push({ x: DH.cv.dist(odo), label: shortVer(u.version) });
    }
    return out;
  }

  /* Days spent parked share one odometer reading; on an odometer axis they
     would stack into a vertical line. Average readings within 1 km. */
  function mergeByOdo(rows) {
    const out = [];
    rows.slice().sort((a, b) => a.odometer_km - b.odometer_km).forEach((r) => {
      const last = out[out.length - 1];
      if (last && r.odometer_km - last.odometer_km < 1) {
        const n = last._n + 1;
        last.range_100_km = (last.range_100_km * last._n + r.range_100_km) / n;
        last._n = n;
        last.date = r.date;
      } else out.push({ ...r, _n: 1 });
    });
    return out;
  }

  /* ---------------------------------------------------------- health */
  DH.block("bt-health", {
    load: () => loadBattery(),
    render(b, body) {
      const cap = b.capacity_kwh || {}, rng = b.range_100_km || {};
      if (b.health_pct == null && cap.now == null) {
        body.innerHTML = DH.empty(t("data.bat.none"));
        return;
      }
      const seen = DH.carInfo && DH.carInfo.last_seen;
      const items = [
        [t("analytics.battery.health"), DH.vu(b.health_pct, "%", 1),
          b.degradation_pct != null ? esc(t(b.degradation_pct >= 0 ? "data.bat.degraded" : "data.bat.gained", { pct: DH.nf(Math.abs(b.degradation_pct), 1) })) : ""],
        [t("analytics.battery.capacity_now"), DH.vu(cap.now, "kWh", 1),
          cap.new != null ? esc(t(cap.new_source === "user" ? "data.bat.new_user" : "data.bat.new_observed", { v: `${DH.nf(cap.new, 1)} kWh` })) : ""],
        [t("analytics.battery.range_100"), DH.vu(DH.cv.dist(rng.now), DH.u.dist(), 0),
          rng.max != null ? esc(t(rng.max_source === "user" ? "data.bat.new_user" : "data.bat.best", { v: `${DH.nf(DH.cv.dist(rng.max), 0)} ${DH.u.dist()}` })) : ""],
        [t("analytics.battery.cycles"), DH.vu(b.cycles, "", 0),
          b.kwh_added_total ? esc(t("data.bat.lifetime_kwh", { kwh: DH.nf(b.kwh_added_total, 0) })) : ""],
        [t("data.bat.soc_now"), DH.vu(b.soc_now, "%", 0),
          DH.dots([b.stored_kwh_now != null ? `${DH.nf(b.stored_kwh_now, 1)} kWh` : "", seen ? t("data.bat.as_of", { when: DH.when(seen) }) : ""])],
      ];
      body.innerHTML = `<div class="dh-kpis" style="--cols:5">${items.map(([l, v, s], i) => DH.kpi(i, l, v, s)).join("")}</div>
        <p class="dh-note" style="margin-top:16px">${esc(t("data.bat.method"))}</p>`;
      paintBaseline(b);
    },
  });

  /* "New" defaults to the best we've seen, which flatters a car that was
     driven for years before logging began. Let the owner state it. */
  function paintBaseline(b) {
    const rng = b.range_100_km || {};
    const box = $("#bt-base-b");
    const user = rng.max_source === "user";
    $("#bt-base-v").textContent = user ? t("data.bat.base_user", { v: `${DH.nf(DH.cv.dist(rng.max), 0)} ${DH.u.dist()}` }) : t("data.bat.base_observed");
    box.innerHTML = `<p class="dh-note" style="max-width:72ch">${esc(t("data.bat.base_note"))}</p>
      <div class="row" style="display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;margin:12px 0 16px">
        <label class="dh-input"><span>${esc(t("data.bat.base_label"))}</span>
          <input type="number" min="0" step="1" inputmode="numeric" id="bt-base-in" value="${user && rng.max != null ? Math.round(DH.cv.dist(rng.max)) : ""}" placeholder="${esc(DH.nf(DH.cv.dist(rng.max), 0) || "")}">
          <span>${esc(DH.u.dist())}</span></label>
        <button type="button" class="dh-btn" id="bt-base-save">${esc(t("common.save"))}</button>
        ${user ? `<button type="button" class="dh-btn quiet" id="bt-base-clear">${esc(t("data.bat.base_clear"))}</button>` : ""}
        <span role="status" class="dh-note" id="bt-base-st"></span>
      </div>`;
    const save = async (km) => {
      const st = $("#bt-base-st");
      st.textContent = t("data.drv.saving");
      try {
        await apiPost(`/api/insights/battery/baseline?car_id=${DH.car}`, { range_km: km });
        st.textContent = t("data.drv.saved");
        DH.reloadAll(false);
      } catch (e) { st.textContent = t("data.drv.save_failed", { msg: e.message }); }
    };
    $("#bt-base-save").addEventListener("click", () => {
      const raw = parseFloat($("#bt-base-in").value);
      if (!isFinite(raw) || raw <= 0) { $("#bt-base-st").textContent = t("data.bat.base_invalid"); return; }
      save(DH.u.dist() === "mi" ? raw * 1.609344 : raw);
    });
    const clr = $("#bt-base-clear");
    if (clr) clr.addEventListener("click", () => save(null));
  }

  /* ---------------------------------------------- capacity vs odometer */
  DH.block("bt-cap", {
    load: () => Promise.all([loadBattery(), loadUpdates(), loadProjected().catch(() => ({ points: [] }))]),
    render([b, up, pr], body, ctx) {
      const pts = (b.capacity_by_mileage || []).filter((p) => p.kwh != null && p.odometer_km != null);
      const cap = b.capacity_kwh || {};
      DH.shell(body, "c", `<p class="dh-subn"></p><div class="dh-chart"></div><div class="dh-legend"></div>`);
      $(".dh-subn", body).textContent = pts.length
        ? t("data.bat.cap_sub", { n: DH.nf(pts.length, 0) }) : " ";
      const marks = installMarks(up.updates, [...(pr.points || []), ...pts, ...(b.range_trend || [])]);
      DC.line($(".dh-chart", body), {
        points: pts.map((p) => [DH.cv.dist(p.odometer_km), p.kwh]), unit: "kWh", morph: ctx.reload,
        refs: cap.new != null ? [{ y: cap.new, label: t(cap.new_source === "user" ? "data.bat.ref_new" : "data.bat.ref_best", { v: DH.nf(cap.new, 1) }) }] : [],
        marks, xFmt: (v) => DH.nf(v, 0), label: t("data.bat.cap"), empty: t("data.bat.cap_none"),
        tip: (p, i) => `<b>${esc(DH.date(pts[i].date))}</b><div class="r"><span>${esc(t("data.bat.capacity"))}</span><span class="v">${esc(DH.nf(p[1], 1))} kWh</span></div>`
          + `<div class="r"><span>${esc(t("common.odometer"))}</span><span class="v">${esc(DH.nf(p[0], 0))} ${esc(DH.u.dist())}</span></div>`,
      });
      DC.legend($(".dh-legend", body), [{ kind: "line", label: t("data.bat.capacity") },
        cap.new != null ? { kind: "ref", label: t(cap.new_source === "user" ? "data.bat.when_new" : "data.bat.best_seen") } : null,
        marks.length ? { kind: "mark", label: t("data.bat.installs") } : null]);
    },
  });

  /* ------------------------------------------- range at 100% vs odometer */
  DH.block("bt-range", {
    load: () => Promise.all([loadProjected(), loadBattery(), loadUpdates()]),
    render([pr, b, up], body, ctx) {
      const pts = mergeByOdo((pr.points || []).filter((p) => p.range_100_km != null && p.odometer_km != null));
      const rng = b.range_100_km || {};
      DH.shell(body, "c", `<p class="dh-subn"></p><div class="dh-chart"></div><div class="dh-legend"></div>`);
      const du = DH.u.dist();
      $(".dh-subn", body).textContent = rng.now != null
        ? t("data.bat.range_sub", { v: `${DH.nf(DH.cv.dist(rng.now), 0)} ${du}` }) : " ";
      const marks = installMarks(up.updates, [...pts, ...(b.range_trend || [])]);
      DC.line($(".dh-chart", body), {
        points: pts.map((p) => [DH.cv.dist(p.odometer_km), DH.cv.dist(p.range_100_km)]), unit: du, morph: ctx.reload,
        refs: rng.max != null ? [{ y: DH.cv.dist(rng.max), label: t(rng.max_source === "user" ? "data.bat.ref_new" : "data.bat.ref_best", { v: DH.nf(DH.cv.dist(rng.max), 0) }) }] : [],
        marks, xFmt: (v) => DH.nf(v, 0), label: t("data.bat.range"), empty: t("data.c.none_period"),
        tip: (p, i) => `<b>${esc(DH.date(pts[i].date))}</b><div class="r"><span>${esc(t("analytics.battery.range_100"))}</span><span class="v">${esc(DH.nf(p[1], 0))} ${esc(du)}</span></div>`
          + `<div class="r"><span>${esc(t("common.odometer"))}</span><span class="v">${esc(DH.nf(p[0], 0))} ${esc(du)}</span></div>`
          + (pts[i].outside_temp_c != null ? `<div class="r"><span>${esc(t("data.bat.temp"))}</span><span class="v">${esc(DH.nf(DH.cv.temp(pts[i].outside_temp_c), 0))}${esc(DH.u.temp())}</span></div>` : ""),
      });
      DC.legend($(".dh-legend", body), [{ kind: "line", label: t("analytics.battery.range_100") },
        rng.max != null ? { kind: "ref", label: t(rng.max_source === "user" ? "data.bat.when_new" : "data.bat.best_seen") } : null,
        marks.length ? { kind: "mark", label: t("data.bat.installs") } : null]);
    },
  });

  /* ------------------------------------------------- charge level */
  const loadLevel = () => DH.get(DH.url("/api/analytics/charge-level", { days: DH.days(), max_points: 600 }));
  DH.block("bt-soc", {
    load: loadLevel,
    render(cl, body, ctx) {
      const pts = (cl.points || []).filter((p) => p.soc != null).map((p) => [new Date(p.t).getTime(), p.soc]);
      const lim = cl.limits || {};
      const st = cl.stats || {};
      DH.shell(body, "c", `<p class="dh-subn"></p><div class="dh-chart"></div><div class="dh-legend"></div>`);
      $(".dh-subn", body).textContent = st.median != null
        ? t("data.bat.level_sub", { med: DH.nf(st.median, 0), lo: DH.nf(st.p7_5, 0), hi: DH.nf(st.p92_5, 0) }) : " ";
      $("#bt-level-n").textContent = st.above_80_pct != null && lim.upper < 100
        ? t("data.bat.level_n", { hi: DH.nf(st.above_80_pct, 0), lo: DH.nf(st.below_20_pct, 0) }) : " ";
      const guides = [{ y: lim.lower ?? 20, label: `${lim.lower ?? 20}%` }];
      if ((lim.upper ?? 80) < 100) guides.push({ y: lim.upper ?? 80, label: `${lim.upper ?? 80}%` });
      // break the line where readings stop for more than a few buckets
      const gap = Math.max((cl.bucket_s || 3600) * 3, 36 * 3600) * 1000;
      const withGaps = [];
      pts.forEach((p, i) => { if (i && p[0] - pts[i - 1][0] > gap) withGaps.push([pts[i - 1][0] + 1, null]); withGaps.push(p); });
      DC.line($(".dh-chart", body), {
        points: withGaps, xTime: true, yMin: 0, yMax: 100, clampMin: 0, clampMax: 100, unit: "%", guides, morph: ctx.reload,
        label: t("data.bat.soc_time"), empty: t("data.c.none_period"),
        tip: (p) => `<b>${esc(DH.dt(p[0]))}</b>${esc(DH.nf(p[1], 0))}%`,
      });
      DC.legend($(".dh-legend", body), [{ kind: "line", label: t("data.bat.soc") }, { kind: "guide", label: t("data.bat.guides") }]);
    },
  });

  DH.block("bt-socd", {
    load: loadLevel,
    render(cl, body, ctx) {
      const vals = (cl.points || []).map((p) => p.soc).filter((v) => v != null);
      const bins = Array.from({ length: 10 }, (_, i) => ({ key: i, x0: i * 10, x1: i * 10 + 10, v: 0 }));
      vals.forEach((v) => { bins[Math.min(9, Math.floor(v / 10))].v++; });
      const n = vals.length || 1;
      bins.forEach((b) => { b.v = (b.v / n) * 100; });
      DH.shell(body, "c", `<p class="dh-subn"></p><div class="dh-chart"></div>`);
      const hours = (vals.length * (cl.bucket_s || 3600)) / 3600;
      $(".dh-subn", body).textContent = vals.length ? t("data.bat.dist_sub", { h: DH.nf(hours, 0) }) : " ";
      DC.hist($(".dh-chart", body), {
        bins, xMin: 0, xMax: 100, unit: "%", morph: ctx.reload, label: t("data.bat.soc_dist"),
        xFmt: (v) => `${DH.nf(v, 0)}%`, yFmt: (v) => DH.nf(v, 0), empty: t("data.c.none_period"),
        tip: (i, b) => `<b>${esc(DH.nf(b.x0, 0))}–${esc(DH.nf(b.x1, 0))}%</b>${esc(t("data.bat.share_time", { pct: DH.nf(b.v, 0) }))}`,
      });
    },
  });

  /* ---------------------------------------------------- parked drain */
  DH.block("bt-drain", {
    load: () => DH.get(DH.url("/api/analytics/vampire", { days: DH.days() })),
    render(v, body, ctx) {
      const s = v.summary || {};
      const ws = (v.windows || []).filter((w) => w.start && w.end);
      if (!ws.length) {
        body.innerHTML = DH.empty(t("data.bat.drain_none"));
        return;
      }
      const du = DH.u.dist();
      const items = [
        [t("data.bat.per_day"), DH.vu(s.kwh_per_day, "kWh", 2), s.km_per_day != null ? esc(t("data.bat.range_day", { v: `${DH.nf(DH.cv.dist(s.km_per_day), 1)} ${du}` })) : ""],
        [t("data.bat.avg_draw"), DH.vu(s.avg_watts, "W", 0), ""],
        [t("data.bat.asleep"), DH.vu(s.asleep_pct, "%", 0), esc(t("data.bat.asleep_sub"))],
        [t("data.bat.windows"), DH.vu(s.windows, "", 0), s.hours != null ? esc(t("data.bat.hours_parked", { h: DH.nf(s.hours, 0) })) : ""],
      ];
      DH.shell(body, "c", `<div class="dh-kpis small" style="--cols:4"></div>
        <p class="dh-subn" style="margin-top:24px">${esc(t("data.bat.drain_chart"))}</p><div class="dh-chart"></div><div class="dh-legend"></div>
        <details class="dh-more"><summary><span>${esc(t("data.bat.drain_table"))}</span><span class="chev ico-slot" data-icon="chevron" data-size="14"></span></summary>
          <div class="dh-tscroll"><table class="dh-table"><thead><tr><th>${esc(t("data.bat.parked_at"))}</th><th class="num">${esc(t("data.k.duration"))}</th>
            <th class="num">${esc(t("data.bat.range_lost"))}</th><th class="num">${esc(t("data.bat.per_day"))}</th><th class="num">${esc(t("data.bat.asleep"))}</th></tr></thead><tbody></tbody></table></div></details>`);
      paintIcons(body);
      $(".dh-kpis", body).innerHTML = items.map(([l, val, sub], i) => DH.kpi(i, l, val, sub)).join("");
      const bins = ws.map((w, i) => ({
        key: w.start, x0: new Date(w.start).getTime(), x1: new Date(w.end).getTime(), v: w.kwh_per_day,
        hollow: w.kwh == null || w.reduced_range, w, i,
      })).filter((b) => b.v != null);
      DC.hist($(".dh-chart", body), {
        bins, xTime: true, unit: t("data.bat.kwh_day"), morph: ctx.reload, gap: 2, minW: 3, label: t("data.bat.drain"),
        yFmt: (val) => DH.nf(val, val < 10 && val % 1 ? 1 : 0), empty: t("data.c.none_period"),
        tip: (i, b) => `<b>${esc(DH.dt(b.w.start))}</b>`
          + `<div class="r"><span>${esc(t("data.k.duration"))}</span><span class="v">${esc(DH.dur(b.w.hours * 60))}</span></div>`
          + `<div class="r"><span>${esc(t("data.bat.per_day"))}</span><span class="v">${esc(DH.nf(b.v, 2))} kWh</span></div>`
          + `<div class="r"><span>${esc(t("data.bat.asleep"))}</span><span class="v">${esc(DH.nf(b.w.standby_pct, 0))}%</span></div>`
          + (b.hollow ? `<div>${esc(t("data.bat.cold"))}</div>` : ""),
      });
      DC.legend($(".dh-legend", body), [{ kind: "bar", label: t("data.bat.drain_bar") },
        bins.some((b) => b.hollow) ? { kind: "hollow", label: t("data.bat.cold_short") } : null]);
      $("tbody", body).innerHTML = ws.slice().reverse().map((w) => `<tr>
        <td class="nowrap dim">${esc(DH.dt(w.start))}</td><td class="num">${esc(DH.dur(w.hours * 60))}</td>
        <td class="num">${DH.n(DH.cv.dist(w.range_lost_km), 1)} ${esc(du)}</td>
        <td class="num">${w.kwh_per_day != null ? `${DH.n(w.kwh_per_day, 2)} kWh` : '<span class="dash">–</span>'}</td>
        <td class="num dim">${w.standby_pct != null ? `${DH.n(w.standby_pct, 0)}%` : '<span class="dash">–</span>'}</td></tr>`).join("");
    },
  });

  /* ----------------------------------------------- where the time goes */
  const STATES = ["driving", "charging", "online", "asleep", "offline", "updating", "unknown"];
  DH.block("bt-states", {
    load: () => DH.get(DH.url("/api/analytics/states", { days: DH.days() })),
    render(st, body) {
      const tot = st.totals_min || {};
      const all = STATES.reduce((a, k) => a + (tot[k] || 0), 0);
      if (!all) { body.innerHTML = `<p class="dh-subn">&nbsp;</p>${DH.empty(t("data.c.none_period"))}`; return; }
      const parts = STATES.map((k) => ({
        key: k, value: tot[k] || 0, label: t(k === "online" ? "data.bat.s_awake" : k === "unknown" ? "data.bat.s_unknown" : "analytics.state." + k),
        display: `${esc(DH.dur(tot[k] || 0))} · ${esc(DH.nf(((tot[k] || 0) / all) * 100, 0))}%`,
      }));
      const parked = (tot.asleep || 0) + (tot.online || 0);
      const sleepShare = parked ? ((tot.asleep || 0) / parked) * 100 : null;
      const cur = st.current || {};
      body.innerHTML = `<p class="dh-subn">${esc([cur.state ? t("data.bat.now_state", { state: t("analytics.state." + cur.state), since: cur.since ? DH.dt(cur.since) : "–" }) : ""].filter(Boolean).join(""))}</p><div id="bt-split"></div>
        ${sleepShare != null ? `<p class="dh-note" style="margin-top:16px;max-width:60ch">${esc(t("data.bat.sleep_note", { pct: DH.nf(sleepShare, 0), awake: DH.dur(tot.online || 0) }))}</p>` : ""}`;
      DC.split($("#bt-split"), parts, { label: t("data.bat.states") });
    },
  });

  /* ------------------------------------------------- software versions */
  DH.block("bt-sw", {
    load: loadUpdates,
    render(u, body) {
      const list = u.updates || [];
      if (!list.length) { body.innerHTML = `<p class="dh-subn">&nbsp;</p>${DH.empty(t("data.bat.sw_none"))}`; return; }
      const asc = list.slice().reverse();
      const t0 = new Date(asc[0].start).getTime(), t1 = Date.now();
      const span = Math.max(1, t1 - t0);
      const segs = asc.map((x, i) => {
        const a = new Date(x.start).getTime();
        const b = i + 1 < asc.length ? new Date(asc[i + 1].start).getTime() : t1;
        return { x, l: ((a - t0) / span) * 100, w: Math.max(0.6, ((b - a) / span) * 100) };
      });
      body.innerHTML = `<p class="dh-subn">${esc([u.current_version ? t("data.bat.sw_now", { v: u.current_version }) : "",
        u.median_days_between != null ? t("data.bat.sw_every", { n: DH.nf(u.median_days_between, 0) }) : ""].filter(Boolean).join(" · "))}</p>
        <div class="bt-ver" role="img" aria-label="${esc(t("data.bat.sw"))}">${segs.map((s, i) =>
          `<i class="${i % 2 ? "alt" : ""}${s.x.current ? " cur" : ""}" style="left:${s.l.toFixed(3)}%;width:${s.w.toFixed(3)}%" title="${esc(s.x.version || "")}"></i>`).join("")}</div>
        <div class="fp-scale" style="margin-top:6px"><span>${esc(DH.date(asc[0].start))}</span><span>${esc(t("data.c.today"))}</span></div>
        <ul class="dh-ledger" style="margin-top:12px">${list.slice(0, 6).map((x, i) => `<li data-rv style="--i:${i}">
          <span class="k" style="color:var(--dh-ink)">${esc(x.version || "–")}${x.current ? ` <span class="faint">· ${esc(t("energy.current_version"))}</span>` : ""}</span>
          <span class="v" style="font-size:14px;font-weight:400">${esc(t(Math.round(x.days_on_version || 0) === 1 ? "energy.days_n_one" : "energy.days_n", { n: DH.nf(x.days_on_version || 0, 0) }))}</span>
          <span class="s">${esc([DH.date(x.start), x.observed_install && x.install_min ? t("data.bat.install_min", { n: DH.nf(x.install_min, 0) }) : x.observed_install ? "" : t("data.bat.install_missed"),
            x.range_100_km_avg != null ? t("data.bat.ver_range", { v: `${DH.nf(DH.cv.dist(x.range_100_km_avg), 0)} ${DH.u.dist()}` }) : ""].filter(Boolean).join(" · "))}</span></li>`).join("")}</ul>`;
    },
  });
})();
