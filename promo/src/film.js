/* film.js — OhmyTesla in 32 seconds, as code: a launch film on a dark stage.
 *
 * The rule: a frame is a pure function of time. FILM.seek(t) sets everything
 * on screen from t alone — no CSS transitions, no timers, no randomness — so
 * scripts/render.py can capture frames in any order and get the same film.
 * The score (scripts/music.py) is cut to the same grid: 120 BPM, a bar is 2 s.
 *
 *   0.0  open        three words on the beat: your Tesla, your data, your machine
 *   4.0  reveal      the light bar comes on, then the studio lights find the truck
 *   8.0  Sentry      two pushes land on a phone; about 5 s from trigger to phone
 *  12.0  data        driving, charging, battery as panels in depth; the year in numbers
 *  16.0  footprint   the lifetime map, tilted like a table
 *  19.0  controls    the camera moves in on a confirm sheet; a tap
 *  22.0  Mac         the menu-bar panel drops; what it costs to run
 *  25.0  principles  self-hosted, open source, free: one a second
 *  28.0  end card    the truck in profile, the mark, the address
 *
 * Screens are real pages of a hub running on synthetic data with the bundled
 * (CC BY 4.0) model, captured by scripts/capture.py into shots/<lang>/; the
 * truck itself is drawn live by car3d.js from that same model.
 */
