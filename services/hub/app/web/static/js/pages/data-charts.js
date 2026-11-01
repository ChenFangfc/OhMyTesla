/* 数据 hub charts. Quiet SVG marks for the record layer.
 *
 * Rules, all from the design brief:
 *  · one series and one hue per chart; 2px straight lines, never splines;
 *  · 3–5 faint gridlines, no y-axis line, axis labels drawn to scale;
 *  · dashed reference lines (rated, "when new"), annotated events (firmware);
 *  · legends whose swatch is the mark itself;
 *  · data doesn't dance: no draw-in on every visit. A chart morphs when the
 *    period changes, and gets one clip reveal on the first hub visit of a
 *    session (DC.revealUntil), nothing else.
 *
 * Colours come from CSS classes (data.css), so both themes and any token
 * change upstream apply without touching this file. Written from scratch for
 * this repo; charts.js stays as it is for the pages that still use it.
 */
(function () {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* cubic-bezier(.77,0,.175,1): the "move" curve. Solved numerically so the
     JS tweens use exactly the curve CSS uses everywhere else. */
  function bezier(p1x, p1y, p2x, p2y) {
    const cx = 3 * p1x, bx = 3 * (p2x - p1x) - cx, ax = 1 - cx - bx;
    const cy = 3 * p1y, by = 3 * (p2y - p1y) - cy, ay = 1 - cy - by;
    const sx = (t) => ((ax * t + bx) * t + cx) * t;
    const sy = (t) => ((ay * t + by) * t + cy) * t;
    const dx = (t) => (3 * ax * t + 2 * bx) * t + cx;
    return (x) => {
      if (x <= 0) return 0;
      if (x >= 1) return 1;
      let t = x;
      for (let i = 0; i < 8; i++) {
        const d = sx(t) - x;
        if (Math.abs(d) < 1e-5) break;
        const s = dx(t);
        if (Math.abs(s) < 1e-6) break;
        t -= d / s;
      }
      return sy(Math.min(1, Math.max(0, t)));
    };
  }
  const MOVE = bezier(0.77, 0, 0.175, 1);

  function E(tag, attrs, parent) {
    const n = document.createElementNS(NS, tag);
    if (attrs) for (const k in attrs) if (attrs[k] != null && attrs[k] !== false) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }
  function T(parent, x, y, text, cls) {
    const n = E("text", { x: r1(x), y: r1(y), class: cls }, parent);
    n.textContent = text;
    return n;
  }
  const r1 = (v) => Math.round(v * 10) / 10;
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  let mctx = null;
  function tw(s, px) {
    if (!mctx) {
      mctx = document.createElement("canvas").getContext("2d");
    }
    mctx.font = `${px || 11}px -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif`;
    return mctx.measureText(String(s)).width;
  }

  /* Round ticks above the maximum: 3–5 gridlines including the baseline. */
  function ticks(lo, hi, opts) {
    const o = opts || {};
    if (!isFinite(lo) || !isFinite(hi)) { lo = 0; hi = 1; }
    if (hi < lo) [lo, hi] = [hi, lo];
    if (hi === lo) { const d = Math.abs(hi) || 1; lo -= o.zero ? 0 : d * 0.1; hi += d * 0.1; }
    const target = o.target || 4;
    const nice = [1, 2, 2.5, 5, 10];
    let raw = (hi - lo) / target;
    let mag = 10 ** Math.floor(Math.log10(raw));
    let step = mag * (nice.find((m) => m * mag >= raw - 1e-12) || 10);
    let a = Math.floor(lo / step + 1e-9) * step, b = Math.ceil(hi / step - 1e-9) * step;
    let guard = 0;
    while ((b - a) / step > 5 && guard++ < 6) {
      const i = nice.findIndex((m) => Math.abs(m * mag - step) < 1e-9 * mag);
      if (i >= 0 && i < nice.length - 1) step = nice[i + 1] * mag; else { mag *= 10; step = mag; }
      a = Math.floor(lo / step + 1e-9) * step; b = Math.ceil(hi / step - 1e-9) * step;
    }
    if (o.integer && step % 1) {
      step = Math.max(1, Math.ceil(step));
      if (step === 3) step = 4;
      a = Math.floor(lo / step) * step; b = Math.ceil(hi / step) * step;
    }
    const out = [];
    for (let v = a; v <= b + step * 1e-6; v += step) out.push(+v.toFixed(10));
    if (out.length < 3) out.push(+(out[out.length - 1] + step).toFixed(10));
    return out;
  }

  const DAY = 864e5;
  /* Time ticks drawn to scale: midnights / week starts / months / years. */
  function timeTicks(t0, t1, maxN) {
    const span = t1 - t0, n = maxN || 6;
    const loc = typeof localeTag === "function" ? localeTag() : undefined;
    const steps = [
      [DAY, "day"], [2 * DAY, "day"], [7 * DAY, "week"], [14 * DAY, "week"],
      [30.4 * DAY, "month", 1], [61 * DAY, "month", 2], [91 * DAY, "month", 3],
      [182 * DAY, "month", 6], [365 * DAY, "year", 12], [730 * DAY, "year", 24],
    ];
    const pick = steps.find(([ms]) => span / ms <= n) || steps[steps.length - 1];
    const [ms, kind, months] = pick;
    const out = [];
    const d = new Date(t0);
    if (kind === "day" || kind === "week") {
      d.setHours(0, 0, 0, 0);
      if (kind === "week") { const wd = (d.getDay() + 6) % 7; if (wd) d.setDate(d.getDate() + 7 - wd); }
      else if (d.getTime() < t0) d.setDate(d.getDate() + 1);
      const stepDays = Math.round(ms / DAY);
      for (; d.getTime() <= t1; d.setDate(d.getDate() + stepDays)) out.push(d.getTime());
    } else {
      d.setHours(0, 0, 0, 0); d.setDate(1);
      if (d.getTime() < t0) d.setMonth(d.getMonth() + 1);
      while (d.getMonth() % months) d.setMonth(d.getMonth() + 1);
      for (; d.getTime() <= t1; d.setMonth(d.getMonth() + months)) out.push(d.getTime());
    }
    const yearOf = (t) => new Date(t).getFullYear();
    const multiYear = yearOf(t0) !== yearOf(t1);
    const fmtT = (t, i) => {
      const dt = new Date(t);
      if (kind === "year") return String(dt.getFullYear());
      if (kind === "month") {
        const showYear = multiYear && (i === 0 || dt.getMonth() === 0);
        return dt.toLocaleDateString(loc, showYear ? { month: "short", year: "numeric" } : { month: "short" });
      }
      return dt.toLocaleDateString(loc, { month: "short", day: "numeric" });
    };
    return out.map((t, i) => ({ v: t, label: fmtT(t, i) }));
  }

  /* ------------------------------------------------------------ state */

  const DC = {
    revealUntil: 0,          // set by data.js on the first hub visit of a session
    ticks, timeTicks, esc, tw,
  };

  const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver((entries) => {
    for (const e of entries) {
      const st = e.target.__dc;
      if (!st || !st.redraw) continue;
      const w = Math.round(e.contentRect.width);
      if (w > 0 && w !== st.w) {
        clearTimeout(st.rt);
        st.rt = setTimeout(() => st.redraw(), 90);
      }
    }
  }) : null;

  function mount(el, kind, opts, drawFn) {
    const prev = el.__dc || null;
    const st = { kind, w: el.clientWidth, redraw: null, geo: null, tok: {} };
    st.redraw = () => {
      st.w = el.clientWidth;
      const keep = el.__dc;
      el.__dc = st;
      drawFn(el, opts, { morphFrom: null, reveal: false, st, prevGeo: keep && keep.geo });
    };
    el.__dc = st;
    if (ro && !el.__dcObserved) { ro.observe(el); el.__dcObserved = true; }
    if (!el.clientWidth) {
      // Not laid out yet (hidden tab, collapsed details). Draw once it has a width.
      requestAnimationFrame(() => { if (el.__dc === st && el.clientWidth) st.redraw(); });
      return;
    }
    const canMorph = opts.morph && prev && prev.kind === kind && prev.geo && !reduced();
    const reveal = !opts.morph && performance.now() < DC.revealUntil;
    drawFn(el, opts, { morphFrom: canMorph ? prev.geo : null, reveal, st, prevGeo: prev && prev.geo });
  }

  /* Replace the svg. When the old picture can't morph into the new one
     (different buckets), cross-fade instead of redrawing from zero. */
  function place(el, svg, ctx, morphed) {
    const old = el.querySelector(":scope > svg");
    el.querySelectorAll(":scope > .dc-empty-msg").forEach((n) => n.remove());
    if (old && ctx.prevGeo && !morphed && !ctx.reveal && el.__dcFade !== false && !reduced() && ctx.fade) {
      old.style.position = "absolute"; old.style.inset = "0"; old.style.pointerEvents = "none";
      el.insertBefore(svg, old);
      svg.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 200, easing: "linear" });
      const a = old.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 160, easing: "linear", fill: "forwards" });
      a.onfinish = () => old.remove();
    } else {
      if (old) old.remove();
      el.prepend(svg);
    }
    if (ctx.reveal) {
      const marks = svg.querySelector(".dc-marks");
      if (marks && marks.animate) {
        marks.animate([{ clipPath: "inset(0 100% 0 0)" }, { clipPath: "inset(0 0 0 0)" }],
          { duration: reduced() ? 1 : 500, easing: "cubic-bezier(.77,0,.175,1)" });
      }
    }
  }

  function tween(st, dur, step) {
    const tok = {};
    st.tok = tok;
    const t0 = performance.now();
    const f = (now) => {
      if (st.tok !== tok) return;
      const p = Math.min(1, (now - t0) / dur);
      step(MOVE(p));
      if (p < 1) requestAnimationFrame(f);
    };
    requestAnimationFrame(f);
  }

  /* ----------------------------------------------------------- tooltip */

  function tipFor(el) {
    let tip = el.querySelector(":scope > .dc-tip");
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "dc-tip";
      tip.setAttribute("role", "status");
      tip.setAttribute("aria-live", "polite");
      el.appendChild(tip);
    }
    return {
      show(html, x, y) {
        tip.innerHTML = html;
        tip.classList.add("on");
        const w = el.clientWidth, tw2 = tip.offsetWidth, th = tip.offsetHeight;
        let left = x + 12;
        if (left + tw2 > w) left = Math.max(0, x - tw2 - 12);
        const top = Math.max(-th - 4, y - th - 10);
        tip.style.transform = `translate(${Math.round(left)}px, ${Math.round(top)}px)`;
      },
      hide() { tip.classList.remove("on"); },
    };
  }

  /* Pointer + keyboard for column-like charts: hover/tap a column, or focus
     the chart and use ←/→. idxAt(x) maps a pointer x to an index. */
  function interact(el, svg, n, idxAt, onIdx, onLeave) {
    let cur = -1;
    const set = (i, px, py) => { cur = i; onIdx(i, px, py); };
    svg.addEventListener("pointermove", (e) => {
      if (e.pointerType === "touch") return;
      const r = svg.getBoundingClientRect();
      const i = idxAt(e.clientX - r.left);
      if (i == null || i < 0 || i >= n) { onLeave(); cur = -1; return; }
      set(i, e.clientX - r.left, e.clientY - r.top);
    });
    svg.addEventListener("pointerdown", (e) => {
      if (e.pointerType !== "touch") return;
      const r = svg.getBoundingClientRect();
      const i = idxAt(e.clientX - r.left);
      if (i == null || i < 0 || i >= n) { onLeave(); return; }
      set(i, e.clientX - r.left, e.clientY - r.top);
    });
    svg.addEventListener("pointerleave", (e) => { if (e.pointerType !== "touch") { onLeave(); cur = -1; } });
    if (!el.__dcKeys) {
      el.__dcKeys = true;
      el.tabIndex = 0;
      el.addEventListener("keydown", (e) => {
        const h = el.__dcKey;
        if (h) h(e);
      });
      el.addEventListener("blur", () => { const h = el.__dcBlur; if (h) h(); });
    }
    el.__dcKey = (e) => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft" && e.key !== "Escape" && e.key !== "Home" && e.key !== "End") return;
      e.preventDefault();
      if (e.key === "Escape") { onLeave(); cur = -1; return; }
      if (e.key === "Home") cur = 0;
      else if (e.key === "End") cur = n - 1;
      else cur = Math.max(0, Math.min(n - 1, (cur < 0 ? (e.key === "ArrowRight" ? -1 : n) : cur) + (e.key === "ArrowRight" ? 1 : -1)));
      onIdx(cur, null, null);
    };
    el.__dcBlur = () => { onLeave(); cur = -1; };
  }

  function emptyMsg(el, text) {
    if (!text) return;
    const m = document.createElement("div");
    m.className = "dc-empty-msg";
    m.style.cssText = "position:absolute;left:0;right:0;top:38%;text-align:center;font-size:13px;color:var(--dh-ink-3)";
    m.textContent = text;
    el.appendChild(m);
  }

  /* Frame: y gutter sized to the widest label, unit caption on top. */
  function frame(el, o, yLabels) {
    const W = Math.max(160, el.clientWidth);
    const H = Math.max(80, el.clientHeight || o.height || 200);
    const gut = o.yAxis === false ? 0 : Math.ceil(Math.max(0, ...yLabels.map((s) => tw(s)))) + 8;
    const top = (o.unit || (o.marks && o.marks.length)) ? 20 : 8;
    const pad = { l: gut, r: o.padR != null ? o.padR : 6, t: top, b: o.xAxis === false ? 4 : 22 };
    const svg = E("svg", {
      viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img",
      "aria-label": o.label || null, preserveAspectRatio: "none",
    });
    return { svg, W, H, pad, iw: Math.max(10, W - pad.l - pad.r), ih: Math.max(10, H - pad.t - pad.b) };
  }

  function yAxis(f, yt, sy, o, base) {
    const g = E("g", { class: "dc-axis" }, f.svg);
    const fmt = o.yFmt || ((v) => fmtNum(v));
    yt.forEach((v, i) => {
      const y = Math.round(sy(v)) + 0.5;
      const isBase = base != null ? v === base : i === 0;
      E("line", { x1: f.pad.l, x2: f.W - f.pad.r, y1: y, y2: y, class: isBase ? "dc-base" : "dc-grid" }, g);
      if (o.yAxis !== false) T(g, f.pad.l - 8, y + 4, fmt(v), "dc-lab end");
    });
    if (o.unit) T(g, 0, 11, o.unit, "dc-unit");
    return g;
  }

  function fmtNum(v) {
    const loc = typeof localeTag === "function" ? localeTag() : undefined;
    const a = Math.abs(v);
    if (a >= 10000) return new Intl.NumberFormat(loc, { notation: "compact", maximumFractionDigits: 1 }).format(v);
    return new Intl.NumberFormat(loc, { maximumFractionDigits: a < 10 && a % 1 ? 1 : 0 }).format(v);
  }
  DC.fmtNum = fmtNum;

  function refsAndMarks(f, o, sx, sy, g) {
    (o.refs || []).forEach((r) => {
      if (r.y == null || !isFinite(r.y)) return;
      const y = Math.round(sy(r.y)) + 0.5;
      E("line", { x1: f.pad.l, x2: f.W - f.pad.r, y1: y, y2: y, class: "dc-ref" }, g);
      if (r.label) {
        const t = T(g, f.W - f.pad.r, y - 5, r.label, "dc-ref-lab");
        t.setAttribute("text-anchor", "end");
      }
    });
    let lastX = -Infinity;
    (o.marks || []).forEach((m) => {
      const x = sx(m.x);
      if (x == null || !isFinite(x) || x < f.pad.l - 1 || x > f.W - f.pad.r + 1) return;
      const xx = Math.round(x) + 0.5;
      E("line", { x1: xx, x2: xx, y1: f.pad.t - 4, y2: f.pad.t + f.ih, class: "dc-mark" }, g);
      if (m.label) {
        const w = tw(m.label);
        if (xx - lastX > w + 8 || xx - lastX > 120) {
          const right = xx + w + 4 > f.W;
          const t = T(g, right ? xx - 4 : xx + 4, f.pad.t - 8, m.label, "dc-mark-lab");
          if (right) t.setAttribute("text-anchor", "end");
          lastX = xx + (right ? 0 : w);
        }
      }
    });
  }

  /* ------------------------------------------------------------- bars
     Categorical buckets (days / weeks / months). values = solid segment;
     alt = a second solid segment in the lighter step of the same hue (DC
     on top of AC); extra = a hollow segment on top (odometer distance that
     wasn't logged). Each segment keeps its key so a period change morphs. */
  function bars(el, o) { mount(el, "bars", o, drawBars); }
  function drawBars(el, o, ctx) {
    const n = (o.values || []).length;
    const segs = [["v", o.values, "dc-bar"], ["a", o.alt, "dc-bar alt"], ["x", o.extra, "dc-bar hollow"]]
      .filter(([, arr]) => arr && arr.length);
    const tot = Array.from({ length: n }, (_, i) => segs.reduce((s, [, arr]) => s + Math.max(0, +arr[i] || 0), 0));
    const maxV = Math.max(0, ...tot, ...(o.refs || []).map((r) => r.y || 0), o.yMax || 0);
    const yt = ticks(0, maxV > 0 ? maxV : 1, { zero: true, integer: o.integer });
    const fmt = o.yFmt || fmtNum;
    const none = !(maxV > 0);
    const f = frame(el, o, (none ? [0] : yt).map(fmt));
    const sy = (v) => f.pad.t + f.ih - (v / yt[yt.length - 1]) * f.ih;
    yAxis(f, none ? [0] : yt, sy, o, 0);
    const slot = f.iw / Math.max(1, n);
    const bw = Math.max(1.5, Math.min(o.maxBar || 28, slot * (o.fill || 0.62)));
    const cx = (i) => f.pad.l + slot * (i + 0.5);
    const hov = E("rect", { class: "dc-col", y: f.pad.t, height: f.ih, width: Math.max(2, slot), x: -100 }, f.svg);
    const marks = E("g", { class: "dc-marks" }, f.svg);
    const geo = new Map();
    const rects = [];
    const base = sy(0);
    for (let i = 0; i < n; i++) {
      let acc = 0;
      const key = o.keys ? o.keys[i] : i;
      const g = {};
      for (const [sk, arr, cls] of segs) {
        const v = Math.max(0, +arr[i] || 0);
        const y0 = sy(acc), y1 = sy(acc + v);
        const h = Math.max(0, y0 - y1 - (acc > 0 && v > 0 ? 1 : 0));
        acc += v;
        g[sk] = { y: y1, h };
        if (v <= 0) continue;
        const hollow = sk === "x";
        const extraCls = o.hi === i ? " hi" : "";
        const r = E("rect", {
          x: r1(cx(i) - bw / 2 + (hollow ? 0.5 : 0)), width: r1(bw - (hollow ? 1 : 0)),
          y: r1(y1 + (hollow ? 0.5 : 0)), height: r1(Math.max(hollow ? 0 : 0.5, h - (hollow ? 1 : 0))),
          class: cls + (sk === "v" ? extraCls : ""), rx: 1,
        }, marks);
        rects.push({ r, key, sk, y: y1, h, hollow });
      }
      geo.set(key, g);
    }
    if (!n || !tot.some((v) => v > 0)) emptyMsg(el, o.empty);
    refsAndMarks(f, o, (k) => {
      const i = typeof k === "number" && !o.keys ? k : (o.keys || []).indexOf(k);
      return i >= 0 ? cx(i) : null;
    }, sy, E("g", {}, f.svg));
    // x labels, thinned to fit, the newest bucket always labelled
    if (o.xAxis !== false && o.labels) {
      const lw = Math.max(...o.labels.map((s) => tw(s || ""))) + 10;
      const every = Math.max(1, Math.ceil(lw / slot));
      for (let i = n - 1; i >= 0; i -= every) {
        const x = cx(i);
        const t = T(f.svg, x, f.H - 6, o.labels[i] || "", "dc-lab mid");
        const w = tw(o.labels[i] || "");
        if (x + w / 2 > f.W) { t.setAttribute("x", f.W); t.setAttribute("class", "dc-lab end"); }
        if (x - w / 2 < f.pad.l - 4 && i !== n - 1) t.remove();
      }
    }
    // interaction
    const tip = tipFor(el);
    interact(el, f.svg, n, (x) => Math.floor((x * f.W / el.clientWidth - f.pad.l) / slot), (i, px, py) => {
      hov.setAttribute("x", r1(f.pad.l + slot * i));
      hov.classList.add("on");
      const html = o.tip ? o.tip(i) : `<b>${esc(o.labels ? o.labels[i] : i)}</b>${esc(fmt(tot[i]))}`;
      const x = px != null ? px : cx(i) * el.clientWidth / f.W;
      const y = py != null ? py : sy(tot[i]) * el.clientHeight / f.H;
      tip.show(html, x, y);
    }, () => { hov.classList.remove("on"); tip.hide(); });
    if (o.onPick) {
      f.svg.style.cursor = "pointer";
      f.svg.addEventListener("click", (e) => {
        const r = f.svg.getBoundingClientRect();
        const i = Math.floor(((e.clientX - r.left) * f.W / r.width - f.pad.l) / slot);
        if (i >= 0 && i < n) o.onPick(i);
      });
    }
    ctx.st.geo = { kind: "bars", map: geo, base };
    // morph from the previous period when most buckets carry over
    let morphed = false;
    if (ctx.morphFrom && ctx.morphFrom.kind === "bars") {
      const old = ctx.morphFrom.map;
      const shared = [...geo.keys()].filter((k) => old.has(k)).length;
      if (shared >= Math.min(geo.size, old.size) * 0.5 && shared > 0) {
        morphed = true;
        const from = rects.map((x) => {
          const og = old.get(x.key);
          const s = og && og[x.sk];
          return s ? { y: s.y, h: s.h } : { y: base, h: 0 };
        });
        rects.forEach((x, k) => { x.r.setAttribute("y", r1(from[k].y)); x.r.setAttribute("height", r1(from[k].h)); });
        tween(ctx.st, 260, (p) => {
          rects.forEach((x, k) => {
            const y = from[k].y + (x.y - from[k].y) * p, h = from[k].h + (x.h - from[k].h) * p;
            x.r.setAttribute("y", r1(y + (x.hollow ? 0.5 : 0)));
            x.r.setAttribute("height", r1(Math.max(0, h - (x.hollow ? 1 : 0))));
          });
        });
      }
    }
    place(el, f.svg, { ...ctx, fade: !!ctx.morphFrom }, morphed);
  }

  /* ------------------------------------------------------------- hist
     Continuous x: each bin spans [x0, x1] on a numeric or time axis, so bin
     widths are honest (a parked-drain window that lasted two days is twice
     as wide). Bins with too little data are drawn hollow. */
  function hist(el, o) { mount(el, "hist", o, drawHist); }
  function drawHist(el, o, ctx) {
    const bins = (o.bins || []).filter((b) => b && isFinite(b.x0) && isFinite(b.x1));
    const maxV = Math.max(0, ...bins.map((b) => +b.v || 0), ...(o.refs || []).map((r) => r.y || 0), o.yMax || 0);
    const yt = ticks(0, maxV > 0 ? maxV : 1, { zero: true, integer: o.integer });
    const fmt = o.yFmt || fmtNum;
    const none = !(maxV > 0);
    const f = frame(el, o, (none ? [0] : yt).map(fmt));
    let x0 = o.xMin != null ? o.xMin : Math.min(...bins.map((b) => b.x0));
    let x1 = o.xMax != null ? o.xMax : Math.max(...bins.map((b) => b.x1));
    if (!bins.length) { x0 = 0; x1 = 1; }
    if (x1 === x0) x1 = x0 + 1;
    const sx = (v) => f.pad.l + ((v - x0) / (x1 - x0)) * f.iw;
    const sy = (v) => f.pad.t + f.ih - (v / yt[yt.length - 1]) * f.ih;
    yAxis(f, none ? [0] : yt, sy, o, 0);
    const marks = E("g", { class: "dc-marks" }, f.svg);
    const hov = E("rect", { class: "dc-col", y: f.pad.t, height: f.ih, width: 0, x: -100 }, f.svg);
    const gap = o.gap != null ? o.gap : 1;
    const geo = new Map();
    const rects = [];
    bins.forEach((b, i) => {
      const xa = sx(b.x0) + gap / 2, xb = sx(b.x1) - gap / 2;
      const w = Math.max(o.minW || 1.5, xb - xa);
      const v = Math.max(0, +b.v || 0);
      const y = sy(v), h = Math.max(v > 0 ? 0.5 : 0, sy(0) - y);
      const key = b.key != null ? b.key : i;
      geo.set(key, { y, h });
      if (v <= 0) return;
      const cls = b.hollow ? "dc-bar hollow" : (b.cls ? "dc-bar " + b.cls : "dc-bar");
      const r = E("rect", {
        x: r1(xa + (b.hollow ? 0.5 : 0)), width: r1(Math.max(1, w - (b.hollow ? 1 : 0))),
        y: r1(y + (b.hollow ? 0.5 : 0)), height: r1(Math.max(0, h - (b.hollow ? 1 : 0))), class: cls, rx: w > 4 ? 1 : 0,
      }, marks);
      rects.push({ r, key, y, h, hollow: b.hollow });
    });
    if (!bins.length || maxV <= 0) emptyMsg(el, o.empty);
    refsAndMarks(f, o, sx, sy, E("g", {}, f.svg));
    // x axis
    if (o.xAxis !== false) {
      const xt = o.xTime ? timeTicks(x0, x1, Math.max(2, Math.floor(f.iw / 90)))
        : ticks(x0, x1, { target: Math.max(2, Math.min(6, Math.floor(f.iw / 70))), integer: o.xInteger })
          .filter((v) => v >= x0 - 1e-9 && v <= x1 + 1e-9).map((v) => ({ v, label: (o.xFmt || fmtNum)(v) }));
      let last = -Infinity;
      xt.forEach(({ v, label }) => {
        const x = sx(v), w = tw(label);
        if (x - w / 2 < last + 6) return;
        let cls = "dc-lab mid", xx = x;
        if (x + w / 2 > f.W) { cls = "dc-lab end"; xx = f.W; }
        if (x - w / 2 < f.pad.l - 2) { cls = "dc-lab"; xx = Math.max(f.pad.l, x); }
        T(f.svg, xx, f.H - 6, label, cls);
        last = xx + (cls === "dc-lab mid" ? w / 2 : cls === "dc-lab" ? w : 0);
      });
    }
    const tip = tipFor(el);
    const centers = bins.map((b) => (sx(b.x0) + sx(b.x1)) / 2);
    const idxAt = (px) => {
      const x = px * f.W / el.clientWidth;
      let best = -1, bd = Infinity;
      bins.forEach((b, i) => {
        const a = sx(b.x0), z = sx(b.x1);
        const d = x < a ? a - x : x > z ? x - z : 0;
        if (d < bd) { bd = d; best = i; }
      });
      return bd < Math.max(8, f.iw / Math.max(1, bins.length) / 2) ? best : -1;
    };
    interact(el, f.svg, bins.length, idxAt, (i, px, py) => {
      const b = bins[i];
      hov.setAttribute("x", r1(sx(b.x0)));
      hov.setAttribute("width", r1(Math.max(2, sx(b.x1) - sx(b.x0))));
      hov.classList.add("on");
      const html = o.tip ? o.tip(i, b) : `<b>${esc(fmtNum(b.x0))}–${esc(fmtNum(b.x1))}</b>${esc(fmt(b.v))}`;
      tip.show(html, px != null ? px : centers[i] * el.clientWidth / f.W, py != null ? py : sy(b.v || 0) * el.clientHeight / f.H);
    }, () => { hov.classList.remove("on"); tip.hide(); });
    ctx.st.geo = { kind: "hist", map: geo, base: sy(0) };
    let morphed = false;
    if (ctx.morphFrom && ctx.morphFrom.kind === "hist") {
      const old = ctx.morphFrom.map;
      const shared = rects.filter((x) => old.has(x.key)).length;
      if (shared >= rects.length * 0.5 && shared > 0) {
        morphed = true;
        const base = sy(0);
        const from = rects.map((x) => old.get(x.key) || { y: base, h: 0 });
        rects.forEach((x, k) => { x.r.setAttribute("y", r1(from[k].y)); x.r.setAttribute("height", r1(from[k].h)); });
        tween(ctx.st, 260, (p) => rects.forEach((x, k) => {
          x.r.setAttribute("y", r1(from[k].y + (x.y - from[k].y) * p + (x.hollow ? 0.5 : 0)));
          x.r.setAttribute("height", r1(Math.max(0, from[k].h + (x.h - from[k].h) * p - (x.hollow ? 1 : 0))));
        }));
      }
    }
    place(el, f.svg, { ...ctx, fade: !!ctx.morphFrom }, morphed);
  }

  /* ------------------------------------------------------------- line
     One series, 2px straight segments; null y breaks the line. Optional
     dashed reference lines (refs), quiet guides (guides) and vertical event
     annotations (marks). Hover: crosshair + a single dot + tooltip. */
  function line(el, o) { mount(el, "line", o, drawLine); }
  function drawLine(el, o, ctx) {
    const pts = (o.points || []).filter((p) => p && isFinite(p[0]));
    const ys = pts.map((p) => p[1]).filter((v) => v != null && isFinite(v));
    const refPts = (o.refLine || []).filter((p) => p && isFinite(p[0]) && p[1] != null && isFinite(p[1]));
    const extra = [...(o.refs || []), ...(o.guides || [])].map((r) => r.y).filter((v) => v != null && isFinite(v))
      .concat(refPts.map((p) => p[1]));
    let lo = Math.min(...ys, ...extra), hi = Math.max(...ys, ...extra);
    if (!ys.length) { lo = 0; hi = 1; }
    if (o.zero) lo = Math.min(0, lo);
    if (!o.zero && hi > lo) { const pad = (hi - lo) * 0.08; lo -= pad; hi += pad; }
    if (o.yMin != null) lo = Math.min(lo, o.yMin);
    if (o.yMax != null) hi = Math.max(hi, o.yMax);
    if (o.clampMin != null) lo = Math.max(lo, o.clampMin);
    if (o.clampMax != null) hi = Math.min(hi, o.clampMax);
    let yt = ticks(lo, hi, { zero: o.zero });
    if (o.clampMin != null || o.clampMax != null) {
      yt = yt.filter((v) => (o.clampMin == null || v >= o.clampMin - 1e-9) && (o.clampMax == null || v <= o.clampMax + 1e-9));
      if (yt.length < 2) yt = [lo, hi];
    }
    const fmt = o.yFmt || fmtNum;
    const none = !ys.length && !refPts.length;
    const f = frame(el, o, (none ? [yt[0]] : yt).map(fmt));
    const xs = pts.map((p) => p[0]).concat(refPts.map((p) => p[0]));
    let x0 = o.xMin != null ? o.xMin : Math.min(...xs), x1 = o.xMax != null ? o.xMax : Math.max(...xs);
    if (!xs.length) { x0 = 0; x1 = 1; }
    if (x1 === x0) { x0 -= 1; x1 += 1; }
    const ya = yt[0], yb = yt[yt.length - 1];
    const sx = (v) => f.pad.l + ((v - x0) / (x1 - x0)) * f.iw;
    const sy = (v) => f.pad.t + f.ih - ((v - ya) / (yb - ya)) * f.ih;
    yAxis(f, none ? [yt[0]] : yt, sy, o, o.zero ? 0 : null);
    (o.guides || []).forEach((gd) => {
      if (gd.y == null) return;
      const y = Math.round(sy(gd.y)) + 0.5;
      E("line", { x1: f.pad.l, x2: f.W - f.pad.r, y1: y, y2: y, class: "dc-guide" }, f.svg);
      if (gd.label) { const t = T(f.svg, f.W - f.pad.r, y - 5, gd.label, "dc-lab"); t.setAttribute("text-anchor", "end"); }
    });
    const marks = E("g", { class: "dc-marks" }, f.svg);
    const path = (arr) => {
      let d = "", pen = false;
      for (const p of arr) {
        if (p[1] == null || !isFinite(p[1])) { pen = false; continue; }
        d += (pen ? "L" : "M") + r1(sx(p[0])) + " " + r1(sy(p[1]));
        pen = true;
      }
      return d;
    };
    let area = null;
    if (o.area && pts.length > 1) {
      const valid = pts.filter((p) => p[1] != null);
      if (valid.length > 1) {
        const b = sy(Math.max(ya, 0));
        area = E("path", { class: "dc-area", d: path(valid) + `L${r1(sx(valid[valid.length - 1][0]))} ${r1(b)}L${r1(sx(valid[0][0]))} ${r1(b)}Z` }, marks);
      }
    }
    if (refPts.length > 1) E("path", { class: "dc-ref", d: path(refPts) }, marks);
    const lp = E("path", { class: "dc-line" + (o.thin ? " thin" : ""), d: path(pts) }, marks);
    // a lone point still has to show up
    if (ys.length === 1) {
      const p = pts.find((q) => q[1] != null);
      E("circle", { cx: r1(sx(p[0])), cy: r1(sy(p[1])), r: 2.5, class: "dc-dot" }, marks);
    }
    if (!ys.length) emptyMsg(el, o.empty);
    refsAndMarks(f, o, sx, sy, E("g", {}, f.svg));
    if (o.xAxis !== false) {
      const xt = o.xTime ? timeTicks(x0, x1, Math.max(2, Math.floor(f.iw / 90)))
        : ticks(x0, x1, { target: Math.max(2, Math.min(6, Math.floor(f.iw / 80))), integer: o.xInteger })
          .filter((v) => v >= x0 - 1e-9 && v <= x1 + 1e-9).map((v) => ({ v, label: (o.xFmt || fmtNum)(v) }));
      let last = -Infinity;
      xt.forEach(({ v, label }) => {
        const x = sx(v), w = tw(label);
        let cls = "dc-lab mid", xx = x;
        if (x + w / 2 > f.W) { cls = "dc-lab end"; xx = f.W; }
        if (x - w / 2 < f.pad.l - 2) { cls = "dc-lab"; xx = Math.max(f.pad.l, x); }
        const left = cls === "dc-lab mid" ? xx - w / 2 : cls === "dc-lab" ? xx : xx - w;
        if (left < last + 8) return;
        T(f.svg, xx, f.H - 6, label, cls);
        last = left + w;
      });
    }
    // hover
    const tip = tipFor(el);
    const cross = E("line", { class: "dc-cross", y1: f.pad.t, y2: f.pad.t + f.ih, x1: -10, x2: -10, visibility: "hidden" }, f.svg);
    const dot = E("circle", { r: 4, class: "dc-dot", visibility: "hidden" }, f.svg);
    const valid = pts.filter((p) => p[1] != null && isFinite(p[1]));
    const idxAt = (px) => {
      if (!valid.length) return -1;
      const xv = x0 + ((px * f.W / el.clientWidth - f.pad.l) / f.iw) * (x1 - x0);
      let best = 0, bd = Infinity;
      valid.forEach((p, i) => { const d = Math.abs(p[0] - xv); if (d < bd) { bd = d; best = i; } });
      return best;
    };
    interact(el, f.svg, valid.length, idxAt, (i, px, py) => {
      const p = valid[i];
      const x = sx(p[0]), y = sy(p[1]);
      cross.setAttribute("x1", r1(x)); cross.setAttribute("x2", r1(x)); cross.setAttribute("visibility", "visible");
      dot.setAttribute("cx", r1(x)); dot.setAttribute("cy", r1(y)); dot.setAttribute("visibility", "visible");
      const html = o.tip ? o.tip(p, i) : `<b>${esc((o.xFmt || fmtNum)(p[0]))}</b>${esc(fmt(p[1]))}`;
      tip.show(html, x * el.clientWidth / f.W, y * el.clientHeight / f.H);
    }, () => { cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); tip.hide(); });
    const geo = { kind: "line", xs: pts.map((p) => p[0]), ys: pts.map((p) => (p[1] == null ? null : sy(p[1]))), sx: pts.map((p) => sx(p[0])) };
    ctx.st.geo = geo;
    let morphed = false;
    const old = ctx.morphFrom;
    if (old && old.kind === "line" && old.xs.length === geo.xs.length && old.xs.every((v, i) => v === geo.xs[i])
        && old.ys.every((v, i) => (v == null) === (geo.ys[i] == null))) {
      morphed = true;
      const dFor = (p) => {
        let d = "", pen = false;
        for (let i = 0; i < geo.xs.length; i++) {
          if (geo.ys[i] == null) { pen = false; continue; }
          const y = old.ys[i] + (geo.ys[i] - old.ys[i]) * p;
          d += (pen ? "L" : "M") + r1(geo.sx[i]) + " " + r1(y);
          pen = true;
        }
        return d;
      };
      lp.setAttribute("d", dFor(0));
      if (area) area.remove();
      tween(ctx.st, 260, (p) => lp.setAttribute("d", dFor(p)));
    }
    place(el, f.svg, { ...ctx, fade: !!ctx.morphFrom }, morphed);
  }

  /* --------------------------------------------------------- heat grid
     Weekday × hour. Lightness of one hue, 5 steps relative to the busiest
     cell; zero is the ground tone, not a colour. */
  function heat(el, o) { mount(el, "heat", o, drawHeat); }
  function drawHeat(el, o, ctx) {
    const rows = o.values.length, cols = o.values[0] ? o.values[0].length : 24;
    const W = Math.max(200, el.clientWidth);
    const gutter = Math.ceil(Math.max(...o.rowLabels.map((s) => tw(s)))) + 10;
    const gap = W < 480 ? 2 : 3;
    const cw = Math.max(6, (W - gutter - gap * (cols - 1)) / cols);
    const ch = Math.min(cw, o.cellH || 22);
    const H = 18 + rows * (ch + gap);
    el.style.height = H + "px";
    const svg = E("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": o.label || null });
    const marks = E("g", { class: "dc-marks" }, svg);
    let max = 0;
    o.values.forEach((r) => r.forEach((v) => { if (v > max) max = v; }));
    const lvl = (v) => (!v ? 0 : Math.max(1, Math.min(4, Math.ceil((v / (max || 1)) * 4))));
    for (let r = 0; r < rows; r++) {
      T(svg, gutter - 10, 18 + r * (ch + gap) + ch / 2 + 4, o.rowLabels[r], "dc-lab end");
      for (let c = 0; c < cols; c++) {
        const v = o.values[r][c] || 0;
        E("rect", {
          x: r1(gutter + c * (cw + gap)), y: r1(18 + r * (ch + gap)), width: r1(cw), height: r1(ch), rx: 2,
          class: `dc-cell l${lvl(v)}`,
        }, marks);
      }
    }
    (o.colTicks || [0, 6, 12, 18]).forEach((c) => {
      T(svg, gutter + c * (cw + gap), 11, o.colFmt ? o.colFmt(c) : String(c), "dc-lab");
    });
    const tip = tipFor(el);
    svg.addEventListener("pointermove", (e) => {
      const rc = svg.getBoundingClientRect();
      const x = (e.clientX - rc.left) * W / rc.width, y = (e.clientY - rc.top) * H / rc.height;
      const c = Math.floor((x - gutter) / (cw + gap)), r = Math.floor((y - 18) / (ch + gap));
      if (c < 0 || c >= cols || r < 0 || r >= rows) { tip.hide(); return; }
      tip.show(o.tip ? o.tip(r, c) : esc(String(o.values[r][c])), e.clientX - rc.left, e.clientY - rc.top);
    });
    svg.addEventListener("pointerleave", () => tip.hide());
    ctx.st.geo = { kind: "heat" };
    place(el, svg, { ...ctx, fade: !!ctx.morphFrom }, false);
  }

  /* --------------------------------------------------------- calendar
     A year (or more) of days in week columns. Intensity is relative to the
     busiest day in view. Days outside the selected period are dimmed but
     still drawn, so the year keeps its shape. */
  function calendar(el, o) { mount(el, "cal", o, drawCal); }
  function drawCal(el, o, ctx) {
    const byDate = new Map((o.days || []).map((d) => [d.date, d]));
    const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    const parse = (s) => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
    const last = parse(o.end);
    const first = parse(o.start);
    const start = new Date(first);
    start.setDate(start.getDate() - ((start.getDay() + 6) % 7));        // Monday columns
    const weeks = Math.max(1, Math.round((last - start) / (7 * DAY)) + 1);
    const W = Math.max(200, el.clientWidth);
    const left = Math.ceil(Math.max(...o.dayLabels.map((s) => tw(s)))) + 8;
    const gap = 3;
    let cell = Math.floor((W - left - gap * (weeks - 1)) / weeks);
    cell = Math.max(o.minCell || 9, Math.min(o.maxCell || 18, cell));
    const gridW = left + weeks * (cell + gap);
    const H = 18 + 7 * (cell + gap);
    el.style.height = H + "px";
    const svg = E("svg", { viewBox: `0 0 ${Math.max(gridW, W)} ${H}`, width: Math.max(gridW, W), height: H, role: "img", "aria-label": o.label || null });
    svg.style.width = Math.max(gridW, W) + "px";
    const marks = E("g", { class: "dc-marks" }, svg);
    const max = Math.max(0, ...(o.days || []).map((d) => o.value(d) || 0)) || 1;
    const lvl = (v) => (!v ? 0 : Math.max(1, Math.min(4, Math.ceil((v / max) * 4))));
    const months = [];
    const cells = [];
    for (let w = 0; w < weeks; w++) {
      for (let d = 0; d < 7; d++) {
        const day = new Date(start);
        day.setDate(day.getDate() + w * 7 + d);
        if (day > last) continue;
        const key = iso(day);
        const rec = byDate.get(key);
        const v = rec ? o.value(rec) : 0;
        const out = (o.inFrom && key < o.inFrom) || day < first;
        const r = E("rect", {
          x: left + w * (cell + gap), y: 18 + d * (cell + gap), width: cell, height: cell, rx: 2,
          class: `dc-cell l${lvl(v)}${out ? " out" : ""}${o.onDay ? " tap" : ""}`, "data-d": key,
        }, marks);
        cells.push([key, r]);
        if (day.getDate() <= 7 && d === 0) months.push([w, day]);
      }
    }
    const loc = typeof localeTag === "function" ? localeTag() : undefined;
    let lastX = -Infinity;
    months.forEach(([w, day]) => {
      const x = left + w * (cell + gap);
      const label = day.toLocaleDateString(loc, day.getMonth() === 0 || months[0][1] === day ? { month: "short", year: "numeric" } : { month: "short" });
      if (x < lastX + 8) return;
      T(svg, x, 11, label, "dc-lab");
      lastX = x + tw(label);
    });
    [0, 2, 4].forEach((d) => T(svg, left - 8, 18 + d * (cell + gap) + cell - 2, o.dayLabels[d], "dc-lab end"));
    const tip = tipFor(el);
    svg.addEventListener("pointermove", (e) => {
      const t = e.target.closest && e.target.closest("rect[data-d]");
      if (!t) { tip.hide(); return; }
      const rc = el.getBoundingClientRect();
      const rec = byDate.get(t.dataset.d);
      tip.show(o.tip(t.dataset.d, rec), e.clientX - rc.left, e.clientY - rc.top);
    });
    svg.addEventListener("pointerleave", () => tip.hide());
    if (o.onDay) svg.addEventListener("click", (e) => {
      const t = e.target.closest && e.target.closest("rect[data-d]");
      if (t) o.onDay(t.dataset.d);
    });
    ctx.st.geo = { kind: "cal" };
    place(el, svg, { ...ctx, fade: false }, false);
    // keep the newest weeks in view on a narrow screen
    if (gridW > W && el.parentElement) el.parentElement.scrollLeft = gridW;
  }

  /* -------------------------------------------------------- HTML marks */

  /* Ranked bar list: label, value, a bar at the true ratio. */
  function rank(el, rows, o) {
    const opt = o || {};
    const max = Math.max(0, ...rows.map((r) => +r.value || 0)) || 1;
    if (!rows.length) { el.innerHTML = opt.empty ? `<div class="dh-empty">${esc(opt.empty)}</div>` : ""; return; }
    el.innerHTML = `<ol class="dh-rank">${rows.map((r, i) => {
      const lbl = r.href ? `<a class="lbl" href="${esc(r.href)}">${esc(r.label)}</a>`
        : r.pick ? `<button type="button" class="lbl" data-i="${i}">${esc(r.label)}</button>`
        : `<span class="lbl">${esc(r.label)}</span>`;
      return `<li class="${r.cls || ""}"${r.t != null ? ` data-t="${r.t}"` : ""} data-rv style="--i:${i}">${lbl}<span class="val">${r.display}</span>
        <span class="bar"><i style="transform:scaleX(${Math.max(0, Math.min(1, (+r.value || 0) / max)).toFixed(4)})"></i></span>
        ${r.sub ? `<span class="sub">${r.sub}</span>` : ""}</li>`;
    }).join("")}</ol>`;
    el.querySelectorAll("button.lbl[data-i]").forEach((b) => b.addEventListener("click", () => rows[+b.dataset.i].pick()));
  }

  /* Proportional split: one bar at the true ratio, with keys underneath. */
  function split(el, parts, o) {
    const opt = o || {};
    const total = parts.reduce((s, p) => s + Math.max(0, p.value || 0), 0);
    if (!total) { el.innerHTML = opt.empty ? `<div class="dh-empty">${esc(opt.empty)}</div>` : ""; return; }
    el.innerHTML = `<div class="dh-split" role="img" aria-label="${esc(opt.label || "")}">${parts.filter((p) => p.value > 0).map((p) =>
      `<i class="s-${esc(p.key)}" style="flex-grow:${(p.value / total).toFixed(5)}" title="${esc(p.label)}"></i>`).join("")}</div>
      <div class="dh-keys">${parts.filter((p) => p.value > 0 || p.always).map((p) =>
        `<span><i class="s-${esc(p.key)}"></i>${esc(p.label)} <b>${p.display}</b></span>`).join("")}</div>`;
  }

  /* Legend: each swatch is drawn exactly like its mark. */
  function legend(el, items) {
    const sw = (k) => {
      if (k === "line") return `<svg width="16" height="8" aria-hidden="true"><line x1="0" x2="16" y1="4" y2="4" class="dc-line"/></svg>`;
      if (k === "ref") return `<svg width="16" height="8" aria-hidden="true"><line x1="0" x2="16" y1="4" y2="4" class="dc-ref"/></svg>`;
      if (k === "mark") return `<svg width="8" height="12" aria-hidden="true"><line x1="4" x2="4" y1="0" y2="12" class="dc-mark"/></svg>`;
      if (k === "guide") return `<svg width="16" height="8" aria-hidden="true"><line x1="0" x2="16" y1="4" y2="4" class="dc-guide"/></svg>`;
      const cls = k === "alt" ? "dc-bar alt" : k === "hollow" ? "dc-bar hollow" : "dc-bar";
      return `<svg width="9" height="9" aria-hidden="true"><rect x="${k === "hollow" ? 0.5 : 0}" y="${k === "hollow" ? 0.5 : 0}" width="${k === "hollow" ? 8 : 9}" height="${k === "hollow" ? 8 : 9}" rx="1" class="${cls}"/></svg>`;
    };
    el.innerHTML = items.filter(Boolean).map((it) => `<span>${sw(it.kind)}${esc(it.label)}</span>`).join("");
  }

  Object.assign(DC, { bars, hist, line, heat, calendar, rank, split, legend });
  window.DC = DC;
})();
