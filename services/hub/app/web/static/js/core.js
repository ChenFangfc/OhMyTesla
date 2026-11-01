/* App-level plumbing shared by every page: which car is selected, one live
 * data stream for everyone, sheets and menus, the global alert region, and
 * sending commands.
 *
 * Classic script (loaded after i18n.js / util.js / icons.js) so templates and
 * ES modules alike can use the globals it defines:
 *   Car, Live, Sheet, Menu, Alerts, toast, Cmd, Odo, initShell
 */

/* ---------------------------------------------------------------- cars */
const Car = (() => {
  let list = [];
  let current = null;
  let loading = null;
  const subs = new Set();
  const KEY = "omt.car";

  function fromUrl() {
    const v = new URLSearchParams(location.search).get("car");
    return v && /^\d+$/.test(v) ? Number(v) : null;
  }
  function saved() {
    try { const v = localStorage.getItem(KEY); return v ? Number(v) : null; } catch (e) { return null; }
  }

  /* Several scripts call load() during boot; they share one request. */
  function load() {
    if (loading) return loading;
    loading = (async () => {
      try { list = await apiGet("/api/cars"); } catch (e) { list = []; }
      // Active = still in the account. Archived cars (sold, removed) stay
      // selectable for their history but never become the default. Evaluated
      // live on every read: the stream may arrive after this list does.
      list.forEach((c) => Object.defineProperty(c, "active", { get: () => isActive(c), configurable: true }));
      const want = fromUrl() ?? saved();
      const ok = (id) => list.some((c) => c.id === id);
      current = ok(want) ? want : (list.find((c) => c.active) || list[0] || {}).id ?? null;
      return list;
    })();
    setTimeout(() => { loading = null; }, 4000);   // a later call may refresh
    return loading;
  }

  function isActive(c) {
    if (c.archived === true) return false;
    const all = Live.cars();
    if (!Object.keys(all).length) return true;          // no data yet: don't claim "archived"
    const l = all[String(c.id)];
    if (!l) return false;
    if (l._live === "1") return true;
    const age = Number(l._seed_age);
    return !(age && age > 14 * 86400);
  }

  function label(c) {
    if (!c) return "—";
    if (c.name) return c.name;
    const v = (c.vin || "").toUpperCase();
    const byVin = { C: "Cybertruck", "3": "Model 3", Y: "Model Y", S: "Model S", X: "Model X" }[v[3]];
    return byVin || (c.model ? (c.model.length === 1 ? `Model ${c.model}` : c.model) : `Car ${c.id}`);
  }

  function variant(c) {
    if (!c) return "";
    if (c.marketing_name) return c.marketing_name;
    const tb = (c.trim_badging || "").toUpperCase();
    if (tb === "CYBERBEAST") return "Cyberbeast";
    return tb && tb !== "DEFAULT" ? tb : "";
  }

  function select(id) {
    if (id === current) return;
    current = id;
    try { localStorage.setItem(KEY, String(id)); } catch (e) { /* private mode */ }
    const u = new URL(location.href);
    if (u.searchParams.has("car")) { u.searchParams.set("car", id); history.replaceState(null, "", u); }
    subs.forEach((fn) => { try { fn(id); } catch (e) { console.error(e); } });
    paintSwitchers();
  }

  function paintSwitchers() {
    const c = list.find((x) => x.id === current);
    const st = c ? (Live.cars()[String(c.id)] || {}) : {};
    document.querySelectorAll("[data-carswitch]").forEach((el) => {
      const nm = el.querySelector(".cs-name"), sb = el.querySelector(".cs-sub");
      if (nm) nm.textContent = c ? label(c) : t("cars.none");
      const parts = [];
      if (c && !c.active) parts.push(t("cars.archived"));
      else if (st.state) parts.push(stateLabel2(st.state));
      if (st.battery_level) parts.push(fmt.pct(Number(st.battery_level)));
      if (sb) sb.textContent = parts.join(" · ");
      const dot = el.querySelector(".status-dot");
      if (dot) dot.className = "status-dot " + dotClass(st, c);
      el.classList.toggle("single", list.length <= 1);
      el.setAttribute("aria-label", [t("cars.choose"), c ? label(c) : "", parts.join(", ")].filter(Boolean).join(" — "));
    });
  }

  async function openMenu(e) {
    if (list.length <= 1) return;
    const live = Live.cars();
    const anchor = e && e.currentTarget;
    const id = await Menu.open(anchor, list.map((c) => {
      const st = live[String(c.id)] || {};
      return {
        value: c.id, label: label(c), checked: c.id === current, dot: dotClass(st, c),
        sub: c.active ? [variant(c), stateLabel2(st.state), st.battery_level ? fmt.pct(Number(st.battery_level)) : ""].filter(Boolean).join(" · ")
                      : t("cars.archived_hint"),
      };
    }), { title: t("cars.choose") });
    if (id != null) select(Number(id));
  }

  return {
    load, select, label, variant, openMenu, paint: paintSwitchers,
    get current() { return current; },
    get list() { return list; },
    info(id) { return list.find((c) => c.id === (id ?? current)); },
    onChange(fn) { subs.add(fn); return () => subs.delete(fn); },
  };
})();

