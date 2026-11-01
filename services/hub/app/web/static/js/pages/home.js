/* Home: the car, what it's doing, and the handful of things you do most.
 *
 * The 3D view is driven by the same live stream as everything else; the
 * quick controls send real commands and the car on screen moves only when
 * the car does (state comes back over MQTT / the fast check), so the picture
 * never claims something the truck hasn't done.
 */
import { VehicleView, webglAvailable } from "/static/js/vehicle3d.js";

const $ = (id) => document.getElementById(id);
let view = null;
let model = null;          // /api/vehicle/model response
let overview = null;       // /api/overview response
let fullState = null;      // /api/vehicle/state (only when awake)
let labelsOn = false;

const truthy = (v) => v === "true" || v === true;
const num = (v) => (v == null || v === "" ? null : Number(v));

function liveToView(c) {
  const sentryOn = truthy(c.sentry_mode);
  return {
    door_fl: truthy(c.driver_front_door_open), door_rl: truthy(c.driver_rear_door_open),
    door_fr: truthy(c.passenger_front_door_open), door_rr: truthy(c.passenger_rear_door_open),
    frunk: truthy(c.frunk_open), tailgate: truthy(c.trunk_open),
    charge_port: truthy(c.charge_port_door_open),
    charging: c.charging_state === "Charging",
    climate: truthy(c.is_climate_on),
    sentry: c._incident || (sentryOn ? "armed" : "off"),
    awake: ["online", "driving", "charging", "updating"].includes(c.state),
    speed_mps: c.state === "driving" ? (num(c.speed) || 0) / 3.6 : 0,
  };
}

/* ---------------------------------------------------------------- hero */

async function mountVehicle(carId) {
  const stage = $("stage");
  if (view) { view.dispose(); view = null; }
  stage.innerHTML = "";
  try { model = await apiGet(`/api/vehicle/model?car_id=${carId}`); } catch (e) { model = null; }
  const ident = (model && model.identity) || {};
  $("h-eyebrow").textContent = [ident.name, ident.variant].filter(Boolean).join(" · ").toUpperCase();
  const info = Car.info(carId);
  $("h-name").textContent = (model && model.name) || Car.label(info);

  if (model && model.model && webglAvailable()) {
    const desc = { ...model.model, view: "hero" };
    if (!desc.rigged) desc.view = "hero";
    view = new VehicleView(stage, {
      interactive: true,
      onPick: (part) => onPick(part),
      // wide screens: nudge the truck right so the battery readout has room
      shiftX: (w) => (w > 900 ? 0.09 : 0),
      // phones: lift the truck clear of the readout and quick controls
      shiftY: (w, h) => (w <= 900 ? 0.07 : 0),
    });
    try {
      await view.load(desc);
      if (ident.paint_hex && !desc.rigged) view.setPaint(ident.paint_hex);
      view.setState(liveToView(Live.car(carId)));
      document.querySelectorAll("#views button").forEach((b) => (b.disabled = false));
      return;
    } catch (e) {
      console.error("3D model failed to load", e);
      view = null;
      stage.innerHTML = "";
    }
  }
  // 2D fallback: a render if one exists, otherwise just the readouts.
  if (model && model.render) {
    const img = document.createElement("img");
    img.className = "fallback-img"; img.alt = ""; img.src = model.render;
    img.onerror = () => img.remove();
    stage.append(img);
  }
  document.querySelectorAll("#views button").forEach((b) => (b.disabled = true));
}

