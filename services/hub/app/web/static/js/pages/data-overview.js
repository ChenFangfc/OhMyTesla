/* 数据 · 概览: how has the car been used, over its life and lately? */
(function () {
  "use strict";
  const $ = (s, r) => (r || document).querySelector(s);

  /* Plural-aware count + noun ("1 drive", "53 drives"; Chinese has one form). */
  const count = (key, n) => {
    const one = key + "_one";
    const k = n === 1 && t(one) !== one ? one : key;
    return t(k, { n: DH.nf(n, 0) });
  };
  const b = (s) => `<b>${s}</b>`;
  /* "53 drives" must not wrap between the number and its noun. */
  const nb = (s) => esc(s).replace(/ /g, "\u00a0");

  /* ----------------------------------------------------- lifetime ledger */
  DH.block("ov-life", {
    load: () => Promise.all([
      DH.get(DH.url("/api/insights/summary")),
      DH.get(DH.url("/api/analytics/locations", { days: 0, limit: 1 })).catch(() => null),
      DH.get(DH.url("/api/analytics/coverage")).catch(() => null),
    ]),
    render([s, loc, cov], body) {
      if (!(s.n_drives || 0) && !(s.n_charges || 0)) {
        body.innerHTML = `<p class="dh-lede">${esc(t("data.ov.lede_none"))}</p>`;
        return;
      }
      const dist = DH.cv.dist(s.km_driven);
      const c = loc && loc.counts ? loc.counts : {};
      const days = loc && loc.summary ? loc.summary.days : null;
      const car = DH.carInfo ? Car.label(DH.carInfo) : t("common.vehicle");
      const vars = {
        date: b(esc(DH.date(s.first_record))),
        car: esc(car),
        dist: b(`${esc(DH.nf(dist, 0))}<span class="u">\u00a0${esc(DH.u.dist())}</span>`),
        drives: b(nb(count("data.c.n_drives", s.n_drives || 0))),
        kwh: b(`${esc(DH.nf(s.kwh_added, 0))}<span class="u">\u00a0kWh</span>`),
        cost: b(esc(DH.money(s.charge_cost, 0) || "–")),
        days: b(nb(count("data.c.n_days", days || 0))),
        places: b(nb(count("data.c.n_places", c.addresses || 0))),
        cities: b(nb(count("data.c.n_cities", c.cities || 0))),
      };
      let html = t(s.charge_cost ? "data.ov.lede" : "data.ov.lede_nocost");
      for (const k in vars) html = html.split("{" + k + "}").join(vars[k]);
      let sub = "";
      if (cov && cov.coverage_pct != null && cov.coverage_pct < 90 && cov.span_km > 1) {
        sub = t("data.ov.coverage", {
          span: `${DH.nf(DH.cv.dist(cov.span_km), 0)} ${DH.u.dist()}`,
          pct: DH.nf(cov.coverage_pct, 0),
        });
      }
      body.innerHTML = `<p class="dh-lede" data-rv>${html}</p>${sub ? `<p class="dh-lede-sub" data-rv style="--i:1">${esc(sub)}</p>` : ""}`;
    },
  });

  /* ------------------------------------------------- this period vs last */
  DH.block("ov-period", {
    load: () => (DH.period === "all"
      ? DH.get(DH.url("/api/insights/summary")).then((s) => ({ all: true, s }))
      : DH.get(DH.url("/api/insights/compare", { days: DH.days() })).then((c) => ({ all: false, c }))),
    render(d, body) {
      $("#ov-period-h").textContent = t("data.period.long_" + DH.period);
      let items;
      if (d.all) {
        const s = d.s;
        $("#ov-period-n").textContent = s.first_record ? t("data.ov.since", { date: DH.date(s.first_record) }) : "";
        const used = s.km_driven && s.avg_consumption_wh_km ? (s.km_driven * s.avg_consumption_wh_km) / 1000 : null;
        items = [
          [t("data.k.distance"), DH.vu(DH.cv.dist(s.km_driven), DH.u.dist(), 0)],
          [t("data.k.drives"), DH.vu(s.n_drives, "", 0)],
          [t("data.k.energy_used"), DH.vu(used, "kWh", 0)],
          [t("data.k.consumption"), DH.vu(DH.cv.cons(s.avg_consumption_wh_km), DH.u.cons(), 0)],
          [t("data.k.charged"), DH.vu(s.kwh_added, "kWh", 0)],
          [t("data.k.cost"), DH.mv(s.charge_cost, 0)],
        ].map(([l, v]) => [l, v, null]);
      } else {
        const { current: c, previous: p } = d.c;
        $("#ov-period-n").textContent = t("data.ov.vs_prev", { days: DH.nf(DH.days(), 0) });
        const km = DH.cv.dist(c.km);
        items = [
          [t("data.k.distance"), DH.vu(km, DH.u.dist(), DH.dd(km)), DH.delta(c.km, p.km, null).html],
          [t("data.k.drives"), DH.vu(c.drives, "", 0), DH.delta(c.drives, p.drives, null).html],
          [t("data.k.energy_used"), DH.vu(c.kwh, "kWh", c.kwh < 100 ? 1 : 0), DH.delta(c.kwh, p.kwh, null).html],
          [t("data.k.consumption"), DH.vu(DH.cv.cons(c.wh_per_km), DH.u.cons(), 0), DH.delta(c.wh_per_km, p.wh_per_km, "down").html],
          [t("data.k.charged"), DH.vu(c.charged_kwh, "kWh", c.charged_kwh < 100 ? 1 : 0), DH.delta(c.charged_kwh, p.charged_kwh, null).html],
          [t("data.k.cost"), DH.mv(c.cost, c.cost < 100 ? 2 : 0), DH.delta(c.cost, p.cost, null).html],
        ];
      }
      body.innerHTML = `<div class="dh-kpis" style="--cols:6">${items.map(([l, v, dd], i) => DH.kpi(i, l, v, dd)).join("")}</div>`;
    },
  });

  /* ------------------------------------------------------------- records */
  DH.block("ov-records", {
    load: () => Promise.all([
      DH.get(DH.url("/api/insights/records")),
      DH.get(DH.url("/api/analytics/charges", { days: 0, limit: 1000 })).catch(() => null),
    ]),
    render([r, ch], body) {
      let fastest = null;
      for (const c of (ch && ch.charges) || []) if (c.peak_kw != null && (!fastest || c.peak_kw > fastest.peak_kw)) fastest = c;
      const rec = (label, valHtml, sub, href) => ({ label, valHtml, sub, href });
      const list = [
        rec(t("data.ov.r_longest"), r.longest_drive_km ? DH.vu(DH.cv.dist(r.longest_drive_km.value), DH.u.dist(), 0) : null,
            r.longest_drive_km ? DH.date(r.longest_drive_km.date) : ""),
        rec(t("data.ov.r_fastest_charge"), fastest ? DH.vu(fastest.peak_kw, "kW", 0) : null,
            fastest ? [DH.date(fastest.start), fastest.location] : "",
            fastest ? `/data/charging?days=all&charge=${fastest.id}` : null),
        rec(t("data.ov.r_best_eff"), r.best_efficiency_wh_km ? DH.vu(DH.cv.cons(r.best_efficiency_wh_km.value), DH.u.cons(), 0) : null,
            r.best_efficiency_wh_km ? [DH.date(r.best_efficiency_wh_km.date),
              r.best_efficiency_wh_km.km ? t("data.ov.over_dist", { dist: `${DH.nf(DH.cv.dist(r.best_efficiency_wh_km.km), 0)} ${DH.u.dist()}` }) : ""] : ""),
        rec(t("data.ov.r_top_speed"), r.top_speed_kmh ? DH.vu(DH.cv.speed(r.top_speed_kmh.value), DH.u.speed(), 0) : null,
            r.top_speed_kmh ? DH.date(r.top_speed_kmh.date) : ""),
        rec(t("data.ov.r_busiest_day"), r.busiest_day_km ? DH.vu(DH.cv.dist(r.busiest_day_km.value), DH.u.dist(), 0) : null,
            r.busiest_day_km ? DH.date(r.busiest_day_km.date) : ""),
        rec(t("data.ov.r_biggest_charge"), r.biggest_charge_kwh ? DH.vu(r.biggest_charge_kwh.value, "kWh", 1) : null,
            r.biggest_charge_kwh ? DH.date(r.biggest_charge_kwh.date) : ""),
      ];
      if (list.every((x) => x.valHtml == null)) { body.innerHTML = DH.empty(t("data.ov.records_none")); return; }
      body.innerHTML = `<div class="dh-kpis small rec">${list.map((x, i) => {
        const v = x.valHtml || '<span class="dash">–</span>';
        const parts = DH.dots(Array.isArray(x.sub) ? x.sub : [x.sub]);
        const sub = x.href ? `<a href="${esc(x.href)}">${parts}</a>` : parts;
        return DH.kpi(i, x.label, v, sub);
      }).join("")}</div>`;
    },
  });

  /* --------------------------------------------- distance + energy trend */
  DH.block("ov-trend", {
    load: async () => {
      const s = await DH.get(DH.url("/api/insights/summary"));
      const span = s.first_record ? (Date.now() - new Date(s.first_record)) / 864e5 : 90;
      const bucket = DH.bucket(span);
      const [mi, st] = await Promise.all([
        DH.get(DH.url("/api/analytics/mileage", { days: DH.days(), bucket })),
        DH.get(DH.url("/api/analytics/statistics", { days: DH.days(), period: bucket })),
      ]);
      return { s, bucket, mi, st };
    },
    render(d, body, ctx) {
      const { bucket } = d;
      const startKey = DH.periodStartKey(d.s.first_record);
      const keys = DH.buckets(startKey, bucket);
      const multiYear = keys.length && keys[0].slice(0, 4) !== keys[keys.length - 1].slice(0, 4);
      const labels = keys.map((k) => DH.bucketLabel(k, bucket, multiYear));
      const srow = new Map((d.st.rows || []).map((r) => [r.date, r]));
      const m = DH.mileage(d.mi.rows, keys);
      const logged = m.logged.map(DH.cv.dist);
      const unlogged = m.un.map(DH.cv.dist);
      const kwh = keys.map((k) => (srow.get(k) || {}).energy_added_kwh || 0);
      const anyUnlogged = unlogged.some((v) => v > 0.5);
      $("#ov-trend-n").textContent = t("data.ov.by_" + bucket);
      DH.shell(body, "trend", `<div class="dh-grid">
        <div class="dh-sub c6"><h3 class="dh-subh">${esc(t("data.ov.trend_dist"))}</h3><p class="dh-subn" id="ov-dist-n"></p><div class="dh-chart" id="ov-dist"></div><div class="dh-legend" id="ov-dist-lg"></div></div>
        <div class="dh-sub c6"><h3 class="dh-subh">${esc(t("data.ov.trend_energy"))}</h3><p class="dh-subn" id="ov-kwh-n"></p><div class="dh-chart" id="ov-kwh"></div></div>
      </div>`);
      const totLogged = logged.reduce((a, v) => a + v, 0), totUn = unlogged.reduce((a, v) => a + v, 0);
      $("#ov-dist-n").textContent = anyUnlogged
        ? t("data.ov.dist_sub_gap", { logged: `${DH.nf(totLogged, 0)} ${DH.u.dist()}`, unlogged: `${DH.nf(totUn, 0)} ${DH.u.dist()}` })
        : t("data.ov.dist_sub", { logged: `${DH.nf(totLogged, 0)} ${DH.u.dist()}` });
      $("#ov-kwh-n").textContent = t("data.ov.kwh_sub", { kwh: DH.nf(kwh.reduce((a, v) => a + v, 0), 0) });
      const du = DH.u.dist();
      DC.bars($("#ov-dist"), {
        keys, labels, values: logged, extra: anyUnlogged ? unlogged : null, unit: du, morph: ctx.reload,
        label: t("data.ov.trend_dist"), empty: t("data.c.none_period"),
        tip: (i) => `<b>${esc(labels[i])}</b><div class="r"><span>${esc(t("data.ov.logged"))}</span><span class="v">${esc(DH.nf(logged[i], DH.dd(logged[i])))} ${esc(du)}</span></div>`
          + (anyUnlogged ? `<div class="r"><span>${esc(t("data.ov.unlogged"))}</span><span class="v">${esc(DH.nf(unlogged[i], DH.dd(unlogged[i])))} ${esc(du)}</span></div>` : "")
          + (m.spread[i] > 1 ? `<div>${esc(t("data.ov.spread_" + bucket, { n: m.spread[i] }))}</div>` : ""),
      });
      DC.legend($("#ov-dist-lg"), anyUnlogged ? [{ kind: "bar", label: t("data.ov.logged") }, { kind: "hollow", label: t("data.ov.unlogged_long") }] : []);
      DC.bars($("#ov-kwh"), {
        keys, labels, values: kwh, unit: "kWh", morph: ctx.reload, label: t("data.ov.trend_energy"), empty: t("data.c.none_period"),
        tip: (i) => `<b>${esc(labels[i])}</b><div class="r"><span>${esc(t("data.k.charged"))}</span><span class="v">${esc(DH.nf(kwh[i], kwh[i] < 100 ? 1 : 0))} kWh</span></div>`
          + ((srow.get(keys[i]) || {}).charges ? `<div class="r"><span>${esc(t("data.k.sessions"))}</span><span class="v">${esc(DH.nf(srow.get(keys[i]).charges, 0))}</span></div>` : ""),
      });
    },
  });

  /* ---------------------------------------------------- activity calendar */
  DH.block("ov-cal", {
    load: async () => {
      const s = await DH.get(DH.url("/api/insights/summary"));
      const sinceFirst = s.first_record ? Math.ceil((Date.now() - new Date(s.first_record)) / 864e5) + 1 : 365;
      const days = DH.period === "all" ? Math.max(365, sinceFirst) : Math.max(365, DH.days());
      const cal = await DH.get(DH.url("/api/insights/calendar", { days }));
      return { s, cal, days };
    },
    render(d, body) {
      const today = DH.parts(Date.now()).key;
      const start = DH.addDays(today, -(d.days - 1));
      const inFrom = DH.periodStartKey(d.s.first_record);
      const du = DH.u.dist();
      DH.shell(body, "cal", `<div class="dh-tscroll"><div class="dh-chart short" id="ov-calc"></div></div>
        <div class="dh-legend" id="ov-cal-lg"></div>`);
      const inPeriod = (d.cal.days || []).filter((x) => x.date >= inFrom);
      const act = inPeriod.filter((x) => x.drives || x.charges).length;
      const km = inPeriod.reduce((a, x) => a + (x.km || 0), 0);
      $("#ov-cal-n").textContent = t("data.ov.cal_n", { days: DH.nf(act, 0), dist: `${DH.nf(DH.cv.dist(km), 0)} ${du}` });
      DC.calendar($("#ov-calc"), {
        days: d.cal.days || [], start, end: today, inFrom,
        value: (x) => x.km || 0, label: t("data.ov.cal"),
        dayLabels: [0, 1, 2, 3, 4, 5, 6].map((i) => DH.fdate(`2026-01-${String(5 + i).padStart(2, "0")}`, { weekday: "short" })),
        tip: (key, x) => `<b>${esc(DH.fdate(key, { weekday: "short", month: "short", day: "numeric" }))}</b>` + (x
          ? `<div class="r"><span>${esc(t("data.k.distance"))}</span><span class="v">${esc(DH.nf(DH.cv.dist(x.km), 1))} ${esc(du)}</span></div>
             <div class="r"><span>${esc(t("data.k.drives"))}</span><span class="v">${esc(DH.nf(x.drives, 0))}</span></div>
             ${x.charges ? `<div class="r"><span>${esc(t("data.k.charged"))}</span><span class="v">${esc(DH.nf(x.kwh, 1))} kWh</span></div>` : ""}`
          : `<div>${esc(t("data.ov.cal_idle"))}</div>`),
        onDay: (key) => { location.href = `/data/timeline?days=${DH.period}#day-${key}`; },
      });
      const lg = $("#ov-cal-lg");
      lg.innerHTML = `<span class="dh-ramp">${esc(t("data.ov.cal_less"))}${[0, 1, 2, 3, 4].map((l) =>
        `<svg width="11" height="11" aria-hidden="true"><rect width="11" height="11" rx="2" class="dc-cell l${l}"/></svg>`).join("")}${esc(t("data.ov.cal_more"))}</span>
        <span class="dh-legend-note">${esc(t("data.ov.cal_dim"))}</span>`;
    },
  });
})();