function stateLabel2(s) {
  const known = ["parked", "asleep", "offline", "online", "driving", "charging", "updating", "suspended"];
  return s ? t(known.includes(s) ? "state." + s : "state.unknown") : "";
}

function dotClass(st, c) {
  if (c && c.active === false) return "archived";
  const s = (st && st.state) || "";
  if (s === "charging") return "charging";
  if (s === "driving") return "driving";
  if (s === "online") return "live";
  if (s === "offline") return "down";
  return "asleep";
}

/* ---------------------------------------------------------- live data
   One EventSource for the page. State messages update the per-car map;
   typed messages ({type: "alert"}) are events, not state, and go out as
   `omt:event` instead. */
const Live = (() => {
  const subs = new Set();
  let cars = {};
  const seen = {};          // car id -> ms timestamp of the newest value
  let started = false;
  function start() {
    if (started) return;
    started = true;
    cars = liveConnect((c, msg) => {
      cars = c;
      if (msg && msg.type === "snapshot") {
        for (const [id, v] of Object.entries(msg.cars || {})) {
          const age = Number(v._live_age ?? v._seed_age);
          if (isFinite(age)) seen[id] = Date.now() - age * 1000;
        }
      } else if (msg && msg.car_id) {
        seen[String(msg.car_id)] = Date.now();
      }
      subs.forEach((fn) => { try { fn(c); } catch (e) { console.error(e); } });
    }, (ev) => window.dispatchEvent(new CustomEvent("omt:event", { detail: ev })));
  }
  return {
    start,
    cars: () => cars,
    car: (id) => cars[String(id ?? Car.current)] || {},
    /* When the newest value for this car arrived (ms), or null. */
    seenAt: (id) => seen[String(id ?? Car.current)] ?? null,
    subscribe(fn) { subs.add(fn); start(); return () => subs.delete(fn); },
  };
})();

/* ------------------------------------------------------------- dialogs
   Native <dialog> with showModal(): focus trap, Escape and an inert page
   for free. The dialog itself is a transparent full-screen frame holding a
   scrim and a panel, so the scrim can follow a drag. Desktop: centred
   modal, scale(.96) + fade. Phone: bottom sheet on the drawer curve, drag
   the grip down to dismiss (velocity or distance), rubber-band upward. */