(function () {
  "use strict";

  const q = new URLSearchParams(location.search);
  const LANG = q.get("lang") === "zh" ? "zh" : "en";
  const RENDER = q.has("render");
  const S = window.STR[LANG];
  const DURATION = 32;
  const FPS = 30;
  document.documentElement.lang = LANG === "zh" ? "zh-CN" : "en";
  if (RENDER) document.documentElement.classList.add("render");

  /* ── maths ─────────────────────────────────────────────────────────── */
  const clamp = (x, a = 0, b = 1) => (x < a ? a : x > b ? b : x);
  const seg = (t, a, b) => clamp((t - a) / (b - a));
  const lerp = (a, b, p) => a + (b - a) * p;
  const ease = {
    out: (x) => 1 - Math.pow(1 - x, 3),
    outQuint: (x) => 1 - Math.pow(1 - x, 5),
    outExpo: (x) => (x >= 1 ? 1 : 1 - Math.pow(2, -10 * x)),
    in: (x) => x * x * x,
    inOut: (x) => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2),
    inOutSine: (x) => -(Math.cos(Math.PI * x) - 1) / 2,
  };
  // Scene opacity: in over `fi` seconds from a, out over `fo` seconds to b.
  const fade = (t, a, b, fi = 0.2, fo = 0.25) => Math.min(ease.out(seg(t, a, a + fi)), 1 - ease.inOutSine(seg(t, b - fo, b)));
  // A deterministic hash for grain offsets: same frame, same noise.
  const hash = (n) => { let x = Math.imul(n ^ 0x9e3779b9, 0x85ebca6b); x ^= x >>> 13; x = Math.imul(x, 0xc2b2ae35); return ((x ^ (x >>> 16)) >>> 0) / 4294967296; };

  /* ── DOM ───────────────────────────────────────────────────────────── */
  const stage = document.getElementById("stage");
  function h(tag, cls, parent, html) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    if (parent) parent.appendChild(e);
    return e;
  }
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  // One transform for everything: position, depth, tilt, scale, plus opacity and blur.
  function put(e, { x = 0, y = 0, z = 0, rx = 0, ry = 0, s = 1, o = 1, blur = 0, pre = "" } = {}) {
    e.style.transform = `${pre} translate3d(${x.toFixed(2)}px, ${y.toFixed(2)}px, ${z.toFixed(1)}px) ` +
      `rotateX(${rx.toFixed(3)}deg) rotateY(${ry.toFixed(3)}deg) scale(${s.toFixed(4)})`;
    e.style.opacity = o.toFixed(3);
    e.style.filter = blur > 0.05 ? `blur(${blur.toFixed(2)}px)` : "none";
  }
  const shot = (name) => `shots/${LANG}/${name}.jpg`;
  const CAR = "M240,112H211.31L168,68.69A15.86,15.86,0,0,0,156.69,64H44.28A16,16,0,0,0,31,71.12L1.34,115.56A8.07,8.07,0,0,0,0,120v48a16,16,0,0,0,16,16H33a32,32,0,0,0,62,0h66a32,32,0,0,0,62,0h17a16,16,0,0,0,16-16V128A16,16,0,0,0,240,112ZM44.28,80H156.69l32,32H23ZM64,192a16,16,0,1,1,16-16A16,16,0,0,1,64,192Zm128,0a16,16,0,1,1,16-16A16,16,0,0,1,192,192Zm48-24H223a32,32,0,0,0-62,0H95a32,32,0,0,0-62,0H16V128H240Z";

  /* Words that rise out of a mask, one chunk at a time. English splits on
     spaces; Chinese chunks are given whole (a line per chunk reads better). */
  function words(parent, lines, size) {
    const el = h("div", "title", parent);
    el.style.fontSize = size + "px";
    const ws = [];
    for (const line of lines) {
      const ln = h("span", "ln", el);
      const parts = LANG === "en" ? line.split(" ") : [line];
      parts.forEach((p, i) => { ws.push(h("span", "w", ln, esc(p) + (i < parts.length - 1 ? "&nbsp;" : ""))); });
    }
    return { el, ws };
  }
  function rise(ws, t, t0, stagger = 0.07, dur = 0.55) {
    ws.forEach((w, i) => {
      const p = ease.outExpo(seg(t, t0 + i * stagger, t0 + i * stagger + dur));
      w.style.transform = `translateY(${((1 - p) * 112).toFixed(2)}%)`;
    });
  }
  // A big centred line that slams in on a beat: from larger and blurred to sharp.
  function big(parent, html, size) {
    const e = h("div", "big", parent, html);
    e.style.fontSize = size + "px";
    return e;
  }
  function slam(e, t, t0, t1, { from = 1.16, blur = 18, out = 0.22 } = {}) {
    const p = ease.outExpo(seg(t, t0, t0 + 0.42));
    const q2 = ease.in(seg(t, t1 - out, t1));
    const s = lerp(from, 1, p) * (1 + 0.06 * q2) + 0.012 * seg(t, t0, t1);
    put(e, { pre: "translateY(-50%)", s, o: Math.min(p * 1.6, 1) * (1 - q2), blur: (1 - p) * blur + q2 * 10 });
  }
  // The accent on the last word (English) or on 设备 (Chinese).
  function accent(line) {
    if (LANG === "zh") return esc(line).replace("设备", '<span class="r">设备</span>');
    const i = line.lastIndexOf(" ");
    return esc(line.slice(0, i + 1)) + `<span class="r">${esc(line.slice(i + 1))}</span>`;
  }
  function count(el, t, t0, dur, to) {
    const p = ease.outQuint(seg(t, t0, t0 + dur));
    el.textContent = Math.round(to * p).toLocaleString(LANG === "zh" ? "zh-CN" : "en-US");
  }
  function caption(parent, title, sub) {
    const box = h("div", "cap", parent);
    const tt = words(box, [title], 76);
    const sb = h("div", "sub", box, esc(sub));
    return { box, ws: tt.ws, sb };
  }
  function playCaption(c, t, t0) {
    rise(c.ws, t, t0, 0.06, 0.55);
    const p = ease.out(seg(t, t0 + 0.3, t0 + 0.9));
    put(c.sb, { y: (1 - p) * 14, o: p });
  }
  function win(parent, src, w) {
    const e = h("div", "win", parent);
    e.style.width = w + "px";
    h("div", "bar", e, `<i></i><i></i><i></i><span>ohmytesla.local</span>`);
    const box = h("div", "", e);
    box.style.position = "relative";
    const img = h("img", "", box);
    img.src = src;
    img.decoding = "sync";
    const sheen = h("div", "sheen", e);
    return { e, box, img, sheen };
  }
  // A band of light crossing a panel once.
  const sheen = (w, t, t0, dur = 1.0) => {
    const p = ease.inOutSine(seg(t, t0, t0 + dur));
    w.sheen.style.backgroundPosition = `${(100 - p * 100).toFixed(1)}% 0`;
    w.sheen.style.opacity = (p > 0 && p < 1 ? 1 : 0).toString();
  };

  /* ── layers over every scene: flash, vignette, grain ───────────────── */
  const scenes = [];
  function scene(a, b, build) {
    const el = h("div", "scene", stage);
    const s = { a, b, el, ...build(el) };
    scenes.push(s);
    return s;
  }

  // 1 · open: three words on the beat
  scene(0, 4.05, (el) => {
    const line = h("div", "line", el);
    const halo = h("div", "halo", el);
    const size = LANG === "en" ? 196 : 176;
    const w = [big(el, esc(S.open[0]), size), big(el, esc(S.open[1]), size), big(el, accent(S.open[2]), size)];
    return {
      seek(t) {
        const g = ease.outExpo(seg(t, 0.15, 0.75));
        line.style.transform = `scaleX(${g.toFixed(4)})`;
        line.style.opacity = (1 - ease.inOutSine(seg(t, 0.85, 1.1))).toFixed(3);
        slam(w[0], t, 1.0, 2.0);
        slam(w[1], t, 2.0, 3.0);
        slam(w[2], t, 3.0, 4.05, { out: 0.35 });
        halo.style.opacity = (ease.out(seg(t, 3.0, 3.4)) * (1 - seg(t, 3.7, 4.05))).toFixed(3);
      },
    };
  });

  // 2 · reveal: the truck (drawn by car3d.js under this scene)
  const reveal = scene(3.95, 8.1, (el) => {
    const glow = h("div", "glow", el);
    const lock = h("div", "lockup", el,
      `<div class="row"><img src="assets/icon.svg" alt=""><div class="word">Ohmy<span>Tesla</span></div></div>` +
      `<div class="tag">${esc(S.tag)}</div>`);
    lock.style.top = "110px";
    const [row, tag] = lock.children;
    return {
      glow,
      view(t) {
        // the light bar flickers on at the drop; the room follows
        const on = t < 4.0 ? 0 : t < 4.06 ? 1 : t < 4.1 ? 0.25 : 1;
        const room = ease.out(seg(t, 4.2, 5.4));
        const c = ease.inOutSine(seg(t, 4.0, 8.1));
        const az = lerp(16, 50, c) * Math.PI / 180, el2 = lerp(5, 10, c) * Math.PI / 180, r = lerp(6.6, 9.4, c);
        return {
          cam: [Math.sin(az) * Math.cos(el2) * r, 0.95 + Math.sin(el2) * r, Math.cos(az) * Math.cos(el2) * r],
          at: [0, lerp(0.95, 1.05, c), lerp(0.9, 0.1, c)], fov: 30,
          env: lerp(0.2, 1.7, c), envI: room, key: 0.55 * room, rim: room,
          sweep: [2.1, 1.5, lerp(3.2, -3.2, ease.inOutSine(seg(t, 4.4, 6.6)))], sweepI: room * (1 - seg(t, 6.2, 6.8)),
          lights: on, exposure: 1,
        };
      },
      seek(t, car) {
        if (car) {
          const on = t < 4.0 ? 0 : t < 4.06 ? 1 : t < 4.1 ? 0.25 : 1;
          const room = ease.out(seg(t, 4.2, 5.4));
          put(glow, { x: car.bar.x, y: car.bar.y, s: 1 + 0.15 * (1 - room), o: on * (1 - 0.55 * room) });
        }
        const a = ease.outExpo(seg(t, 5.6, 6.3));
        put(row, { y: (1 - a) * 24, o: a });
        const b = ease.out(seg(t, 6.0, 6.7));
        put(tag, { y: (1 - b) * 14, o: b });
      },
    };
  });

  // 3 · Sentry: two pushes land on a phone
  scene(7.95, 12.1, (el) => {
    const persp = h("div", "persp", el);
    const phone = h("div", "phone", persp);
    h("div", "wall", phone);
    const alarm = h("div", "alarm", phone);
    h("div", "island", phone);
    h("div", "date", phone, esc(S.phoneDate));
    h("div", "clock", phone, esc(S.phoneTime));
    const note = ([title, body], crit) => h("div", "note" + (crit ? " crit" : ""), phone,
      `<div class="hd"><img src="assets/icon.svg" alt=""><b>${esc(S.pushApp)}</b><span>${esc(S.pushNow)}</span></div>` +
      `<div class="t">${esc(title)}</div><div class="b">${esc(body)}</div>`);
    const n1 = note(S.push1, false);
    const n2 = note(S.push2, true);
    const head = words(el, S.sentry, LANG === "en" ? 92 : 88);
    Object.assign(head.el.style, { left: "140px", top: "250px" });
    const stat = h("div", "stat", el, `<div><span class="n">${esc(S.stat)}</span></div><div class="l">${esc(S.statSub)}</div>`);
    Object.assign(stat.style, { left: "140px", top: "560px" });
    const [statN, statL] = stat.children;
    return {
      seek(t) {
        const p = ease.outExpo(seg(t, 8.0, 8.8));
        const tilt = ease.inOutSine(seg(t, 8.0, 12.1));
        const k = t - 9.6;
        const shake = k > 0 && k < 0.7 ? 7 * Math.sin(2 * Math.PI * 26 * k) * Math.exp(-k * 7) : 0;
        put(phone, { x: 1250 + shake, y: 110 + (1 - p) * 900, ry: lerp(-16, -8, tilt), rx: 3, o: 1 });
        const a = ease.outExpo(seg(t, 8.6, 9.2));
        put(n1, { y: 300 - (1 - a) * 50 + ease.inOut(seg(t, 9.6, 10.0)) * 136, s: 0.94 + 0.06 * a, o: a });
        const b = ease.outExpo(seg(t, 9.6, 10.2));
        put(n2, { y: 300 - (1 - b) * 50, s: 0.94 + 0.06 * b, o: b });
        alarm.style.opacity = (seg(t, 9.6, 9.7) * (0.55 + 0.45 * Math.cos(2 * Math.PI * 2 * (t - 9.6))) * (1 - seg(t, 11.4, 12.0))).toFixed(3);
        rise(head.ws, t, 8.15, LANG === "en" ? 0.06 : 0.45, 0.55);
        const s = ease.outExpo(seg(t, 10.4, 10.9));
        put(statN, { s: lerp(1.2, 1, s), o: s, blur: (1 - s) * 14, pre: "" });
        statN.style.transformOrigin = "0 70%";
        const l = ease.out(seg(t, 10.7, 11.2));
        put(statL, { y: (1 - l) * 12, o: l });
      },
    };
  });

  // 4 · data: three screens in depth, the year in numbers
  scene(11.95, 16.1, (el) => {
    const persp = h("div", "persp", el);
    const ws = ["driving", "charging", "battery"].map((n) => win(persp, shot(n), 1100));
    const head = words(el, S.data, LANG === "en" ? 76 : 72);
    Object.assign(head.el.style, { left: "140px", top: "130px" });
    const nums = S.stats.map(([n, u, l], i) => {
      const box = h("div", "num", el, `<div class="n"><span>0</span>${esc(u)}</div><div class="l">${esc(l)}</div>`);
      Object.assign(box.style, { left: "140px", top: `${380 + i * 190}px` });
      return { box, n: box.querySelector("span"), to: n };
    });
    const at = [[780, 120, 0], [930, 250, -280], [1080, 380, -560]];
    return {
      seek(t) {
        const drift = ease.inOutSine(seg(t, 12.0, 16.1));
        ws.forEach((w, i) => {
          const p = ease.outExpo(seg(t, 12.0 + i * 0.25, 12.9 + i * 0.25));
          put(w.e, { x: at[i][0] + (1 - p) * 420 - drift * 90, y: at[i][1] + (1 - p) * 60, z: at[i][2] - (1 - p) * 500,
                     ry: lerp(-30, -20, drift), rx: 5, o: p });
          sheen(w, t, 12.9 + i * 0.25, 1.1);
        });
        rise(head.ws, t, 12.15, 0.06, 0.55);
        nums.forEach((m, i) => {
          const a = ease.outExpo(seg(t, 12.6 + i * 0.3, 13.2 + i * 0.3));
          put(m.box, { y: (1 - a) * 30, o: a });
          count(m.n, t, 12.6 + i * 0.3, 1.1, m.to);
        });
      },
    };
  });

  // 5 · footprint: the lifetime map, tilted like a table
  scene(15.95, 19.1, (el) => {
    const persp = h("div", "persp", el);
    const w = win(persp, shot("footprint"), 1500);
    h("div", "shade", el);
    const c = caption(el, S.foot, S.footSub);
    return {
      seek(t) {
        const p = ease.outExpo(seg(t, 16.0, 16.9));
        const z = ease.inOutSine(seg(t, 16.0, 19.1));
        put(w.e, { x: 210, y: 70 + (1 - p) * 120, rx: lerp(30, 14, z), s: lerp(0.9, 1.04, z), o: p });
        sheen(w, t, 16.6, 1.2);
        playCaption(c, t, 16.35);
      },
    };
  });

  // 6 · controls: the camera moves in on the confirm sheet; a tap
  scene(18.95, 22.1, (el) => {
    const w = win(el, shot("controls"), 1560);
    w.e.style.transformOrigin = "50% 47%";      // the sheet sits at the page's centre
    const ripple = h("div", "ripple", w.box);
    Object.assign(ripple.style, { left: "60.6%", top: "54.4%" });
    h("div", "shade", el);
    const c = caption(el, S.ctrl, S.ctrlSub);
    return {
      seek(t) {
        const p = ease.outExpo(seg(t, 19.0, 19.8));
        const z = ease.inOut(seg(t, 19.3, 21.0));
        put(w.e, { x: 180, y: 40 + (1 - p) * 80 - z * 60, s: 0.96 + 0.04 * p + z * 0.42, o: p });
        const r = seg(t, 20.6, 21.2);
        ripple.style.transform = `scale(${lerp(0.25, 1.15, ease.out(r)).toFixed(3)})`;
        ripple.style.opacity = (r > 0 && r < 1 ? 1 - r : 0).toFixed(3);
        playCaption(c, t, 19.3);
      },
    };
  });

  // 7 · the Mac menu bar
  scene(21.95, 25.1, (el) => {
    h("div", "desk", el);
    const bar = h("div", "mbar", el,
      `<span class="ic on"><svg viewBox="0 0 256 256"><path d="${CAR}"/></svg></span><span>${esc(S.menuClock)}</span>`);
    const panel = h("div", "panel", el);
    const img = h("img", "", panel);
    img.src = `shots/mac/panel-${LANG}.png`;
    const head = words(el, [S.mac], LANG === "en" ? 92 : 88);
    Object.assign(head.el.style, { left: "140px", top: "330px" });
    const chips = h("div", "chips", el);
    Object.assign(chips.style, { left: "144px", top: "500px" });
    const cs = S.macChips.map((c) => h("div", "chip", chips, `<i></i>${esc(c)}`));
    return {
      seek(t) {
        bar.style.opacity = ease.out(seg(t, 22.0, 22.3)).toFixed(3);
        const p = ease.outExpo(seg(t, 22.4, 23.0));
        put(panel, { x: 1400, y: 52 - (1 - p) * 16, s: 0.95 + 0.05 * p, o: p });
        rise(head.ws, t, 22.2, 0.06, 0.55);
        cs.forEach((c, i) => {
          const a = ease.outExpo(seg(t, 22.9 + i * 0.3, 23.5 + i * 0.3));
          put(c, { x: (1 - a) * -40, o: a });
        });
      },
    };
  });

  // 8 · principles: one a second
  scene(24.95, 28.05, (el) => {
    const halo = h("div", "halo", el);
    const size = LANG === "en" ? 200 : 184;
    const w = S.words.map((x, i) => big(el, i === S.words.length - 1 ? `<span class="r">${esc(x)}</span>` : esc(x), size));
    return {
      seek(t) {
        w.forEach((e, i) => slam(e, t, 25.0 + i, 26.0 + i + (i === w.length - 1 ? 0.05 : 0), { from: 1.22 }));
        halo.style.opacity = (ease.out(seg(t, 27.0, 27.4)) * (1 - seg(t, 27.7, 28.05))).toFixed(3);
      },
    };
  });

  // 9 · end card: the truck in profile, the mark, the address
  const end = scene(27.95, DURATION + 1, (el) => {
    const glow = h("div", "glow", el);
    const lock = h("div", "lockup", el,
      `<div class="row"><img src="assets/icon.svg" alt=""><div class="word">Ohmy<span>Tesla</span></div></div>`);
    lock.style.top = "120px";
    const line = h("div", "endline", el, esc(S.endLine));
    line.style.top = "250px";
    const url = h("div", "url", el, esc(S.endUrl));
    url.style.top = "306px";
    const credit = h("div", "credit", el, esc(S.credit));
    return {
      glow,
      view(t) {
        const c = ease.outQuint(seg(t, 28.0, 32.0));
        return {
          cam: [lerp(11.4, 9.9, c), 1.25, lerp(0.9, 0.2, c)], at: [0, 1.35, 0], fov: 27,
          env: lerp(1.9, 2.5, c), envI: 1, key: 0.5, rim: 1, sweep: [2.4, 1.9, lerp(-3, 3, c)], sweepI: 0.6,
          lights: 1, exposure: 1,
        };
      },
      seek(t, car) {
        if (car) put(glow, { x: car.bar.x, y: car.bar.y, s: 0.5, o: car.bar.front ? 0.35 : 0 });
        const a = ease.outExpo(seg(t, 28.3, 29.0));
        put(lock, { y: (1 - a) * 24, o: a });
        const b = ease.out(seg(t, 28.8, 29.4));
        put(line, { y: (1 - b) * 12, o: b });
        const d = ease.out(seg(t, 29.2, 29.8));
        put(url, { y: (1 - d) * 10, o: d });
        credit.style.opacity = (0.95 * ease.out(seg(t, 29.6, 30.2))).toFixed(3);
      },
    };
  });

  const flash = h("div", "flash", stage);
  h("div", "vignette", stage);
  const grain = h("div", "grain", stage);
  {
    // film grain: one noise tile, shifted per frame by a hash of the frame number
    const c = document.createElement("canvas");
    c.width = c.height = 256;
    const g = c.getContext("2d");
    const im = g.createImageData(256, 256);
    let s = 1234567;
    for (let i = 0; i < im.data.length; i += 4) {
      s = (Math.imul(s, 1103515245) + 12345) >>> 0;
      const v = (s >>> 24);
      im.data[i] = im.data[i + 1] = im.data[i + 2] = v;
      im.data[i + 3] = 255;
    }
    g.putImageData(im, 0, 0);
    grain.style.backgroundImage = `url(${c.toDataURL()})`;
  }

  /* ── the clock ─────────────────────────────────────────────────────── */
  const car3d = () => window.CAR3D;
  function seek(t) {
    t = clamp(t, 0, DURATION);
    // the truck is on screen in the reveal and on the end card
    const carOn = [reveal, end].find((s) => t >= s.a && t <= s.b);
    const C = car3d();
    let res = null;
    if (C) {
      if (carOn) {
        res = C.render(carOn.view(t));
        const o = carOn === end ? Math.min(ease.out(seg(t, end.a, end.a + 0.5)), 1 - seg(t, 31.4, 32.0))
                                : fade(t, reveal.a, reveal.b, 0.05, 0.3);
        C.canvas.style.opacity = o.toFixed(3);
      } else {
        C.canvas.style.opacity = "0";
      }
    }
    for (const s of scenes) {
      let o = s.b > DURATION ? Math.min(ease.out(seg(t, s.a, s.a + 0.3)), 1 - seg(t, 31.4, 32.0)) : fade(t, s.a, s.b);
      s.el.style.opacity = o.toFixed(3);
      s.el.style.visibility = o <= 0.001 ? "hidden" : "visible";
      if (o > 0.001) s.seek(t, s === carOn ? res : null);
    }
    // a flash on the two big hits: the light bar, the end card
    const f = Math.max(0.18 * Math.exp(-Math.max(0, t - 4.0) * 12) * (t >= 4.0 ? 1 : 0),
                       0.14 * Math.exp(-Math.max(0, t - 28.0) * 10) * (t >= 28.0 ? 1 : 0));
    flash.style.opacity = f.toFixed(3);
    const fi = Math.floor(t * FPS / 2);         // grain at 15 fps, like film; also kinder to the encoder
    grain.style.transform = `translate(${(-hash(fi) * 64).toFixed(1)}px, ${(-hash(fi + 7777) * 64).toFixed(1)}px)`;
  }

  // Ready when every image has decoded, the fonts are in and the truck has loaded.
  const truck = new Promise((r) => (window.CAR3D ? r() : addEventListener("car3d", r, { once: true })))
    .then(() => window.CAR3D.ready);
  const ready = Promise.all([document.fonts.ready, truck,
    ...[...stage.querySelectorAll("img")].map((i) => i.decode().catch(() => null))]);
  window.FILM = { duration: DURATION, lang: LANG, seek, ready };

  // Preview: fit the stage to the window, play in real time, scrub with arrows.
  if (!RENDER) {
    const fit = () => { const k = Math.min(innerWidth / 1920, innerHeight / 1080); stage.style.transform = `scale(${k})`; };
    addEventListener("resize", fit); fit();
    const hud = h("div", "hud", document.body);
    let t = Number(q.get("t") || 0), playing = !q.has("still"), last = performance.now();
    addEventListener("keydown", (e) => {
      if (e.key === " ") playing = !playing;
      if (e.key === "ArrowRight") t += e.shiftKey ? 0.1 : 1;
      if (e.key === "ArrowLeft") t -= e.shiftKey ? 0.1 : 1;
    });
    const loop = (now) => {
      if (playing) t += (now - last) / 1000;
      last = now;
      if (t > DURATION) t = 0;
      seek(t);
      hud.textContent = `${t.toFixed(2)} s · ${LANG} · space pause · ←→ seek`;
      requestAnimationFrame(loop);
    };
    ready.then(() => requestAnimationFrame(loop));
  } else {
    ready.then(() => seek(0));
  }
})();
