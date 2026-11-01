/* 控制 — what do I want it to do?
 *
 * Grouped by job: climate, charging, openings, security, Cybertruck; media,
 * navigation and software folded away under 更多. State comes from the full
 * vehicle read (/api/vehicle/state — only while the car is awake, it never
 * wakes it) with TeslaMate's live values as the fallback; what we can't know
 * is shown as unknown rather than guessed.
 *
 * Every command goes pressed → pending (on the button, aria-busy, naming the
 * operation) → done / failed, written inline in its row. Risky commands
 * (catalogue risk ≥ 1: unlock, frunk, trunk, windows, Sentry off …) ask once
 * in a sheet that says what will happen; the rest run on a tap.
 */
(function () {
  const $ = (id) => document.getElementById(id);
  let S = null;            // full state (or the last known one while asleep)
  let caps = null;         // capabilities for this car
  let pending = 0;         // commands in flight: hold re-renders until they land
  let dirty = false;
  const status = new Map();   // row key -> {cls, html, until}
  const truthy = (v) => v === true || v === "true";
  const num = (v) => (v == null || v === "" || !isFinite(Number(v)) ? null : Number(v));

  const SEATS = [["fl", 0], ["fr", 1], ["rl", 2], ["rc", 4], ["rr", 5]];
  const SUSPENSION = [[1, "entry"], [2, "low"], [3, "medium"], [4, "high"], [5, "very_high"], [6, "extract"]];

  function has(cmd) {
    return !caps || !caps.commands || !caps.commands.length || caps.commands.some((c) => c.id === cmd);
  }

  /* --------------------------------------------------------- markup */
  function btn(cmd, label, o = {}) {
    if (!has(cmd)) return "";
    const text = label || t("cmd." + cmd);
    return `<button type="button" class="cb${o.danger ? " danger" : ""}" data-cmd="${cmd}"
      ${o.params ? `data-params='${esc(JSON.stringify(o.params))}'` : ""}
      ${o.on != null ? `aria-pressed="${o.on ? "true" : "false"}"` : ""}
      ${o.icon ? ` aria-label="${esc(text)}"` : ""}>${o.icon ? icon(o.icon, 16) : `<span class="cb-l">${esc(text)}</span>`}</button>`;
  }
  const seg = (inner) => (inner.trim() ? `<div class="segmented" role="group">${inner}</div>` : "");
  function row(key, label, sub, controls, o = {}) {
    if (!controls || !controls.trim()) return "";
    return `<div class="cr${o.stack ? " stack" : ""}" data-row="${key}">
      <div class="cr-l">${esc(label)}${sub ? `<small class="${o.subCls || ""}">${esc(sub)}</small>` : ""}</div>
      <div class="cr-c">${controls}</div>
      <span class="inline-status" role="status" aria-live="polite"></span></div>`;
  }
  function group(id, ic, title, state, o, body) {
    if (!body || !body.trim()) return "";
    return `<section class="cg rail${o.live ? " is-live" : ""}" id="${id}" aria-labelledby="${id}-h">
      <header class="cg-h"><span class="cg-ic">${icon(ic, 18)}</span><h2 id="${id}-h">${esc(title)}</h2>
        <span class="cg-state">${esc(state || "")}</span></header>${body}</section>`;
  }
  const sub = (title) => `<div class="cg-sub">${esc(title)}</div>`;
  const yesno = (v, on, off) => (v == null ? t("controls.unknown") : v ? on : off);
  const tempStr = (c, d = 1) => (c == null || c === "" ? "–" : `${fmt.num(U.temp(Number(c), d), d)}${U.tempLabel()}`);
  const hhmm = (min) => `${String(Math.floor(min / 60)).padStart(2, "0")}:${String(min % 60).padStart(2, "0")}`;

  /* --------------------------------------------------------- render */
  function render(force) {
    if (!force && pending) { dirty = true; return; }
    dirty = false;
    const live = Live.car();
    const ct = (caps && caps.model === "cybertruck") || (Car.info() && /cyber/i.test(Car.label(Car.info())));
    const c = (S && S.charge) || {}, cl = (S && S.climate) || {}, x = (S && S.closures) || {};
    const html = [];

    /* climate */
    const on = cl.on ?? (live.is_climate_on != null ? truthy(live.is_climate_on) : null);
    const inside = cl.inside_c ?? num(live.inside_temp);
    const tempC = cl.driver_c ?? 21;
    const seats = cl.seats || {};
    const lv = (n) => `<span class="lv" aria-hidden="true">${[1, 2, 3].map((i) => `<i class="${(n || 0) >= i ? "on" : ""}"></i>`).join("")}</span>`;
    const seatHtml = `<div class="seatgrid">${SEATS.filter(([k]) => k !== "rc" || seats.rc != null).map(([k, pos]) =>
      `<button type="button" class="cb seat" data-seat="${pos}" data-level="${seats[k] ?? 0}"
         aria-label="${esc(t("controls.seat_" + k))} ${esc(t("controls.level_n", { n: seats[k] ?? 0 }))}">${esc(t("controls.seat_" + k))}${lv(seats[k])}</button>`).join("")}</div>`;
    const cool = cl.seat_cool || {};
    const coolHtml = (S && S.config && S.config.has_seat_cooling) || cool.fl != null
      ? `<div class="seatgrid two">${[["fl", 1], ["fr", 2]].map(([k, pos]) =>
        `<button type="button" class="cb seat" data-cool="${pos}" data-level="${cool[k] ?? 0}"
           aria-label="${esc(t("controls.vent_" + k))} ${esc(t("controls.level_n", { n: cool[k] ?? 0 }))}">${esc(t("controls.vent_" + k))}${lv(cool[k])}</button>`).join("")}</div>` : "";
    const keeper = cl.keeper ?? live.climate_keeper_mode;
    html.push(group("climate", "climate", t("home.card.climate"),
      on == null ? tempStr(inside, 0) : on ? t("controls.climate_on_in", { temp: tempStr(inside, 0) }) : t("controls.climate_off_in", { temp: tempStr(inside, 0) }),
      { live: on === true },
      row("climate", t("controls.climate"), "", btn("climate_on", t("controls.on"), { on: on === true }) + btn("climate_off", t("controls.off"), { on: on === false }))
      + (has("set_temps") ? row("temp", t("controls.temperature"), t("controls.both_zones"),
          `<span class="stepper"><button type="button" class="cb" data-temp="-0.5" aria-label="${esc(t("controls.cooler"))}">−</button>
           <span class="val" id="temp-val" data-c="${tempC}" aria-live="polite">${tempStr(tempC)}</span>
           <button type="button" class="cb" data-temp="0.5" aria-label="${esc(t("controls.warmer"))}">+</button></span>`) : "")
      + (has("seat_heater") ? row("seats", t("controls.seat_heat"), t("controls.tap_cycle"), seatHtml, { stack: true }) : "")
      + (has("seat_cooler") && coolHtml ? row("cool", t("controls.seat_vent"), "", coolHtml, { stack: true }) : "")
      + row("steering", t("controls.steering"), "", btn("steering_heater_on", t("controls.on"), { on: (cl.steering_heat || 0) > 0 }) + btn("steering_heater_off", t("controls.off"), { on: cl.steering_heat === 0 }))
      + row("defrost", t("controls.defrost"), "", btn("defrost_on", t("controls.on"), { on: cl.defrost_front === true }) + btn("defrost_off", t("controls.off"), { on: cl.defrost_front === false }))
      + row("keeper", t("controls.keeper"), "", seg(
          btn("keeper_off", t("controls.keeper_off"), { on: keeper === "off" }) + btn("keeper_on", t("controls.keeper_on"), { on: keeper === "on" })
          + btn("dog_mode_on", t("controls.keeper_dog"), { on: keeper === "dog" }) + btn("camp_mode_on", t("controls.keeper_camp"), { on: keeper === "camp" })))
      + row("cop", t("controls.cop"), "", seg(btn("cabin_overheat_off", t("controls.off"), { on: cl.cop === "Off" })
          + btn("cabin_overheat_fan", t("controls.fan_only"), { on: cl.cop === "FanOnly" }) + btn("cabin_overheat_on", t("controls.on"), { on: cl.cop === "On" })))
      + row("bioweapon", t("controls.bioweapon"), "", btn("bioweapon_on", t("controls.on"), { on: cl.bioweapon === true }) + btn("bioweapon_off", t("controls.off"), { on: cl.bioweapon === false }))));

    /* charging */
    const cstate = c.state || live.charging_state;
    const charging = cstate === "Charging";
    const limit = c.limit ?? num(live.charge_limit_soc) ?? 80;
    const amps = c.amps ?? num(live.charge_current_request);
    const ampsMax = c.amps_max || num(live.charge_current_request_max) || 48;
    const port = c.port_open ?? (live.charge_port_door_open != null ? truthy(live.charge_port_door_open) : null);
    const kw = c.power_kw ?? num(live.charger_power);
    html.push(group("charging", "bolt", t("home.card.charging"),
      charging ? t("controls.charging_at", { kw: fmt.num(kw, 0) }) : cstate ? t("controls.charge_state." + cstate) : "",
      { live: charging },
      row("charge", t("controls.charging"), "", btn("charge_start", t("controls.start"), { on: charging }) + btn("charge_stop", t("controls.stop"), { on: cstate === "Stopped" }))
      + (has("set_charge_limit") ? row("limit", t("controls.limit"), "",
          `<div class="range-row"><input type="range" min="50" max="100" step="1" value="${limit}" id="limit" aria-label="${esc(t("controls.limit"))}">
           <span class="val" id="limit-val">${fmt.pct(limit)}</span><button type="button" class="cb" id="limit-set"><span class="cb-l">${esc(t("controls.set"))}</span></button></div>`, { stack: true }) : "")
      + (has("set_charge_amps") ? row("amps", t("controls.amps"), amps == null ? "" : t("controls.amps_now", { a: amps }),
          `<div class="range-row"><input type="range" min="1" max="${ampsMax}" step="1" value="${amps ?? ampsMax}" id="amps" aria-label="${esc(t("controls.amps"))}">
           <span class="val" id="amps-val">${amps ?? ampsMax} A</span><button type="button" class="cb" id="amps-set"><span class="cb-l">${esc(t("controls.set"))}</span></button></div>`, { stack: true }) : "")
      + (has("schedule_charge") ? row("sched", t("controls.scheduled"), c.scheduled_start_min != null ? t("controls.starts_at", { t: hhmm(c.scheduled_start_min) }) : "",
          `<input type="time" id="sched" value="${c.scheduled_start_min != null ? hhmm(c.scheduled_start_min) : "00:00"}" aria-label="${esc(t("controls.scheduled"))}">
           <button type="button" class="cb" id="sched-set"><span class="cb-l">${esc(t("controls.set"))}</span></button>${btn("schedule_charge_off", t("controls.off"))}`) : "")
      + row("port", t("home.q.port"), yesno(port, t("controls.open"), t("controls.closed")),
          btn("charge_port_open", t("controls.open_btn"), { on: port === true }) + btn("charge_port_close", t("controls.close_btn"), { on: port === false }))));

    /* openings */
    const frunk = x.frunk ?? (live.frunk_open != null ? truthy(live.frunk_open) : null);
    const trunk = x.trunk ?? (live.trunk_open != null ? truthy(live.trunk_open) : null);
    const winOpen = x.windows ? Object.values(x.windows).some(Boolean) : (live.windows_open != null ? truthy(live.windows_open) : null);
    const doorsOpen = x.doors ? Object.values(x.doors).some(Boolean) : (live.doors_open != null ? truthy(live.doors_open) : null);
    const openList = [frunk && t("home.q.frunk"), trunk && (ct ? t("controls.tailgate") : t("home.q.trunk")), winOpen && t("controls.windows"), doorsOpen && t("home.hud.doors")].filter(Boolean);
    const p = (S && S.cybertruck) || {};
    html.push(group("closures", "box", t("controls.closures"),
      openList.length ? t("home.now.open", { what: fmt.list(openList) }) : (frunk === false ? t("controls.all_closed") : ""),
      {},
      row("frunk", t("home.q.frunk"), yesno(frunk, t("controls.open"), t("controls.closed")),
          frunk ? `<span class="cr-note">${esc(t("home.hint.frunk_close"))}</span>` : btn("frunk"), { subCls: frunk ? "is-warn" : "" })
      + row("trunk", ct ? t("controls.tailgate") : t("home.q.trunk"), yesno(trunk, t("controls.open"), t("controls.closed")), btn("trunk", t("controls.toggle")), { subCls: trunk ? "is-warn" : "" })
      + (ct ? row("tonneau", t("home.q.tonneau"), p.tonneau_percent_open != null ? t("controls.tonneau_pct", { n: fmt.int(p.tonneau_percent_open) }) : "",
          btn("tonneau_open", t("controls.open_btn")) + btn("tonneau_stop", t("controls.stop")) + btn("tonneau_close", t("controls.close_btn"))) : "")
      + row("windows", t("controls.windows"), yesno(winOpen, t("controls.open"), t("controls.closed")), btn("vent_windows") + btn("close_windows", t("controls.close_btn"), { on: winOpen === false }), { subCls: winOpen ? "is-warn" : "" })
      + (has("sunroof_vent") && S && S.config && S.config.sun_roof ? row("sunroof", t("controls.sunroof"), "", btn("sunroof_vent", t("controls.vent")) + btn("sunroof_close", t("controls.close_btn"))) : "")));

    /* security */
    const locked = x.locked ?? (live.locked != null ? truthy(live.locked) : null);
    const sentry = x.sentry ?? (live.sentry_mode != null ? truthy(live.sentry_mode) : null);
    html.push(group("security", "shield", t("controls.security"),
      [yesno(locked, t("home.locked"), t("home.unlocked")), sentry ? t("home.sentry_on") : ""].filter(Boolean).join(" · "),
      { live: sentry === true },
      row("doors", t("controls.doors"), "", btn("door_lock", t("controls.lock"), { on: locked === true }) + btn("door_unlock"))
      + row("sentry", t("controls.sentry"), t("controls.sentry_sub"), btn("sentry_on", t("controls.on"), { on: sentry === true }) + btn("sentry_off", t("controls.off"), { on: sentry === false }))
      + row("signal", t("controls.signal"), "", btn("flash_lights", t("controls.flash")) + btn("honk_horn", t("controls.honk")))
      + row("clip", t("controls.dashcam"), t("controls.dashcam_sub"), btn("dashcam_save_clip", t("controls.save_clip")))
      + row("homelink", t("controls.homelink"), "", btn("homelink", t("controls.trigger")))
      + row("keyless", t("controls.remote_start"), t("controls.remote_start_sub"), btn("remote_start", t("controls.start"), { danger: true }))));

    /* Cybertruck: power, lights, ride height in one group */
    if (ct) {
      const outlet = p.outlet_state || p.outlets_state;
      html.push(group("cybertruck", "bed", "Cybertruck", outlet ? t("controls.outlets_" + String(outlet).toLowerCase()) : "", {},
        sub(t("controls.power"))
        + row("outlets", t("controls.outlets"), t("controls.outlets_sub"), seg(btn("outlets_off", t("controls.off"), { on: outlet === "Off" })
            + btn("outlets_cabin", t("controls.cabin"), { on: outlet === "Cabin" }) + btn("outlets_cabin_bed", t("controls.cabin_bed"), { on: outlet === "CabinAndBed" })))
        + row("powershare", t("controls.powershare"), t("controls.powershare_sub"), btn("powershare_on", t("controls.start")) + btn("powershare_off", t("controls.stop")))
        + row("accessory", t("controls.accessory"), t("controls.accessory_sub"), btn("accessory_power_on", t("controls.on")) + btn("accessory_power_off", t("controls.off")))
        + row("lowpower", t("controls.low_power"), "", btn("low_power_on", t("controls.on")) + btn("low_power_off", t("controls.off")))
        + sub(t("controls.lights"))
        + row("lightbar", t("controls.lightbar"), t("controls.lightbar_sub"), seg([["0", "off"], ["33", "low"], ["66", "med"], ["100", "high"]].map(([v, k]) =>
            btn("lightbar_brightness", t("controls.level_" + k), { params: { brightness: Number(v) } })).join("")), { stack: true })
        + row("ditch", t("controls.ditch"), "", btn("ditch_lights_on", t("controls.on")) + btn("ditch_lights_off", t("controls.off")))
        + row("middle", t("controls.lightbar_middle"), "", btn("lightbar_middle_on", t("controls.on")) + btn("lightbar_middle_off", t("controls.off")))
        + row("bed", t("controls.bed_lights"), "", seg(btn("bed_light_auto_on", t("controls.auto")) + [["0", "off"], ["50", "med"], ["100", "high"]].map(([v, k]) =>
            btn("bed_light_brightness", t("controls.level_" + k), { params: { brightness: Number(v) } })).join("")), { stack: true })
        + row("tent", t("controls.tent"), t("controls.tent_sub"), btn("tent_mode_on", t("controls.on")) + btn("tent_mode_off", t("controls.off")))
        + sub(t("controls.suspension"))
        + row("ride", t("controls.ride_height"), t("controls.ride_note"), seg(SUSPENSION.map(([lvl, k]) =>
            btn("suspension_level", t("controls.ride_" + k), { params: { level: lvl } })).join("")), { stack: true })));
    }

    /* the rarely used rest, folded */
    const m = (S && S.media) || {};
    const sw = (S && S.software) || {};
    const more = sub(t("controls.media"))
      + row("playback", m.title ? `${m.title}${m.artist ? " — " + m.artist : ""}` : t("controls.playback"), "",
          btn("media_prev", t("cmd.media_prev"), { icon: "prev" }) + btn("media_toggle", t("controls.play_pause"))
          + btn("media_next", t("cmd.media_next"), { icon: "next" }))
      + row("volume", t("controls.volume"), m.volume != null ? `${fmt.num(m.volume, 1)} / ${fmt.num(m.volume_max || 11, 0)}` : "",
          btn("volume_down", t("cmd.volume_down"), { icon: "minus" }) + btn("volume_up", t("cmd.volume_up"), { icon: "plus" }))
      + sub(t("controls.navigation"))
      + (has("navigate_to") ? row("nav", t("cmd.navigate_to"), "",
          `<input type="text" id="dest" placeholder="${esc(t("controls.dest_ph"))}" autocomplete="street-address" aria-label="${esc(t("controls.dest_ph"))}">
           <button type="button" class="cb" id="dest-send"><span class="cb-l">${esc(t("controls.send"))}</span></button>`, { stack: true }) : "")
      + `<div id="software">${sub(t("home.card.software"))}</div>`
      + row("software", sw.version || live.version || t("home.card.software"),
          sw.update_status ? t("controls.update_status", { s: sw.update_status, v: sw.update_version || "" }) : truthy(live.update_available) ? t("home.card.update", { v: live.update_version || "" }) : t("home.card.up_to_date"),
          btn("schedule_update", t("controls.install"), { params: { offset_sec: 0 } }) + btn("cancel_update", t("controls.cancel")));
    html.push(`<details class="disclose cg-more" id="more"${location.hash === "#software" ? " open" : ""}>
      <summary><span>${esc(t("controls.more"))}</span><span class="value">${esc(t("controls.more_sub"))}</span><span class="chev">${icon("chevron", 16)}</span></summary>
      <div class="body">${more}</div></details>`);

    $("grid").innerHTML = html.join("");
    bind();
    // transient row messages survive a re-render
    for (const [key, s] of status) {
      if (s.until && s.until < Date.now()) { status.delete(key); continue; }
      const el = document.querySelector(`[data-row="${key}"] > .inline-status`);
      if (el) { el.className = "inline-status " + s.cls; el.innerHTML = s.html; }
    }
  }

  /* ----------------------------------------------------------- commands */
  function rowStatus(el) {
    const r = el.closest("[data-row]");
    return { key: r && r.dataset.row, el: r && r.querySelector(":scope > .inline-status") };
  }
  function remember(key, el) {
    if (!key || !el) return;
    const done = el.classList.contains("is-done");
    status.set(key, { cls: el.className.replace("inline-status", "").trim(), html: el.innerHTML, until: done ? Date.now() + 6000 : 0 });
  }

  async function send(button, cmd, params, opts = {}) {
    const { key, el } = rowStatus(button);
    const report = Cmd.bindStatus(button, el, {
      onState: (phase) => {
        remember(key, el);
        if (phase === "pending") return;
        if (phase === "done" || phase === "sent" || phase === "unconfirmed") {
          if (phase !== "sent") setTimeout(() => refresh(true), 1500);
        }
      },
    });
    pending++;
    try {
      const r = await Cmd.run(cmd, params, { ...opts, onState: report });
      return r;
    } finally {
      pending--;
      if (dirty && !pending) setTimeout(() => render(), 50);
    }
  }

  function bind() {
    document.querySelectorAll("#grid [data-cmd]").forEach((b) => {
      const cmd = b.dataset.cmd;
      const params = b.dataset.params ? JSON.parse(b.dataset.params) : null;
      b.addEventListener("click", () => send(b, cmd, params));
    });

    document.querySelectorAll("#grid [data-temp]").forEach((b) => b.addEventListener("click", () => {
      const v = $("temp-val");
      const c = Math.max(15, Math.min(28, Number(v.dataset.c) + Number(b.dataset.temp)));
      v.dataset.c = c; v.textContent = tempStr(c);
      clearTimeout(v._t);
      // send once the taps stop
      v._t = setTimeout(() => send(b, "set_temps", { driver_temp: c, passenger_temp: c }), 900);
    }));
    const cycle = (attr, cmd, key) => document.querySelectorAll(`#grid .seat[${attr}]`).forEach((b) => b.addEventListener("click", () => {
      const next = (Number(b.dataset.level) + 1) % 4;      // 0 off … 3 high
      b.dataset.level = next;
      b.querySelectorAll(".lv i").forEach((i, k) => i.classList.toggle("on", next > k));
      clearTimeout(b._t);
      b._t = setTimeout(() => send(b, cmd, { [key]: Number(b.dataset[attr === "data-seat" ? "seat" : "cool"]), level: next }), 700);
    }));
    cycle("data-seat", "seat_heater", "seat");
    cycle("data-cool", "seat_cooler", "seat");

    const lim = $("limit");
    if (lim) {
      lim.addEventListener("input", () => { $("limit-val").textContent = fmt.pct(Number(lim.value)); });
      $("limit-set").addEventListener("click", (e) => send(e.currentTarget, "set_charge_limit", { percent: Number(lim.value) }));
    }
    const amps = $("amps");
    if (amps) {
      amps.addEventListener("input", () => { $("amps-val").textContent = `${amps.value} A`; });
      $("amps-set").addEventListener("click", (e) => send(e.currentTarget, "set_charge_amps", { charging_amps: Number(amps.value) }));
    }
    const ss = $("sched-set");
    if (ss) ss.addEventListener("click", (e) => {
      const [h, mi] = $("sched").value.split(":").map(Number);
      send(e.currentTarget, "schedule_charge", { time: h * 60 + mi });
    });
    const ds = $("dest-send");
    if (ds) ds.addEventListener("click", (e) => {
      const v = $("dest").value.trim();
      if (v) send(e.currentTarget, "navigate_to", { address: v });
    });
  }

  /* ------------------------------------------------------------ state */
  function say(cls, html) {
    const el = $("c-status");
    el.className = "inline-status " + cls;
    el.innerHTML = html;
  }

  async function refresh(force) {
    const carId = Car.current;
    try { S = await apiGet(`/api/vehicle/state?car_id=${carId}${force ? "&refresh=1" : ""}`); } catch (e) { S = null; }
    const asleep = S && S.asleep;
    $("c-wake").hidden = !asleep;
    const fetched = S && (S.fetched_at || (S.last && S.last.fetched_at));
    $("c-fresh").textContent = asleep
      ? (fetched ? t("controls.asleep_at", { t: fmt.time(fetched * 1000) }) : t("controls.asleep"))
      : S && S.fetched_at ? t("controls.updated", { ago: fmt.ago(Date.now() / 1000 - S.fetched_at) }) : " ";
    if (asleep && S.last) S = { ...S.last, asleep: true };
    render();
  }

  function paintSetup() {
    const note = $("c-setup");
    const ready = !caps || caps.ready !== false || caps.reason === "error";
    note.hidden = ready || (Car.info() && Car.info().active === false);
    if (!note.hidden) {
      const why = caps.reason === "not_paired" ? t("controls.setup_pair") : t("controls.setup_short");
      note.innerHTML = `${icon("key", 16)}<span>${esc(why)}${fmt.gap(why)}<a href="/settings#commands">${esc(t("cmd.setup.go"))}</a></span>`;
    }
  }

  async function boot() {
    await i18nReady.catch(() => {});
    await Car.load(); Car.paint();
    caps = await Cmd.capabilities(Car.current, true);
    paintSetup();
    await refresh(false);
    $("c-refresh").addEventListener("click", async (e) => {
      const b = e.currentTarget;
      b.setAttribute("aria-busy", "true"); b.disabled = true;
      say("is-pending", `${icon("clock", 14)}<span>${esc(t("controls.refreshing"))}</span>`);
      await refresh(true);
      b.removeAttribute("aria-busy"); b.disabled = false;
      say("", "");
    });
    $("c-wake").addEventListener("click", async (e) => {
      const b = e.currentTarget;
      b.disabled = true; b.setAttribute("aria-busy", "true");
      say("is-pending", `${icon("clock", 14)}<span>${esc(t("controls.waking"))}</span>`);
      try {
        await apiPost(`/api/vehicle/wake?car_id=${Car.current}`);
        say("is-pending", `${icon("clock", 14)}<span>${esc(t("controls.waking_sent"))}</span>`);
        setTimeout(async () => { await refresh(true); say("", ""); b.disabled = false; b.removeAttribute("aria-busy"); }, 15000);
      } catch (err) {
        say("is-fail", `${icon("warn", 14)}<span>${esc(err.message)}</span>`);
        b.disabled = false; b.removeAttribute("aria-busy");
      }
    });
    Car.onChange(async () => { caps = await Cmd.capabilities(Car.current, true); paintSetup(); status.clear(); refresh(false); });
    let tm = null;
    Live.subscribe(() => { if (!S || S.asleep) { clearTimeout(tm); tm = setTimeout(() => render(), 400); } });
    window.addEventListener("omt:locale", () => { paintSetup(); render(true); });
    if (location.hash) setTimeout(() => document.querySelector(location.hash)?.scrollIntoView({ block: "start" }), 100);
  }
  boot();
})();