const Sheet = (() => {
  let dlg = null, panel = null, scrim = null, resolveFn = null, closing = false;
  const phone = () => matchMedia("(max-width: 640px)").matches;

  function finish(value) {
    if (resolveFn) { const r = resolveFn; resolveFn = null; r(value); }
  }

  function close(value = null) {
    if (!dlg || closing) return;
    closing = true;
    const d = dlg;
    d.classList.remove("in");
    d.classList.add("out");
    panel.style.transform = ""; scrim.style.opacity = "";
    const done = () => {
      if (d.open) d.close();
      d.remove();
      document.documentElement.classList.remove("sheet-open");
      if (dlg === d) { dlg = panel = scrim = null; }
      closing = false;
    };
    const ms = matchMedia("(prefers-reduced-motion: reduce)").matches ? 160 : 260;
    setTimeout(done, ms);
    finish(value);
  }

  function rubber(dy, h) { return h * (1 - 1 / (Math.abs(dy) / h * 0.55 + 1)); }

  function bindDrag(grip) {
    let drag = null;
    grip.addEventListener("pointerdown", (e) => {
      if (!phone() || drag) return;
      grip.setPointerCapture(e.pointerId);
      drag = { y0: e.clientY, t0: e.timeStamp, h: panel.offsetHeight, dy: 0 };
      panel.style.transition = "none"; scrim.style.transition = "none";
    });
    grip.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const dy = e.clientY - drag.y0;
      drag.dy = dy < 0 ? -rubber(-dy, drag.h) : dy;
      panel.style.transform = `translateY(${drag.dy}px)`;
      scrim.style.opacity = String(1 - Math.max(0, drag.dy) / drag.h);
    });
    const end = (e) => {
      if (!drag) return;
      const v = drag.dy / Math.max(1, e.timeStamp - drag.t0);
      panel.style.transition = ""; scrim.style.transition = "";
      if (v > 0.11 || drag.dy > drag.h * 0.4) close(null);
      else { panel.style.transform = ""; scrim.style.opacity = ""; }
      drag = null;
    };
    grip.addEventListener("pointerup", end);
    grip.addEventListener("pointercancel", end);
  }

  /* open({title, lead, html, actions:[{label, kind, value, href}], wide})
     → Promise<value | null>. `lead` and `html` are trusted markup. */
  function open({ title, lead, html, actions, wide, className } = {}) {
    if (dlg) { const d = dlg; dlg = null; d.remove(); finish(null); closing = false; }
    dlg = document.createElement("dialog");
    dlg.className = "sheet" + (wide ? " wide" : "") + (className ? " " + className : "");
    const tid = "sheet-t-" + Math.random().toString(36).slice(2, 8);
    if (title) dlg.setAttribute("aria-labelledby", tid);
    dlg.innerHTML = `<div class="sheet-scrim"></div>
      <div class="sheet-panel">
        <div class="sheet-grip" aria-hidden="true"><i></i></div>
        ${title ? `<h2 class="sheet-t" id="${tid}">${esc(title)}</h2>` : ""}
        ${lead ? `<p class="sheet-lead">${lead}</p>` : ""}
        ${html ? `<div class="sheet-body">${html}</div>` : ""}
        ${actions && actions.length ? `<div class="sheet-acts">${actions.map((a, i) =>
          `<button type="button" class="btn ${a.kind || ""}" data-i="${i}">${esc(a.label)}</button>`).join("")}</div>` : ""}
      </div>`;
    panel = dlg.querySelector(".sheet-panel");
    scrim = dlg.querySelector(".sheet-scrim");
    document.body.append(dlg);
    document.documentElement.classList.add("sheet-open");
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); close(null); });
    scrim.addEventListener("click", () => close(null));
    bindDrag(dlg.querySelector(".sheet-grip"));
    dlg.querySelectorAll(".sheet-acts .btn").forEach((b) => b.addEventListener("click", () => {
      const a = actions[Number(b.dataset.i)];
      if (a.href) { location.href = a.href; return; }
      close(a.value ?? a.label);
    }));
    dlg.showModal();
    // Focus the confirming action — unless it is destructive, then Cancel.
    const btns = [...dlg.querySelectorAll(".sheet-acts .btn")];
    const last = btns[btns.length - 1];
    const focus = last && last.classList.contains("danger") ? btns[0] : last;
    if (focus) focus.focus({ preventScroll: true });
    requestAnimationFrame(() => requestAnimationFrame(() => dlg && dlg.classList.add("in")));
    return new Promise((res) => { resolveFn = res; });
  }

  return { open, close, get el() { return dlg; } };
})();

/* --------------------------------------------------------------- menus
   A short list of choices. With a fine pointer and room, a popover grows
   out of its trigger; on a phone the same list is a sheet. */
