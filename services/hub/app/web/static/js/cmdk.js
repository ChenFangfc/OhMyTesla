/* Ask your Tesla — the signature interaction (§53).
 *
 * Raycast/Linear, not ChatGPT: you type, you get ranked things you can DO,
 * you hit Enter. Every entry resolves to a navigation, a vehicle command, or
 * an answer computed from data we already hold — no round trip to a model.
 *
 * When an LLM is configured the same input additionally accepts open
 * questions; without one we never show a prompt we can't honour.
 */
const cmdk = (() => {
  let el, input, list, items = [], sel = 0, ctx = {}, llm = false;

  /* ------------------------------------------------------------ registry --
     `run` returns undefined to close, or a node to render as an answer. */

  const NAV = [
    { key: "nav.overview", href: "/",             words: "overview home car vehicle 首页 总览" },
    { key: "nav.controls", href: "/controls",     words: "controls commands climate charge 控制 车辆控制 空调 充电" },
    { key: "data.nav.overview",  href: "/data",           words: "data stats statistics analytics overview records 数据 统计 概览 纪录" },
    { key: "data.nav.driving",   href: "/data/driving",   words: "driving trips drives routes efficiency speed 行车 行程 路线 能耗 车速" },
    { key: "data.nav.charging",  href: "/data/charging",  words: "charging cost kwh tariff supercharger 充电 电费 超充" },
    { key: "data.nav.battery",   href: "/data/battery",   words: "battery health degradation drain range 电池 健康 衰减 掉电 续航" },
    { key: "data.nav.footprint", href: "/data/footprint", words: "footprint map places visited cities states 足迹 地图 城市 去过" },
    { key: "data.nav.timeline",  href: "/data/timeline",  words: "timeline history calendar activity 时间线 历史 日历" },
    { key: "nav.dashcam",  href: "/dashcam",      words: "dashcam clips footage video 录像 行车记录仪" },
    { key: "cmdk.nav_usb", href: "/dashcam/usb",  words: "usb drive import stick u盘 导入" },
    { key: "nav.security", href: "/security",       words: "security sentry alerts notifications 哨兵 告警 安全" },
    { key: "nav.settings", href: "/settings",     words: "settings preferences config 设置 偏好" },
  ];

  /* Vehicle commands. `risky` ones get a confirm step (§40). */
  const CMD = [
    { id: "door_lock",    key: "cmd.door_lock",    ico: "lock", words: "lock secure 锁车 上锁" },
    { id: "door_unlock",  key: "cmd.door_unlock",  ico: "unlock", words: "unlock open 解锁", risky: true },
    { id: "climate_on",   key: "cmd.climate_on",   ico: "climate",  words: "climate ac heat precondition 空调 预热 制冷" },
    { id: "climate_off",  key: "cmd.climate_off",  ico: "climate",  words: "climate off stop ac 关空调" },
    { id: "charge_start", key: "cmd.charge_start", ico: "bolt", words: "charge start begin 充电 开始充电" },
    { id: "charge_stop",  key: "cmd.charge_stop",  ico: "bolt",  words: "charge stop halt 停止充电", risky: true },
    { id: "charge_port_open", key: "cmd.charge_port_open", ico: "plug", words: "charge port open 充电口" },
    { id: "sentry_on",    key: "cmd.sentry_on",    ico: "shield",  words: "sentry guard security on 哨兵 开启哨兵" },
    { id: "sentry_off",   key: "cmd.sentry_off",   ico: "shield",  words: "sentry off disable 关闭哨兵", risky: true },
    { id: "flash_lights", key: "cmd.flash_lights", ico: "lightbulb", words: "flash lights find 闪灯 找车" },
    { id: "honk_horn",    key: "cmd.honk_horn",    ico: "horn", words: "honk horn 鸣笛 喇叭" },
    { id: "frunk",        key: "cmd.frunk",        ico: "box", words: "frunk front trunk 前备箱", risky: true },
    { id: "trunk",        key: "cmd.trunk",        ico: "box", words: "trunk rear boot 后备箱", risky: true },
    { id: "vent_windows", key: "cmd.vent_windows", ico: "window", words: "vent windows air 通风 车窗", risky: true },
    { id: "close_windows", key: "cmd.close_windows", ico: "window", words: "close windows shut 关车窗" },
    { id: "charge_port_close", key: "cmd.charge_port_close", ico: "plug", words: "charge port close 关充电口" },
    // 带参指令：服务端 catalog 里一直有，但过去没有任何界面能传值，
    // 选中就只能收到一个 502。数字直接打在命令行里，例如 "limit 80"、"temp 22"。
    { id: "set_charge_limit", key: "cmd.set_charge_limit", ico: "bolt",
      words: "charge limit percent soc 充电上限 百分比", param: "percent", unit: "%" },
    // 单位跟用户偏好走；后端一律收摄氏，华氏在发送前换算（见 send()）
    { id: "set_temps", key: "cmd.set_temps", ico: "climate",
      words: "temp temperature climate set 空调温度 设定温度", param: "driver_temp",
      get unit() { return typeof U !== "undefined" && U.tempLabel ? U.tempLabel() : "°C"; },
      fahrenheit: true },
  ];

  /* Questions we can answer exactly, from data already on the client. */
  const ASK = [
    {
      key: "cmdk.q_range", words: "range how far reach distance remaining 续航 还能开 多远",
      run: () => {
        const km = ctx.range_km, soc = ctx.soc;
        if (km == null) return msg(t("cmdk.a_no_data"));
        return answer(t("cmdk.q_range"), [
          [t("common.range"), U.dist(km, 0) + " " + U.distLabel()],
          [t("common.battery"), soc != null ? Math.round(soc) + "%" : "–"],
        ]);
      },
    },
    {
      key: "cmdk.q_charge_cost", words: "cost charging price money spent 费用 充电花了 多少钱",
      run: () => {
        const c = ctx.charging;
        if (!c || c.active || c.cost == null) return msg(t("cmdk.a_no_charge"));
        return answer(t("cmdk.q_charge_cost"), [
          [t("cmdk.last_session"), fmt.num(c.added_kwh, 1) + " kWh"],
          [t("common.cost"), fmt.money(c.cost)],
          [t("common.location"), c.place || "–"],
        ]);
      },
    },
    {
      key: "cmdk.q_today", words: "today driven energy used efficiency 今天 开了 用了多少电",
      run: () => {
        const d = ctx.today;
        if (!d || !d.drives) return msg(t("cmdk.a_no_drives"));
        const rows = [
          [t("cmdk.distance"), U.dist(d.km, 1) + " " + U.distLabel()],
          [t("cmdk.energy"), fmt.num(d.kwh, 1) + " kWh"],
        ];
        if (d.wh_per_km) rows.push([t("cmdk.efficiency"), U.cons(d.wh_per_km) + " " + U.consLabel()]);
        return answer(t("cmdk.q_today"), rows);
      },
    },
    {
      key: "cmdk.q_health", words: "health tire pressure tpms problem unusual 健康 胎压 异常 问题",
      run: () => {
        const h = ctx.health || { ok: true, issues: [] };
        if (h.ok) return msg(t("cmdk.a_healthy"));
        return answer(t("cmdk.q_health"), h.issues.map((i) =>
          i.kind === "update"
            ? [t("overview.update_available"), i.version || ""]
            : [t("tire." + i.corner), i.value_bar != null
                ? U.press(i.value_bar) + " " + U.pressLabel() +
                  " · " + t("cmdk.expected") + " " + U.press(i.expected_bar)
                : t("cmdk.check_pressure")]));
      },
    },
    {
      key: "cmdk.q_where", words: "where is my car located parked location 车在哪 位置 停在",
      run: () => {
        if (!ctx.place && ctx.latitude == null) return msg(t("cmdk.a_no_data"));
        const rows = [[t("common.location"), ctx.place || fmt.fixed(+ctx.latitude, 4) + ", " + fmt.fixed(+ctx.longitude, 4)]];
        if (ctx.stale_seconds) rows.push([t("cmdk.as_of"), fmt.ago(ctx.stale_seconds)]);
        return answer(t("cmdk.q_where"), rows);
      },
    },
  ];

  /* ------------------------------------------------------------- render -- */

  function answer(title, rows) {
    const d = document.createElement("div");
    d.className = "cmdk-answer";
    d.setAttribute("role", "status");
    d.innerHTML = `<div class="cmdk-q">${esc(title)}</div>` +
      rows.map(([k, v]) =>
        `<div class="stat-line" style="padding:9px 0">
           <span class="label">${esc(k)}</span><span class="spacer"></span>
           <span class="stat-value num">${esc(v)}</span></div>`).join("");
    return d;
  }

  function msg(text) {
    const d = document.createElement("div");
    d.className = "cmdk-answer";
    d.setAttribute("role", "status");
    d.innerHTML = `<div class="body-sm">${esc(text)}</div>`;
    return d;
  }

  /* Subsequence match, so "chst" finds "charge stop" — the Raycast feel. */
  function score(q, hay) {
    if (!q) return 0;
    hay = hay.toLowerCase();
    if (hay.includes(q)) return 100 - hay.indexOf(q);
    let i = 0;
    for (const ch of q) {
      i = hay.indexOf(ch, i);
      if (i < 0) return -1;
      i++;
    }
    return 20;
  }

  function build(q) {
    q = q.trim().toLowerCase();
    const out = [];
    const push = (group, entry, s) => out.push({ ...entry, group, _s: s });

    for (const a of ASK) {
      const s = Math.max(score(q, t(a.key)), score(q, a.words));
      if (!q || s >= 0) push("cmdk.g_ask", { label: t(a.key), ico: "spark", run: a.run }, s);
    }
    // 带参指令直接从输入里取数字："limit 80" / "温度 22"
    const typed = (q.match(/\d+(?:\.\d+)?/) || [])[0];
    for (const c of CMD) {
      // 数字本身不该参与匹配打分，否则 "limit 80" 会被 80 拖低分
      const qWords = c.param ? q.replace(/\d+(?:\.\d+)?/g, " ").trim() : q;
      const s = Math.max(score(qWords, t(c.key)), score(qWords, c.words));
      if (!(!q || s >= 0)) continue;
      if (!c.param) {
        push("cmdk.g_control", { label: t(c.key), ico: c.ico, cmd: c }, s);
        continue;
      }
      const has = typed !== undefined;
      push("cmdk.g_control", {
        label: t(c.key) + (has ? ` → ${typed}${c.unit}` : ` … ${c.unit}`),
        ico: c.ico,
        cmd: has ? { ...c, value: +typed } : c,
      }, s);
    }
    for (const n of NAV) {
      const s = Math.max(score(q, t(n.key)), score(q, n.words));
      if (!q || s >= 0) push("cmdk.g_go", { label: t(n.key), ico: "arrow", href: n.href }, s);
    }

    out.sort((a, b) => b._s - a._s);
    // With no query, lead with actions rather than a wall of everything.
    return q ? out.slice(0, 12) : out.filter(x => x.group !== "cmdk.g_go").slice(0, 7);
  }

  function paint(q) {
    items = build(q);
    sel = 0;
    if (!items.length) {
      list.innerHTML = `<div class="cmdk-empty">${esc(
        llm ? t("cmdk.empty_llm") : t("cmdk.empty"))}</div>`;
      return;
    }
    let html = "", group = null;
    items.forEach((it, i) => {
      if (it.group !== group) {
        group = it.group;
        html += `<div class="cmdk-group" role="presentation">${esc(t(group))}</div>`;
      }
      html += `<div class="cmdk-item${it.cmd?.risky ? " risky" : ""}" role="option" id="cmdk-i-${i}"
                    aria-selected="${i === 0}" data-i="${i}">
                 <span class="ico">${icon(it.ico, 16)}</span><span>${esc(it.label)}</span>
                 ${it.cmd?.risky ? `<span class="hint">${esc(t("cmdk.confirm_hint"))}</span>` : ""}
               </div>`;
    });
    list.innerHTML = html;
    input.setAttribute("aria-activedescendant", "cmdk-i-0");
    list.querySelectorAll(".cmdk-item").forEach((n) => {
      n.addEventListener("mouseenter", () => move(+n.dataset.i));
      n.addEventListener("click", () => choose());
    });
  }

  function move(i) {
    sel = Math.max(0, Math.min(items.length - 1, i));
    list.querySelectorAll(".cmdk-item").forEach((n, k) =>
      n.setAttribute("aria-selected", k === sel));
    list.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
    input.setAttribute("aria-activedescendant", "cmdk-i-" + sel);
  }

  async function choose() {
    const it = items[sel];
    if (!it) return;
    if (it.href) { location.href = it.href; return; }
    if (it.run) {
      const node = it.run();
      list.innerHTML = "";
      list.appendChild(node);
      items = [];
      return;
    }
    if (it.cmd) await send(it.cmd);
  }

  async function send(c, forceConfirm) {
    // Risky commands confirm in place — no separate dialog to dismiss.
    // forceConfirm: 服务端回了 428，说明它认为这条比客户端列表更危险 ——
    // 以服务端为准补一次确认，而不是把 428 当错误甩给用户。
    if ((c.risky || forceConfirm) && !c._confirmed) {
      list.innerHTML = "";
      const d = document.createElement("div");
      d.className = "cmdk-answer";
      d.innerHTML = `<div class="title" style="margin-bottom:4px">${esc(t("cmd.confirm.title", { what: t(c.key) }))}</div>
        <div class="body-sm">${esc(ctx.name || t("overview.unnamed_vehicle", { id: ctx.car_id || "" }))}${
          ctx.place ? " · " + esc(ctx.place) : ""}</div>
        <div class="row gap-2" style="margin-top:16px">
          <button type="button" class="btn" id="ck-no">${esc(t("common.cancel"))}</button>
          <button type="button" class="btn accent" id="ck-yes">${esc(t(c.key))}</button>
        </div>`;
      list.appendChild(d);
      items = [];
      d.querySelector("#ck-no").onclick = () => { input.value = ""; paint(""); input.focus(); };
      d.querySelector("#ck-yes").onclick = () => send({ ...c, _confirmed: true });
      d.querySelector("#ck-yes").focus();
      return;
    }
    // 带参指令没给数字就别发出去——服务端会拒，用户只会看到一句报错
    if (c.param && c.value == null) {
      list.innerHTML = "";
      list.appendChild(msg(t("cmdk.needs_value", { unit: c.unit })));
      return;
    }
    list.innerHTML = `<div class="cmdk-empty" role="status">${esc(i18n.has("cmd.ing." + c.id) ? t("cmd.ing." + c.id) : t("cmdk.sending"))}</div>`;
    try {
      // 只有真的走过确认框才带 confirm。以前无条件传 true，服务端那道
      // 428「危险操作需二次确认」的闸门就永远触发不了 —— 等于把解锁车门、
      // 开后备箱、关哨兵的最后一道保险交给了前端一个布尔量。
      // 也因此，服务端和客户端对 risky 的判断谁更严都不再要紧：以服务端为准。
      await apiPost("/api/tesla/command", {
        car_id: ctx.car_id || 1, command: c.id, confirm: !!c._confirmed,
        ...(c.param ? { params: { [c.param]:
            // 车接受的是摄氏。用户界面显示华氏时，这里换算回去 ——
            // 不换的话输入 72 会被当成 72°C 发出去。
            (c.fahrenheit && typeof U !== "undefined" && U.tempLabel && U.tempLabel() === "°F")
              ? Math.round((c.value - 32) * 5 / 9 * 10) / 10
              : c.value } } : {}),
      });
      list.innerHTML = "";
      list.appendChild(msg(t("cmdk.sent", { command: t(c.key) })));
      setTimeout(close, 1400);
    } catch (e) {
      if (e.status === 428 && !c._confirmed) return send(c, true);
      list.innerHTML = "";
      list.appendChild(msg(e.message));
    }
  }

  /* --------------------------------------------------------------- open -- */

  function open() {
    if (el) return;
    el = document.createElement("div");
    el.className = "cmdk-scrim";
    el.innerHTML = `<div class="cmdk" role="dialog" aria-modal="true" aria-label="${esc(t("cmdk.title"))}">
        <input type="text" autocomplete="off" spellcheck="false" role="combobox" aria-expanded="true"
               aria-controls="cmdk-list" aria-autocomplete="list" aria-label="${esc(t("cmdk.title"))}">
        <div class="cmdk-list" id="cmdk-list" role="listbox"></div>
      </div>`;
    document.body.appendChild(el);
    input = el.querySelector("input");
    list = el.querySelector(".cmdk-list");
    input.placeholder = llm ? t("cmdk.placeholder_llm") : t("cmdk.placeholder");

    el.addEventListener("click", (e) => { if (e.target === el) close(); });
    input.addEventListener("input", () => paint(input.value));
    input.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") { e.preventDefault(); move(sel + 1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); move(sel - 1); }
      else if (e.key === "Enter") { e.preventDefault(); choose(); }
      else if (e.key === "Escape") { e.preventDefault(); close(); }
    });
    paint("");
    input.focus();
  }

  function close() { el?.remove(); el = null; items = []; }

  function init() {
    document.addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "/")) {
        e.preventDefault();
        el ? close() : open();
      }
    });
    document.getElementById("cmdk-open")?.addEventListener("click", open);
    document.querySelectorAll("[data-cmdk-open]").forEach((n) =>
      n.addEventListener("click", open));
  }

  return {
    init, open, close,
    /* The overview page feeds us the same snapshot it renders, so answers
       never disagree with what's on screen. */
    setContext(c) { ctx = c || {}; },
    setLLM(on) { llm = !!on; },
  };
})();
