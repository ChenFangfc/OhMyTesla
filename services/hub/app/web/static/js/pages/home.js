/* 总览 — is the truck all right, right now?
 *
 * The truck and one hero figure answer first. The 3D view mirrors live
 * state and moves only when the car does (state comes back over the live
 * stream), so the picture never claims something the truck hasn't done.
 * Quick controls carry their own state; one "now" slot names the single
 * most important current thing; the rest sits in one plate.
 *
 * Live values keep their last good reading: a cleared or failed value
 * never flickers to a dash.
 */
import { VehicleView, webglAvailable } from "/static/js/vehicle3d.js";

const $ = (id) => document.getElementById(id);
const truthy = (v) => v === "true" || v === true;
const num = (v) => (v == null || v === "" || v === "nil" || !isFinite(Number(v)) ? null : Number(v));
const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

let carId = null;
let view = null;            // VehicleView
let model = null;           // /api/vehicle/model
let overview = null;        // /api/overview
let caps = null;            // command capabilities
let tyresOn = false;
const keep = {};            // car id -> last good live values

/* ------------------------------------------------------------------ data */

function car() {
  const c = Live.car(carId);
  const k = keep[carId] || (keep[carId] = {});
  for (const [key, v] of Object.entries(c)) if (v !== "" && v != null && v !== "nil") k[key] = v;
  return k;
}
const ov = () => (overview && overview.car_id === carId ? overview : {});
const isCT = () => !!(model && model.identity && model.identity.key === "cybertruck");
const archived = () => { const i = Car.info(carId); return !!(i && i.active === false); };
const awake = (c) => ["online", "driving", "charging", "updating"].includes(c.state);
const incident = () => (window.ShellSentry && window.ShellSentry.active || {})[String(carId)] || null;

function sinceSec(c) {
  const s = c.since || ov().since;
  const ms = s ? Date.parse(s) : NaN;
  return isNaN(ms) ? null : Math.max(0, (Date.now() - ms) / 1000);
}
function place(c) { return c.geofence || ov().place || ""; }
function tempStr(v) { return v == null ? "–" : `${fmt.num(U.temp(v, 0), 0)}°`; }
function clock(ms) {
  if (ms == null) return "";
  const d = new Date(ms), today = new Date();
  return d.toDateString() === today.toDateString() ? fmt.time(ms)
    : d.toLocaleString(localeTag(), { month: "short", day: "numeric", hour: fmt.hour(), minute: "2-digit" });
}

function liveToView(c) {
  const inc = incident();
  return {
    door_fl: truthy(c.driver_front_door_open), door_rl: truthy(c.driver_rear_door_open),
    door_fr: truthy(c.passenger_front_door_open), door_rr: truthy(c.passenger_rear_door_open),
    frunk: truthy(c.frunk_open), tailgate: truthy(c.trunk_open),
    charge_port: truthy(c.charge_port_door_open),
    charging: c.charging_state === "Charging",
    climate: truthy(c.is_climate_on),
    sentry: inc ? (inc.peak === "panic" ? "panic" : "aware") : truthy(c.sentry_mode) ? "armed" : "off",
    awake: awake(c),
    speed_mps: c.state === "driving" ? (num(c.speed) || 0) / 3.6 : 0,
  };
}

/* ------------------------------------------------------------- the truck */

function stillFallback(stage) {
  // A still render if the backend has one, else the line drawing. No WebGL
  // is not an error state: the page reads the same without it.
  stage.innerHTML = "";
  const img = document.createElement("img");
  img.className = "stage-still"; img.alt = ""; img.decoding = "async";
  img.onerror = () => { img.remove(); stage.insertAdjacentHTML("beforeend", silhouette()); };
  if (model && model.render) { img.src = model.render; stage.append(img); }
  else stage.insertAdjacentHTML("beforeend", silhouette());
  $("views").hidden = true;
}
function silhouette() {
  return `<svg class="stage-still svg" viewBox="0 0 1260 440" fill="none" aria-hidden="true">
    <path d="M22 318V168l38-30 488-112 152 4 512 58 8 230h-106l-32-116H946l-32 116H310l-32-116H142l-32 116Z"
      stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>
    <path d="M432 169 404 76l144-33 152 3 102 18 8 105Z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>
    <circle cx="210" cy="320" r="66" stroke="currentColor" stroke-width="2"/><circle cx="1014" cy="320" r="66" stroke="currentColor" stroke-width="2"/>
    <path d="M60 386h1140" stroke="currentColor" stroke-opacity=".4"/></svg>`;
}