const Menu = (() => {
  let pop = null, resolveFn = null, trigger = null;

  function close(value = null) {
    if (pop) {
      const p = pop; pop = null;
      p.classList.remove("in");
      setTimeout(() => p.remove(), 140);
      document.removeEventListener("pointerdown", outside, true);
      document.removeEventListener("keydown", onKey, true);
      if (trigger) { trigger.setAttribute("aria-expanded", "false"); trigger.focus({ preventScroll: true }); }
    }
    if (resolveFn) { const r = resolveFn; resolveFn = null; r(value); }
  }
  function outside(e) { if (pop && !pop.contains(e.target) && !(trigger && trigger.contains(e.target))) close(null); }
  function onKey(e) {
    if (!pop) return;
    const items = [...pop.querySelectorAll(".menu-i")];
    const i = items.indexOf(document.activeElement);
    if (e.key === "Escape") { e.preventDefault(); close(null); }
    else if (e.key === "ArrowDown") { e.preventDefault(); items[(i + 1) % items.length]?.focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); items[(i - 1 + items.length) % items.length]?.focus(); }
    else if (e.key === "Tab") close(null);
  }

  function itemHtml(it, i) {
    if (it.sep) return `<div class="menu-sep" role="separator"></div>`;
    return `<button type="button" class="menu-i" role="menuitem${it.checked != null ? "radio" : ""}" data-i="${i}"
        ${it.checked != null ? `aria-checked="${it.checked ? "true" : "false"}"` : ""}>
      ${it.dot ? `<span class="status-dot ${esc(it.dot)}"></span>` : it.icon ? `<span class="menu-ic">${icon(it.icon, 16)}</span>` : ""}
      <span class="menu-tx"><span class="menu-l">${esc(it.label)}</span>${it.sub ? `<span class="menu-s">${esc(it.sub)}</span>` : ""}</span>
      ${it.checked ? `<span class="menu-ck">${icon("check", 16)}</span>` : ""}</button>`;
  }

  /* open(anchor, items:[{label, sub, value, checked, icon, dot} | {sep:true}], {title})
     → Promise<value | null> */
  function open(anchor, items, opts = {}) {
    close(null);
    const wide = matchMedia("(hover: hover) and (pointer: fine) and (min-width: 641px)").matches;
    if (!wide || !anchor) {
      const html = `<div class="menu-list" role="menu">${items.map(itemHtml).join("")}</div>`;
      const p = Sheet.open({ title: opts.title, html });
      Sheet.el?.querySelectorAll(".menu-i").forEach((b) => b.addEventListener("click", () => Sheet.close(items[Number(b.dataset.i)].value)));
      // start on the current choice, as the popover does (not on the first row)
      Sheet.el?.querySelector('.menu-i[aria-checked="true"]')?.focus({ preventScroll: true });
      return p;
    }
    trigger = anchor;
    pop = document.createElement("div");
    pop.className = "menu";
    pop.setAttribute("role", "menu");
    if (opts.title) pop.setAttribute("aria-label", opts.title);
    pop.innerHTML = items.map(itemHtml).join("");
    document.body.append(pop);
    const r = anchor.getBoundingClientRect();
    const pw = pop.offsetWidth, ph = pop.offsetHeight;
    const below = r.bottom + 6 + ph < innerHeight - 8;
    const left = Math.max(8, Math.min(innerWidth - pw - 8, (opts.align === "left" ? r.left : r.right - pw)));
    pop.style.left = left + "px";
    pop.style.top = (below ? r.bottom + 6 : r.top - 6 - ph) + "px";
    pop.style.transformOrigin = `${r.left + r.width / 2 - left}px ${below ? "top" : "bottom"}`;
    anchor.setAttribute("aria-expanded", "true");
    pop.querySelectorAll(".menu-i").forEach((b) => b.addEventListener("click", () => close(items[Number(b.dataset.i)].value)));
    document.addEventListener("pointerdown", outside, true);
    document.addEventListener("keydown", onKey, true);
    requestAnimationFrame(() => { pop && pop.classList.add("in"); (pop?.querySelector('[aria-checked="true"]') || pop?.querySelector(".menu-i"))?.focus({ preventScroll: true }); });
    return new Promise((res) => { resolveFn = res; });
  }
  return { open, close };
})();

/* -------------------------------------------------------- alert region
   ONE place for anything the system says without being asked: Sentry
   alarms, a stopped data pipeline, and (for older pages) toast messages.
   Items enter and leave through the top edge, exits ~20 % faster; the
   newest is on top; critical ones stay until acknowledged; timers pause
   while the tab is hidden or the pointer rests on the item. */
