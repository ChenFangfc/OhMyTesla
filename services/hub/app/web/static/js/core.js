/* App-level plumbing shared by every page: which car is selected, one live
 * data stream for everyone, sheets, toasts, and sending commands.
 *
 * Classic script (loaded after util.js / i18n.js / icons.js) so templates and
 * ES modules alike can use the globals it defines: Car, Live, Sheet, toast, Cmd.
 */

/* ---------------------------------------------------------------- cars */
const Car = (() => {
  let list = [];
  let current = null;
  const subs = new Set();
  const KEY = "omt.car";

  function fromUrl() {
    const v = new URLSearchParams(location.search).get("car");
    return v && /^\d+$/.test(v) ? Number(v) : null;
  }
  function saved() {
    try { const v = localStorage.getItem(KEY); return v ? Number(v) : null; } catch (e) { return null; }
  }

  async function load() {
    try { list = await apiGet("/api/cars"); } catch (e) { list = []; }
    // Active = still in the account. Archived cars (sold, removed) stay
    // selectable for their history but never become the default. Evaluated
    // live on every read: the stream may arrive after this list does, and a
    // one-off check at load time labelled the car you're driving "archived".
    list.forEach((c) => Object.defineProperty(c, "active", { get: () => isActive(c), configurable: true }));
    const want = fromUrl() ?? saved();
    const ok = (id) => list.some((c) => c.id === id);
    current = ok(want) ? want : (list.find((c) => c.active) || list[0] || {}).id ?? null;
    return list;
  }

  function isActive(c) {
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
    const t = (c.trim_badging || "").toUpperCase();
    if (t === "CYBERBEAST") return "Cyberbeast";
    return t && t !== "DEFAULT" ? t : "";
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
      el.querySelector(".cs-name").textContent = c ? label(c) : t("cars.none");
      const parts = [];
      if (c && !c.active) parts.push(t("cars.archived"));
      else if (st.state) parts.push(stateLabel2(st.state));
      if (st.battery_level) parts.push(`${Math.round(st.battery_level)}%`);
      el.querySelector(".cs-sub").textContent = parts.join(" · ");
      el.querySelector(".status-dot").className = "status-dot " + dotClass(st, c);
    });
  }

  function openMenu() {
    if (list.length <= 1) return;
    const live = Live.cars();
    const html = `<div class="carmenu">${list.map((c) => {
      const st = live[String(c.id)] || {};
      const sub = c.active ? [variant(c), stateLabel2(st.state)].filter(Boolean).join(" · ")
                           : t("cars.archived_hint");
      return `<button class="car-opt ${c.id === current ? "on" : ""}" data-id="${c.id}">
          <span class="status-dot ${dotClass(st, c)}"></span>
          <span><div class="nm">${esc(label(c))}</div><div class="sb">${esc(sub)}</div></span>
          ${c.active ? "" : `<span class="tag">${esc(t("cars.archived"))}</span>`}</button>`;
    }).join("")}</div>`;
    Sheet.open({ title: t("cars.choose"), html }).then(() => {});
    document.querySelectorAll(".carmenu .car-opt").forEach((b) => b.addEventListener("click", () => {
      select(Number(b.dataset.id)); Sheet.close();
    }));
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
  if (c && c.active === false) return "asleep";
  if (st && st._alarm) return "alarm";
  const s = (st && st.state) || "";
  if (s === "charging") return "charging";
  if (s === "driving") return "driving";
  if (s === "online") return "live";
  if (s === "offline") return "down";
  return "asleep";
}

/* ---------------------------------------------------------- live data */
const Live = (() => {
  const subs = new Set();
  let cars = {};
  let started = false;
  function start() {
    if (started) return;
    started = true;
    cars = liveConnect((c) => {
      cars = c;
      subs.forEach((fn) => { try { fn(c); } catch (e) { console.error(e); } });
    });
  }
  return {
    start,
    cars: () => cars,
    car: (id) => cars[String(id ?? Car.current)] || {},
    subscribe(fn) { subs.add(fn); start(); return () => subs.delete(fn); },
  };
})();

/* --------------------------------------------------------------- sheet */
const Sheet = (() => {
  let resolveFn = null;
  let scrim, sheet;
  function close(value) {
    if (!sheet) return;
    sheet.classList.remove("open"); scrim.classList.remove("open");
    const s = sheet, sc = scrim;
    setTimeout(() => { s.remove(); sc.remove(); }, 260);
    sheet = scrim = null;
    if (resolveFn) { const r = resolveFn; resolveFn = null; r(value); }
    document.removeEventListener("keydown", onKey);
  }
  function onKey(e) { if (e.key === "Escape") close(null); }
  /* open({title, html, lead, actions:[{label, kind, value}]}) → Promise<value|null> */
  function open({ title, lead, html, actions }) {
    close(null);
    scrim = document.createElement("div"); scrim.className = "scrim2";
    sheet = document.createElement("div"); sheet.className = "sheet2"; sheet.setAttribute("role", "dialog");
    sheet.setAttribute("aria-modal", "true");
    sheet.innerHTML = `${title ? `<h3>${esc(title)}</h3>` : ""}${lead ? `<p class="lead">${lead}</p>` : ""}${html || ""}
      ${actions && actions.length ? `<div class="actions">${actions.map((a, i) =>
        `<button class="btn ${a.kind || "ghost"}" data-i="${i}">${esc(a.label)}</button>`).join("")}</div>` : ""}`;
    document.body.append(scrim, sheet);
    requestAnimationFrame(() => { scrim.classList.add("open"); sheet.classList.add("open"); });
    scrim.addEventListener("click", () => close(null));
    document.addEventListener("keydown", onKey);
    sheet.querySelectorAll(".actions .btn").forEach((b) => b.addEventListener("click", () => {
      const a = actions[Number(b.dataset.i)];
      if (a.href) { location.href = a.href; return; }
      close(a.value ?? a.label);
    }));
    const first = sheet.querySelector(".actions .btn:last-child");
    if (first) first.focus({ preventScroll: true });
    return new Promise((res) => { resolveFn = res; });
  }
  return { open, close, get el() { return sheet; } };
})();