async function mountVehicle() {
  const stage = $("stage");
  if (view) { view.dispose(); view = null; }
  stage.innerHTML = "";
  $("hud").innerHTML = "";
  hud.els = {};
  try { model = await apiGet(`/api/vehicle/model?car_id=${carId}`); } catch (e) { model = null; }
  if (model && model.model && webglAvailable()) {
    view = new VehicleView(stage, {
      interactive: true,
      margin: 0.06,
      onPick: onPick,
      onFrame: () => hud.place(),
      onLost: () => { view = null; stillFallback(stage); paintHud(); },
    });
    try {
      await view.load({ ...model.model, view: "hero" });
      const ident = model.identity || {};
      if (ident.paint_hex && !model.model.rigged) view.setPaint(ident.paint_hex);
      view.setState(liveToView(car()));
      $("views").hidden = false;
      window.omtView = view;           // for debugging: omtView.dimensions()
      return;
    } catch (e) {
      console.warn("3D model unavailable, showing a still", e);
      if (view) view.dispose();
      view = null;
    }
  }
  stillFallback(stage);
}

/* Tap the truck: the part under your finger is the control. */
function onPick(part) {
  if (part === "tonneau") return quick.run("tonneau");
  if (part === "frunk") return quick.run("frunk");
  if (part === "tailgate") return isCT() ? runPlain("trunk") : quick.run("tonneau");
  if (part === "charge_port") return quick.run("port");
  if (part && part.startsWith("wheel")) return setTyres(true);
  if (part && part.startsWith("door")) say("hint", t("home.doors_hint"));
}

/* ---------------------------------------------------------- HUD labels
   Small mono labels pinned to the body: doors, frunk, charge port, tonneau
   (and, on demand, the four tyres). Text changes with state; position is
   recomputed on every rendered frame. */
const hud = {
  els: {},
  items() {
    const c = car();
    const out = [];
    const known = (v) => v != null && v !== "";
    const open = (v) => truthy(v);
    if (known(c.frunk_open)) out.push(["frunk", t("home.q.frunk"), open(c.frunk_open) ? t("home.st.open") : t("home.st.closed"), open(c.frunk_open) ? "is-open" : ""]);
    if (isCT()) {
      const pct = num(c.tonneau_percent_open ?? c.tonneau_open_percent);
      // TeslaMate doesn't report the tonneau; an unknown label is noise.
      if (pct != null) out.push(["tonneau", t("home.q.tonneau"), pct > 1 ? t("home.st.pct_open", { n: fmt.int(pct) }) : t("home.st.closed"), pct > 1 ? "is-open" : ""]);
    } else if (known(c.trunk_open)) {
      out.push(["tonneau", t("home.q.trunk"), open(c.trunk_open) ? t("home.st.open") : t("home.st.closed"), open(c.trunk_open) ? "is-open" : ""]);
    }
    if (c.charging_state === "Charging") out.push(["charge_port", t("home.q.port"), t("home.st.charging"), "is-live"]);
    else if (known(c.charge_port_door_open)) out.push(["charge_port", t("home.q.port"), open(c.charge_port_door_open) ? t("home.st.open") : t("home.st.closed"), open(c.charge_port_door_open) ? "is-open" : ""]);
    const doors = ["driver_front_door_open", "driver_rear_door_open", "passenger_front_door_open", "passenger_rear_door_open"];
    if (doors.some((k) => known(c[k])) || known(c.doors_open)) {
      const n = doors.filter((k) => open(c[k])).length;
      out.push(["doors", t("home.hud.doors"), n ? t("home.st.n_open", { n }) : t("home.st.closed"), n ? "is-open" : ""]);
    }
    if (tyresOn) {
      for (const k of ["fl", "fr", "rl", "rr"]) {
        const bar = num(c[`tpms_pressure_${k}`]);
        if (bar == null) continue;
        out.push([`wheel_${k}`, t("tire." + k), `${fmt.fixed(U.press(bar), U.pressDigits())} ${U.pressLabel()}`, truthy(c[`tpms_soft_warning_${k}`]) ? "is-open" : ""]);
      }
    }
    return out;
  },
  paint() {
    const layer = $("hud");
    if (!view) { layer.innerHTML = ""; this.els = {}; return; }
    // On a small stage only what deserves attention is pinned to the body
    // (open, charging, or the tyres on request); the quick controls below
    // already say "closed" for everything else.
    const small = $("ov-stage").clientWidth < 560;
    const want = this.items().filter((w) => !small || w[3] || tyresOn);
    const keys = new Set(want.map((w) => w[0]));
    for (const [k, el] of Object.entries(this.els)) if (!keys.has(k)) { el.remove(); delete this.els[k]; }
    for (const [key, name, value, cls] of want) {
      let el = this.els[key];
      if (!el) {
        el = document.createElement("span");
        el.innerHTML = `<i class="hl-dot"></i><i class="hl-ln"></i><span class="hl-tx"><b></b><span></span></span>`;
        layer.append(el);
        this.els[key] = el;
      }
      el.className = "hl " + cls;
      el.querySelector("b").textContent = name;
      el.querySelector(".hl-tx > span").textContent = value;
      el._w = 0;
    }
    this.place();
  },
  place() {
    if (!view) return;
    const W = $("ov-stage").clientWidth;
    for (const [key, el] of Object.entries(this.els)) {
      const p = view.anchor(key);
      if (!p || p.behind) { el.classList.add("is-hidden"); continue; }
      const visible = p.facing > 0.25 || key === "frunk" || key === "tonneau";
      el.classList.toggle("is-hidden", !visible);
      if (!el._w) { el._w = el.offsetWidth; el._h = el.offsetHeight; }
      // Text points away from the middle of the frame, so it sits off the
      // body — and never past the edge of the stage.
      const roomRight = p.x + el._w < W - 4, roomLeft = p.x - el._w > 4;
      const flip = (p.x < W / 2 - 40 && roomLeft) || (!roomRight && roomLeft);
      el.classList.toggle("flip", flip);
      const x = flip ? p.x - el._w + 2.5 : p.x - 2.5;
      el.style.transform = `translate(${Math.round(x)}px, ${Math.round(p.y - el._h / 2)}px)`;
    }
  },
};
const paintHud = () => hud.paint();