const Alerts = (() => {
  const MAX = 4;
  const ICON = { critical: "warn", error: "warn", warning: "warn", info: "info", ok: "check", busy: "clock" };

  function host() {
    let h = document.getElementById("alerts");
    if (!h) {
      h = document.createElement("div");
      h.id = "alerts"; h.className = "alerts";
      h.setAttribute("aria-live", "polite");
      document.body.append(h);
    }
    return h;
  }

  function dismiss(target) {
    const el = typeof target === "string" ? host().querySelector(`[data-id="${CSS.escape(target)}"]`) : target;
    if (!el || el.dataset.leaving) return;
    el.dataset.leaving = "1";
    clearTimeout(el._timer);
    el.classList.add("leaving");
    setTimeout(() => el.remove(), matchMedia("(prefers-reduced-motion: reduce)").matches ? 170 : 250);
  }

  function arm(el, ttl) {
    if (!ttl) return;
    let left = ttl, t0 = 0;
    const run = () => { t0 = Date.now(); el._timer = setTimeout(() => dismiss(el), left); };
    const pause = () => { clearTimeout(el._timer); left -= Date.now() - t0; };
    el.addEventListener("pointerenter", pause);
    el.addEventListener("pointerleave", run);
    el.addEventListener("focusin", pause);
    el.addEventListener("focusout", run);
    const vis = () => (document.hidden ? pause() : run());
    document.addEventListener("visibilitychange", vis);
    el._cleanup = () => document.removeEventListener("visibilitychange", vis);
    if (!document.hidden) run();
  }

  /* push({id, level: critical|warning|info|ok|error|busy, title, body,
           href, actionLabel, action, ttl, dismissible}) → element */
  function push(o) {
    const h = host();
    const level = o.level || "info";
    if (o.id) {
      const old = h.querySelector(`[data-id="${CSS.escape(o.id)}"]`);
      if (old) { old._cleanup?.(); clearTimeout(old._timer); old.remove(); }
    }
    const el = document.createElement("div");
    el.className = `alert lv-${level}`;
    el.setAttribute("role", level === "critical" || level === "error" ? "alert" : "status");
    if (o.id) el.dataset.id = o.id;
    el.innerHTML = `<span class="alert-ic">${icon(ICON[level] || "info", 18)}</span>
      <div class="alert-tx"><span class="alert-t">${esc(o.title || "")}</span>${o.body ? `<span class="alert-b">${esc(o.body)}</span>` : ""}</div>
      ${o.href ? `<a class="alert-act" href="${esc(o.href)}">${esc(o.actionLabel || t("common.view"))}</a>` : ""}
      ${o.action ? `<button type="button" class="alert-act">${esc(o.action.label)}</button>` : ""}
      ${o.dismissible === false ? "" : `<button type="button" class="alert-x" aria-label="${esc(t("common.dismiss"))}">${icon("x", 16)}</button>`}`;
    el.querySelector(".alert-x")?.addEventListener("click", () => { dismiss(el); o.onDismiss && o.onDismiss(); });
    // A flick up (or sideways) dismisses, like a notification: on velocity
    // (> 0.11 px/ms) or distance. Critical alerts need the explicit button.
    if (level !== "critical") {
      let d = null;
      el.addEventListener("pointerdown", (e) => {
        if (e.pointerType === "mouse" || e.target.closest("a, button")) return;
        d = { x: e.clientX, y: e.clientY, t: e.timeStamp };
        el.setPointerCapture(e.pointerId);
        el.style.transition = "none";
      });
      el.addEventListener("pointermove", (e) => {
        if (!d) return;
        const dx = e.clientX - d.x, dy = Math.min(0, e.clientY - d.y);
        el.style.transform = `translate(${dx}px, ${dy}px)`;
      });
      const end = (e) => {
        if (!d) return;
        const dx = e.clientX - d.x, dy = e.clientY - d.y, dt = Math.max(1, e.timeStamp - d.t);
        el.style.transition = "";
        const v = Math.hypot(dx, Math.min(0, dy)) / dt;
        if (v > 0.11 || -dy > 40 || Math.abs(dx) > 120) { dismiss(el); o.onDismiss && o.onDismiss(); }
        else el.style.transform = "";
        d = null;
      };
      el.addEventListener("pointerup", end);
      el.addEventListener("pointercancel", end);
    }
    if (o.action) el.querySelector("button.alert-act").addEventListener("click", () => { o.action.run(); dismiss(el); });
    h.prepend(el);
    // Over the cap, the oldest transient item goes first; critical ones stay.
    const items = [...h.querySelectorAll(".alert:not(.leaving)")];
    if (items.length > MAX) {
      const drop = items.reverse().find((x) => !x.classList.contains("lv-critical"));
      if (drop) dismiss(drop);
    }
    const ttl = o.ttl ?? (level === "critical" ? 0 : level === "warning" ? 15000 : level === "error" ? 8000 : 4000);
    arm(el, ttl);
    el.remove = ((orig) => function () { this._cleanup?.(); clearTimeout(this._timer); return orig.call(this); })(el.remove);
    return el;
  }

  return { push, dismiss, clear: () => host().querySelectorAll(".alert").forEach(dismiss) };
})();

/* --------------------------------------------------------------- toast
   Kept for pages that still call it. New UI puts status inline next to
   the control instead; this lands in the alert region. */
function toast(msg, kind = "ok", opts = {}) {
  const level = kind === "err" ? "error" : kind === "busy" ? "busy" : kind === "warn" ? "warning" : "ok";
  return Alerts.push({ level, title: msg, ttl: opts.ttl ?? (kind === "err" ? 7000 : 3200), action: opts.action });
}

