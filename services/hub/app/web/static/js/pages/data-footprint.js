/* 数据 · 足迹 (一生足迹): where has the car been?
 *
 * The basemap is MapLibre (desaturated in CSS). Everything that is the car's
 * own record is drawn on one canvas above it, from precomputed Web-Mercator
 * coordinates and a per-frame affine transform, so years of ~1 km cells and
 * hundreds of routes stay smooth while the user scrubs:
 *   lit countries (zoomed out) → lit regions → explored cells → routes →
 *   places; the accent only on "now", as a DOM marker.
 * Regions light from Natural Earth (public domain, lazy-loaded here only):
 * matched by name + country code, and by any explored cell inside them, so
 * a state driven through without stopping still lights.
 * Motion: none on load. The 「首次出行 → 今天」 scrubber (and its replay
 * button) is the only thing that animates, and only when the user asks.
 */
(function () {
  "use strict";
  const $ = (s, r) => (r || document).querySelector(s);
  const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
  const DAY = 864e5;

  const F = {
    fp: null, map: null, mapP: null, ink: null, ctx: null, dpr: 1, colors: null,
    cells: [], cellT: [], cumArea: [], routes: [], places: [], cities: [], firsts: [],
    regions: [], countries: [], neReady: false, t0: 0, t1: 0, T: 0, play: 0, tab: "regions",
    here: null, hereMarker: null, gen: 0, raf: 0, apiRegions: [], apiCountries: [],
  };

  /* --------------------------------------------- contour lines (empty state)
     A seeded value-noise field traced with marching squares. Shown while the
     map loads and when there is no footprint yet. */
  function drawTopo(svg, seed) {
    const W = 160, H = 100;
    let s = seed >>> 0 || 7;
    const rnd = () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; };
    const lattice = (n) => Array.from({ length: n * n }, rnd);
    const octs = [[6, 0.55], [12, 0.3], [24, 0.15]].map(([n, a]) => ({ n, a, g: lattice(n + 1) }));
    const smooth = (x) => x * x * (3 - 2 * x);
    const val = (x, y) => octs.reduce((acc, o) => {
      const fx = (x / W) * o.n, fy = (y / H) * o.n;
      const ix = Math.floor(fx), iy = Math.floor(fy), tx = smooth(fx - ix), ty = smooth(fy - iy);
      const g = (i, j) => o.g[Math.min(o.n, j) * (o.n + 1) + Math.min(o.n, i)];
      const a = g(ix, iy) + (g(ix + 1, iy) - g(ix, iy)) * tx;
      const b = g(ix, iy + 1) + (g(ix + 1, iy + 1) - g(ix, iy + 1)) * tx;
      return acc + (a + (b - a) * ty) * o.a;
    }, 0);
    const field = [];
    for (let y = 0; y <= H; y++) { const row = []; for (let x = 0; x <= W; x++) row.push(val(x, y)); field.push(row); }
    let lo = Infinity, hi = -Infinity;
    field.forEach((r) => r.forEach((v) => { if (v < lo) lo = v; if (v > hi) hi = v; }));
    const levels = 14;
    let out = "";
    for (let k = 1; k < levels; k++) {
      const th = lo + ((hi - lo) * k) / levels;
      let d = "";
      for (let y = 0; y < H; y++) {
        for (let x = 0; x < W; x++) {
          const a = field[y][x], b = field[y][x + 1], c = field[y + 1][x + 1], e = field[y + 1][x];
          const idx = (a > th ? 8 : 0) | (b > th ? 4 : 0) | (c > th ? 2 : 0) | (e > th ? 1 : 0);
          if (idx === 0 || idx === 15) continue;
          const lerp = (p, q) => (th - p) / (q - p);
          const T = [x + lerp(a, b), y], R = [x + 1, y + lerp(b, c)], B = [x + lerp(e, c), y + 1], L = [x, y + lerp(a, e)];
          const segs = {
            1: [[L, B]], 2: [[B, R]], 3: [[L, R]], 4: [[T, R]], 5: [[T, L], [B, R]], 6: [[T, B]], 7: [[T, L]],
            8: [[T, L]], 9: [[T, B]], 10: [[T, R], [L, B]], 11: [[T, R]], 12: [[L, R]], 13: [[B, R]], 14: [[L, B]],
          }[idx];
          for (const [p, q] of segs) d += `M${p[0].toFixed(2)} ${p[1].toFixed(2)}L${q[0].toFixed(2)} ${q[1].toFixed(2)}`;
        }
      }
      if (d) out += `<path class="${k % 4 === 0 ? "major" : "minor"}" d="${d}"/>`;
    }
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.innerHTML = out;
  }
  drawTopo($("#fp-topo"), 20261031 + (Number(new URLSearchParams(location.search).get("car")) || 1) * 977);

  /* --------------------------------------------------------- geometry */
  const mx = (lng) => (lng + 180) / 360;
  const my = (lat) => {
    const s = Math.max(-0.9999, Math.min(0.9999, Math.sin((lat * Math.PI) / 180)));
    return 0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI);
  };
  /* WGS-84 → world coordinates in the map's own datum (GCJ-02 in China). */
  const world = (lat, lon) => {
    const [lng, la] = F.map ? OmtMap.ll(F.map, lat, lon) : [lon, lat];
    return [mx(lng), my(la)];
  };
  /* Pixels = a + world × s. Valid because rotation and pitch are off. */
  function xform() {
    const c = F.map.getCenter();
    const lat2 = Math.max(-80, Math.min(80, c.lat + 0.5)), lng2 = c.lng + 0.5;
    const a = F.map.project([c.lng, c.lat]), b = F.map.project([lng2, lat2]);
    const wa = [mx(c.lng), my(c.lat)], wb = [mx(lng2), my(lat2)];
    const sx = (b.x - a.x) / (wb[0] - wa[0]), sy = (b.y - a.y) / (wb[1] - wa[1]);
    return { ax: a.x - wa[0] * sx, ay: a.y - wa[1] * sy, sx, sy };
  }

  function resolveColors() {
    F.colors = {
      lit: DH.color("var(--fp-lit)"), data: DH.color("var(--dh-data)"), ink: DH.color("var(--dh-ink)"),
      ink2: DH.color("var(--dh-ink-2)"), ground: DH.color("var(--dh-ground)"), dark: DH.isDark(),
    };
  }

  /* ------------------------------------------------------------- draw */
  function size() {
    if (!F.ink) return;
    const r = F.ink.getBoundingClientRect();
    F.dpr = Math.min(2, window.devicePixelRatio || 1);
    F.ink.width = Math.max(1, Math.round(r.width * F.dpr));
    F.ink.height = Math.max(1, Math.round(r.height * F.dpr));
  }
  function schedule() {
    if (F.raf) return;
    F.raf = requestAnimationFrame(() => { F.raf = 0; draw(); });
  }
  function rgba(rgb, a) { return rgb.replace("rgb(", "rgba(").replace(")", `, ${a})`); }

  function draw() {
    const ctx = F.ctx;
    if (!ctx || !F.map || !F.colors) return;
    const W = F.ink.width, H = F.ink.height, d = F.dpr;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const x = xform();
    const z = F.map.getZoom();
    const T = F.T, C = F.colors;
    const PX = (wx) => (x.ax + wx * x.sx) * d, PY = (wy) => (x.ay + wy * x.sy) * d;
    const vx0 = -x.ax / x.sx, vx1 = (W / d - x.ax) / x.sx, vy0 = -x.ay / x.sy, vy1 = (H / d - x.ay) / x.sy;
    const visible = (b) => b[2] >= vx0 && b[0] <= vx1 && b[3] >= vy0 && b[1] <= vy1;
    const recency = (t) => { const age = (T - t) / DAY; return age < 0 ? 0 : Math.max(0, 1 - age / 30); };

    const polys = (list, fillA, strokeA, boost) => {
      for (const p of list) {
        if (p.t > T || !visible(p.wb)) continue;
        ctx.beginPath();
        for (const ring of p.wr) {
          for (let i = 0; i < ring.length; i += 2) {
            const X = PX(ring[i]), Y = PY(ring[i + 1]);
            if (i) ctx.lineTo(X, Y); else ctx.moveTo(X, Y);
          }
          ctx.closePath();
        }
        const r = boost ? recency(p.t) : 0;
        if (fillA > 0) { ctx.fillStyle = rgba(C.lit, fillA + r * 0.12); ctx.fill("evenodd"); }
        if (strokeA > 0) { ctx.lineWidth = d; ctx.strokeStyle = rgba(C.lit, strokeA + r * 0.3); ctx.stroke(); }
      }
    };
    // countries, only when zoomed far out
    const ca = Math.max(0, Math.min(1, (5.5 - z) / 2.5));
    if (ca > 0.02) polys(F.countries, 0.07 * ca, 0.35 * ca, false);
    // regions driven in: a real fill when zoomed out (the cells are specks
    // there), almost none up close where the cells tell the story; state
    // polygons include lakes and coastal water, which must not glow
    const rf = Math.max(0.03, Math.min(0.2, 0.2 - (z - 4) * 0.055));
    polys(F.regions, C.dark ? rf : rf * 0.85, C.dark ? 0.5 : 0.55, true);

    // explored cells, quantised into a few brightness steps (one path each)
    const steps = [[], [], [], [], []];
    const minW = 1.6 * d;
    const base = C.dark ? 0.34 : 0.30;
    const n = upper(F.cellT, T);
    for (let i = 0; i < n; i++) {
      const c = F.cells[i];
      if (c.wx1 < vx0 || c.wx0 > vx1 || c.wy1 < vy0 || c.wy0 > vy1) continue;
      steps[Math.min(4, Math.ceil(recency(c.t) * 4))].push(c);
    }
    steps.forEach((list, k) => {
      if (!list.length) return;
      ctx.beginPath();
      for (const c of list) {
        let x0 = PX(c.wx0), x1 = PX(c.wx1), y0 = PY(c.wy0), y1 = PY(c.wy1);
        if (x1 - x0 < minW) { const m = (x0 + x1) / 2; x0 = m - minW / 2; x1 = m + minW / 2; }
        if (y1 - y0 < minW) { const m = (y0 + y1) / 2; y0 = m - minW / 2; y1 = m + minW / 2; }
        ctx.rect(x0, y0, x1 - x0, y1 - y0);
      }
      ctx.fillStyle = rgba(C.lit, base + k * 0.11);
      ctx.fill();
    });

    // routes: thin lines; where they overlap, brightness builds with frequency
    ctx.lineWidth = (z < 7 ? 0.8 : z < 11 ? 1.1 : 1.5) * d;
    ctx.lineJoin = "round";
    ctx.strokeStyle = rgba(C.ink, C.dark ? 0.26 : 0.22);
    ctx.beginPath();
    for (const r of F.routes) {
      if (r.t > T || !visible(r.wb)) continue;
      const w = r.w;
      for (let i = 0; i < w.length; i += 2) {
        const X = PX(w[i]), Y = PY(w[i + 1]);
        if (i) ctx.lineTo(X, Y); else ctx.moveTo(X, Y);
      }
    }
    ctx.stroke();

    // places you stopped: only in the final state, scrubbing is about the reveal
    if (T >= F.t1) {
      for (const p of F.places) {
        const X = PX(p.wx), Y = PY(p.wy);
        if (X < -10 || Y < -10 || X > W + 10 || Y > H + 10) continue;
        ctx.beginPath();
        ctx.arc(X, Y, p.r * d, 0, Math.PI * 2);
        ctx.fillStyle = C.ink;
        ctx.fill();
        ctx.lineWidth = 1.5 * d;
        ctx.strokeStyle = C.ground;
        ctx.stroke();
      }
    }
  }
  /* number of items with t <= T in an ascending array */
  function upper(arr, T) {
    let lo = 0, hi = arr.length;
    while (lo < hi) { const m = (lo + hi) >> 1; if (arr[m] <= T) lo = m + 1; else hi = m; }
    return lo;
  }

  /* --------------------------------------------------------- the map */
  function initMap() {
    if (F.mapP) return F.mapP;
    F.mapP = (async () => {
      const map = await OmtMap.create($("#fp-gl"), { center: [39, -96], zoom: 3 });
      map.dragRotate.disable();
      map.touchZoomRotate.disableRotation();
      map.keyboard.disableRotation && map.keyboard.disableRotation();
      if (matchMedia("(pointer: coarse)").matches && map.cooperativeGestures) {
        try {
          map._locale = { ...(map._locale || {}), "CooperativeGesturesHandler.MobileHelpText": t("data.fp.two_fingers"),
            "CooperativeGesturesHandler.WindowsHelpText": t("data.fp.ctrl_scroll"), "CooperativeGesturesHandler.MacHelpText": t("data.fp.cmd_scroll") };
          map.cooperativeGestures.enable();
        } catch (e) { /* plain gestures then */ }
      }
      await OmtMap.ready(map);
      F.map = map;
      F.ink = $("#fp-ink");
      F.ctx = F.ink.getContext("2d");
      resolveColors();
      size();
      map.on("render", draw);
      map.on("resize", () => { size(); draw(); });
      map.on("mousemove", hover);
      map.on("mouseout", () => $("#fp-tip").classList.remove("on"));
      $("#fp-map").classList.add("ready");
      return map;
    })().catch((e) => {
      console.warn("map unavailable", e);
      $("#fp-gl").hidden = true;
      showMsg(t("data.c.map_unavailable"), t("data.fp.map_down"));
      return null;
    });
    return F.mapP;
  }
  matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => { if (F.map) { resolveColors(); draw(); } });

  function hover(e) {
    const tip = $("#fp-tip");
    if (F.T < F.t1 || !F.places.length) { tip.classList.remove("on"); return; }
    const x = xform();
    let best = null, bd = 144;
    for (const p of F.places) {
      const dx = x.ax + p.wx * x.sx - e.point.x, dy = x.ay + p.wy * x.sy - e.point.y;
      const dd = dx * dx + dy * dy;
      if (dd < bd) { bd = dd; best = p; }
    }
    if (!best) { tip.classList.remove("on"); F.map.getCanvas().style.cursor = ""; return; }
    tip.innerHTML = `<b>${esc(best.label)}</b>${esc(t("data.fp.place_tip", { n: best.arrivals, dwell: DH.dur(best.dwell) }))}`;
    tip.style.left = `${Math.round(e.point.x + 12)}px`;
    tip.style.top = `${Math.round(e.point.y - 12)}px`;
    tip.classList.add("on");
  }

  function showMsg(title, body) {
    const m = $("#fp-msg");
    m.hidden = false;
    m.innerHTML = `<b>${esc(title)}</b>${body ? esc(body) : ""}`;
  }

  /* ----------------------------------------------- Natural Earth regions */
  let NE = null;
  function loadNE() {
    if (!NE) {
      NE = Promise.all(["/static/vendor/naturalearth/admin1.topo.json", "/static/vendor/naturalearth/admin0.topo.json"]
        .map((u) => fetch(u).then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })))
        .then(([a1, a0]) => ({ regions: topo(a1, "regions"), countries: topo(a0, "countries") }));
      NE.catch(() => { NE = null; });
    }
    return NE;
  }
  /* A minimal TopoJSON reader: quantised, delta-encoded arcs → polygons.
     Each polygon's bounds come from its arcs' bounds, so rings are only
     materialised for the few polygons a footprint actually touches. */
  function topo(tj, name) {
    const [sx, sy] = tj.transform.scale, [tx, ty] = tj.transform.translate;
    const arcs = [], abox = [];
    for (const a of tj.arcs) {
      const out = new Float64Array(a.length * 2);
      let x = 0, y = 0, b0 = Infinity, b1 = Infinity, b2 = -Infinity, b3 = -Infinity;
      for (let i = 0; i < a.length; i++) {
        x += a[i][0]; y += a[i][1];
        const lo = x * sx + tx, la = y * sy + ty;
        out[2 * i] = lo; out[2 * i + 1] = la;
        if (lo < b0) b0 = lo; if (lo > b2) b2 = lo; if (la < b1) b1 = la; if (la > b3) b3 = la;
      }
      arcs.push(out); abox.push([b0, b1, b2, b3]);
    }
    const ring = (ids) => {
      const pts = [];
      ids.forEach((id, k) => {
        const a = arcs[id < 0 ? ~id : id], n = a.length / 2;
        for (let i = k ? 1 : 0; i < n; i++) {
          const j = id < 0 ? n - 1 - i : i;
          pts.push(a[2 * j], a[2 * j + 1]);
        }
      });
      return Float64Array.from(pts);
    };
    return tj.objects[name].geometries.map((g) => {
      const parts = g.type === "Polygon" ? [g.arcs] : g.type === "MultiPolygon" ? g.arcs : [];
      const polys = parts.map((rings) => {
        let b0 = Infinity, b1 = Infinity, b2 = -Infinity, b3 = -Infinity;
        for (const id of rings[0] || []) {
          const b = abox[id < 0 ? ~id : id];
          if (b[0] < b0) b0 = b[0]; if (b[1] < b1) b1 = b[1]; if (b[2] > b2) b2 = b[2]; if (b[3] > b3) b3 = b[3];
        }
        const poly = { bbox: [b0, b1, b2, b3], _r: null };
        poly.rings = () => poly._r || (poly._r = rings.map(ring));
        return poly;
      });
      return { p: g.properties || {}, polys };
    });
  }
  const inside = (rings, lon, lat) => {
    let c = false;
    for (const r of rings) {
      for (let i = 0, j = r.length - 2; i < r.length; j = i, i += 2) {
        const xi = r[i], yi = r[i + 1], xj = r[j], yj = r[j + 1];
        if ((yi > lat) !== (yj > lat) && lon < ((xj - xi) * (lat - yi)) / (yj - yi) + xi) c = !c;
      }
    }
    return c;
  };
  /* Name variants, tolerant of 省/市/自治区 and "State"/"Province" style
     suffixes, accents and punctuation. Two names match if any variant does. */
  const SUFFIX_ZH = /(特别行政区|维吾尔自治区|壮族自治区|回族自治区|自治区|自治州|地区|省|市|州|县|都|道|府)$/u;
  const SUFFIX_EN = /\s+(state|province|prefecture|region|oblast|krai|county|territory|district|governorate|department|canton|land)$/u;
  function variants(s) {
    const out = new Set();
    let v = String(s || "").toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "").trim();
    if (!v) return out;
    v = v.replace(/^(state of|province of|provincia de|provincia di|estado de|région|region)\s+/u, "");
    const add = (x) => { const k = x.replace(/[^\p{L}\p{N}]+/gu, ""); if (k) out.add(k); };
    add(v);
    const zh = v.replace(SUFFIX_ZH, "");
    add(zh);
    add(zh.replace(SUFFIX_ZH, ""));
    add(v.replace(SUFFIX_EN, ""));
    return out;
  }
  const meets = (a, b) => { for (const x of a) if (b.has(x)) return true; return false; };

  async function lightRegions(gen) {
    let ne;
    try { ne = await loadNE(); } catch (e) { return; }
    if (gen !== F.gen) return;
    const fp = F.fp;
    const B = fp.bounds ? [fp.bounds[1] - 0.05, fp.bounds[0] - 0.05, fp.bounds[3] + 0.05, fp.bounds[2] + 0.05] : null;
    const meetsB = (b) => B && b[2] >= B[0] && b[0] <= B[2] && b[3] >= B[1] && b[1] <= B[3];
    // admin-1 polygons that touch the footprint, indexed by whole degree
    const cand = [], tiles = new Map();
    ne.regions.forEach((f) => f.polys.forEach((poly) => {
      if (!meetsB(poly.bbox)) return;
      const k = cand.push({ f, poly }) - 1;
      const [a, b, c, e] = poly.bbox;
      for (let la = Math.floor(Math.max(b, B[1])); la <= Math.floor(Math.min(e, B[3])); la++) {
        for (let lo = Math.floor(Math.max(a, B[0])); lo <= Math.floor(Math.min(c, B[2])); lo++) {
          const key = la + "|" + lo;
          (tiles.get(key) || tiles.set(key, []).get(key)).push(k);
        }
      }
    }));
    // each explored cell lands in at most one region; cells come in time
    // order, so the first one seen is that region's first visit
    const firstT = new Map();
    for (const cell of F.cells) {
      for (const k of tiles.get(Math.floor(cell.lat) + "|" + Math.floor(cell.lon)) || []) {
        const { f, poly } = cand[k];
        const [a, b, c, e] = poly.bbox;
        if (cell.lon < a || cell.lon > c || cell.lat < b || cell.lat > e) continue;
        if (!inside(poly.rings(), cell.lon, cell.lat)) continue;
        if (!firstT.has(f)) firstT.set(f, cell.t);
        break;
      }
    }
    // regions from the car's addresses, matched by country code + name
    const apiCC = new Set(F.apiCountries.map((c) => String(c.country_code || "").toUpperCase()));
    const byApi = new Map();
    for (const f of ne.regions) {
      if (!apiCC.has(f.p.c)) continue;
      const names = new Set([...variants(f.p.e), ...variants(f.p.z), ...String(f.p.k || "").split("|").flatMap((x) => [...variants(x)])]);
      const api = F.apiRegions.find((r) => String(r.country_code || "").toUpperCase() === f.p.c && r.region && meets(variants(r.region), names));
      if (api) byApi.set(f, api);
    }
    const zh = DH.locale() === "zh";
    const regions = [];
    new Set([...firstT.keys(), ...byApi.keys()]).forEach((f) => {
      const api = byApi.get(f);
      const tApi = api && api.first_visit ? new Date(api.first_visit).getTime() : Infinity;
      regions.push({ f, cc: f.p.c, t: Math.min(tApi, firstT.get(f) ?? Infinity), api, passed: !api,
        name: zh && f.p.z ? f.p.z : api ? api.region : f.p.e });
    });
    // countries follow from their regions, plus the ones in the addresses
    const ccT = new Map();
    regions.forEach((r) => ccT.set(r.cc, Math.min(ccT.get(r.cc) ?? Infinity, r.t)));
    F.apiCountries.forEach((c) => {
      const cc = String(c.country_code || "").toUpperCase();
      if (c.first) ccT.set(cc, Math.min(ccT.get(cc) ?? Infinity, new Date(c.first).getTime()));
    });
    const countries = [];
    for (const f of ne.countries) {
      if (!ccT.has(f.p.c)) continue;
      const api = F.apiCountries.find((c) => String(c.country_code || "").toUpperCase() === f.p.c);
      countries.push({ f, cc: f.p.c, t: ccT.get(f.p.c), name: api ? api.country : (zh && f.p.z ? f.p.z : f.p.e) });
    }
    const prep = (list) => list.filter((x) => isFinite(x.t)).map((x) => {
      const wr = [];
      let a = Infinity, b = Infinity, c = -Infinity, e = -Infinity;
      x.f.polys.forEach((poly) => poly.rings().forEach((r) => {
        const w = new Float64Array(r.length);
        for (let i = 0; i < r.length; i += 2) {
          const p = world(r[i + 1], r[i]);
          w[i] = p[0]; w[i + 1] = p[1];
          if (p[0] < a) a = p[0]; if (p[0] > c) c = p[0]; if (p[1] < b) b = p[1]; if (p[1] > e) e = p[1];
        }
        wr.push(w);
      }));
      return { ...x, wr, wb: [a, b, c, e] };
    });
    if (gen !== F.gen) return;
    F.countries = prep(countries);
    F.regions = prep(regions).sort((a, b) => a.t - b.t);
    F.neReady = true;
    // the timeline speaks the UI's language too
    const renamed = new Map(regions.filter((r) => r.api).map((r) => [r.api.region, r.name]));
    F.firsts.forEach((x) => { if (x.kind === "region" && renamed.has(x.name)) x.name = renamed.get(x.name); });
    F.cities.forEach((c) => { if (renamed.has(c.region)) c.region = renamed.get(c.region); });
    // regions only driven through join the "firsts" timeline
    const known = new Set((fp.firsts || []).filter((x) => x.kind === "region").map((x) => renamed.get(x.name) || x.name));
    F.firsts = F.firsts.filter((x) => !x.passed).concat(F.regions.filter((r) => r.passed && !known.has(r.name))
      .map((r) => ({ kind: "region", name: r.name, passed: true, t: r.t, country: (countries.find((c) => c.cc === r.cc) || {}).name })))
      .sort((a, b) => a.t - b.t);
    paintTicks();
    counters(true);
    paintList();
    schedule();
  }

  /* ----------------------------------------------------------- counters */
  function countsAt(T) {
    const n = upper(F.cellT, T);
    const area = n ? F.cumArea[n - 1] : 0;
    const cities = F.cities.filter((c) => c.t <= T);
    const regions = F.neReady ? F.regions.filter((r) => r.t <= T) : F.apiRegions.filter((r) => r.region && new Date(r.first_visit).getTime() <= T);
    const countries = F.neReady ? F.countries.filter((c) => c.t <= T) : F.apiCountries.filter((c) => new Date(c.first).getTime() <= T);
    return { n, area, cities, regions, countries };
  }
  function counters(still) {
    const c = countsAt(F.T);
    const box = $("#fp-count .dh-body");
    if (!box.querySelector(".fp-counter .odo-v")) {
      box.innerHTML = `<div class="fp-counters">${[
        ["area", t("data.fp.c_area")], ["cities", t("data.fp.c_cities")], ["regions", t("data.fp.c_regions")], ["countries", t("data.fp.c_countries")],
      ].map(([k, l], i) => `<div class="fp-counter" data-k="${k}" data-rv style="--i:${i}"><div class="l">${esc(l)}</div>
        <div class="v"><span class="odo-v"></span>${k === "area" ? `<span class="u">${esc(DH.u.area())}</span>` : ""}</div><div class="s"></div></div>`).join("")}</div>`;
      still = true;
    }
    const area = DH.cv.area(c.area);
    const set = (k, text, sub) => {
      const el = box.querySelector(`[data-k="${k}"]`);
      DH.odo(el.querySelector(".odo-v"), text, still);
      el.querySelector(".u") && (el.querySelector(".u").textContent = DH.u.area());
      el.querySelector(".s").textContent = sub || " ";
    };
    set("area", DH.nf(area, area < 100 ? 1 : 0), t(c.n === 1 ? "data.fp.cells_one" : "data.fp.cells", { n: DH.nf(c.n, 0) }));
    const topCity = c.cities.slice().sort((a, b) => (b.days || 0) - (a.days || 0))[0];
    set("cities", DH.nf(c.cities.length, 0), topCity ? t("data.fp.most", { name: topCity.city }) : "");
    set("regions", DH.nf(c.regions.length, 0), c.regions.slice(0, 2).map((r) => r.name || r.region).join(" · "));
    set("countries", DH.nf(c.countries.length, 0), c.countries.slice(0, 2).map((x) => x.name || x.country).join(" · "));
  }

  /* ------------------------------------------------------------- list */
  function paintList() {
    const box = $("#fp-list");
    const T = F.T;
    const later = (t) => (t > T ? " later" : "");
    if (!F.cells.length && !F.cities.length) { box.innerHTML = DH.empty(t("data.fp.list_none")); return; }
    if (F.tab === "regions") {
      const rows = (F.neReady ? F.regions : F.apiRegions.filter((r) => r.region).map((r) => ({ name: r.region, api: r, t: new Date(r.first_visit).getTime() })))
        .slice().sort((a, b) => ((b.api && b.api.days) || 0) - ((a.api && a.api.days) || 0) || a.t - b.t);
      DC.rank(box, rows.map((r) => ({
        label: r.name, value: (r.api && r.api.days) || 0, cls: later(r.t).trim(), t: isFinite(r.t) ? r.t : null,
        display: r.api ? `<span class="n">${esc(DH.nf(r.api.days, 0))}</span><span class="u">${esc(t("data.fp.days_u"))}</span>` : `<span class="u">${esc(t("data.fp.passed"))}</span>`,
        sub: esc([r.api ? t(r.api.cities === 1 ? "data.c.n_cities_one" : "data.c.n_cities", { n: r.api.cities }) : t("data.fp.passed_long"),
                  isFinite(r.t) ? t("data.fp.first_on", { date: DH.date(r.t) }) : ""].filter(Boolean).join(" · ")),
        pick: r.wb ? () => fly(r.wb) : null,
      })), { empty: t("data.fp.list_none") });
    } else if (F.tab === "cities") {
      const rows = F.cities.slice().sort((a, b) => (b.days || 0) - (a.days || 0));
      DC.rank(box, rows.map((c) => ({
        label: c.city, value: c.days || 0, cls: later(c.t).trim(), t: c.t,
        display: `<span class="n">${esc(DH.nf(c.days, 0))}</span><span class="u">${esc(t("data.fp.days_u"))}</span>`,
        sub: esc([c.region, t("data.fp.first_on", { date: DH.date(c.first_visit) })].filter(Boolean).join(" · ")),
        pick: c.lat != null ? () => flyTo(c.lat, c.lon, 11) : null,
      })), { empty: t("data.fp.list_none") });
    } else {
      box.innerHTML = F.firsts.length ? `<ol class="fp-firsts">${F.firsts.map((f) => `
        <li class="${f.kind}${later(f.t)}" data-t="${f.t}"><span class="d">${esc(DH.dateShort(f.t))}</span>
          <span class="t"><button type="button" class="dh-linkbtn" data-t="${f.t}">${esc(t(f.kind === "region" ? (f.passed ? "data.fp.first_region_passed" : "data.fp.first_region") : "data.fp.first_city", { name: f.name }))}</button>
          <small>${esc([f.kind === "city" ? f.region : f.country, DH.fdate(f.t, { year: "numeric" })].filter(Boolean).join(" · "))}</small></span></li>`).join("")}</ol>`
        : DH.empty(t("data.fp.list_none"));
      box.querySelectorAll("button[data-t]").forEach((b) => b.addEventListener("click", () => scrubTo(+b.dataset.t)));
    }
  }
  function fly(wb) {
    if (!F.map) return;
    const lng = (w) => w * 360 - 180;
    const lat = (w) => (Math.atan(Math.sinh(Math.PI * (1 - 2 * w))) * 180) / Math.PI;
    F.map.fitBounds([[lng(wb[0]), lat(wb[3])], [lng(wb[2]), lat(wb[1])]], { padding: 48, maxZoom: 10, duration: reduced() ? 0 : 700 });
  }
  function flyTo(la, lo, z) {
    if (!F.map) return;
    F.map.flyTo({ center: OmtMap.ll(F.map, la, lo), zoom: z, duration: reduced() ? 0 : 700, essential: false });
  }

  /* --------------------------------------------------------- scrubber
     「首次出行 → 今天」. Opens on today. The user drags it (or presses
     replay) to light the record up in the order it happened. */
  const span = () => Math.max(1, F.t1 - F.t0);
  function setT(T, opts) {
    F.T = Math.max(F.t0, Math.min(F.t1, T));
    const v = Math.round(((F.T - F.t0) / span()) * 1000);
    const rng = $("#fp-range");
    if (!opts || !opts.fromInput) rng.value = String(v);
    const pct = (v / 1000) * 100;
    $("#fp-fill").style.transform = `scaleX(${(pct / 100).toFixed(4)})`;
    $("#fp-knob").style.left = `${pct}%`;
    const atEnd = F.T >= F.t1;
    $("#fp-when").textContent = atEnd ? "" : DH.date(F.T);
    rng.setAttribute("aria-valuetext", atEnd ? t("data.c.today") : DH.date(F.T));
    if (F.hereMarker) F.hereMarker.getElement().style.visibility = atEnd ? "" : "hidden";
    counters(false);
    paintListLater();
    schedule();
  }
  function paintListLater() {
    $("#fp-list").querySelectorAll("[data-t]").forEach((li) => li.classList.toggle("later", +li.dataset.t > F.T));
  }
  function scrubTo(T) { stopPlay(); setT(T); }
  function stopPlay() { if (F.play) { cancelAnimationFrame(F.play); F.play = 0; } $("#fp-play").setAttribute("aria-pressed", "false"); }
  function replay() {
    if (F.play) { stopPlay(); return; }
    if (reduced()) { setT(F.t1); return; }
    const dur = 2800, t0 = performance.now();
    const ease = (p) => (p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2);
    $("#fp-play").setAttribute("aria-pressed", "true");
    const step = (now) => {
      const p = Math.min(1, (now - t0) / dur);
      setT(F.t0 + span() * ease(p));
      F.play = p < 1 ? requestAnimationFrame(step) : 0;
      if (!F.play) $("#fp-play").setAttribute("aria-pressed", "false");
    };
    setT(F.t0);
    F.play = requestAnimationFrame(step);
  }
  function paintTicks() {
    const box = $("#fp-ticks");
    box.innerHTML = F.firsts.filter((f) => f.t >= F.t0 && f.t <= F.t1).map((f) =>
      `<i class="${f.kind === "region" ? "first" : ""}" style="left:${(((f.t - F.t0) / span()) * 100).toFixed(3)}%"></i>`).join("");
  }
  $("#fp-range").addEventListener("input", (e) => {
    stopPlay();
    setT(F.t0 + (span() * +e.target.value) / 1000, { fromInput: true });
  });
  $("#fp-range").addEventListener("pointerdown", stopPlay);
  $("#fp-play").addEventListener("click", replay);
  document.querySelectorAll("#fp-aside [data-tab]").forEach((b) => b.addEventListener("click", () => {
    F.tab = b.dataset.tab;
    document.querySelectorAll("#fp-aside [data-tab]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    paintList();
  }));

  /* ------------------------------------------------------------- load */
  DH.block("fp-count", {
    load: async () => {
      const [fp, dv, tracks, ov] = await Promise.all([
        DH.get(DH.url("/api/analytics/footprint", { days: DH.days(), cell: 0.01 })),
        DH.get(DH.url("/api/analytics/drives", { days: DH.days(), limit: 1000 })).catch(() => ({ drives: [] })),
        DH.get(DH.url("/api/drives/tracks", { limit: 200 })).catch(() => []),
        DH.get(DH.url("/api/overview")).catch(() => null),
      ]);
      return { fp, dv, tracks, ov };
    },
    render(data, body, ctx) {
      F.gen++;
      stopPlay();
      const { fp } = data;
      F.fp = fp;
      F.apiRegions = fp.regions || [];
      F.apiCountries = (fp.countries || []).map((c) => ({ ...c }));
      F.cities = (fp.cities || []).map((c) => ({ ...c, t: new Date(c.first_visit).getTime() }));
      F.firsts = (fp.firsts || []).map((f) => ({ ...f, t: new Date(f.at).getTime() }));
      F.regions = []; F.countries = []; F.neReady = false;
      const raw = fp.cells || [];
      const cell = fp.cell_deg || 0.01;
      F.t1 = Date.now();
      F.t0 = Math.min(raw.length ? raw[0][2] * 1000 : F.t1, fp.first_drive ? new Date(fp.first_drive).getTime() : F.t1);
      if (!(F.t0 < F.t1)) F.t0 = F.t1 - DAY;
      // per-cell area (km²) shrinks with latitude
      let acc = 0;
      F.cumArea = raw.map(([y]) => { acc += (111.32 * cell) ** 2 * Math.cos(((y + 0.5) * cell * Math.PI) / 180); return acc; });
      F.cellT = raw.map((c) => c[2] * 1000);
      F.cells = raw.map(([y, x, tt]) => ({ y, x, t: tt * 1000, lat: (y + 0.5) * cell, lon: (x + 0.5) * cell }));
      const starts = new Map((data.dv.drives || []).map((d) => [d.id, new Date(d.start).getTime()]));
      const tracks = (data.tracks || []).filter((r) => starts.has(r.id) && r.points && r.points.length > 1);
      const maxArr = Math.max(1, ...(fp.top_places || []).map((p) => p.arrivals || 0));
      F.T = F.t1;
      $("#fp-t0").textContent = DH.date(F.t0);
      $("#fp-scrub-l").textContent = DH.period === "all" ? t("data.fp.scrub") : t("data.fp.scrub_period");
      const pn = $("#fp-period-n");
      pn.hidden = DH.period === "all";
      pn.innerHTML = DH.period === "all" ? "" : `${esc(t("data.fp.period_note"))} <a href="?days=all">${esc(t("data.fp.see_all"))}</a>`;
      $("#fp-lg-routes").textContent = t(tracks.length === 1 ? "data.fp.lg_routes_one" : "data.fp.lg_routes", { n: tracks.length });
      counters(true);
      paintList();

      if (!raw.length) {
        $("#fp-scrub").hidden = true;
        $("#fp-legend").hidden = true;
        $("#fp-map").classList.remove("ready");
        showMsg(t("data.fp.none_t"), t(DH.period === "all" ? "data.fp.none_b" : "data.fp.none_period"));
        return;
      }
      $("#fp-msg").hidden = true;
      $("#fp-scrub").hidden = false;
      $("#fp-legend").hidden = false;
      setT(F.t1);
      paintTicks();
      const gen = F.gen;
      initMap().then((map) => {
        if (!map || gen !== F.gen) return;
        // geometry in the map's datum, once per load
        F.cells.forEach((c) => {
          const [wx, wy] = world(c.lat, c.lon);
          const hw = cell / 360 / 2;
          const wyTop = my(c.lat + cell / 2) - my(c.lat) + wy, wyBot = my(c.lat - cell / 2) - my(c.lat) + wy;
          c.wx0 = wx - hw; c.wx1 = wx + hw; c.wy0 = wyTop; c.wy1 = wyBot;
        });
        F.routes = tracks.map((r) => {
          const w = new Float64Array(r.points.length * 2);
          let a = Infinity, b = Infinity, c = -Infinity, e = -Infinity;
          r.points.forEach(([la, lo], i) => {
            const p = world(la, lo); w[2 * i] = p[0]; w[2 * i + 1] = p[1];
            if (p[0] < a) a = p[0]; if (p[0] > c) c = p[0]; if (p[1] < b) b = p[1]; if (p[1] > e) e = p[1];
          });
          return { t: starts.get(r.id), w, wb: [a, b, c, e] };
        });
        F.places = (fp.top_places || []).filter((p) => p.lat != null).map((p) => {
          const [wx, wy] = world(p.lat, p.lon);
          return { wx, wy, r: 2.2 + 2.8 * Math.sqrt((p.arrivals || 0) / maxArr), label: p.label || p.city || "", arrivals: p.arrivals || 0, dwell: p.dwell_min || 0 };
        });
        // you are here: the one accent on the page
        const ov = data.ov;
        if (F.hereMarker) { F.hereMarker.remove(); F.hereMarker = null; }
        if (ov && ov.latitude != null && ov.longitude != null) {
          const el = document.createElement("div");
          el.className = "fp-here";
          el.setAttribute("data-label", t("data.fp.here"));
          el.setAttribute("title", ov.since ? t("data.fp.here_since", { when: DH.dt(ov.since) }) : t("data.fp.here"));
          F.hereMarker = new maplibregl.Marker({ element: el }).setLngLat(OmtMap.ll(map, ov.latitude, ov.longitude)).addTo(map);
        }
        if (fp.bounds) {
          const [la0, lo0, la1, lo1] = fp.bounds;
          map.fitBounds([OmtMap.ll(map, la0, lo0), OmtMap.ll(map, la1, lo1)], { padding: 40, maxZoom: 12, duration: 0 });
        }
        size();
        schedule();
        lightRegions(gen);
      });
      void ctx;
    },
  });
})();