function setTyres(on) {
  tyresOn = on;
  if (view) view.view(on ? "top" : "hero");
  paintHud();
  paintPlate();
}

async function openViews(e) {
  const v = await Menu.open(e.currentTarget, [
    { label: t("home.view.hero"), value: "hero" },
    { label: t("home.view.side"), value: "side" },
    { label: t("home.view.rear"), value: "rear" },
    { label: t("home.view.top"), value: "top" },
    { sep: true },
    { label: t("home.view.tires"), value: "tires", checked: tyresOn },
  ], { title: t("home.views") });
  if (v == null) return;
  if (v === "tires") return setTyres(!tyresOn);
  tyresOn = false;
  paintHud(); paintPlate();
  if (view) view.view(v);
}

/* ------------------------------------------------------------- the head */

function paintHead() {
  const c = car(), o = ov();
  const info = Car.info(carId);
  $("ov-name").textContent = (model && model.name) || (info ? Car.label(info) : "") || " ";
  const parts = [];
  if (archived()) {
    parts.push(t("cars.archived"));
  } else {
    const st = c.state || o.state;
    const pl = place(c);
    if (st === "driving") parts.push(t("home.s.driving"));
    else if (c.charging_state === "Charging") parts.push(pl ? t("home.s.charging_at", { place: pl }) : t("home.s.charging"));
    else if (pl) parts.push(t("home.s.parked_at", { place: pl }));
    else if (["asleep", "offline", "online", "suspended"].includes(st)) parts.push(t("home.s.parked"));
    else if (st) parts.push(stateLabel2(st));
    const locked = c.locked != null ? truthy(c.locked) : o.locked;
    if (locked != null) parts.push(t(locked ? "home.locked" : "home.unlocked"));
    if (truthy(c.sentry_mode) || (c.sentry_mode == null && o.sentry)) parts.push(t("home.sentry_on"));
  }
  $("ov-sentence").textContent = parts.join(" · ");

  const fresh = $("ov-fresh");
  if (archived()) {
    const age = num(c._seed_age);
    fresh.textContent = age ? t("home.fresh.last_seen", { ago: fmt.ago(age) }) : "";
  } else if (awake(c)) {
    fresh.innerHTML = `<span class="micro is-live">${esc(t("home.fresh.live"))}</span>`;
  } else {
    const since = sinceSec(c);
    const at = since != null ? Date.now() - since * 1000 : Live.seenAt(carId);
    fresh.textContent = at ? t("home.fresh.updated", { t: clock(at) }) : "";
  }
}