/* --------------------------------------------------------------- toast */
function toast(msg, kind = "ok", opts = {}) {
  let host = document.querySelector(".toasts");
  if (!host) { host = document.createElement("div"); host.className = "toasts"; document.body.append(host); }
  const el = document.createElement("div");
  el.className = `toast2 ${kind}`;
  el.setAttribute("role", kind === "err" ? "alert" : "status");
  el.innerHTML = `${icon(kind === "err" ? "warn" : kind === "busy" ? "clock" : "check", 18)}<span>${esc(msg)}</span>`
    + (opts.action ? `<button class="act">${esc(opts.action.label)}</button>` : "");
  if (opts.action) el.querySelector(".act").addEventListener("click", () => { opts.action.run(); el.remove(); });
  host.append(el);
  const ttl = opts.ttl ?? (kind === "err" ? 7000 : 3200);
  if (ttl > 0) setTimeout(() => el.remove(), ttl);
  return el;
}

/* ------------------------------------------------------------ commands */
const Cmd = (() => {
  let caps = {};          // car_id -> capabilities
  async function capabilities(carId, force) {
    const id = carId ?? Car.current;
    if (!force && caps[id]) return caps[id];
    try { caps[id] = await apiGet(`/api/tesla/capabilities?car_id=${id}`); }
    catch (e) { caps[id] = { ready: false, reason: "error", commands: [] }; }
    return caps[id];
  }

  async function post(body) {
    const r = await fetch("/api/tesla/command", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    let j = {};
    try { j = await r.json(); } catch (e) { /* empty */ }
    return { status: r.status, body: j };
  }

  function setupSheet(reason) {
    const lead = reason === "not_paired" ? t("cmd.setup.not_paired") : t("cmd.setup.needed");
    return Sheet.open({
      title: t("cmd.setup.title"), lead,
      actions: [{ label: t("common.later"), value: null },
                { label: t("cmd.setup.go"), kind: "accent", href: "/settings#commands" }],
    });
  }

  /* run(command, params, {car, label, confirmText}) → result | null */
  async function run(command, params, opts = {}) {
    const carId = opts.car ?? Car.current;
    const label = opts.label || t("cmd." + command);
    const busy = toast(t("cmd.sending", { what: label }), "busy", { ttl: 0 });
    let res = await post({ car_id: carId, command, params: params || null });
    if (res.status === 428) {
      busy.remove();
      const ok = await Sheet.open({
        title: opts.confirmTitle || t("cmd.confirm.title", { what: label }),
        lead: opts.confirmText || t("cmd.confirm.lead." + command) ,
        actions: [{ label: t("common.cancel"), value: false },
                  { label: opts.confirmLabel || label, kind: res.body.risk >= 2 ? "danger" : "accent", value: true }],
      });
      if (!ok) return null;
      const busy2 = toast(t("cmd.sending", { what: label }), "busy", { ttl: 0 });
      res = await post({ car_id: carId, command, params: params || null, confirm: true });
      busy2.remove();
    } else {
      busy.remove();
    }
    if (res.status >= 200 && res.status < 300) {
      toast(t("cmd.done", { what: label }), "ok");
      return res.body;
    }
    const b = res.body || {};
    if (b.code === "needs_setup" || b.code === "not_paired") { await setupSheet(b.code); return null; }
    if (b.code === "timeout" && b.maybe_executed) { toast(t("cmd.maybe"), "err"); return null; }
    toast(b.error || t("cmd.failed", { what: label }), "err");
    return null;
  }

  return { run, capabilities, setupSheet };
})();

/* ---------------------------------------------------------- app shell */
function initShell() {
  document.querySelectorAll("[data-carswitch]").forEach((el) => el.addEventListener("click", Car.openMenu));
  Live.subscribe(() => Car.paint());
  Car.load().then(() => Car.paint());
  const more = document.getElementById("tab-more");
  if (more) more.addEventListener("click", () => {
    const items = [["trips", "route"], ["energy", "bolt"], ["timeline", "timeline"], ["settings", "gear"]];
    Sheet.open({
      title: t("nav.more"),
      html: `<div class="rail-nav">${items.map(([k, ic]) =>
        `<a class="rail-link" href="/${k}">${icon(ic, 20)}<span>${esc(t("nav." + k))}</span></a>`).join("")}</div>`,
    });
  });
}
