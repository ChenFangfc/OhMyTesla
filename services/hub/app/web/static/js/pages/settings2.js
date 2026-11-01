/* Settings v2: notification channels, vehicle-command setup, Sentry behaviour,
 * 3D models. Each section renders from its API and re-renders after a change,
 * so what you see is always what the hub will actually do.
 */
(function () {
  const $ = (id) => document.getElementById(id);
  const qrSvg = (text) => {
    if (!window.qrcode) return "";
    const q = qrcode(0, "M"); q.addData(text); q.make();
    return q.createSvgTag({ cellSize: 4, margin: 0, scalable: true });
  };
  function loadQr() {
    if (window.qrcode) return Promise.resolve();
    return new Promise((res) => {
      const s = document.createElement("script");
      s.src = "/static/vendor/qrcode/qrcode.js"; s.onload = res; s.onerror = res;
      document.head.append(s);
    });
  }
  async function send(method, url, body) {
    const r = await fetch(url, { method, headers: { "Content-Type": "application/json" },
                                 body: body === undefined ? undefined : JSON.stringify(body) });
    let j = {};
    try { j = await r.json(); } catch (e) { /* empty */ }
    if (!r.ok) throw new Error(j.error || j.detail || r.statusText);
    return j;
  }

  /* ------------------------------------------------------------ channels */
  const CH_FIELDS = {
    ntfy: [["url", "text", "https://ntfy.sh"], ["topic", "text", "omt-…"], ["token", "password", ""]],
    bark: [["server", "text", "https://api.day.app"], ["key", "password", ""], ["critical", "checkbox", ""]],
    telegram: [["token", "password", "123456:ABC…"], ["chat_id", "text", ""]],
    email: [["host", "text", "smtp.gmail.com"], ["port", "number", "587"], ["user", "text", ""], ["password", "password", ""],
            ["sender", "text", ""], ["to", "text", "you@example.com"]],
    webhook: [["url", "password", "https://…"]],
    apprise: [["urls", "password", "pover://… , discord://…"]],
  };

  async function renderChannels() {
    let d;
    try { d = await apiGet("/api/alerts/channels/config"); } catch (e) { return; }
    const host = $("channels");
    if (!host) return;
    const ready = d.channels.filter((c) => c.ready).map((c) => t("ch." + c.name));
    if (d.webpush_devices) ready.push(t("ch.webpush"));
    const v = $("v-push");
    if (v) v.textContent = ready.length ? ready.join(" · ") : t("ch.none");
    host.innerHTML = d.channels.map((c) => {
      const st = c.ready ? `<span class="st ok">${esc(t("ch.ready"))}</span>`
        : c.configured ? `<span class="st warn">${esc(t("ch.problem." + c.problem))}</span>`
        : `<span class="st">${esc(t("ch.not_set"))}</span>`;
      return `<div class="ch" data-ch="${c.name}">
        <div class="ch-head"><span class="nm">${esc(t("ch." + c.name))}</span>${st}
          ${c.source === "env" ? `<span class="st">· ${esc(t("ch.from_env"))}</span>` : ""}<span class="sp"></span>
          ${c.ready ? `<button class="btn ghost" data-test>${esc(t("ch.test"))}</button>` : ""}
          <button class="btn ghost" data-edit>${esc(c.configured ? t("ch.edit") : t("ch.setup"))}</button></div>
        <div class="body-sm" style="margin-top:4px">${esc(t("ch.desc." + c.name))}</div>
        <div class="ch-form" hidden></div></div>`;
    }).join("");
    host.querySelectorAll(".ch").forEach((row) => {
      const name = row.dataset.ch;
      const c = d.channels.find((x) => x.name === name);
      const tb = row.querySelector("[data-test]");
      if (tb) tb.addEventListener("click", async () => {
        tb.disabled = true; tb.textContent = t("ch.testing");
        try {
          const r = await send("POST", `/api/alerts/channels/${name}/test`);
          toast(r.ok ? (r.verified === false ? t("ch.sent_unverified") : r.verified ? t("ch.verified") : t("ch.sent"))
                     : t("ch.test_failed"), r.ok ? "ok" : "err");
        } catch (e) { toast(e.message, "err"); }
        tb.disabled = false; tb.textContent = t("ch.test");
      });
      row.querySelector("[data-edit]").addEventListener("click", () => openForm(row, name, c));
    });
  }

  async function openForm(row, name, c) {
    const box = row.querySelector(".ch-form");
    if (!box.hidden) { box.hidden = true; return; }
    box.hidden = false;
    const f = c.fields || {};
    box.innerHTML = `<div class="form-grid">${CH_FIELDS[name].map(([k, type, ph]) => {
        const set = f[k + "_set"];
        if (type === "checkbox") return `<label>${esc(t("ch.f." + k))}</label><label class="switch"><input type="checkbox" data-k="${k}" ${f[k] !== false ? "checked" : ""}><span class="slider"></span></label>`;
        return `<label>${esc(t("ch.f." + k))}</label><input type="${type}" data-k="${k}" value="${esc(type === "password" ? "" : (f[k] ?? ""))}"
          placeholder="${esc(set ? t("ch.unchanged") : ph)}" autocomplete="off">`;
      }).join("")}</div>
      ${name === "ntfy" ? `<div class="row mt"><button class="btn" data-newtopic>${esc(t("ch.ntfy_new"))}</button><span class="body-sm">${esc(t("ch.ntfy_new_hint"))}</span></div><div data-ntfyqr class="mt"></div>` : ""}
      <div class="row mt2"><button class="btn primary" data-save>${esc(t("common.save"))}</button>
        ${c.source === "settings" ? `<button class="btn ghost" data-reset>${esc(t("ch.reset"))}</button>` : ""}
        <span class="sp"></span><label class="check"><input type="checkbox" data-enabled ${c.enabled ? "checked" : ""}> ${esc(t("ch.enabled"))}</label></div>`;
    const ntfyQr = async (topic, base) => {
      await loadQr();
      const url = `${(base || "https://ntfy.sh").replace(/\/$/, "")}/${topic}`;
      box.querySelector("[data-ntfyqr]").innerHTML = `<div class="row" style="align-items:flex-start;gap:18px">
        <div class="qr">${qrSvg(url)}</div><div class="body-sm" style="max-width:40ch">${t("ch.ntfy_scan", { url: `<code>${esc(url)}</code>` })}</div></div>`;
    };
    if (name === "ntfy" && f.topic) ntfyQr(f.topic, f.url);
    const nt = box.querySelector("[data-newtopic]");
    if (nt) nt.addEventListener("click", async () => {
      const r = await send("POST", "/api/alerts/channels/ntfy/new-topic");
      box.querySelector('[data-k="topic"]').value = r.topic;
      ntfyQr(r.topic, box.querySelector('[data-k="url"]').value);
      renderChannels();
      toast(t("ch.ntfy_created"), "ok");
    });
    box.querySelector("[data-save]").addEventListener("click", async () => {
      const values = {};
      box.querySelectorAll("[data-k]").forEach((i) => { values[i.dataset.k] = i.type === "checkbox" ? i.checked : i.value; });
      values._disabled = !box.querySelector("[data-enabled]").checked;
      try { await send("PUT", `/api/alerts/channels/${name}`, { values }); toast(t("ch.saved"), "ok"); renderChannels(); }
      catch (e) { toast(e.message, "err"); }
    });
    const rs = box.querySelector("[data-reset]");
    if (rs) rs.addEventListener("click", async () => { await send("DELETE", `/api/alerts/channels/${name}`); renderChannels(); });
  }

  /* --------------------------------------------------------- fleet setup */
  async function renderFleet() {
    const host = $("fleet-steps");
    if (!host) return;
    let st;
    try { st = await apiGet("/api/tesla/fleet"); } catch (e) { return; }
    let cars = [];
    try { cars = await apiGet("/api/cars"); } catch (e) { /* none */ }
    const paired = st.paired || {};
    const anyPaired = cars.some((c) => paired[c.vin]);
    const v = $("v-commands");
    if (v) v.textContent = anyPaired ? t("fleet.sum_ready") : st.signed_in ? t("fleet.sum_pair") : st.configured ? t("fleet.sum_signin") : t("fleet.sum_off");
    const redirect = st.redirect_uri || `${location.origin}/fleet/callback`;
    const steps = [];

    steps.push([st.key, t("fleet.s1"), `<div class="hint">${t("fleet.s1_hint")}</div>
      <div class="row mt">${st.key ? `<span class="body-sm">${esc(t("fleet.fingerprint"))} <code>${esc(st.key_fingerprint)}</code></span>
        <a class="btn ghost" href="/api/tesla/fleet/public-key.pem" download>${esc(t("fleet.download_key"))}</a>`
        : `<button class="btn primary" data-act="key">${esc(t("fleet.gen_key"))}</button>`}</div>`]);

    steps.push([st.configured && st.has_secret, t("fleet.s2"), `<div class="hint">${t("fleet.s2_hint", { redirect: `<code>${esc(redirect)}</code>` })}</div>
      <div class="form-grid">
        <label>${esc(t("fleet.client_id"))}</label><input type="text" id="f-cid" value="${esc(st.client_id || "")}" autocomplete="off">
        <label>${esc(t("fleet.client_secret"))}</label><input type="password" id="f-sec" placeholder="${esc(st.has_secret ? t("ch.unchanged") : "")}" autocomplete="off">
        <label>${esc(t("fleet.domain"))}</label><input type="text" id="f-dom" value="${esc(st.domain || "")}" placeholder="yourname.github.io">
        <label>${esc(t("fleet.region"))}</label><select id="f-reg">${["na", "eu", "cn"].map((r) => `<option value="${r}" ${st.region === r ? "selected" : ""}>${esc(t("fleet.region_" + r))}</option>`).join("")}</select>
        <label>${esc(t("fleet.redirect"))}</label><input type="text" id="f-red" value="${esc(redirect)}">
      </div><div class="row mt"><button class="btn primary" data-act="save">${esc(common("save"))}</button></div>`]);

    steps.push([st.registered, t("fleet.s3"), `<div class="hint">${t("fleet.s3_hint", { url: `<code>${esc(st.public_key_url || "https://<domain>/.well-known/appspecific/com.tesla.3p.public-key.pem")}</code>` })}</div>
      <div class="row mt"><button class="btn" data-act="check">${esc(t("fleet.check_key"))}</button>
        <button class="btn primary" data-act="register" ${st.configured ? "" : "disabled"}>${esc(t("fleet.register"))}</button><span id="f-msg3" class="body-sm"></span></div>`]);

    steps.push([st.signed_in, t("fleet.s4"), `<div class="hint">${t("fleet.s4_hint")}</div>
      <div class="row mt"><button class="btn primary" data-act="login" ${st.configured ? "" : "disabled"}>${esc(t("fleet.signin"))}</button>
        ${st.signed_in ? `<span class="okmsg">${esc(t("fleet.signed_in"))}</span>` : ""}</div>
      <div class="row mt"><input type="text" id="f-paste" style="flex:1" placeholder="${esc(t("fleet.paste_ph"))}"><button class="btn" data-act="paste">${esc(t("fleet.paste_go"))}</button></div>`]);

    await loadQr();
    const pairRows = st.domain ? cars.filter((c) => c.vin).map((c) => {
      const link = `https://tesla.com/_ak/${st.domain}?vin=${c.vin}`;
      return `<div class="row" style="align-items:flex-start;gap:18px;margin-top:12px">
        <div class="qr">${qrSvg(link)}</div>
        <div><div style="font-weight:550">${esc(Car.label(c))} ${paired[c.vin] ? `<span class="okmsg">· ${esc(t("fleet.paired"))}</span>` : ""}</div>
        <div class="hint" style="margin-top:4px">${t("fleet.pair_scan")}</div>
        <a class="btn ghost mt" href="${esc(link)}" target="_blank" rel="noopener">${esc(t("fleet.pair_open"))}</a></div></div>`;
    }).join("") : "";
    steps.push([anyPaired, t("fleet.s5"), `<div class="hint">${t("fleet.s5_hint")}</div>${pairRows}
      <div class="row mt"><button class="btn" data-act="pairing" ${st.signed_in ? "" : "disabled"}>${esc(t("fleet.check_pair"))}</button>
      <button class="btn" data-act="test" ${anyPaired ? "" : "disabled"}>${esc(t("fleet.test_flash"))}</button><span id="f-msg5" class="body-sm"></span></div>`]);

    host.innerHTML = steps.map(([done, title, body]) => `<li class="${done ? "done" : ""}"><h4>${esc(title)}</h4>${body}</li>`).join("")
      + `<li><h4>${esc(t("fleet.s6"))}</h4><div class="hint">${t("fleet.s6_hint")}</div>
         <div class="row mt"><button class="btn ghost" data-act="reset">${esc(t("fleet.reset"))}</button></div></li>`;

    const act = (name, fn) => host.querySelectorAll(`[data-act="${name}"]`).forEach((b) => b.addEventListener("click", async () => {
      b.disabled = true;
      try { await fn(b); } catch (e) { toast(e.message, "err"); }
      b.disabled = false;
    }));
    act("key", async () => { await send("POST", "/api/tesla/fleet/key"); renderFleet(); });
    act("save", async () => {
      await send("POST", "/api/tesla/fleet/config", {
        client_id: $("f-cid").value, client_secret: $("f-sec").value || null, domain: $("f-dom").value,
        region: $("f-reg").value, redirect_uri: $("f-red").value });
      toast(t("ch.saved"), "ok"); renderFleet();
    });
    act("check", async () => {
      const r = await apiGet("/api/tesla/fleet/check-key");
      $("f-msg3").innerHTML = r.ok ? `<span class="okmsg">${esc(t("fleet.key_ok"))}</span>`
        : `<span class="errmsg">${esc(t("fleet.key_bad", { why: r.reason }))}</span>`;
    });
    act("register", async () => { await send("POST", "/api/tesla/fleet/register"); toast(t("fleet.registered"), "ok"); renderFleet(); });
    act("login", async () => { const r = await send("POST", "/api/tesla/fleet/login"); window.open(r.url, "_blank", "noopener"); });
    act("paste", async () => {
      await send("POST", "/api/tesla/fleet/callback", { code_or_url: $("f-paste").value });
      toast(t("fleet.signed_in"), "ok"); renderFleet();
    });
    act("pairing", async () => {
      const r = await send("POST", "/api/tesla/fleet/check-pairing");
      $("f-msg5").textContent = r.paired.length ? t("fleet.pair_found", { n: r.paired.length }) : t("fleet.pair_none");
      renderFleet();
    });
    act("test", async () => { await Cmd.run("flash_lights"); });
    act("reset", async () => {
      const ok = await Sheet.open({ title: t("fleet.reset_q"), lead: t("fleet.reset_lead"),
        actions: [{ label: t("common.cancel"), value: false }, { label: t("fleet.reset"), kind: "danger", value: true }] });
      if (ok) { await send("POST", "/api/tesla/fleet/reset"); renderFleet(); }
    });
  }
  const common = (k) => t("common." + k);

  /* --------------------------------------------------------------- sentry */
  async function renderSentry() {
    const host = $("sentry-body");
    if (!host) return;
    let d;
    try { d = await apiGet("/api/alerts/sentry/config"); } catch (e) { return; }
    const home = d.home;
    const pol = d.policies || {};
    const homePol = pol.__home__ || "escalated", awayPol = pol.__away__ || "always";
    const fp = d.fast_poll_seconds;
    const v = $("v-sentry");
    if (v) v.textContent = [home ? t("sentrycfg.sum_home") : t("sentrycfg.sum_nohome"),
                            fp ? t("sentrycfg.sum_fast", { n: fp }) : t("sentrycfg.sum_slow")].join(" · ");
    const seg = (name, cur, opts) => `<div class="seg2" data-seg="${name}">${opts.map(([val, lbl]) =>
      `<button data-v="${val}" class="${cur === val ? "on" : ""}">${esc(lbl)}</button>`).join("")}</div>`;
    const stats = Object.entries((d.fast_poll && d.fast_poll.cars) || {}).map(([cid, s]) =>
      `${esc(Car.label(Car.info(Number(cid))))}: ${s.last_ok ? t("sentrycfg.fast_ok", { ago: fmt.ago(Date.now() / 1000 - s.last_ok), ms: s.latency_ms ?? "–" }) : esc(s.last_error || "…")}`).join("<br>");
    host.innerHTML = `
      <p class="body-sm" style="margin:0 0 16px;max-width:66ch">${t("sentrycfg.intro")}</p>
      <div class="setting-row"><div class="info"><div class="name">${esc(t("sentrycfg.home"))}</div>
        <div class="desc">${home ? esc(t("sentrycfg.home_set", { lat: home.lat.toFixed(4), lon: home.lon.toFixed(4), r: Math.round(home.radius_m) })) : esc(t("sentrycfg.home_none"))}</div></div>
        <div class="row"><button class="btn" id="s-home">${esc(t("sentrycfg.home_here", { car: Car.label(Car.info()) }))}</button>
        ${home ? `<button class="btn ghost" id="s-home-clear">${esc(t("sentrycfg.clear"))}</button>` : ""}</div></div>
      <div class="setting-row"><div class="info"><div class="name">${esc(t("sentrycfg.at_home"))}</div><div class="desc">${esc(t("sentrycfg.at_home_desc"))}</div></div>
        ${seg("__home__", homePol, [["escalated", t("sentrycfg.p_alarm")], ["always", t("sentrycfg.p_all")], ["log", t("sentrycfg.p_log")]])}</div>
      <div class="setting-row"><div class="info"><div class="name">${esc(t("sentrycfg.away"))}</div><div class="desc">${esc(t("sentrycfg.away_desc"))}</div></div>
        ${seg("__away__", awayPol, [["always", t("sentrycfg.p_all")], ["escalated", t("sentrycfg.p_alarm")], ["log", t("sentrycfg.p_log")]])}</div>
      <div class="setting-row"><div class="info"><div class="name">${esc(t("sentrycfg.fast"))}</div><div class="desc">${t("sentrycfg.fast_desc")}${stats ? `<br>${stats}` : ""}</div></div>
        ${seg("fast", String(fp), [["0", t("controls.off")], ["5", "5 s"], ["10", "10 s"], ["20", "20 s"], ["30", "30 s"]])}</div>
      <p class="body-sm mt2" style="max-width:66ch">${t("sentrycfg.fleet_note")}</p>`;
    host.querySelectorAll(".seg2").forEach((g) => g.querySelectorAll("button").forEach((b) => b.addEventListener("click", async () => {
      const name = g.dataset.seg;
      try {
        if (name === "fast") await send("POST", "/api/alerts/sentry/fast-poll", { seconds: Number(b.dataset.v) });
        else await send("POST", "/api/alerts/sentry/policies", { geofence: name, policy: b.dataset.v });
        toast(t("ch.saved"), "ok"); renderSentry();
      } catch (e) { toast(e.message, "err"); }
    })));
    const hb = $("s-home");
    if (hb) hb.addEventListener("click", async () => {
      try { await send("POST", "/api/alerts/sentry/home", { from_car: String(Car.current), radius_m: 150 }); toast(t("ch.saved"), "ok"); renderSentry(); }
      catch (e) { toast(e.message, "err"); }
    });
    const hc = $("s-home-clear");
    if (hc) hc.addEventListener("click", async () => { await send("DELETE", "/api/alerts/sentry/home"); renderSentry(); });
  }

  /* --------------------------------------------------------------- models */
  async function renderModels() {
    const host = $("models");
    if (!host) return;
    let list;
    try { list = await apiGet("/api/vehicle/models"); } catch (e) { return; }
    host.innerHTML = list.map((m) => {
      const src = m.user ? t("models.yours", { rig: m.user.rigged ? t("models.rigged") : t("models.static") })
        : m.bundled ? t("models.bundled", { lic: m.bundled.license || "" }) : t("models.none");
      return `<div class="setting-row"><div class="info"><div class="name">${esc(m.name)}</div><div class="desc">${esc(src)}</div></div>
        <div class="row"><label class="btn ghost" style="cursor:pointer">${esc(t("models.upload"))}<input type="file" accept=".glb,model/gltf-binary" data-key="${m.key}" hidden></label>
        ${m.user ? `<button class="btn ghost" data-del="${m.key}">${esc(t("models.remove"))}</button>` : ""}</div></div>`;
    }).join("");
    host.querySelectorAll('input[type="file"]').forEach((inp) => inp.addEventListener("change", async () => {
      const f = inp.files[0];
      if (!f) return;
      const fd = new FormData(); fd.append("file", f);
      const busy = toast(t("models.uploading"), "busy", { ttl: 0 });
      const r = await fetch(`/api/vehicle/models/${inp.dataset.key}`, { method: "POST", body: fd });
      busy.remove();
      const j = await r.json().catch(() => ({}));
      if (r.ok) toast(t("models.installed", { parts: (j.rig || []).length }), "ok"); else toast(j.detail || t("models.failed"), "err");
      renderModels();
    }));
    host.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
      await send("DELETE", `/api/vehicle/models/${b.dataset.del}`); renderModels();
    }));
  }

  i18nReady.then(async () => {
    await Car.load();
    renderChannels(); renderFleet(); renderSentry(); renderModels();
    if (location.hash) {
      const el = document.querySelector(location.hash);
      if (el && el.tagName === "DETAILS") { el.open = true; setTimeout(() => el.scrollIntoView({ behavior: "smooth", block: "start" }), 200); }
    }
  });
})();