function paintHero() {
  const carId = Car.current;
  const c = Live.car(carId);
  const o = overview && overview.car_id === carId ? overview : null;
  const soc = num(c.battery_level) ?? (o && o.soc);
  $("h-soc").innerHTML = soc == null ? "–" : `${Math.round(soc)}<small>%</small>`;
  const limit = num(c.charge_limit_soc);
  const bar = document.querySelector(".socbar > i");
  bar.style.width = `${Math.max(0, Math.min(100, soc || 0))}%`;
  bar.classList.toggle("charging", c.charging_state === "Charging");
  const mark = document.querySelector(".socbar > b");
  mark.style.display = limit ? "" : "none";
  if (limit) mark.style.left = `${limit}%`;

  const rangeKm = num(c.rated_battery_range_km) ?? num(c.est_battery_range_km) ?? (o && o.range_km);
  const parts = [];
  if (rangeKm != null) parts.push(`${U.dist(rangeKm, 0)} ${U.distLabel()}`);
  if (c.charging_state === "Charging") {
    const kw = num(c.charger_power);
    const mins = num(c.time_to_full_charge) != null ? Math.round(num(c.time_to_full_charge) * 60) : null;
    parts.push(t("home.charging_at", { kw: kw ?? "–" }) + (mins ? " · " + t("home.full_in", { t: fmtMinutes(mins) }) : ""));
  } else if (limit) {
    parts.push(t("home.limit", { n: limit }));
  }
  $("h-range").textContent = parts.join(" · ");

  // status pills
  const pills = [];
  const archived = Car.info(carId) && Car.info(carId).active === false;
  if (archived) pills.push(["", t("cars.archived")]);
  else {
    const st = c.state || (o && o.state);
    if (st) pills.push([st === "online" || st === "driving" ? "info" : st === "charging" ? "good" : "", stateLabel2(st)]);
    if (c.locked != null) pills.push([truthy(c.locked) ? "" : "warn", t(truthy(c.locked) ? "home.locked" : "home.unlocked")]);
    if (truthy(c.sentry_mode)) pills.push([c._incident ? "bad" : "bad", t(c._incident ? "home.sentry_active" : "home.sentry_on")]);
    if (truthy(c.is_climate_on)) pills.push(["info", t("home.climate_on", { temp: tempStr(c.inside_temp) })]);
    if (c.geofence) pills.push(["", c.geofence]);
    const age = num(c._seed_age);
    if (age && age > 600) pills.push(["", t("home.last_seen", { ago: fmt.ago(age) })]);
  }
  $("h-status").innerHTML = pills.map(([cls, txt]) => `<span class="tagpill ${cls}">${esc(txt)}</span>`).join("");

  if (view) view.setState(liveToView(c));
  paintQuick(c);
  if (labelsOn) paintLabels();
}