/* ------------------------------------------------------------ odometer
   Odo.set(el, "51") rolls only the digits that changed, reusing the digit
   columns; the first paint and any change in shape are set directly. The
   real value is kept for assistive technology. */
const Odo = (() => {
  function build(el, text) {
    el.textContent = "";
    el.classList.add("od");
    const sr = document.createElement("span"); sr.className = "sr-only";
    const vis = document.createElement("span"); vis.setAttribute("aria-hidden", "true"); vis.className = "od-v";
    for (const ch of text) {
      if (/\d/.test(ch)) {
        const d = document.createElement("span"); d.className = "od-d";
        const col = document.createElement("span"); col.className = "od-col";
        col.innerHTML = "0123456789".split("").map((n) => `<span>${n}</span>`).join("");
        col.style.transition = "none";
        col.style.transform = `translateY(${-Number(ch)}em)`;
        d.append(col); vis.append(d);
      } else {
        const s = document.createElement("span"); s.className = "od-s"; s.textContent = ch; vis.append(s);
      }
    }
    el.append(vis, sr);
    el._odo = text;
    // re-enable the transition after this frame, so the next change rolls
    requestAnimationFrame(() => el.querySelectorAll(".od-col").forEach((c) => { c.style.transition = ""; }));
  }
  function shape(s) { return s.replace(/\d/g, "0"); }
  function set(el, text) {
    if (!el) return;
    text = String(text);
    if (el._odo === text) return;
    if (el._odo == null || shape(el._odo) !== shape(text)) build(el, text);
    else {
      const cols = el.querySelectorAll(".od-col");
      let i = 0;
      for (const ch of text) if (/\d/.test(ch)) { cols[i].style.transform = `translateY(${-Number(ch)}em)`; i++; }
      el._odo = text;
    }
    el.querySelector(".sr-only").textContent = text;
  }
  return { set };
})();

/* ------------------------------------------------------------ commands
   pressed (instant) → pending (on the control itself, aria-busy, naming
   the real operation) → sent → confirmed when the live state matches what
   the command promised → or failed, inline, with the next step.

   Risk (tesla/catalog.py): 0 runs on a tap; 1 asks once in a sheet that
   says what will happen (unlock, frunk, trunk, windows, Sentry off …); 2 asks
   the same way with a danger button (remote start, HomeLink). The server
   still answers 428 without `confirm`, and Cmd honours it. */