/* --------------------------------------------------------- hero figure */

function paintFigure() {
  const c = car(), o = ov();
  const soc = num(c.battery_level) ?? (o.soc != null ? o.soc : null);
  if (soc != null) Odo.set($("fig-soc"), fmt.int(soc));
  const rangeKm = num(c.rated_battery_range_km) ?? num(c.est_battery_range_km) ?? (o.range_km ?? null);
  const rangeEl = $("fig-range");
  if (rangeKm != null) {
    if (!rangeEl.querySelector(".fr-n")) rangeEl.innerHTML = `<small class="fr-l"></small><span class="fr-n"></span><small class="fr-u"></small>`;
    rangeEl.querySelector(".fr-l").textContent = t("common.range");
    Odo.set(rangeEl.querySelector(".fr-n"), fmt.int(U.dist(rangeKm, 0)));
    rangeEl.querySelector(".fr-u").textContent = U.distLabel();
  }
  const charging = c.charging_state === "Charging";
  const track = $("soc-track");
  track.classList.toggle("is-live", charging);
  track.querySelector(".fill").style.transform = `scaleX(${Math.max(0, Math.min(1, (soc || 0) / 100))})`;
  const limit = num(c.charge_limit_soc);
  const mark = track.querySelector(".limit");
  mark.hidden = !limit;
  if (limit) mark.style.left = `${limit}%`;

  const meta = [];
  if (limit) meta.push(t("home.limit", { n: limit }));
  const chg = o.charging || {};
  if (!charging && chg.ended_at) meta.push(t("home.last_charge", { ago: fmt.ago(Date.now() / 1000 - chg.ended_at) }));
  $("fig-meta").textContent = meta.join(" · ") || " ";
}

/* -------------------------------------------------------------- now slot
   The single most important current thing, in priority order. */

function nowItem() {
  const c = car(), o = ov();
  const inc = incident();
  if (inc) {
    return { kind: "alarm", ic: "alarm", t: t(inc.peak === "panic" ? "home.now.alarm" : "home.now.activity"),
             b: t("home.now.since", { ago: fmt.ago(Date.now() / 1000 - inc.started) }),
             acts: [{ href: "/security", label: t("home.now.see") }] };
  }
  const h = window.ShellHealth;
  if (h && h.stale) {
    return { kind: "warn", ic: "warn", t: t("home.now.stale", { n: Math.round((h.seconds_since_data || 0) / 60) }),
             b: t("home.now.stale_b") };
  }
  if (archived()) return { kind: "", ic: "info", t: t("cars.archived"), b: t("cars.archived_hint") };
  if (c.charging_state === "Charging") {
    const kw = num(c.charger_power);
    const hrs = num(c.time_to_full_charge);
    const limit = num(c.charge_limit_soc);
    return { kind: "live", ic: "bolt", t: t("home.now.charging", { kw: kw == null ? "–" : fmt.num(kw, 0) }),
             b: hrs ? t("home.now.full_in", { t: fmt.dur(hrs * 3600), n: limit ?? 100 }) : limit ? t("home.limit", { n: limit }) : "",
             track: { soc: num(c.battery_level) || 0, limit } };
  }
  if (c.state === "driving") {
    const kmh = num(c.speed);
    return { kind: "live", ic: "route", t: t("home.now.driving"), b: kmh == null ? "" : U.speedStr(kmh) };
  }
  if (truthy(c.is_climate_on)) {
    return { kind: "live", ic: "climate", t: t("home.now.climate"), b: t("home.now.inside", { temp: tempStr(num(c.inside_temp)) }) };
  }
  if (truthy(c.update_available)) {
    return { kind: "", ic: "download", t: t("home.now.update"), b: c.update_version || "",
             acts: [{ href: "/controls#software", label: t("home.now.update_go") }] };
  }
  const open = [];
  if (truthy(c.frunk_open)) open.push(t("home.q.frunk"));
  if (truthy(c.trunk_open)) open.push(isCT() ? t("controls.tailgate") : t("home.q.trunk"));
  if (truthy(c.doors_open) || ["driver_front_door_open", "driver_rear_door_open", "passenger_front_door_open", "passenger_rear_door_open"].some((k) => truthy(c[k]))) open.push(t("home.hud.doors"));
  if (truthy(c.windows_open)) open.push(t("controls.windows"));
  if (open.length && c.state !== "driving") return { kind: "warn", ic: "warn", t: t("home.now.open", { what: fmt.list(open) }), b: "" };
  if (c.locked != null && !truthy(c.locked) && c.state !== "driving") {
    return { kind: "warn", ic: "unlock", t: t("home.now.unlocked"), b: t("home.now.unlocked_b") };
  }
  const soft = ["fl", "fr", "rl", "rr"].find((k) => truthy(c[`tpms_soft_warning_${k}`]));
  if (soft) {
    const bar = num(c[`tpms_pressure_${soft}`]);
    return { kind: "warn", ic: "tire", t: t("home.now.tyre", { corner: t("tire." + soft) }),
             b: bar == null ? "" : `${fmt.fixed(U.press(bar), U.pressDigits())} ${U.pressLabel()}` };
  }
  if (caps && caps.ready === false && !dismissedSetup()) {
    return { kind: "", ic: "key", t: t("home.setup.title"),
             b: caps.reason === "not_paired" ? t("home.setup.pair") : t("home.setup.body"),
             acts: [{ href: "/settings#commands", label: t("home.setup.cta") }, { run: dismissSetup, label: t("common.later"), quiet: true }] };
  }
  if (!Object.keys(c).length && !o.car_id) return null;
  return { kind: "ok", ic: "check", t: t("home.now.ok"), b: "" };
}

