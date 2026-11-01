/* 数据 · 充电: how much did I charge, and what did it cost? */
(function () {
  "use strict";
  const $ = (s, r) => (r || document).querySelector(s);
  const loadCharges = () => DH.get(DH.url("/api/analytics/charges", { days: DH.days(), limit: 1000 }));
  const loadStats = () => DH.get(DH.url("/api/analytics/charging-stats", { days: DH.days() }));
  const loadPlan = () => DH.get("/api/tariff/plan").catch(() => null);
  const loadSummary = () => DH.get(DH.url("/api/insights/summary"));
  /* A cost is known once a tariff is saved, or when TeslaMate recorded one
     itself. "$0" without either would claim the charging was free. */
  const isPriced = (plan, st) => !!((plan && plan.saved && !plan.unconfigured) || (st && st.totals && st.totals.cost > 0));
  const ratesLink = () => `<a href="/settings#s-rates">${esc(t("data.chg.set_rates"))}</a>`;
  const per100 = (v) => (v == null ? null : DH.u.dist() === "mi" ? v * 1.609344 : v);
  const placeOf = (c) => (typeof c.location === "string" ? c.location : c.location && (c.location.label || c.location.city)) || t("common.unknown");

  async function bucketing() {
    await loadCharges();                         // carries the hub's time zone
    const s = await loadSummary();
    const span = s.first_record ? (Date.now() - new Date(s.first_record)) / 864e5 : 90;
    const bucket = DH.bucket(span);
    const keys = DH.buckets(DH.periodStartKey(s.first_record), bucket);
    const multi = keys.length > 0 && keys[0].slice(0, 4) !== keys[keys.length - 1].slice(0, 4);
    return { bucket, keys, labels: keys.map((k) => DH.bucketLabel(k, bucket, multi)) };
  }

  /* ------------------------------------------------------------------ KPIs */
  DH.block("cg-sum", {
    load: () => Promise.all([loadStats(), loadPlan()]),
    render([st, plan], body) {
      const tt = st.totals || {}, ac = (st.ac_dc || {}).AC || {}, dc = (st.ac_dc || {}).DC || {};
      if (!tt.charges) { body.innerHTML = DH.empty(t("data.chg.none"), `<a href="?days=all">${esc(t("data.c.try_all"))}</a>`); return; }
      const priced = isPriced(plan, st);
      const split = [ac.sessions ? t("data.chg.n_ac", { n: ac.sessions }) : "", dc.sessions ? t("data.chg.n_dc", { n: dc.sessions }) : ""].filter(Boolean).join(" · ");
      const items = [
        [t("data.k.sessions"), DH.vu(tt.charges, "", 0), esc(split)],
        [t("data.chg.added"), DH.vu(tt.added_kwh, "kWh", tt.added_kwh < 100 ? 1 : 0),
          esc(t("data.chg.per_session", { kwh: DH.nf(tt.added_kwh / tt.charges, 1) }))],
        [t("data.chg.used"), DH.vu(tt.used_kwh, "kWh", tt.used_kwh < 100 ? 1 : 0),
          tt.efficiency_pct != null ? esc(t("data.chg.reached", { pct: DH.nf(tt.efficiency_pct, 0) })) : ""],
        [t("data.k.cost"), priced ? DH.mv(tt.cost, tt.cost < 100 ? 2 : 0) : '<span class="dash">–</span>',
          priced ? (tt.suc_cost ? esc(t("data.chg.at_suc", { cost: DH.money(tt.suc_cost) })) : "") : ratesLink()],
        [t("data.chg.per_kwh"), priced ? DH.mv(tt.cost_per_kwh, 2) : '<span class="dash">–</span>',
          priced ? esc([ac.cost_per_kwh != null ? `${t("analytics.charge.ac")} ${DH.money(ac.cost_per_kwh)}` : "",
                        dc.cost_per_kwh != null ? `${t("data.chg.dc_short")} ${DH.money(dc.cost_per_kwh)}` : ""].filter(Boolean).join(" · ")) : ""],
        [t(DH.u.dist() === "mi" ? "data.chg.per_100mi" : "data.chg.per_100km"), priced ? DH.mv(per100(tt.cost_per_100km), 2) : '<span class="dash">–</span>',
          tt.gross_wh_per_km ? esc(t("data.chg.at_cons", { v: `${DH.nf(DH.cv.cons(tt.gross_wh_per_km), 0)} ${DH.u.cons()}` })) : ""],
      ];
      body.innerHTML = `<div class="dh-kpis" style="--cols:6">${items.map(([l, v, s], i) => DH.kpi(i, l, v, s)).join("")}</div>`;
    },
  });

  /* ------------------------------------------- energy by bucket, AC + DC */
  DH.block("cg-energy", {
    load: async () => ({ ...(await bucketing()), ch: await loadCharges() }),
    render(d, body, ctx) {
      const idx = new Map(d.keys.map((k, i) => [k, i]));
      const ac = d.keys.map(() => 0), dc = d.keys.map(() => 0), n = d.keys.map(() => 0);
      for (const c of d.ch.charges || []) {
        if (c.added_kwh == null) continue;
        const i = idx.get(DH.bucketKey(DH.parts(c.start).key, d.bucket));
        if (i == null) continue;
        (c.type === "DC" ? dc : ac)[i] += c.added_kwh;
        n[i]++;
      }
      $("#cg-energy-n").textContent = t("data.ov.by_" + d.bucket);
      DH.shell(body, "c", `<p class="dh-subn"></p><div class="dh-chart"></div><div class="dh-legend"></div>`);
      const tot = ac.reduce((a, v) => a + v, 0) + dc.reduce((a, v) => a + v, 0);
      $(".dh-subn", body).textContent = t("data.ov.kwh_sub", { kwh: DH.nf(tot, 0) });
      const anyDc = dc.some((v) => v > 0);
      DC.bars($(".dh-chart", body), {
        keys: d.keys, labels: d.labels, values: ac, alt: anyDc ? dc : null, unit: "kWh", morph: ctx.reload,
        label: t("data.chg.energy_by"), empty: t("data.c.none_period"),
        tip: (i) => `<b>${esc(d.labels[i])}</b>`
          + `<div class="r"><span>${esc(t("analytics.charge.ac"))}</span><span class="v">${esc(DH.nf(ac[i], 1))} kWh</span></div>`
          + (anyDc ? `<div class="r"><span>${esc(t("data.chg.dc_short"))}</span><span class="v">${esc(DH.nf(dc[i], 1))} kWh</span></div>` : "")
          + `<div class="r"><span>${esc(t("data.k.sessions"))}</span><span class="v">${esc(DH.nf(n[i], 0))}</span></div>`,
      });
      DC.legend($(".dh-legend", body), anyDc ? [{ kind: "bar", label: t("analytics.charge.ac") }, { kind: "alt", label: t("analytics.charge.dc") }] : []);
    },
  });

  /* ----------------------------------------------------------- AC vs DC */
  DH.block("cg-acdc", {
    load: () => Promise.all([loadStats(), loadPlan()]),
    render([st, plan], body) {
      const priced = isPriced(plan, st);
      const rows = [["AC", t("analytics.charge.ac")], ["DC", t("analytics.charge.dc")]]
        .map(([k, label]) => ({ k, label, x: (st.ac_dc || {})[k] }))
        .filter((r) => r.x && r.x.sessions);
      if (!rows.length) { body.innerHTML = `<p class="dh-subn">&nbsp;</p>${DH.empty(t("data.chg.none"))}`; return; }
      const totKwh = rows.reduce((a, r) => a + (r.x.added_kwh || 0), 0) || 1;
      body.innerHTML = `<p class="dh-subn">${esc(t("data.chg.acdc_sub"))}</p><ul class="dh-ledger">${rows.map((r, i) => `
        <li data-rv style="--i:${i}"><span class="k">${esc(r.label)}</span><span class="v">${DH.vu(r.x.added_kwh, "kWh", 0)}</span>
          <span class="s">${esc([t("data.chg.share", { pct: DH.nf((r.x.added_kwh / totKwh) * 100, 0) }),
            t(r.x.sessions === 1 ? "data.chg.n_sessions_one" : "data.chg.n_sessions", { n: r.x.sessions }),
            DH.dur(r.x.duration_min),
            priced && r.x.cost_per_kwh != null ? t("data.chg.rate", { v: DH.money(r.x.cost_per_kwh) }) : ""].filter(Boolean).join(" · "))}</span></li>`).join("")}</ul>`;
    },
  });

  /* ------------------------------------------------------- cost by bucket */
  DH.block("cg-costc", {
    load: async () => ({ ...(await bucketing()), ch: await loadCharges(), st: await loadStats(), plan: await loadPlan() }),
    render(d, body, ctx) {
      const priced = isPriced(d.plan, d.st);
      const idx = new Map(d.keys.map((k, i) => [k, i]));
      const cost = d.keys.map(() => 0), kwh = d.keys.map(() => 0);
      for (const c of d.ch.charges || []) {
        const i = idx.get(DH.bucketKey(DH.parts(c.start).key, d.bucket));
        if (i == null) continue;
        if (c.cost != null) cost[i] += c.cost;
        if (c.billed_kwh != null) kwh[i] += c.billed_kwh;
      }
      const tt = d.st.totals || {};
      $("#cg-cost-n").textContent = priced && tt.cost != null
        ? t("data.chg.cost_n", { cost: DH.money(tt.cost), basis: t("data.chg.basis") }) : " ";
      if (!priced) {
        DH.shell(body, "none", `<p class="dh-subn">&nbsp;</p><div class="dh-empty">${esc(t("data.chg.no_rates"))} ${ratesLink()}</div>`);
        return;
      }
      DH.shell(body, "c", `<p class="dh-subn"></p><div class="dh-chart"></div>`);
      const peak = cost.reduce((a, v, i) => (v > cost[a] ? i : a), 0);
      $(".dh-subn", body).textContent = cost[peak] ? t("data.chg.cost_peak", { label: d.labels[peak], cost: DH.money(cost[peak]) }) : " ";
      DC.bars($(".dh-chart", body), {
        keys: d.keys, labels: d.labels, values: cost, morph: ctx.reload, unit: DH.currencySymbol(),
        yFmt: (v) => DH.nf(v, v < 10 && v % 1 ? 1 : 0), label: t("data.chg.cost_by"), empty: t("data.c.none_period"),
        tip: (i) => `<b>${esc(d.labels[i])}</b><div class="r"><span>${esc(t("data.k.cost"))}</span><span class="v">${esc(DH.money(cost[i]))}</span></div>`
          + (kwh[i] ? `<div class="r"><span>${esc(t("data.chg.per_kwh"))}</span><span class="v">${esc(DH.money(cost[i] / kwh[i]))}</span></div>` : ""),
      });
    },
  });

  /* ------------------------------------------------- wall vs battery */
  DH.block("cg-wall", {
    load: loadStats,
    render(st, body) {
      const tt = st.totals || {};
      const rows = [
        [t("data.chg.all"), tt.used_kwh, tt.added_kwh],
        [t("analytics.charge.ac"), ((st.ac_dc || {}).AC || {}).used_kwh, ((st.ac_dc || {}).AC || {}).added_kwh],
        [t("data.chg.dc_short"), ((st.ac_dc || {}).DC || {}).used_kwh, ((st.ac_dc || {}).DC || {}).added_kwh],
      ].filter((r) => r[1] > 0 && r[2] != null);
      if (!rows.length) { body.innerHTML = `<p class="dh-subn">&nbsp;</p>${DH.empty(t("data.chg.none"))}`; return; }
      body.innerHTML = `<p class="dh-subn">${esc(t("data.chg.wall_sub"))}</p><div class="dh-flow">${rows.map(([label, used, added], i) => {
        const pct = Math.min(100, (added / used) * 100);
        const loss = Math.max(0, used - added);
        return `<div class="row" data-rv style="--i:${i}"><span class="k">${esc(label)}</span>
          <div class="dh-split" role="img" aria-label="${esc(t("data.chg.reached", { pct: DH.nf(pct, 0) }))}"><i class="s-battery" style="flex-grow:${pct.toFixed(2)}"></i><i class="s-loss" style="flex-grow:${(100 - pct).toFixed(2)}"></i></div>
          <span class="v"><b>${esc(DH.nf(pct, 0))}%</b> · ${esc(t("data.chg.lost", { kwh: DH.nf(loss, 1) }))}</span></div>`;
      }).join("")}</div>
        <div class="dh-keys"><span><i class="s-battery"></i>${esc(t("data.chg.into_battery"))}</span><span><i class="s-loss"></i>${esc(t("data.chg.losses"))}</span></div>`;
    },
  });

  /* --------------------------------------------- sessions + curve explorer */
  const PAGE = () => (matchMedia("(max-width: 900px)").matches ? 15 : 40);
  const S = { list: [], shown: PAGE(), cur: null, gen: 0, priced: false, dcCurve: [] };
  S.deep = +(new URLSearchParams(location.search).get("charge") || 0) || null;

  function sessionRow(c) {
    const cost = c.cost_source === "ongoing" ? `<span class="dim">${esc(t("analytics.cost_source.ongoing"))}</span>`
      : c.cost == null || !S.priced ? '<span class="dash">–</span>' : esc(DH.money(c.cost));
    return `<tr class="tap" data-id="${c.id}" aria-selected="${c.id === S.cur}">
      <td class="nowrap dim">${esc(DH.dateShort(c.start))}<span class="s">${esc(DH.time(c.start))}</span></td>
      <td>${esc(placeOf(c))}<span class="s">${esc([c.type === "DC" ? t("data.chg.dc_short") : t("analytics.charge.ac"),
        c.peak_kw != null ? `${DH.nf(c.peak_kw, 0)} kW` : "", c.start_soc != null && c.end_soc != null ? `${c.start_soc}→${c.end_soc}%` : ""].filter(Boolean).join(" · "))}</span></td>
      <td class="num">${DH.n(c.added_kwh, 1)}</td><td class="num">${cost}</td></tr>`;
  }
  function paintSessions() {
    const box = $("#cg-list .tr-list");
    if (!box) return;
    const n = S.list.length;
    if (!n) { box.innerHTML = DH.empty(t("data.chg.none"), `<a href="?days=all">${esc(t("data.c.try_all"))}</a>`); $("#cg-more").innerHTML = ""; return; }
    const rows = S.list.slice(0, S.shown);
    box.innerHTML = `<table class="dh-table"><thead><tr><th>${esc(t("data.k.date"))}</th><th>${esc(t("data.chg.where_col"))}</th>
      <th class="num">kWh</th><th class="num">${esc(t("data.k.cost"))}</th></tr></thead><tbody>${rows.map(sessionRow).join("")}</tbody></table>`;
    box.querySelectorAll("tbody tr").forEach((tr) => tr.addEventListener("click", () => select(+tr.dataset.id)));
    $("#cg-more").innerHTML = `<span>${esc(t("data.drv.showing", { a: 1, b: DH.nf(rows.length, 0), n: DH.nf(n, 0) }))}</span>`
      + (S.total > n ? `<span>${esc(t("data.c.capped", { n: DH.nf(n, 0), total: DH.nf(S.total, 0) }))}</span>` : "")
      + (n > rows.length ? `<button type="button" class="dh-btn quiet">${esc(t("data.chg.show_more_n", { n: Math.min(PAGE(), n - rows.length) }))}</button>` : "");
    const b = $("#cg-more button");
    if (b) b.addEventListener("click", () => { S.shown += PAGE(); paintSessions(); });
  }

  DH.block("cg-list", {
    load: () => Promise.all([loadCharges(), loadStats(), loadPlan()]),
    render([ch, st, plan]) {
      S.priced = isPriced(plan, st);
      S.list = ch.charges || [];
      S.total = ch.total || S.list.length;
      S.dcCurve = (st.dc_curve || []).filter((p) => p.kw != null);
      paintSessions();
      const want = S.deep;
      if (want) { S.deep = null; select(want, true); return; }
      if (S.cur && S.list.some((c) => c.id === S.cur)) { select(S.cur); return; }
      const first = S.list.find((c) => c.added_kwh != null) || S.list[0];
      if (first) select(first.id);
      else $("#cg-curve .dh-body").innerHTML = DH.empty(t("data.chg.none"));
    },
  });

  async function select(id, deep) {
    S.cur = id;
    const idx = S.list.findIndex((c) => c.id === id);
    if (idx >= S.shown) { S.shown = idx + 1; paintSessions(); }
    document.querySelectorAll("#cg-list tbody tr").forEach((tr) => tr.setAttribute("aria-selected", String(+tr.dataset.id === id)));
    if (deep) document.getElementById("cg-sessions-sec").scrollIntoView({ block: "start" });
    const gen = ++S.gen;
    const box = $("#cg-curve .dh-body");
    box.setAttribute("aria-busy", "true");
    let d;
    try { d = await DH.get(`/api/analytics/charges/${id}?max_points=400`); }
    catch (e) {
      if (gen !== S.gen) return;
      box.removeAttribute("aria-busy");
      box.innerHTML = `<div class="dh-err" role="alert"><span class="ic">${icon("warn", 18)}</span><span class="msg"><b>${esc(t("data.chg.session_err"))}</b>${esc(DH.errText(e))}</span></div>`;
      return;
    }
    if (gen !== S.gen) return;
    box.removeAttribute("aria-busy");
    paintCurve(d, idx < 0);
  }

  function paintCurve(d, outside) {
    const c = d.charge || {}, en = c.energy || {}, cost = c.cost || {};
    const box = $("#cg-curve .dh-body");
    const type = c.type === "DC" ? t("analytics.charge.dc")
      : [t("analytics.charge.ac"), c.ac_phases >= 2 ? t("analytics.charge.phases", { n: Math.round(c.ac_phases) }) : ""].filter(Boolean).join(" · ");
    const src = cost.source ? t("analytics.cost_source." + cost.source) : "";
    const costHtml = cost.source === "ongoing" ? `<span class="n">${esc(src)}</span>`
      : cost.cost != null && S.priced ? DH.mv(cost.cost) : '<span class="dash">–</span>';
    const figs = [
      DH.fig(t("data.chg.added"), DH.vu(en.added_kwh, "kWh", 1), c.range_added_km != null ? esc(t("data.chg.range_added", { v: `${DH.nf(DH.cv.dist(c.range_added_km), 0)} ${DH.u.dist()}` })) : ""),
      DH.fig(t("data.chg.used"), DH.vu(en.used_kwh, "kWh", 1), en.efficiency_pct != null ? esc(t("data.chg.reached", { pct: DH.nf(en.efficiency_pct, 0) })) : ""),
      DH.fig(t("data.chg.peak"), DH.vu(c.peak_kw, "kW", 0), c.avg_kw != null ? esc(t("data.chg.avg_kw", { v: DH.nf(c.avg_kw, 1) })) : ""),
      DH.fig(t("data.k.battery"), c.start_soc != null ? `<span class="n">${esc(DH.nf(c.start_soc, 0))}→${c.end_soc != null ? esc(DH.nf(c.end_soc, 0)) : "…"}</span><span class="u">%</span>` : '<span class="dash">–</span>',
        `${esc(DH.dur(c.duration_min))}`),
      DH.fig(t("data.k.cost"), costHtml, esc([cost.source && cost.source !== "ongoing" ? src : "", cost.stale ? t("analytics.cost.stale") : ""].filter(Boolean).join(" · "))),
    ];
    const pts = (d.curve || []).filter((p) => p.soc != null && p.kw != null).map((p) => [p.soc, p.kw]);
    const ref = c.type === "DC" && S.dcCurve.length > 1 ? S.dcCurve.map((p) => [p.soc, p.kw]) : [];
    DH.shell(box, "curve", `<h3 class="dh-subh" id="cg-c-h"></h3><p class="dh-subn" id="cg-c-w"></p><p class="dh-note" id="cg-c-o" hidden></p>
      <div class="dh-figs" id="cg-c-f" style="margin:12px 0 20px"></div>
      <div class="dh-chart tall" id="cg-c-c"></div><div class="dh-legend" id="cg-c-l"></div><p class="dh-note" id="cg-c-n" style="margin-top:12px"></p>`);
    $("#cg-c-h").textContent = placeOf(c);
    $("#cg-c-w").textContent = [DH.date(c.start), `${DH.time(c.start)}${c.end ? "–" + DH.time(c.end) : ""}`, type, c.charger_brand].filter(Boolean).join(" · ");
    $("#cg-c-o").hidden = !outside;
    $("#cg-c-o").textContent = t("data.chg.outside_period");
    $("#cg-c-f").innerHTML = figs.join("");
    DC.line($("#cg-c-c"), {
      points: pts, refLine: ref, zero: true, unit: "kW", xMin: pts.length ? undefined : 0, xMax: pts.length ? undefined : 100,
      xFmt: (v) => `${DH.nf(v, 0)}%`, yFmt: (v) => DH.nf(v, 0), label: t("data.chg.curve"), xInteger: true,
      empty: t("data.chg.no_curve"),
      tip: (p) => `<b>${esc(DH.nf(p[0], 0))}%</b><div class="r"><span>${esc(t("data.chg.power"))}</span><span class="v">${esc(DH.nf(p[1], 1))} kW</span></div>`
        + (ref.length ? (() => { const r = ref.reduce((a, q) => (Math.abs(q[0] - p[0]) < Math.abs(a[0] - p[0]) ? q : a), ref[0]);
          return Math.abs(r[0] - p[0]) <= 1 ? `<div class="r"><span>${esc(t("data.chg.dc_typical"))}</span><span class="v">${esc(DH.nf(r[1], 0))} kW</span></div>` : ""; })() : ""),
    });
    DC.legend($("#cg-c-l"), [{ kind: "line", label: t("data.chg.this_session") }, ref.length ? { kind: "ref", label: t("data.chg.dc_typical_long") } : null]);
    $("#cg-c-n").textContent = [en.losses_kwh != null ? t("data.chg.lost_session", { kwh: DH.nf(en.losses_kwh, 1) }) : "",
      cost.written_back ? t("analytics.cost.written_back") : "", S.priced && cost.source && cost.source !== "ongoing" ? t("analytics.cost.basis") : ""].filter(Boolean).join(" · ");
  }

  /* ----------------------------------------------------- where you charge */
  DH.block("cg-where", {
    load: () => Promise.all([loadStats(), loadPlan()]),
    render([st, plan], body) {
      const priced = isPriced(plan, st);
      const top = (st.top_stations_by_energy || []).slice(0, 6);
      body.innerHTML = `<p class="dh-subn">${esc(t("data.chg.where_sub"))}</p><div></div>`;
      DC.rank(body.lastElementChild, top.map((x) => ({
        label: x.location || t("common.unknown"), value: x.added_kwh,
        display: DH.vu(x.added_kwh, "kWh", 0),
        sub: esc([t(x.sessions === 1 ? "data.chg.n_sessions_one" : "data.chg.n_sessions", { n: x.sessions }),
                  x.type === "DC" ? t("data.chg.dc_short") : t("analytics.charge.ac"),
                  priced && x.cost != null ? DH.money(x.cost) : ""].filter(Boolean).join(" · ")),
      })), { empty: t("data.chg.none") });
    },
  });

  /* ----------------------------------------- when charging starts / stops */
  DH.block("cg-when", {
    load: () => DH.get(DH.url("/api/analytics/charging-schedule", { days: DH.period === "all" ? 3650 : Math.max(7, DH.days()) })),
    render(sc, body) {
      const stops = (sc.stop_times || []).slice(0, 3), starts = (sc.start_times || []).slice(0, 3);
      const hm = (m) => new Intl.DateTimeFormat(localeTag(), { hour: "numeric", minute: "2-digit", timeZone: "UTC" }).format(new Date(Date.UTC(2026, 0, 5, 0, m)));
      const sub = stops.length ? t("data.chg.when_stop", { time: hm(stops[0].minute), n: stops[0].sessions })
        : starts.length ? t("data.chg.when_start", { time: hm(starts[0].minute), n: starts[0].sessions }) : "";
      if (!stops.length && !starts.length) {
        body.innerHTML = `<p class="dh-subn">&nbsp;</p>${DH.empty(t("data.chg.when_none"))}`;
        return;
      }
      body.innerHTML = `<p class="dh-subn">${esc(sub)}</p><div class="cg-clock" role="img" aria-label="${esc(sub)}"></div>
        <div class="dh-keys"><span><i class="s-battery" style="border-radius:50%"></i>${esc(t("data.chg.stops"))}</span><span><i class="s-loss" style="border-radius:50%"></i>${esc(t("data.chg.starts"))}</span></div>`;
      drawClock($(".cg-clock", body), stops, starts, hm);
    },
  });

  /* A 24-hour rail with the learned start (hollow) and stop (solid) times. */
  function drawClock(el, stops, starts, hm) {
    const W = Math.max(240, el.clientWidth), H = 96, y = 40, pad = 8;
    const x = (m) => pad + (m / 1440) * (W - pad * 2);
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("width", "100%"); svg.setAttribute("height", H);
    let s = `<line x1="${pad}" x2="${W - pad}" y1="${y}" y2="${y}" class="dc-base"/>`;
    for (let h = 0; h <= 24; h += 3) {
      const xx = x(h * 60);
      s += `<line x1="${xx}" x2="${xx}" y1="${y - 3}" y2="${y + 3}" class="dc-base"/>`;
      if (h % 6 === 0) s += `<text x="${xx}" y="${y + 18}" class="dc-lab ${h === 0 ? "" : h === 24 ? "end" : "mid"}">${esc(hm(h * 60 % 1440))}</text>`;
    }
    const label = (m, txt, above) => {
      const xx = x(m);
      const anchor = xx < 70 ? "start" : xx > W - 70 ? "end" : "middle";
      return `<text x="${xx}" y="${above ? y - 14 : y + 36}" class="dc-mark-lab" text-anchor="${anchor}">${esc(txt)}</text>`;
    };
    stops.forEach((p, i) => {
      s += `<circle cx="${x(p.minute)}" cy="${y}" r="${5 - i}" class="dc-dot"/>`;
      if (i === 0) s += label(p.minute, `${hm(p.minute)} · ${t("data.chg.times", { n: p.sessions })}`, true);
    });
    starts.forEach((p, i) => {
      s += `<circle cx="${x(p.minute)}" cy="${y}" r="${5 - i}" fill="var(--dh-ground)" stroke="var(--dh-data)" stroke-width="1.5"/>`;
      if (i === 0) s += label(p.minute, `${hm(p.minute)} · ${t("data.chg.times", { n: p.sessions })}`, false);
    });
    svg.innerHTML = s;
    el.innerHTML = "";
    el.appendChild(svg);
  }

  /* --------------------------------------------- how full: start and end */
  function socBlock(id, field, subKey) {
    DH.block(id, {
      load: loadCharges,
      render(ch, body, ctx) {
        const vals = (ch.charges || []).map((c) => c[field]).filter((v) => v != null);
        const bins = Array.from({ length: 10 }, (_, i) => ({ key: i, x0: i * 10, x1: i * 10 + 10, v: 0 }));
        vals.forEach((v) => { bins[Math.min(9, Math.floor(v / 10))].v++; });
        DH.shell(body, "c", `<p class="dh-subn"></p><div class="dh-chart short"></div>`);
        const sorted = vals.slice().sort((a, b) => a - b);
        const med = sorted.length ? sorted[Math.floor((sorted.length - 1) / 2)] : null;
        $(".dh-subn", body).textContent = med != null ? t(subKey, { pct: DH.nf(med, 0), n: DH.nf(vals.length, 0) }) : " ";
        const lfp = ch.car && ch.car.lfp_battery;
        DC.hist($(".dh-chart", body), {
          bins, xMin: 0, xMax: 100, integer: true, morph: ctx.reload, label: $("h3", body.parentElement).textContent,
          xFmt: (v) => `${DH.nf(v, 0)}%`, empty: t("data.chg.none"),
          marks: field === "end_soc" && !lfp ? [{ x: 80, label: t("data.chg.daily_limit") }] : [],
          tip: (i, b) => `<b>${esc(DH.nf(b.x0, 0))}–${esc(DH.nf(b.x1, 0))}%</b>${esc(t(b.v === 1 ? "data.chg.n_sessions_one" : "data.chg.n_sessions", { n: b.v }))}`,
        });
      },
    });
  }
  socBlock("cg-soc-start", "start_soc", "data.chg.soc_start_sub");
  socBlock("cg-soc-end", "end_soc", "data.chg.soc_end_sub");
})();
