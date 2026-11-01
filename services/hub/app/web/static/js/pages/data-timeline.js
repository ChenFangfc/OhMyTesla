/* 数据 · 时间线: what happened, and when? */
(function () {
  "use strict";
  const $ = (s, r) => (r || document).querySelector(s);
  const loadSummary = () => DH.get(DH.url("/api/insights/summary"));
  const loadTimeline = () => DH.get(DH.url("/api/insights/timeline", { days: DH.days(), limit: 600 }));
  let kind = "", bucket = "month", shown = 60, want = /^#day-\d{4}-\d{2}-\d{2}$/.test(location.hash) ? location.hash.slice(5) : null;

  /* ------------------------------------------------------------------ KPIs */
  DH.block("tl-sum", {
    load: async () => {
      const mi = await DH.get(DH.url("/api/analytics/mileage", { days: DH.days(), bucket: "month" }));
      const [s, tl] = await Promise.all([loadSummary(), loadTimeline()]);
      const sinceFirst = s.first_record ? Math.ceil((Date.now() - new Date(s.first_record)) / 864e5) + 1 : 365;
      const cal = await DH.get(DH.url("/api/insights/calendar", { days: DH.period === "all" ? sinceFirst : DH.days() }));
      return { mi, tl, cal };
    },
    render({ mi, tl, cal }, body) {
      const tot = tl.totals || {};
      const events = (tot.drives || 0) + (tot.charges || 0);
      const cov = mi.total_km > 0 ? (mi.logged_km / mi.total_km) * 100 : null;
      const items = [
        [t("data.tl.active_days"), DH.vu(cal.active_days || 0, t("data.tl.days_u"), 0), ""],
        [t("data.k.distance"), DH.vu(DH.cv.dist(cal.total_km), DH.u.dist(), 0), ""],
        [t("data.tl.events_n"), DH.vu(events, "", 0), esc(t("data.tl.split", { d: DH.nf(tot.drives || 0, 0), c: DH.nf(tot.charges || 0, 0) }))],
        [t("data.tl.coverage"), cov == null ? '<span class="dash">–</span>' : DH.vu(Math.min(100, cov), "%", 0),
          cov != null && mi.unlogged_km > 1 ? `<span class="${cov < 90 ? "bad" : "flat"}">${esc(t("data.tl.unlogged", { v: `${DH.nf(DH.cv.dist(mi.unlogged_km), 0)} ${DH.u.dist()}` }))}</span>` : esc(t("data.tl.cov_ok"))],
      ];
      body.innerHTML = `<div class="dh-kpis" style="--cols:4">${items.map(([l, v, s], i) => DH.kpi(i, l, v, s)).join("")}</div>`;
    },
  });

  /* ------------------------------------------------------- the calendar */
  DH.block("tl-cal", {
    load: async () => {
      const s = await loadSummary();
      const sinceFirst = s.first_record ? Math.ceil((Date.now() - new Date(s.first_record)) / 864e5) + 1 : 365;
      const days = DH.period === "all" ? Math.max(365, sinceFirst) : Math.max(365, DH.days());
      await DH.get(DH.url("/api/analytics/mileage", { days: DH.days(), bucket: "month" }));   // hub time zone
      return { s, cal: await DH.get(DH.url("/api/insights/calendar", { days })), days };
    },
    render(d, body) {
      const today = DH.parts(Date.now()).key;
      const du = DH.u.dist();
      DH.shell(body, "cal", `<div class="dh-tscroll"><div class="dh-chart short"></div></div>`);
      DC.calendar($(".dh-chart", body), {
        days: d.cal.days || [], start: DH.addDays(today, -(d.days - 1)), end: today, inFrom: DH.periodStartKey(d.s.first_record),
        value: (x) => (x.km || 0) + (x.charges ? 0.01 : 0), label: t("data.tl.cal"),
        dayLabels: [0, 1, 2, 3, 4, 5, 6].map((i) => DH.fdate(`2026-01-${String(5 + i).padStart(2, "0")}`, { weekday: "short" })),
        tip: (key, x) => `<b>${esc(DH.fdate(key, { weekday: "short", month: "short", day: "numeric" }))}</b>` + (x
          ? `<div class="r"><span>${esc(t("data.k.drives"))}</span><span class="v">${esc(DH.nf(x.drives, 0))} · ${esc(DH.nf(DH.cv.dist(x.km), 1))} ${esc(du)}</span></div>`
            + (x.charges ? `<div class="r"><span>${esc(t("data.tl.k_charge"))}</span><span class="v">${esc(DH.nf(x.charges, 0))} · ${esc(DH.nf(x.kwh, 1))} kWh</span></div>` : "")
          : `<div>${esc(t("data.ov.cal_idle"))}</div>`),
        onDay: (key) => jump(key),
      });
    },
  });

  /* ------------------------------------------------------------ events */
  const ICON = { drive: "drive", charge: "bolt", update: "spark", idle: "pin", gap: "route" };
  function line(e) {
    const du = DH.u.dist();
    if (e.kind === "drive") {
      return [e.distance != null ? `${DH.nf(DH.cv.dist(e.distance), 1)} ${du}` : "", e.duration_min != null ? DH.dur(e.duration_min) : "",
              e.start_soc != null && e.end_soc != null ? `${e.start_soc}% → ${e.end_soc}%` : ""].filter(Boolean).join(" · ");
    }
    if (e.kind === "charge") {
      return [e.energy != null ? `${DH.nf(e.energy, 1)} kWh` : "", e.start_soc != null && e.end_soc != null ? `${e.start_soc}% → ${e.end_soc}%` : "",
              e.duration_min != null ? DH.dur(e.duration_min) : "", e.cost ? DH.money(e.cost) : ""].filter(Boolean).join(" · ");
    }
    if (e.kind === "gap") {
      const bits = [DH.dur(e.duration_min), t("timeline.gap_km", { km: `${DH.nf(DH.cv.dist(e.unlogged_km), 1)} ${du}` })];
      if (e.start_soc != null && e.end_soc != null && e.start_soc > e.end_soc) bits.push(t("timeline.gap_soc", { pct: e.start_soc - e.end_soc }));
      return bits.join(" · ");
    }
    if (e.kind === "idle") {
      const bits = [DH.dur(e.duration_min)];
      if (e.start_soc != null && e.end_soc != null && e.start_soc > e.end_soc) bits.push(t("timeline.idle_drop", { pct: e.start_soc - e.end_soc }));
      return bits.join(" · ");
    }
    return t("data.tl.software");
  }
  function title(e) {
    if (e.kind === "drive") return e.from && e.to && e.from !== e.to ? `${e.from} → ${e.to}` : e.to || e.from || t("data.tl.a_drive");
    if (e.kind === "charge") return e.to || t("data.tl.a_charge");
    if (e.kind === "gap") return e.from ? t("timeline.gap_at", { place: e.from }) : t("timeline.k_gap");
    if (e.kind === "idle") return e.from ? t("timeline.idle_at", { place: e.from }) : t("timeline.k_idle");
    return t("timeline.update_to", { version: e.version || "" });
  }

  function paintEvents(tl, body) {
    const all = tl.events || [];
    const list = all.filter((e) => !kind || e.kind === kind);
    const n = $("#tl-events-n");
    const real = all.filter((e) => e.kind === "drive" || e.kind === "charge").length;
    n.textContent = tl.truncated
      ? t("data.tl.truncated", { shown: DH.nf(real, 0), total: DH.nf(tl.total || real, 0) })
      : t("data.tl.events_sub", { n: DH.nf(list.length, 0) });
    if (!list.length) { body.innerHTML = DH.empty(t(kind ? "data.tl.none_kind" : "data.tl.none")); return; }
    // a jump target further down the list must be rendered to be reachable
    if (want) {
      const at = list.findIndex((e) => DH.parts(e.start).key <= want);
      if (at >= shown) shown = at + 40;
    }
    let lastDay = "";
    const p = DH.period;
    const page = list.slice(0, shown);
    body.innerHTML = `<ol class="tl-evts">${page.map((e) => {
      const day = DH.parts(e.start).key;
      const head = day !== lastDay ? `<li class="tl-day" id="day-${day}">${esc(DH.fdate(day, { weekday: "short", year: "numeric", month: "short", day: "numeric" }))}</li>` : "";
      lastDay = day;
      const href = e.kind === "drive" && e.id ? `/data/driving?days=${p}&drive=${e.id}`
        : e.kind === "charge" && e.id ? `/data/charging?days=${p}&charge=${e.id}` : null;
      const inner = `<span class="tm">${esc(DH.time(e.start))}</span><span class="ic">${icon(ICON[e.kind] || "pin", 15)}</span>
        <span class="bd"><span class="tt">${esc(title(e))}</span><span class="sb">${esc(line(e))}</span></span>
        <span class="go">${href ? icon("chevron", 14) : ""}</span>`;
      return head + `<li class="tl-row k-${e.kind}">${href ? `<a href="${href}">${inner}</a>` : `<div>${inner}</div>`}</li>`;
    }).join("")}</ol>${list.length > page.length ? `<div class="dh-tfoot"><span>${esc(t("data.drv.showing", { a: 1, b: DH.nf(page.length, 0), n: DH.nf(list.length, 0) }))}</span>
      <button type="button" class="dh-btn quiet" id="tl-more">${esc(t("data.tl.show_more", { n: Math.min(60, list.length - page.length) }))}</button></div>` : ""}`;
    const more = body.querySelector("#tl-more");
    if (more) more.addEventListener("click", () => { shown += 60; paintEvents(tl, body); });
    if (want) { const w = want; want = null; requestAnimationFrame(() => jump(w)); }
  }

  /* Native smooth scroll to the day; the day's header lights up briefly.
     A day without events says so instead of jumping somewhere else. */
  function jump(key) {
    const el = document.getElementById("day-" + key);
    const note = $("#tl-events-n");
    if (!el) {
      note.textContent = t("data.tl.no_day", { date: DH.fdate(key, { month: "short", day: "numeric", year: "numeric" }) });
      document.getElementById("tl-events").scrollIntoView({ block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
      return;
    }
    const top = el.getBoundingClientRect().top + scrollY - (parseFloat(getComputedStyle(DH.root).getPropertyValue("--dh-top")) || 0) - 56;
    scrollTo({ top, behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    el.classList.add("flash");
    setTimeout(() => el.classList.remove("flash"), 1600);
    history.replaceState(history.state, "", `#day-${key}`);
  }

  const evBlock = DH.block("tl-events", {
    load: loadTimeline,
    render(tl, body) { paintEvents(tl, body); },
  });
  document.querySelectorAll("#tl-kind [data-kind]").forEach((b) => b.addEventListener("click", () => {
    kind = b.dataset.kind;
    document.querySelectorAll("#tl-kind [data-kind]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    if (evBlock && evBlock.data) paintEvents(evBlock.data, evBlock.body);   // client-side: no refetch
  }));

  /* ------------------------------------------------------ period totals */
  const statBlock = DH.block("tl-stats", {
    load: () => DH.get(DH.url("/api/analytics/statistics", { days: DH.days(), period: bucket })),
    render(st, body) {
      const rows = (st.rows || []).filter((r) => r.drives || r.charges);
      if (!rows.length) { body.innerHTML = DH.empty(t("data.c.none_period")); return; }
      const du = DH.u.dist(), cu = DH.u.cons();
      const label = (r) => (st.bucket === "year" ? r.date.slice(0, 4) : st.bucket === "month" ? DH.monthLabel(r.date, true) : t("data.tl.week_of", { date: DH.dateShort(r.date) }));
      body.innerHTML = `<div class="dh-tscroll"><table class="dh-table"><thead><tr>
        <th>${esc(t("data.tl.th_period"))}</th><th class="num">${esc(t("data.k.drives"))}</th><th class="num">${esc(t("data.k.distance"))} <span class="faint">${esc(du)}</span></th>
        <th class="num dh-hide-sm">${esc(t("data.k.time_driving"))}</th><th class="num dh-hide-sm">${esc(t("data.k.energy_used"))} <span class="faint">kWh</span></th>
        <th class="num">${esc(t("data.k.consumption"))} <span class="faint">${esc(cu)}</span></th><th class="num dh-hide-sm">${esc(t("data.k.sessions"))}</th>
        <th class="num">${esc(t("data.k.charged"))} <span class="faint">kWh</span></th><th class="num">${esc(t("data.k.cost"))}</th></tr></thead><tbody>
        ${rows.map((r) => `<tr><td class="nowrap">${esc(label(r))}</td><td class="num">${DH.n(r.drives, 0)}</td>
          <td class="num">${r.logged_km ? DH.n(DH.cv.dist(r.logged_km), 0) : '<span class="dash">–</span>'}</td>
          <td class="num dh-hide-sm">${r.duration_min ? esc(DH.dur(r.duration_min)) : '<span class="dash">–</span>'}</td>
          <td class="num dh-hide-sm">${DH.n(r.net_kwh, 1)}</td><td class="num">${DH.n(DH.cv.cons(r.consumption_net_wh_per_km), 0)}</td>
          <td class="num dh-hide-sm">${DH.n(r.charges, 0)}</td><td class="num">${DH.n(r.energy_added_kwh, 1)}</td>
          <td class="num">${r.cost ? esc(DH.money(r.cost)) : '<span class="dash">–</span>'}</td></tr>`).join("")}
        </tbody></table></div>`;
    },
  });
  document.querySelectorAll("#tl-bucket [data-bucket]").forEach((b) => b.addEventListener("click", () => {
    bucket = b.dataset.bucket;
    document.querySelectorAll("#tl-bucket [data-bucket]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    if (statBlock) statBlock.run(false);
  }));
})();