function dismissedSetup() { try { return localStorage.getItem("omt.setup.later") === "1"; } catch (e) { return false; } }
function dismissSetup() { try { localStorage.setItem("omt.setup.later", "1"); } catch (e) { /* storage blocked */ } paintNow(); }

let nowKey = "";
function paintNow() {
  const el = $("now");
  const it = nowItem();
  el.hidden = !it;
  if (!it) return;
  const key = JSON.stringify([it.kind, it.ic, it.t, it.b, (it.acts || []).map((a) => a.label)]);
  el.className = `now rail is-${it.kind || "plain"}`;
  if (key !== nowKey) {
    nowKey = key;
    el.innerHTML = `<span class="now-ic">${icon(it.ic, 18)}</span><span class="now-t">${esc(it.t)}</span>
      ${it.b ? `<span class="now-b">${esc(it.b)}</span>` : ""}
      ${it.track ? `<div class="charge-track"><i class="fill"></i>${it.track.limit ? `<i class="limit" style="left:${it.track.limit}%"></i>` : ""}</div>` : ""}
      ${it.acts && it.acts.length ? `<span class="now-acts">${it.acts.map((a, i) => a.href
        ? `<a class="now-a" href="${esc(a.href)}">${esc(a.label)}</a>`
        : `<button type="button" class="now-a${a.quiet ? " quiet" : ""}" data-i="${i}">${esc(a.label)}</button>`).join("")}</span>` : ""}`;
    el.querySelectorAll("button.now-a").forEach((b) => b.addEventListener("click", () => it.acts[Number(b.dataset.i)].run()));
  }
  const fill = el.querySelector(".charge-track .fill");
  if (fill && it.track) fill.style.transform = `scaleX(${Math.max(0, Math.min(1, it.track.soc / 100))})`;
}

/* -------------------------------------------------------- quick controls
   Built once; each paint only updates text and state, so a
   pending command is never torn down by the next live update. */