function tempStr(c) { return c == null || c === "" ? "–" : `${U.temp(Number(c), 0)}${U.tempLabel()}`; }
function fmtMinutes(m) { return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m} min`; }

/* -------------------------------------------------------- quick actions */

function quickSpec(c) {
  const ct = model && model.identity && model.identity.key === "cybertruck";
  const locked = truthy(c.locked);
  const items = [
    { id: "lock", icon: locked ? "lock" : "unlock", label: t(locked ? "home.q.locked" : "home.q.unlocked"),
      on: !locked, color: locked ? "" : "", cmd: locked ? "door_unlock" : "door_lock" },
    { id: "climate", icon: "climate", label: t("home.q.climate"), on: truthy(c.is_climate_on),
      cmd: truthy(c.is_climate_on) ? "climate_off" : "climate_on" },
    { id: "frunk", icon: "box", label: t("home.q.frunk"), on: truthy(c.frunk_open), cmd: "frunk" },
    ct ? { id: "tonneau", icon: "bed", label: t("home.q.tonneau"), on: false, cmd: "tonneau_open", alt: "tonneau_close" }
       : { id: "trunk", icon: "box", label: t("home.q.trunk"), on: truthy(c.trunk_open), cmd: "trunk" },
    { id: "port", icon: "plug", label: t("home.q.port"), on: truthy(c.charge_port_door_open), color: "green",
      cmd: truthy(c.charge_port_door_open) ? "charge_port_close" : "charge_port_open" },
    { id: "sentry", icon: "shield", label: t("home.q.sentry"), on: truthy(c.sentry_mode), color: "red",
      cmd: truthy(c.sentry_mode) ? "sentry_off" : "sentry_on" },
    { id: "flash", icon: "lightbulb", label: t("home.q.flash"), on: false, cmd: "flash_lights" },
    { id: "more", icon: "sliders", label: t("home.q.more"), href: "/controls" },
  ];
  return items;
}

let capsReady = null;
function paintQuick(c) {
  const host = $("quick");
  const items = quickSpec(c);
  const locked = capsReady === false;
  host.innerHTML = items.map((q) => `<button class="qbtn ${q.on ? "on " + (q.color || "") : ""} ${locked && !q.href ? "locked" : ""}"
      data-id="${q.id}" aria-pressed="${q.on ? "true" : "false"}">
      <span class="qi">${icon(q.icon, 20)}</span><span>${esc(q.label)}</span></button>`).join("");
  host.querySelectorAll(".qbtn").forEach((b) => b.addEventListener("click", () => {
    const q = items.find((x) => x.id === b.dataset.id);
    if (q.href) { location.href = q.href; return; }
    if (q.id === "tonneau") return tonneauSheet();
    b.classList.add("busy");
    Cmd.run(q.cmd, null, { label: t("cmd." + q.cmd) }).then((r) => {
      b.classList.remove("busy");
      if (r && q.id === "flash" && view) view.flash();
    });
  }));
}

async function tonneauSheet() {
  const choice = await Sheet.open({
    title: t("home.tonneau.title"),
    actions: [{ label: t("home.tonneau.close"), value: "tonneau_close" },
              { label: t("home.tonneau.stop"), value: "tonneau_stop" },
              { label: t("home.tonneau.open"), kind: "accent", value: "tonneau_open" }],
  });
  if (choice) Cmd.run(choice, null, { label: t("cmd." + choice) });
}

/* Tap the truck: the part under your finger is the control. */
function onPick(part) {
  const c = Live.car();
  const map = {
    frunk: "frunk", tailgate: "trunk",
    charge_port: truthy(c.charge_port_door_open) ? "charge_port_close" : "charge_port_open",
  };
  if (part === "tonneau") return tonneauSheet();
  if (map[part]) return Cmd.run(map[part], null, { label: t("cmd." + map[part]) });
  if (part && part.startsWith("wheel")) return toggleLabels(true);
  if (part && part.startsWith("door")) toast(t("home.doors_hint"), "ok");
}

/* --------------------------------------------------------- tyre labels */

function toggleLabels(on) {
  labelsOn = on ?? !labelsOn;
  $("labels").innerHTML = "";
  if (labelsOn && view) { view.view("top"); setTimeout(paintLabels, 950); }
}

function paintLabels() {
  if (!view) return;
  const c = Live.car();
  const host = $("labels");
  const corners = { wheel_fl: "fl", wheel_fr: "fr", wheel_rl: "rl", wheel_rr: "rr" };
  host.innerHTML = Object.entries(corners).map(([node, k]) => {
    const p = view.screenPoint(node, [0, 0.55, 0]);
    const bar = num(c[`tpms_pressure_${k}`]) ?? (fullState && fullState.tires ? fullState.tires[k] : null);
    if (!p || bar == null) return "";
    const warn = truthy(c[`tpms_soft_warning_${k}`]);
    return `<span class="v3d-label ${warn ? "warn" : ""}" style="left:${p.x}px;top:${p.y}px">${U.press(bar)} ${U.pressLabel()}</span>`;
  }).join("");
}

/* --------------------------------------------------------------- cards */

async function paintCards() {
  const carId = Car.current;
  const c = Live.car(carId);
  const o = overview && overview.car_id === carId ? overview : {};
  const cards = [];

  // charging
  const charging = c.charging_state === "Charging";
  const chg = o.charging || {};
  cards.push(`<a class="tile2" href="/energy">
    <div class="t-head">${icon("bolt", 16)}<span>${esc(t("home.card.charging"))}</span><span class="go">${icon("chevron", 16)}</span></div>
    <div class="t-big">${charging ? `${esc(c.charger_power || "–")}<small>kW</small>` : chg.added_kwh != null ? `${fmt.num(chg.added_kwh, 1)}<small>kWh</small>` : "–"}</div>
    <div class="t-sub">${esc(charging ? t("home.card.charging_now", { v: c.charger_voltage || "–", a: c.charger_actual_current || "–" })
      : chg.ended_at ? t("home.card.last_charge", { ago: fmt.ago(Date.now() / 1000 - chg.ended_at), soc: chg.end_soc ?? "–" }) : t("home.card.no_charge"))}</div></a>`);

  // climate
  cards.push(`<a class="tile2" href="/controls#climate">
    <div class="t-head">${icon("thermo", 16)}<span>${esc(t("home.card.climate"))}</span><span class="go">${icon("chevron", 16)}</span></div>
    <div class="t-big">${esc(tempStr(c.inside_temp ?? o.inside_temp))}<small>${esc(t("home.card.inside"))}</small></div>
    <div class="t-sub">${esc(t("home.card.outside", { temp: tempStr(c.outside_temp ?? o.outside_temp) }))} · ${esc(truthy(c.is_climate_on) ? t("home.card.climate_running") : t("home.card.climate_off"))}</div></a>`);

  // location (map)
  cards.push(`<a class="tile2" href="/trips" id="loc-card" style="min-height:200px">
    <div class="minimap" id="minimap"></div>
    <div class="over"><div class="t-head" style="color:var(--text-primary)">${icon("pin", 16)}<span id="loc-name">${esc(c.geofence || o.place || t("home.card.location"))}</span></div>
    <div class="t-sub" id="loc-sub"></div></div></a>`);

  // security
  let sentry = null;
  try { sentry = await apiGet(`/api/alerts/sentry/sessions?limit=20&car_id=${carId}`); } catch (e) { /* optional */ }
  const today = (Array.isArray(sentry) ? sentry : []).filter((s) => Date.now() / 1000 - s.started < 86400);
  const alarm = today.some((s) => s.peak_state === "panic");
  cards.push(`<a class="tile2 ${alarm ? "alert" : ""}" href="/security">
    <div class="t-head">${icon("shield", 16)}<span>${esc(t("home.card.security"))}</span><span class="go">${icon("chevron", 16)}</span></div>
    <div class="t-big">${today.length}<small>${esc(t("home.card.events_24h"))}</small></div>
    <div class="t-sub">${esc(truthy(c.sentry_mode) ? t("home.sentry_on") : t("home.card.sentry_off"))}</div></a>`);

  // tyres
  const ty = ["fl", "fr", "rl", "rr"].map((k) => num(c[`tpms_pressure_${k}`]));
  if (ty.some((v) => v != null)) {
    const cell = (k, v, r) => `<div class="ty ${r ? "r" : ""} ${truthy(c[`tpms_soft_warning_${k}`]) ? "warn" : ""}">${v == null ? "–" : U.press(v)}<small>${U.pressLabel()}</small></div>`;
    cards.push(`<button class="tile2" id="tyre-card">
      <div class="t-head">${icon("tire", 16)}<span>${esc(t("home.card.tyres"))}</span></div>
      <div class="tyres">${cell("fl", ty[0])}<div class="car"></div>${cell("fr", ty[1], true)}${cell("rl", ty[2])}${cell("rr", ty[3], true)}</div></button>`);
  }

  // software & health
  const health = o.health || {};
  cards.push(`<a class="tile2" href="/settings#vehicles">
    <div class="t-head">${icon("download", 16)}<span>${esc(t("home.card.software"))}</span><span class="go">${icon("chevron", 16)}</span></div>
    <div class="t-big" style="font-size:22px">${esc(c.version || o.version || "–")}</div>
    <div class="t-sub">${esc(truthy(c.update_available) ? t("home.card.update", { v: c.update_version || "" })
      : health.ok === false ? t("home.card.issues", { n: (health.issues || []).length }) : t("home.card.up_to_date"))}</div></a>`);

  const host = $("cards");
  const mapCard = $("loc-card");
  // Keep the live map instance across refreshes: re-creating a WebGL map on
  // every update would flicker and leak contexts.
  const html = cards.join("");
  if (mapCard && mini) {
    const tmp = document.createElement("div");
    tmp.innerHTML = html;
    tmp.querySelector("#loc-card").replaceWith(mapCard);
    host.replaceChildren(...tmp.childNodes);
    paintLocation(c, o);
  } else {
    host.innerHTML = html;
    mountMiniMap(c, o);
  }
  const tc = $("tyre-card");
  if (tc) tc.addEventListener("click", () => { toggleLabels(true); window.scrollTo({ top: 0, behavior: "smooth" }); });
}

let cardTimer = null;
function scheduleCards() {
  clearTimeout(cardTimer);
  cardTimer = setTimeout(paintCards, 1500);
}

let mini = null;
async function mountMiniMap(c, o) {
  const lat = num(c.latitude) ?? o.latitude, lon = num(c.longitude) ?? o.longitude;
  const el = $("minimap");
  if (!el || lat == null || lon == null) return;
  try {
    mini = await OmtMap.create(el, { center: [lat, lon], zoom: 14.2, interactive: false });
    OmtMap.ready(mini).then(() => { miniMarker = OmtMap.carMarker(mini, OmtMap.ll(mini, lat, lon), num(c.heading)); });
  } catch (e) { /* map optional */ }
  paintLocation(c, o);
}

let miniMarker = null;
function paintLocation(c, o) {
  const name = $("loc-name"), sub = $("loc-sub");
  if (name) name.textContent = c.geofence || o.place || t("home.card.location");
  if (!sub) return;
  if (c.state === "driving") {
    const kmh = num(c.speed);
    sub.textContent = t("home.card.driving", { speed: kmh == null ? "–" : `${U.speed(kmh)} ${U.speedLabel()}` });
  } else {
    sub.textContent = c.since ? t("home.card.since", { ago: fmt.ago((Date.now() - Date.parse(c.since)) / 1000) }) : "";
  }
  const lat = num(c.latitude), lon = num(c.longitude);
  if (mini && lat != null && lon != null) {
    const at = OmtMap.ll(mini, lat, lon);
    if (miniMarker) miniMarker.setLngLat(at); else if (mini.loaded()) miniMarker = OmtMap.carMarker(mini, at, num(c.heading));
    mini.easeTo({ center: at, duration: 600 });
  }
}

/* ------------------------------------------------------------- setup */

async function paintSetup() {
  const caps = await Cmd.capabilities(Car.current, true);
  capsReady = !!caps.ready;
  const el = $("setup");
  const archived = Car.info() && Car.info().active === false;
  el.hidden = capsReady || archived;
  if (!el.hidden) {
    el.querySelector(".txt").innerHTML = `<b>${esc(t("home.setup.title"))}</b>${esc(
      caps.reason === "not_paired" ? t("home.setup.pair") : t("home.setup.body"))}`;
  }
  paintQuick(Live.car());
}

/* -------------------------------------------------------------- boot */

async function boot() {
  await i18nReady;
  await Car.load();
  Car.paint();
  document.querySelectorAll("#views button").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll("#views button").forEach((x) => x.classList.toggle("on", x === b));
    if (!view) return;
    if (b.dataset.v === "tires") { toggleLabels(true); return; }
    toggleLabels(false);
    view.view(b.dataset.v);
  }));
  window.onOverviewData = (d) => { overview = d; paintHero(); scheduleCards(); };
  await mountVehicle(Car.current);
  paintHero();
  paintCards();
  paintSetup();
  Live.subscribe(() => { paintHero(); scheduleCards(); });
  Car.onChange(async (id) => {
    overview = null;
    await mountVehicle(id);
    paintHero(); paintCards(); paintSetup();
    refreshConnection();
  });
  // When the car is awake, read the full state once (tyres, limits, seats).
  const st = Live.car().state;
  if (["online", "charging", "driving"].includes(st)) {
    try { fullState = await apiGet(`/api/vehicle/state?car_id=${Car.current}`); } catch (e) { /* optional */ }
  }
  window.addEventListener("resize", () => { if (labelsOn) paintLabels(); });
}

boot();
