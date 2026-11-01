/* Controls — everything the official app can do that the public API allows.
 *
 * State comes from the full vehicle read (/api/vehicle/state, only while the
 * car is awake) with TeslaMate's live values as the fallback; what we can't
 * know is shown as unknown rather than guessed. Every button goes through
 * Cmd.run, which handles confirmation, setup and "may have executed".
 */
(function () {
  const $ = (id) => document.getElementById(id);
  let S = null;            // full state
  let caps = null;         // capabilities for this car
  const truthy = (v) => v === true || v === "true";

  const SEATS = [["fl", 0], ["fr", 1], ["rl", 2], ["rc", 4], ["rr", 5]];
  const SUSPENSION = [[1, "entry"], [2, "low"], [3, "medium"], [4, "high"], [5, "very_high"], [6, "extract"]];

  function has(cmd) {
    return !caps || !caps.commands || caps.commands.some((c) => c.id === cmd);
  }
  function btn(cmd, label, opts = {}) {
    if (!has(cmd)) return "";
    return `<button class="cbtn ${opts.on ? "on" : ""}" data-cmd="${cmd}" ${opts.params ? `data-params='${esc(JSON.stringify(opts.params))}'` : ""}>${esc(label || t("cmd." + cmd))}</button>`;
  }
  function row(label, sub, right) {
    return `<div class="ctl-row"><div class="lbl">${esc(label)}${sub ? `<small>${esc(sub)}</small>` : ""}</div><div class="ctl-btns">${right}</div></div>`;
  }
  function card(id, ic, title, state, body) {
    if (!body.trim()) return "";
    return `<section class="ctl-card" id="${id}"><h3>${icon(ic, 18)}<span>${esc(title)}</span><span class="state">${esc(state || "")}</span></h3>${body}</section>`;
  }
  const yesno = (v, on, off) => (v == null ? t("controls.unknown") : v ? on : off);

  function render() {
    const live = Live.car();
    const ct = caps && caps.model === "cybertruck";
    const c = (S && S.charge) || {}, cl = (S && S.climate) || {}, x = (S && S.closures) || {};
    const locked = x.locked ?? (live.locked != null ? truthy(live.locked) : null);
    const sentry = x.sentry ?? (live.sentry_mode != null ? truthy(live.sentry_mode) : null);
    const html = [];

    // security
    html.push(card("security", "shield", t("controls.security"),
      [yesno(locked, t("home.locked"), t("home.unlocked")), sentry ? t("home.sentry_on") : ""].filter(Boolean).join(" · "),
      row(t("controls.doors"), "", btn("door_lock", t("controls.lock"), { on: locked === true }) + btn("door_unlock", t("controls.unlock"), { on: locked === false }))
      + row(t("controls.sentry"), t("controls.sentry_sub"), btn("sentry_on", t("controls.on"), { on: sentry === true }) + btn("sentry_off", t("controls.off"), { on: sentry === false }))
      + row(t("controls.signal"), "", btn("flash_lights", t("controls.flash")) + btn("honk_horn", t("controls.honk")))
      + row(t("controls.dashcam"), t("controls.dashcam_sub"), btn("dashcam_save_clip", t("controls.save_clip")))
      + row(t("controls.homelink"), "", btn("homelink", t("controls.trigger")))
      + row(t("controls.remote_start"), t("controls.remote_start_sub"), btn("remote_start", t("controls.start")))));

    // closures
    const doors = x.doors || {};
    const anyDoor = Object.values(doors).some((v) => v);
    html.push(card("closures", "box", t("controls.closures"), anyDoor ? t("controls.door_open") : "",
      row(t("home.q.frunk"), yesno(x.frunk ?? truthy(live.frunk_open), t("controls.open"), t("controls.closed")), btn("frunk", t("controls.open_btn")))
      + row(ct ? t("controls.tailgate") : t("home.q.trunk"), yesno(x.trunk ?? truthy(live.trunk_open), t("controls.open"), t("controls.closed")), btn("trunk", t("controls.toggle")))
      + (ct ? row(t("home.q.tonneau"), tonneauState(), btn("tonneau_open", t("controls.open_btn")) + btn("tonneau_stop", t("controls.stop")) + btn("tonneau_close", t("controls.close_btn"))) : "")
      + row(t("controls.windows"), "", btn("vent_windows", t("controls.vent")) + btn("close_windows", t("controls.close_btn")))
      + row(t("home.q.port"), yesno(c.port_open ?? truthy(live.charge_port_door_open), t("controls.open"), t("controls.closed")),
            btn("charge_port_open", t("controls.open_btn")) + btn("charge_port_close", t("controls.close_btn")))
      + (has("sunroof_vent") ? row(t("controls.sunroof"), "", btn("sunroof_vent", t("controls.vent")) + btn("sunroof_close", t("controls.close_btn"))) : "")));

    // climate
    const on = cl.on ?? truthy(live.is_climate_on);
    const tempC = cl.driver_c ?? 21;
    const seats = cl.seats || {};
    const seatHtml = `<div class="seatgrid" style="margin:8px 0 6px">${SEATS.filter(([k]) => k !== "rc" || seats.rc != null).map(([k, pos]) =>
      `<button class="seat" data-seat="${pos}" data-level="${seats[k] ?? 0}">${esc(t("controls.seat_" + k))}
        <span class="lv">${[1, 2, 3].map((i) => `<i class="${(seats[k] || 0) >= i ? "on" : ""}"></i>`).join("")}</span></button>`).join("")}</div>`;
    const cool = cl.seat_cool || {};
    const coolHtml = (S && S.config && S.config.has_seat_cooling) || cool.fl != null
      ? `<div class="seatgrid" style="grid-template-columns:repeat(2,1fr);margin:4px 0 6px">${[["fl", 1], ["fr", 2]].map(([k, pos]) =>
        `<button class="seat cool" data-cool="${pos}" data-level="${cool[k] ?? 0}">${esc(t("controls.vent_" + k))}
          <span class="lv">${[1, 2, 3].map((i) => `<i class="${(cool[k] || 0) >= i ? "on" : ""}"></i>`).join("")}</span></button>`).join("")}</div>` : "";
    html.push(card("climate", "thermo", t("home.card.climate"),
      on == null ? "" : on ? t("controls.climate_on_in", { temp: tempStr(cl.inside_c ?? live.inside_temp) }) : t("controls.off"),
      row(t("controls.climate"), "", btn("climate_on", t("controls.on"), { on: on === true }) + btn("climate_off", t("controls.off"), { on: on === false }))
      + (has("set_temps") ? row(t("controls.temperature"), t("controls.both_zones"),
          `<span class="stepper"><button data-temp="-0.5" aria-label="−">−</button><span class="val" id="temp-val" data-c="${tempC}">${tempStr(tempC)}</span><button data-temp="0.5" aria-label="+">+</button></span>`) : "")
      + (has("seat_heater") ? `<div class="ctl-row" style="display:block;padding:10px 0"><div class="lbl" style="margin-bottom:4px">${esc(t("controls.seat_heat"))}<small>${esc(t("controls.tap_cycle"))}</small></div>${seatHtml}${coolHtml ? `<div class="lbl" style="margin:8px 0 4px">${esc(t("controls.seat_vent"))}</div>${coolHtml}` : ""}</div>` : "")
      + row(t("controls.steering"), "", btn("steering_heater_on", t("controls.on"), { on: (cl.steering_heat || 0) > 0 }) + btn("steering_heater_off", t("controls.off")))
      + row(t("controls.defrost"), "", btn("defrost_on", t("controls.on"), { on: cl.defrost_front === true }) + btn("defrost_off", t("controls.off")))
      + row(t("controls.keeper"), cl.keeper ? t("controls.keeper_" + cl.keeper) : "",
            btn("keeper_off", t("controls.off"), { on: cl.keeper === "off" }) + btn("keeper_on", t("controls.keeper_on"), { on: cl.keeper === "on" })
            + btn("dog_mode_on", t("controls.keeper_dog"), { on: cl.keeper === "dog" }) + btn("camp_mode_on", t("controls.keeper_camp"), { on: cl.keeper === "camp" }))
      + row(t("controls.cop"), cl.cop || "", btn("cabin_overheat_off", t("controls.off"), { on: cl.cop === "Off" })
            + btn("cabin_overheat_fan", t("controls.fan_only"), { on: cl.cop === "FanOnly" }) + btn("cabin_overheat_on", t("controls.on"), { on: cl.cop === "On" }))
      + row(t("controls.bioweapon"), "", btn("bioweapon_on", t("controls.on"), { on: cl.bioweapon === true }) + btn("bioweapon_off", t("controls.off")))));

    // charging
    const limit = c.limit ?? (live.charge_limit_soc ? Number(live.charge_limit_soc) : 80);
    const amps = c.amps ?? null, ampsMax = c.amps_max || 48;
    const charging = (c.state || live.charging_state) === "Charging";
    html.push(card("charging", "bolt", t("home.card.charging"), c.state ? t("controls.charge_state." + c.state, {}) : "",
      row(t("controls.charging"), "", btn("charge_start", t("controls.start"), { on: charging }) + btn("charge_stop", t("controls.stop")))
      + (has("set_charge_limit") ? `<div class="ctl-row"><div class="lbl">${esc(t("controls.limit"))}</div></div>
          <div class="range-row" style="padding:0 0 12px"><input type="range" min="50" max="100" step="1" value="${limit}" id="limit">
          <span class="val" id="limit-val">${limit}%</span><button class="cbtn" id="limit-set">${esc(t("controls.set"))}</button></div>` : "")
      + (has("set_charge_amps") ? `<div class="ctl-row"><div class="lbl">${esc(t("controls.amps"))}<small>${esc(amps == null ? "" : t("controls.amps_now", { a: amps }))}</small></div></div>
          <div class="range-row" style="padding:0 0 12px"><input type="range" min="1" max="${ampsMax}" step="1" value="${amps ?? ampsMax}" id="amps">
          <span class="val" id="amps-val">${amps ?? ampsMax} A</span><button class="cbtn" id="amps-set">${esc(t("controls.set"))}</button></div>` : "")
      + row(t("controls.scheduled"), c.scheduled_start_min != null ? t("controls.starts_at", { t: hhmm(c.scheduled_start_min) }) : "",
            `<input type="time" id="sched" value="${c.scheduled_start_min != null ? hhmm(c.scheduled_start_min) : "00:00"}" style="width:110px">`
            + btn("schedule_charge", t("controls.set")) + btn("schedule_charge_off", t("controls.off")))));

    // Cybertruck: power, lights, ride height
    if (ct) {
      const p = (S && S.cybertruck) || {};
      html.push(card("power", "outlet", t("controls.power"), p.outlet_state ? t("controls.outlets_" + String(p.outlet_state).toLowerCase()) : "",
        row(t("controls.outlets"), t("controls.outlets_sub"), btn("outlets_off", t("controls.off")) + btn("outlets_cabin", t("controls.cabin")) + btn("outlets_cabin_bed", t("controls.cabin_bed")))
        + row(t("controls.powershare"), t("controls.powershare_sub"), btn("powershare_on", t("controls.start")) + btn("powershare_off", t("controls.stop")))
        + row(t("controls.accessory"), t("controls.accessory_sub"), btn("accessory_power_on", t("controls.on")) + btn("accessory_power_off", t("controls.off")))
        + row(t("controls.low_power"), "", btn("low_power_on", t("controls.on")) + btn("low_power_off", t("controls.off")))));
      html.push(card("lights", "sun", t("controls.lights"), "",
        row(t("controls.lightbar"), t("controls.lightbar_sub"),
            [["0", "off"], ["33", "low"], ["66", "med"], ["100", "high"]].map(([v, k]) =>
              btn("lightbar_brightness", t("controls.level_" + k), { params: { brightness: Number(v) } })).join(""))
        + row(t("controls.ditch"), "", btn("ditch_lights_on", t("controls.on")) + btn("ditch_lights_off", t("controls.off")))
        + row(t("controls.lightbar_middle"), "", btn("lightbar_middle_on", t("controls.on")) + btn("lightbar_middle_off", t("controls.off")))
        + row(t("controls.bed_lights"), "", btn("bed_light_auto_on", t("controls.auto")) + [["0", "off"], ["50", "med"], ["100", "high"]].map(([v, k]) =>
              btn("bed_light_brightness", t("controls.level_" + k), { params: { brightness: Number(v) } })).join(""))
        + row(t("controls.tent"), t("controls.tent_sub"), btn("tent_mode_on", t("controls.on")) + btn("tent_mode_off", t("controls.off")))));
      html.push(card("ride", "height", t("controls.ride_height"), "",
        `<div class="ctl-btns" style="padding:4px 0 2px">${SUSPENSION.map(([lv, k]) =>
          btn("suspension_level", t("controls.ride_" + k), { params: { level: lv } })).join("")}</div>
         <p class="body-sm" style="margin:12px 0 0">${esc(t("controls.ride_note"))}</p>`));
    }

    // media & navigation & software
    const m = (S && S.media) || {};
    html.push(card("media", "play", t("controls.media"), m.title ? `${m.title}${m.artist ? " — " + m.artist : ""}` : "",
      row(t("controls.playback"), "", btn("media_prev", "‹‹") + btn("media_toggle", t("controls.play_pause")) + btn("media_next", "››"))
      + row(t("controls.volume"), m.volume != null ? `${fmt.num(m.volume, 1)} / ${fmt.num(m.volume_max || 11, 0)}` : "",
            btn("volume_down", "−") + btn("volume_up", "+"))));
    html.push(card("nav", "pin", t("controls.navigation"), "",
      `<div class="row" style="gap:8px;padding:6px 0"><input type="text" id="dest" placeholder="${esc(t("controls.dest_ph"))}" style="flex:1">
        <button class="cbtn" id="dest-send">${esc(t("controls.send"))}</button></div>`));
    const sw = (S && S.software) || {};
    html.push(card("software", "download", t("home.card.software"), sw.version || live.version || "",
      row(sw.update_status ? t("controls.update_status", { s: sw.update_status, v: sw.update_version || "" }) : t("home.card.up_to_date"), "",
          btn("schedule_update", t("controls.install"), { params: { offset_sec: 0 } }) + btn("cancel_update", t("controls.cancel")))));

    $("grid").innerHTML = html.join("");
    bind();
  }

  function tonneauState() {
    const p = (S && S.cybertruck) || {};
    if (p.tonneau_percent_open != null) return t("controls.tonneau_pct", { n: Math.round(p.tonneau_percent_open) });
    return "";
  }
  function tempStr(c) { return c == null || c === "" ? "–" : `${U.temp(Number(c), 1)}${U.tempLabel()}`; }
  function hhmm(min) { return `${String(Math.floor(min / 60)).padStart(2, "0")}:${String(min % 60).padStart(2, "0")}`; }

  function bind() {
    document.querySelectorAll("#grid [data-cmd]").forEach((b) => b.addEventListener("click", async () => {
      const params = b.dataset.params ? JSON.parse(b.dataset.params) : null;
      b.disabled = true;
      const r = await Cmd.run(b.dataset.cmd, params);
      b.disabled = false;
      if (r) setTimeout(() => refresh(true), 2500);
    }));
    document.querySelectorAll("#grid [data-temp]").forEach((b) => b.addEventListener("click", () => {
      const v = $("temp-val");
      const c = Math.max(15, Math.min(28, Number(v.dataset.c) + Number(b.dataset.temp)));
      v.dataset.c = c; v.textContent = tempStr(c);
      clearTimeout(v._t);
      v._t = setTimeout(() => Cmd.run("set_temps", { driver_temp: c }), 900);   // send once the taps stop
    }));
    document.querySelectorAll("#grid .seat[data-seat]").forEach((b) => b.addEventListener("click", () => {
      const next = (Number(b.dataset.level) + 1) % 4;
      b.dataset.level = next;
      b.querySelectorAll(".lv i").forEach((i, k) => i.classList.toggle("on", next > k));
      Cmd.run("seat_heater", { seat: Number(b.dataset.seat), level: next });
    }));
    document.querySelectorAll("#grid .seat[data-cool]").forEach((b) => b.addEventListener("click", () => {
      const next = (Number(b.dataset.level) + 1) % 4;          // 0 off … 3 high
      b.dataset.level = next;
      b.querySelectorAll(".lv i").forEach((i, k) => i.classList.toggle("on", next > k));
      Cmd.run("seat_cooler", { seat: Number(b.dataset.cool), level: next });
    }));
    const lim = $("limit");
    if (lim) {
      lim.addEventListener("input", () => { $("limit-val").textContent = `${lim.value}%`; });
      $("limit-set").addEventListener("click", () => Cmd.run("set_charge_limit", { percent: Number(lim.value) }));
    }
    const amps = $("amps");
    if (amps) {
      amps.addEventListener("input", () => { $("amps-val").textContent = `${amps.value} A`; });
      $("amps-set").addEventListener("click", () => Cmd.run("set_charge_amps", { charging_amps: Number(amps.value) }));
    }
    const sched = $("sched");
    if (sched) {
      const setBtn = sched.parentElement.querySelector('[data-cmd="schedule_charge"]');
      if (setBtn) setBtn.replaceWith(Object.assign(document.createElement("button"), {
        className: "cbtn", textContent: t("controls.set"),
        onclick: () => { const [h, mi] = sched.value.split(":").map(Number); Cmd.run("schedule_charge", { time: h * 60 + mi }); },
      }));
    }
    const send = $("dest-send");
    if (send) send.addEventListener("click", () => {
      const v = $("dest").value.trim();
      if (v) Cmd.run("navigate_to", { address: v });
    });
  }

  async function refresh(force) {
    const carId = Car.current;
    const info = Car.info(carId);
    $("c-eyebrow").textContent = [Car.label(info), Car.variant(info)].filter(Boolean).join(" · ").toUpperCase();
    try { S = await apiGet(`/api/vehicle/state?car_id=${carId}${force ? "&refresh=1" : ""}`); } catch (e) { S = null; }
    const asleep = S && S.asleep;
    $("c-wake").hidden = !asleep;
    $("c-fresh").textContent = asleep ? t("controls.asleep") : S && S.fetched_at
      ? t("controls.updated", { ago: fmt.ago(Date.now() / 1000 - S.fetched_at) }) : "";
    if (asleep && S.last) S = { ...S.last, asleep: true };
    render();
  }

  async function boot() {
    await i18nReady;
    await Car.load(); Car.paint();
    caps = await Cmd.capabilities(Car.current, true);
    const banner = $("setup");
    banner.hidden = !!caps.ready || (Car.info() && Car.info().active === false);
    if (!banner.hidden) banner.querySelector(".txt").innerHTML =
      `<b>${esc(t("home.setup.title"))}</b>${esc(caps.reason === "not_paired" ? t("home.setup.pair") : t("controls.setup_body"))}`;
    await refresh(false);
    $("c-refresh").addEventListener("click", () => refresh(true));
    $("c-wake").addEventListener("click", async () => {
      toast(t("controls.waking"), "busy");
      await apiPost(`/api/vehicle/wake?car_id=${Car.current}`);
      setTimeout(() => refresh(true), 15000);
    });
    Car.onChange(async () => { caps = await Cmd.capabilities(Car.current, true); refresh(false); });
    Live.subscribe(() => { if (!S || S.asleep) render(); });
    if (location.hash) setTimeout(() => document.querySelector(location.hash)?.scrollIntoView({ behavior: "smooth" }), 300);
  }
  boot();
})();