const QUICK = [
  { id: "lock", icon: (c) => (truthy(c.locked) ? "lock" : "unlock"), label: () => t("home.q.doors"),
    state: (c) => (c.locked == null ? null : truthy(c.locked) ? [t("home.st.locked"), ""] : [t("home.st.unlocked"), "is-warn"]),
    action: (c) => ({ cmd: truthy(c.locked) ? "door_unlock" : "door_lock" }) },
  { id: "climate", icon: () => "climate", label: () => t("home.q.climate"),
    state: (c) => (c.is_climate_on == null ? null : truthy(c.is_climate_on)
      ? [t("home.st.climate_on", { temp: tempStr(num(c.inside_temp)) }), "is-live"]
      : [t("home.st.climate_off", { temp: tempStr(num(c.inside_temp)) }), ""]),
    action: (c) => ({ cmd: truthy(c.is_climate_on) ? "climate_off" : "climate_on" }) },
  { id: "frunk", icon: () => "box", label: () => t("home.q.frunk"),
    state: (c) => (c.frunk_open == null ? null : truthy(c.frunk_open) ? [t("home.st.open"), "is-warn"] : [t("home.st.closed"), ""]),
    action: (c) => (truthy(c.frunk_open) ? { none: t("home.hint.frunk_close") } : { cmd: "frunk" }) },
  { id: "tonneau", icon: () => (isCT() ? "bed" : "box"), label: () => (isCT() ? t("home.q.tonneau") : t("home.q.trunk")),
    state: (c) => {
      if (isCT()) {
        const pct = num(c.tonneau_percent_open ?? c.tonneau_open_percent);
        return pct == null ? null : pct > 1 ? [t("home.st.pct_open", { n: fmt.int(pct) }), "is-warn"] : [t("home.st.closed"), ""];
      }
      return c.trunk_open == null ? null : truthy(c.trunk_open) ? [t("home.st.open"), "is-warn"] : [t("home.st.closed"), ""];
    },
    action: () => (isCT() ? { menu: "tonneau" } : { cmd: "trunk" }) },
  { id: "port", icon: () => "plug", label: () => t("home.q.port"),
    state: (c) => (c.charging_state === "Charging" ? [t("home.st.charging"), "is-live"]
      : c.charge_port_door_open == null ? null : truthy(c.charge_port_door_open) ? [t("home.st.open"), ""] : [t("home.st.closed"), ""]),
    action: (c) => ({ cmd: truthy(c.charge_port_door_open) ? "charge_port_close" : "charge_port_open" }) },
  { id: "sentry", icon: () => "shield", label: () => t("home.q.sentry"),
    state: (c) => (c.sentry_mode == null ? null : truthy(c.sentry_mode) ? [t("home.st.armed"), "is-live"] : [t("home.st.off"), ""]),
    action: (c) => ({ cmd: truthy(c.sentry_mode) ? "sentry_off" : "sentry_on" }) },
];

let statusTimer = null;
function say(kind, text, link) {
  const el = $("quick-status");
  clearTimeout(statusTimer);
  const cls = { hint: "", pending: "is-pending", done: "is-done", fail: "is-fail", wait: "is-wait" }[kind] ?? "";
  const ic = { hint: "info", pending: "clock", done: "check", fail: "warn", wait: "clock" }[kind] || "info";
  el.className = "inline-status " + cls;
  el.innerHTML = text ? `${icon(ic, 14)}<span>${esc(text)}</span>${link ? ` <a href="${esc(link.href)}">${esc(link.label)}</a>` : ""}` : "";
  if (kind === "done" || kind === "hint") statusTimer = setTimeout(() => { el.innerHTML = ""; el.className = "inline-status"; }, 5000);
}