const Cmd = (() => {
  const caps = {};          // car_id -> capabilities
  async function capabilities(carId, force) {
    const id = carId ?? Car.current;
    if (!force && caps[id]) return caps[id];
    try { caps[id] = await apiGet(`/api/tesla/capabilities?car_id=${id}`); }
    catch (e) { caps[id] = { ready: false, reason: "error", commands: [] }; }
    return caps[id];
  }
  function spec(command, carId) {
    const c = caps[carId ?? Car.current];
    return c && c.commands ? c.commands.find((x) => x.id === command) : null;
  }
  function risk(command, carId) { const s = spec(command, carId); return s ? s.risk : 0; }

  async function post(body) {
    let r;
    try {
      r = await fetch("/api/tesla/command", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
    } catch (e) {
      return { status: 0, body: { code: "network", error: t("cmd.err.network") } };
    }
    let j = {};
    try { j = await r.json(); } catch (e) { /* empty */ }
    return { status: r.status, body: j };
  }

  function setupSheet(reason) {
    const lead = reason === "not_paired" ? t("cmd.setup.not_paired") : t("cmd.setup.needed");
    return Sheet.open({
      title: t("cmd.setup.title"), lead: esc(lead),
      actions: [{ label: t("common.later"), value: null },
                { label: t("cmd.setup.go"), kind: "accent", href: "/settings#commands" }],
    });
  }

  function label(command) { return t("cmd." + command); }
  function pendingText(command, carId) {
    const st = Live.car(carId).state;
    if (st === "asleep" || st === "offline") return t("cmd.waking");
    return i18n.has("cmd.ing." + command) ? t("cmd.ing." + command) : t("cmd.ing", { what: label(command) });
  }
  function errorText(b, what) {
    const code = b.code || "";
    if (code === "timeout" && b.maybe_executed) return t("cmd.maybe");
    if (i18n.has("cmd.err." + code)) return t("cmd.err." + code);
    return b.error || t("cmd.failed", { what });
  }

  async function confirmSheet(command, carId, opts, r) {
    const lv = r ?? risk(command, carId);
    const what = opts.label || label(command);
    return !!(await Sheet.open({
      title: opts.confirmTitle || t("cmd.confirm.title", { what }),
      lead: esc(opts.confirmText || (i18n.has("cmd.confirm.lead." + command) ? t("cmd.confirm.lead." + command) : "")),
      actions: [{ label: t("common.cancel"), value: false },
                { label: opts.confirmLabel || what, kind: lv >= 2 ? "danger" : "accent", value: true }],
    }));
  }

  /* Resolve once the live stream shows what the command promised. */
  function watch(expect, carId, ms = 45000) {
    const keys = Object.entries(expect || {}).filter(([k]) => k !== "tonneau");
    if (!keys.length) return Promise.resolve(null);
    const norm = (v) => (v === true || v === "true" ? "true" : v === false || v === "false" ? "false" : String(v));
    const match = () => { const c = Live.car(carId); return keys.every(([k, v]) => norm(c[k]) === norm(v)); };
    if (match()) return Promise.resolve(true);
    return new Promise((res) => {
      const off = Live.subscribe(() => { if (match()) { off(); clearTimeout(tm); res(true); } });
      const tm = setTimeout(() => { off(); res(false); }, ms);
    });
  }

  /* run(command, params, {car, label, confirmed, onState, confirmText})
     → the server's body on success, null otherwise.
     onState(phase, info) with phase ∈ pending | sent | done | unconfirmed |
     failed | cancelled | setup. Without onState, progress goes to toast(). */
  async function run(command, params, opts = {}) {
    const carId = opts.car ?? Car.current;
    const what = opts.label || label(command);
    const report = opts.onState || ((phase, info) => {
      if (phase === "failed") toast(info.message, "err");
      else if (phase === "done") toast(t("cmd.done", { what }), "ok");
      else if (phase === "unconfirmed") toast(t("cmd.unconfirmed"), "ok");
    });
    if (!caps[carId]) await capabilities(carId);
    const cp = caps[carId];
    if (cp && cp.ready === false && cp.reason !== "error") {
      report("setup", { reason: cp.reason });
      await setupSheet(cp.reason);
      return null;
    }
    let confirmed = !!opts.confirmed;
    if (!confirmed && risk(command, carId) >= 1) {
      if (!(await confirmSheet(command, carId, opts))) { report("cancelled", {}); return null; }
      confirmed = true;
    }
    report("pending", { text: pendingText(command, carId) });
    const body = { car_id: carId, command, params: params || null };
    let res = await post(confirmed ? { ...body, confirm: true } : body);
    if (res.status === 428) {
      // The server is stricter than our copy of the catalogue: ask, once.
      if (!(await confirmSheet(command, carId, opts, res.body.risk))) { report("cancelled", {}); return null; }
      report("pending", { text: pendingText(command, carId) });
      res = await post({ ...body, confirm: true });
    }
    if (res.status >= 200 && res.status < 300) {
      const expect = (res.body && res.body.expect) || {};
      const watching = Object.keys(expect).some((k) => k !== "tonneau");
      report("sent", { watching, body: res.body });
      if (watching) {
        watch(expect, carId).then((ok) => report(ok ? "done" : "unconfirmed", { body: res.body }));
      } else {
        report("done", { body: res.body });
      }
      return res.body;
    }
    const b = res.body || {};
    if (b.code === "needs_setup" || b.code === "not_paired") {
      report("setup", { reason: b.code });
      await setupSheet(b.code);
      return null;
    }
    report("failed", { message: errorText(b, what), code: b.code, maybe: !!b.maybe_executed });
    return null;
  }

  /* bind(button, command, params, {status, label, confirmed, after})
     Runs `command` with the full pressed → pending → done/failed cycle on
     `button` and an inline status element. Returns the click handler's
     promise. */
  function bindStatus(button, statusEl, opts = {}) {
    // Only a text label is swapped for the pending text; an icon-only
    // button keeps its icon and shows progress through aria-busy + status.
    const labelEl = button.querySelector(".cb-l");
    const original = labelEl ? labelEl.textContent : null;
    const setLabel = (txt) => { if (labelEl) labelEl.textContent = txt; };
    const say = (cls, html) => {
      if (!statusEl) return;
      statusEl.className = "inline-status" + (cls ? " " + cls : "");
      statusEl.innerHTML = html;
      clearTimeout(statusEl._t);
      if (cls === "is-done") statusEl._t = setTimeout(() => { statusEl.innerHTML = ""; statusEl.className = "inline-status"; }, 6000);
    };
    return (phase, info = {}) => {
      if (phase === "pending") {
        button.setAttribute("aria-busy", "true");
        button.classList.add("is-pending");
        button.disabled = true;
        setLabel(info.text || original);
        say("is-pending", labelEl ? "" : `${icon("clock", 14)}<span>${esc(info.text || "")}</span>`);
      } else {
        button.removeAttribute("aria-busy");
        button.classList.remove("is-pending");
        button.disabled = false;
        setLabel(original);
      }
      if (phase === "sent") say("is-pending", `${icon("clock", 14)}<span>${esc(info.watching ? t("cmd.sent_wait") : t("cmd.sent"))}</span>`);
      if (phase === "done") say("is-done", `${icon("check", 14)}<span>${esc(opts.doneText || t("cmd.done_short"))}</span>`);
      if (phase === "unconfirmed") say("is-wait", `${icon("clock", 14)}<span>${esc(t("cmd.unconfirmed"))}</span>`);
      if (phase === "failed") say("is-fail", `${icon("warn", 14)}<span>${esc(info.message || "")}</span>`);
      if (phase === "setup") say("is-wait", `${icon("key", 14)}<span>${esc(t("cmd.setup.short"))}</span> <a class="link" href="/settings#commands">${esc(t("cmd.setup.go"))}</a>`);
      if (phase === "cancelled") say("", "");
      if (opts.onState) opts.onState(phase, info);
    };
  }

  return { run, capabilities, setupSheet, risk, spec, watch, bindStatus, confirmSheet: (c, o = {}) => confirmSheet(c, Car.current, o) };
})();

/* ---------------------------------------------------------- app shell */
function initShell() {
  document.querySelectorAll("[data-carswitch]").forEach((el) => el.addEventListener("click", Car.openMenu));
  Live.subscribe(() => Car.paint());
  Car.load().then(() => Car.paint());

  /* Sentry: the nav badge counts incidents happening now, and pages can
     listen for `omt:sentry`. Polled, and refreshed on any alert event. */
  let lastActive = {};
  async function refreshSentry() {
    let active = lastActive;
    try { active = (await apiGet("/api/alerts/sentry/active")).active || {}; } catch (e) { return; }
    lastActive = active;
    const n = Object.keys(active).length;
    document.querySelectorAll("[data-sec-badge]").forEach((b) => { b.hidden = !n; b.textContent = n; });
    window.dispatchEvent(new CustomEvent("omt:sentry", { detail: active }));
  }
  window.ShellSentry = { get active() { return lastActive; }, refresh: refreshSentry };

  /* The dangerous failure for an alerting product is silence: pages render,
     health checks pass, and the pipeline is dead. Say so, once, at the top. */
  let staleShown = false;
  async function checkDataHealth() {
    let h;
    try { h = await apiGet("/api/health/detail"); } catch (e) { return; }
    const d = h.data || {};
    window.ShellHealth = d;
    window.dispatchEvent(new CustomEvent("omt:health", { detail: d }));
    if (d.stale) {
      let quiet = false;
      try { quiet = sessionStorage.getItem("omt.stale.quiet") === "1"; } catch (e) { /* storage blocked */ }
      if (quiet) return;
      Alerts.push({
        id: "stale", level: "warning", ttl: 0,
        title: t("banner.stale_title", { n: Math.round((d.seconds_since_data || 0) / 60) }),
        body: t("banner.stale_body"),
        onDismiss: () => { try { sessionStorage.setItem("omt.stale.quiet", "1"); } catch (e) { /* ignore */ } },
      });
      staleShown = true;
    } else if (staleShown) {
      Alerts.dismiss("stale"); staleShown = false;
    }
  }

  /* Alerts the hub raises while a page is open arrive on the live stream. */
  window.addEventListener("omt:event", (e) => {
    const a = e.detail || {};
    if (a.type !== "alert") return;
    const sentry = /^(sentry|possible_theft|new_sentry)/.test(a.rule || "");
    if (sentry) refreshSentry();
    if (!(a.level === "critical" || !a.in_app_only || sentry)) return;
    Alerts.push({
      id: a.id != null ? "a" + a.id : undefined,
      level: a.level === "critical" ? "critical" : a.level === "warning" ? "warning" : "info",
      title: plain(a.title), body: plain(a.body),
      href: a.url || (sentry ? "/security" : null), actionLabel: t("common.view"),
    });
  });

  const every = (fn, ms) => { fn(); setInterval(() => { if (!document.hidden) fn(); }, ms); };
  every(refreshSentry, 30000);
  every(checkDataHealth, 60000);
}
