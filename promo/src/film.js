/* film.js — OhmyTesla in about 30 seconds, as code.
 *
 * The rule: a frame is a pure function of time. FILM.seek(t) sets everything
 * on screen from t alone — no CSS transitions, no timers, no randomness — so
 * scripts/render.py can capture frames in any order and get the same film.
 *
 *   0.0  the idea      your car, your data, on your own machine
 *   4.8  overview      the 3D truck and the live state
 *   9.2  Sentry        the Security page, then the push lands on a phone
 *  13.8  data          driving, charging and battery, fanned out
 *  18.2  footprint     the lifetime map
 *  21.4  controls      a risky command asks first; dashcam behind it
 *  24.8  Mac           the menu-bar panel drops from its icon
 *  27.8  end card      the mark, the promise, the address
 *
 * Screens are real pages of a hub running on synthetic data with the bundled
 * (CC BY 4.0) model, captured by scripts/capture.py into shots/<lang>/.
 */
(function () {
  "use strict";

  const q = new URLSearchParams(location.search);
  const LANG = q.get("lang") === "zh" ? "zh" : "en";
  const RENDER = q.has("render");
  const S = window.STR[LANG];
  const DURATION = 31;
  document.documentElement.lang = LANG === "zh" ? "zh-CN" : "en";
  if (RENDER) document.documentElement.classList.add("render");

  /* ── maths ─────────────────────────────────────────────────────────── */
  const clamp = (x, a = 0, b = 1) => (x < a ? a : x > b ? b : x);
  const seg = (t, a, b) => clamp((t - a) / (b - a));
  const lerp = (a, b, p) => a + (b - a) * p;
  const ease = {
    out: (x) => 1 - Math.pow(1 - x, 3),
    outQuint: (x) => 1 - Math.pow(1 - x, 5),
    inOut: (x) => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2),
    inOutSine: (x) => -(Math.cos(Math.PI * x) - 1) / 2,
  };
  // Scene opacity: in over `fi` seconds from a, out over `fo` seconds to b.
  const fade = (t, a, b, fi = 0.4, fo = 0.4) => Math.min(ease.out(seg(t, a, a + fi)), 1 - ease.inOutSine(seg(t, b - fo, b)));

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
  function put(e, { x = 0, y = 0, s = 1, o = 1 } = {}) {
    e.style.transform = `translate(${x.toFixed(2)}px, ${y.toFixed(2)}px) scale(${s.toFixed(4)})`;
    e.style.opacity = o.toFixed(3);
  }
  const shot = (name) => `shots/${LANG}/${name}.jpg`;
  const CAR = "M240,112H211.31L168,68.69A15.86,15.86,0,0,0,156.69,64H44.28A16,16,0,0,0,31,71.12L1.34,115.56A8.07,8.07,0,0,0,0,120v48a16,16,0,0,0,16,16H33a32,32,0,0,0,62,0h66a32,32,0,0,0,62,0h17a16,16,0,0,0,16-16V128A16,16,0,0,0,240,112ZM44.28,80H156.69l32,32H23ZM64,192a16,16,0,1,1,16-16A16,16,0,0,1,64,192Zm128,0a16,16,0,1,1,16-16A16,16,0,0,1,192,192Zm48-24H223a32,32,0,0,0-62,0H95a32,32,0,0,0-62,0H16V128H240Z";

  /* Words that rise out of a mask, one chunk at a time. English splits on
     spaces; Chinese chunks are given whole (a line per chunk reads better). */
  function words(parent, lines, cls, size) {
    const el = h("div", "title " + (cls || ""), parent);
    el.style.fontSize = size + "px";
    const ws = [];
    for (const line of lines) {
      const ln = h("span", "ln", el);
      const parts = LANG === "en" ? line.split(" ") : [line];
      parts.forEach((p, i) => { ws.push(h("span", "w", ln, esc(p) + (i < parts.length - 1 ? "&nbsp;" : ""))); });
    }
    return { el, ws };
  }
  function rise(ws, t, t0, stagger = 0.08, dur = 0.6) {
    ws.forEach((w, i) => {
      const p = ease.outQuint(seg(t, t0 + i * stagger, t0 + i * stagger + dur));
      w.style.transform = `translateY(${((1 - p) * 112).toFixed(2)}%)`;
    });
  }
  function caption(parent, title, sub) {
    const box = h("div", "cap", parent);
    const tt = words(box, [title], "", 60);
    const sb = h("div", "sub", box, esc(sub));
    return { box, ws: tt.ws, sb };
  }
  function playCaption(c, t, t0) {
    rise(c.ws, t, t0, 0.06, 0.6);
    const p = ease.out(seg(t, t0 + 0.35, t0 + 0.95));
    c.sb.style.opacity = p.toFixed(3);
    c.sb.style.transform = `translateY(${((1 - p) * 14).toFixed(2)}px)`;
  }
  function win(parent, src, w, url) {
    const e = h("div", "win", parent);
    e.style.width = w + "px";
    h("div", "bar", e, `<i></i><i></i><i></i><span>${esc(url || "ohmytesla.local")}</span>`);
    const img = h("img", "", e);
    img.src = src;
    img.decoding = "sync";
    return e;
  }

  /* ── scenes ────────────────────────────────────────────────────────── */
  const scenes = [];
  function scene(a, b, build) {
    const el = h("div", "scene", stage);
    const s = { a, b, el, ...build(el) };
    scenes.push(s);
    return s;
  }

  // 1 · the idea
  scene(0, 5.2, (el) => {
    const t1 = words(el, S.open, "", LANG === "en" ? 112 : 104);
    t1.el.style.left = "160px"; t1.el.style.top = "250px";
    const sub = h("div", "sub", el, esc(S.openSub));
    Object.assign(sub.style, { left: "164px", top: LANG === "en" ? "650px" : "640px", width: "1180px", fontSize: "32px" });
    return {
      seek(t) {
        // English rises word by word; Chinese line by line, a touch slower.
        rise(t1.ws, t, 0.35, LANG === "en" ? 0.16 : 0.42, 0.7);
        const p = ease.out(seg(t, 2.3, 3.1));
        put(sub, { y: (1 - p) * 16, o: p });
        t1.el.style.transform = `translateY(${(-6 * ease.inOutSine(seg(t, 0, 5.2))).toFixed(2)}px)`;
      },
    };
  });

  // 2 · overview
  scene(4.8, 9.6, (el) => {
    const w = win(el, shot("overview"), 1560);
    h("div", "shade", el);
    const c = caption(el, S.over, S.overSub);
    return {
      seek(t) {
        const p = ease.outQuint(seg(t, 4.8, 5.9));
        const z = ease.inOutSine(seg(t, 5.4, 9.6));
        put(w, { x: 180 - z * 60, y: 64 + (1 - p) * 70 - z * 40, s: lerp(0.95, 1, p) + z * 0.07, o: 1 });
        playCaption(c, t, 5.5);
      },
    };
  });

  // 3 · Sentry: the page, then the push on a phone
  scene(9.2, 14.2, (el) => {
    const w = win(el, shot("security"), 1180);
    const phone = h("div", "phone", el);
    h("div", "wall", phone);
    h("div", "clock", phone, esc(S.phoneTime));
    const note = (title, body) => h("div", "note", phone,
      `<div class="hd"><img src="assets/icon.svg" alt=""><b>${esc(S.pushApp)}</b><span>${esc(S.pushNow)}</span></div>` +
      `<div class="t">${esc(title)}</div><div class="b">${esc(body)}</div>`);
    const older = note(S.pushTitle2, S.pushBody2);
    const fresh = note(S.pushTitle, S.pushBody);
    h("div", "shade", el);
    const c = caption(el, S.sentry, S.sentrySub);
    return {
      seek(t) {
        const p = ease.outQuint(seg(t, 9.2, 10.3));
        put(w, { x: 90 - ease.inOutSine(seg(t, 9.6, 14.2)) * 30, y: 96 + (1 - p) * 60, s: 0.97 + p * 0.03, o: 1 });
        const ph = ease.outQuint(seg(t, 9.6, 10.6));
        put(phone, { x: 1400, y: 120 + (1 - ph) * 120, o: ph });
        // the older alert sits on the lock screen; the new one drops in on top
        put(older, { y: 330 + ease.inOut(seg(t, 11.0, 11.5)) * 132, o: 1 });
        const n = ease.outQuint(seg(t, 11.0, 11.7));
        put(fresh, { y: 330 - (1 - n) * 40, s: 0.94 + 0.06 * n, o: n });
        playCaption(c, t, 9.9);
      },
    };
  });

  // 4 · data: driving, charging, battery fanned out
  scene(13.8, 18.6, (el) => {
    const ws = ["driving", "charging", "battery"].map((n) => win(el, shot(n), 1180));
    h("div", "shade", el);
    const c = caption(el, S.data, S.dataSub);
    const at = [[150, 40], [420, 120], [690, 200]];
    return {
      seek(t) {
        ws.forEach((w, i) => {
          const p = ease.outQuint(seg(t, 13.8 + i * 0.32, 14.9 + i * 0.32));
          const drift = ease.inOutSine(seg(t, 14.2, 18.6));
          put(w, { x: at[i][0] - drift * (12 + i * 10), y: at[i][1] + (1 - p) * 90, s: 0.96 + 0.04 * p, o: p });
        });
        playCaption(c, t, 14.6);
      },
    };
  });

  // 5 · footprint
  scene(18.2, 21.8, (el) => {
    const w = win(el, shot("footprint"), 1560);
    const img = w.querySelector("img");
    h("div", "shade", el);
    const c = caption(el, S.foot, S.footSub);
    return {
      seek(t) {
        const p = ease.outQuint(seg(t, 18.2, 19.2));
        put(w, { x: 180, y: 64 + (1 - p) * 60, s: 0.97 + 0.03 * p, o: 1 });
        const z = ease.inOutSine(seg(t, 18.4, 21.8));
        img.style.transformOrigin = "58% 46%";
        img.style.transform = `scale(${(1 + z * 0.12).toFixed(4)})`;
        playCaption(c, t, 18.8);
      },
    };
  });

  // 6 · a risky command asks first: the camera moves in on the sheet
  scene(21.4, 25.2, (el) => {
    const w = win(el, shot("controls"), 1560);
    w.style.transformOrigin = "50% 47%";      // the sheet sits at the page's centre
    h("div", "shade", el);
    const c = caption(el, S.ctrl, S.ctrlSub);
    return {
      seek(t) {
        const p = ease.outQuint(seg(t, 21.4, 22.4));
        const z = ease.inOut(seg(t, 21.9, 24.4));
        put(w, { x: 180, y: 40 + (1 - p) * 60 - z * 60, s: 0.97 + 0.03 * p + z * 0.42, o: 1 });
        playCaption(c, t, 22.0);
      },
    };
  });

  // 7 · the Mac menu bar
  scene(24.8, 28.2, (el) => {
    h("div", "desk", el);
    const bar = h("div", "mbar", el,
      `<span class="ic on"><svg viewBox="0 0 256 256"><path d="${CAR}"/></svg></span><span>${esc(S.menuClock)}</span>`);
    const panel = h("div", "panel", el);
    const img = h("img", "", panel);
    img.src = `shots/mac/panel-${LANG}.png`;
    const c = caption(el, S.mac, S.macSub);
    c.box.style.bottom = "120px";
    return {
      seek(t) {
        bar.style.opacity = ease.out(seg(t, 24.8, 25.3)).toFixed(3);
        const p = ease.outQuint(seg(t, 25.3, 26.0));
        put(panel, { x: 1400, y: 52 - (1 - p) * 14, s: 0.96 + 0.04 * p, o: p });
        playCaption(c, t, 25.4);
      },
    };
  });

  // 8 · end card
  scene(27.8, DURATION + 1, (el) => {
    const m = h("div", "mark", el,
      `<img class="logo" src="assets/icon.svg" alt=""><div class="word">Ohmy<span>Tesla</span></div>` +
      `<div class="line">${esc(S.endLine)}</div><div class="url">${esc(S.endUrl)}</div>`);
    const [logo, word, line, url] = m.children;
    const note = h("div", "endnote", el, esc(S.endNote));
    return {
      seek(t) {
        const a = ease.outQuint(seg(t, 28.0, 28.8));
        put(logo, { y: (1 - a) * 20, s: 0.9 + 0.1 * a, o: a });
        const b = ease.outQuint(seg(t, 28.3, 29.1));
        put(word, { y: (1 - b) * 26, o: b });
        const c = ease.out(seg(t, 28.8, 29.5));
        put(line, { y: (1 - c) * 12, o: c });
        const d = ease.out(seg(t, 29.2, 29.9));
        put(url, { y: (1 - d) * 10, o: d });
        note.style.opacity = (0.9 * ease.out(seg(t, 29.6, 30.3))).toFixed(3);
      },
    };
  });

  /* ── the clock ─────────────────────────────────────────────────────── */
  function seek(t) {
    t = clamp(t, 0, DURATION);
    for (const s of scenes) {
      const o = s.b > DURATION ? Math.min(ease.out(seg(t, s.a, s.a + 0.4)), 1) : fade(t, s.a, s.b);
      s.el.style.opacity = o.toFixed(3);
      s.el.style.visibility = o <= 0.001 ? "hidden" : "visible";
      if (o > 0.001) s.seek(t);
    }
  }
  // Ready when every image has decoded and the fonts are in.
  const ready = Promise.all([document.fonts.ready,
    ...[...stage.querySelectorAll("img")].map((i) => i.decode().catch(() => null))]);
  window.FILM = { duration: DURATION, lang: LANG, seek, ready };

  // Preview: fit the stage to the window, play in real time, scrub with arrows.
  if (!RENDER) {
    const fit = () => { const k = Math.min(innerWidth / 1920, innerHeight / 1080); stage.style.transform = `scale(${k})`; };
    addEventListener("resize", fit); fit();
    const hud = h("div", "hud", document.body);
    let t = Number(q.get("t") || 0), playing = true, last = performance.now();
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
    seek(0);
  }
})();