const quick = {
  els: {},
  build() {
    const host = $("quick");
    host.innerHTML = QUICK.map((q) => `<button type="button" class="qc" data-id="${q.id}">
        <span class="qc-ic"></span><span class="qc-l"></span><span class="qc-s">–</span></button>`).join("");
    host.querySelectorAll(".qc").forEach((b) => {
      const q = QUICK.find((x) => x.id === b.dataset.id);
      this.els[q.id] = b;
      // Risky commands (unlock, frunk, Sentry off …) ask in Cmd.run's sheet.
      b.addEventListener("click", () => this.exec(q));
    });
  },
  paint() {
    const c = car();
    for (const q of QUICK) {
      const b = this.els[q.id];
      if (!b || b.classList.contains("is-pending")) continue;
      const st = q.state(c);
      const a = q.action(c);
      const ic = q.icon(c);
      if (b._ic !== ic) { b.querySelector(".qc-ic").innerHTML = icon(ic, 20); b._ic = ic; }
      b.querySelector(".qc-l").textContent = q.label();
      b.querySelector(".qc-s").textContent = st ? st[0] : "–";
      b.classList.remove("is-live", "is-warn");
      if (st && st[1]) b.classList.add(st[1]);
      b.classList.toggle("is-unknown", !st);
      const off = archived() || !!a.none;
      b.setAttribute("aria-disabled", off ? "true" : "false");
      const what = a.cmd ? t("cmd." + a.cmd) : a.menu ? t("home.q.tonneau") : a.none || "";
      b.setAttribute("aria-label", `${q.label()}: ${st ? st[0] : "–"}${what ? ` — ${what}` : ""}`);
      b.title = what;
    }
  },
  run(id) { const q = QUICK.find((x) => x.id === id); if (q) this.exec(q); },
  async exec(q) {
    const c = car();
    const a = q.action(c);
    const b = this.els[q.id];
    if (archived()) return say("hint", t("cars.archived_hint"));
    if (a.none) return say("hint", a.none);
    let cmd = a.cmd;
    if (a.menu === "tonneau") {
      cmd = await Menu.open(b, [
        { label: t("cmd.tonneau_open"), value: "tonneau_open", icon: "bed" },
        { label: t("cmd.tonneau_close"), value: "tonneau_close", icon: "bed" },
        { label: t("cmd.tonneau_stop"), value: "tonneau_stop", icon: "x" },
      ], { title: t("home.tonneau.title") });
      if (!cmd) return;
    }
    const what = t("cmd." + cmd);
    await Cmd.run(cmd, null, {
      onState: (phase, info) => {
        if (phase === "pending") {
          b.classList.add("is-pending"); b.setAttribute("aria-busy", "true");
          b.querySelector(".qc-s").textContent = info.text;
          say("pending", info.text);
        } else {
          b.classList.remove("is-pending"); b.removeAttribute("aria-busy");
        }
        if (phase === "sent") say(info.watching ? "pending" : "done", info.watching ? t("cmd.sent_wait") : t("cmd.done", { what }));
        if (phase === "done") { say("done", t("cmd.done", { what })); if (cmd === "flash_lights" && view) view.flash(); }
        if (phase === "unconfirmed") say("wait", t("cmd.unconfirmed"));
        if (phase === "failed") say("fail", info.message);
        if (phase === "setup") say("wait", t("cmd.setup.short"), { href: "/settings#commands", label: t("cmd.setup.go") });
        if (phase === "cancelled") say("", "");
        this.paint();
      },
    });
  },
};

/* A command with no quick control of its own: status line only. */
function runPlain(cmd) {
  const what = t("cmd." + cmd);
  return Cmd.run(cmd, null, {
    onState: (phase, info) => {
      if (phase === "pending") say("pending", info.text);
      if (phase === "sent") say(info.watching ? "pending" : "done", info.watching ? t("cmd.sent_wait") : t("cmd.done", { what }));
      if (phase === "done") say("done", t("cmd.done", { what }));
      if (phase === "unconfirmed") say("wait", t("cmd.unconfirmed"));
      if (phase === "failed") say("fail", info.message);
      if (phase === "setup") say("wait", t("cmd.setup.short"), { href: "/settings#commands", label: t("cmd.setup.go") });
      if (phase === "cancelled") say("", "");
    },
  });
}

/* ---------------------------------------------------------------- plate */

function paintPlate() {
  const c = car(), o = ov();
  const cells = [];
  // where
  const pl = place(c);
  const where = pl || "–";   // a place in words, never raw coordinates
  let whereSub = "";
  if (c.state === "driving") whereSub = t("home.card.driving", { speed: U.speedStr(num(c.speed)) });
  else { const s = sinceSec(c); if (s != null) whereSub = t("home.plate.parked_for", { t: fmt.dur(s) }); }
  cells.push(`<div class="pc"><span class="pc-l">${esc(t("home.plate.where"))}</span><span class="pc-v">${esc(where)}</span><span class="pc-s">${esc(whereSub)}</span></div>`);
  // today
  const td = o.today || {};
  const todayV = td.km != null ? `${fmt.num(U.dist(td.km, 1), 1)} ${U.distLabel()} · ${fmt.num(td.kwh, 1)} kWh` : "–";
  const todaySub = td.drives ? t("home.plate.drives", { n: td.drives }) : td.km != null ? t("home.plate.no_drives") : "";
  cells.push(`<a class="pc" href="/data"><span class="pc-l">${esc(t("home.plate.today"))}</span><span class="pc-v">${esc(todayV)}</span><span class="pc-s">${esc(todaySub)}</span></a>`);
  // tyres
  const bars = ["fl", "fr", "rl", "rr"].map((k) => num(c[`tpms_pressure_${k}`]));
  const have = bars.filter((v) => v != null);
  const d = U.pressDigits();
  let tyreV = "–", tyreSub = "", tyreWarn = false;
  if (have.length) {
    const lo = U.press(Math.min(...have)), hi = U.press(Math.max(...have));
    tyreV = `${lo === hi ? fmt.fixed(lo, d) : `${fmt.fixed(lo, d)}–${fmt.fixed(hi, d)}`} ${U.pressLabel()}`;
    const soft = ["fl", "fr", "rl", "rr"].filter((k) => truthy(c[`tpms_soft_warning_${k}`]));
    tyreWarn = soft.length > 0;
    tyreSub = tyreWarn ? t("home.plate.tyre_low", { corner: fmt.list(soft.map((k) => t("tire." + k))) }) : t("home.plate.tyres_ok");
  }
  cells.push(`<button type="button" class="pc" id="pc-tyres" aria-pressed="${tyresOn}"><span class="pc-l">${esc(t("home.card.tyres"))}</span><span class="pc-v">${esc(tyreV)}</span><span class="pc-s${tyreWarn ? " is-warn" : ""}">${esc(tyresOn ? t("home.plate.tyres_hide") : tyreSub)}</span></button>`);
  // software
  const ver = c.version || o.version || "–";
  const upd = truthy(c.update_available);
  cells.push(`<a class="pc" href="/controls#software"><span class="pc-l">${esc(t("home.card.software"))}</span><span class="pc-v">${esc(ver)}</span><span class="pc-s${upd ? " is-warn" : ""}">${esc(upd ? t("home.card.update", { v: c.update_version || "" }) : t("home.card.up_to_date"))}</span></a>`);
  const host = $("plate");
  const html = cells.join("");
  if (host._html !== html) {
    host._html = html;
    host.innerHTML = html;
    $("pc-tyres").addEventListener("click", () => {
      setTyres(!tyresOn);
      if (tyresOn && !view) say("hint", t("home.hint.no_3d"));
    });
  }
}

/* ---------------------------------------------------------------- paint */

let lastView = "";
function paint() {
  const c = car();
  paintHead();
  paintFigure();
  paintNow();
  quick.paint();
  paintPlate();
  if (view) {
    const v = liveToView(c);
    const sig = JSON.stringify(v);
    if (sig !== lastView) { lastView = sig; view.setState(v); }
  }
  paintHud();
}

let rafPending = false;
function schedule() {
  if (rafPending) return;
  rafPending = true;
  requestAnimationFrame(() => { rafPending = false; paint(); });
}

async function loadOverview() {
  if (document.hidden) return;
  // tz_offset_min is minutes EAST of UTC; getTimezoneOffset() counts west. Without it
  // "today" was the UTC day, which reset to 0 every evening in the Americas.
  const tz = -new Date().getTimezoneOffset();
  try { overview = await apiGet(`/api/overview?car_id=${carId}&tz_offset_min=${tz}`); } catch (e) { /* keep the last good one */ }
  schedule();
}

async function loadCaps() {
  caps = await Cmd.capabilities(carId, true);
  schedule();
}

/* ----------------------------------------------------------------- boot */

async function boot() {
  await i18nReady.catch(() => {});
  await Car.load();
  carId = Car.current;
  Car.paint();
  quick.build();
  $("views").addEventListener("click", openViews);
  window.onOverviewData = (d) => { overview = d; schedule(); };
  window.addEventListener("omt:sentry", schedule);
  window.addEventListener("omt:health", schedule);
  window.addEventListener("omt:locale", () => { hud.els = {}; $("hud").innerHTML = ""; $("plate")._html = ""; nowKey = ""; schedule(); });
  Live.subscribe(schedule);
  await Promise.all([mountVehicle(), loadOverview(), loadCaps()]);
  paint();
  setInterval(loadOverview, 30000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) loadOverview(); });
  Car.onChange(async (id) => {
    carId = id; overview = null; tyresOn = false; lastView = "";
    $("plate")._html = ""; nowKey = "";
    await Promise.all([mountVehicle(), loadOverview(), loadCaps()]);
    paint();
  });
  if (reduced()) document.documentElement.classList.add("reduced");
}

boot();
