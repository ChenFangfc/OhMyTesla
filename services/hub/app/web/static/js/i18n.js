/* 中英双语 + 单位系统。
 *
 * 两条原则：
 * 1. 语言和单位解耦——加拿大是英文 + 公里 + 摄氏度，英国是英文 + 英里 + 摄氏度。
 *    语言只决定"默认"单位，用户可逐项覆盖。
 * 2. 数据层永远是公制（TeslaMate 库里就是 km/°C/kW），换算只发生在显示的那一刻。
 *
 * 用法：
 *   静态文案  <span data-i18n="nav.overview"></span>   → 切换语言时自动重写
 *   动态文案  t("alerts.sessions", {n: 3})
 *   数值      U.dist(km) / U.temp(c) / U.speed(kmh) / U.press(bar)
 */
(function () {
  let DICT = { zh: {}, en: {} };

  const state = {
    locale: "zh",
    length: "km",
    temp: "C",
    pressure: "bar",
    currency: "USD",
    labels: {},
  };

  const missing = new Set();
  function t(key, vars) {
    const table = DICT[state.locale] || {};
    let s = table[key];
    if (s === undefined) {
      // 回退到英文，绝不回退到中文——中文混进英文界面看起来像"设计如此"，
      // 英语使用者根本分不出那是漏翻还是故意的，问题会一直藏着
      s = DICT.en[key];
      if (s === undefined) { s = key; }
      if (!missing.has(key)) { missing.add(key); console.warn("[i18n] missing key:", key); }
    }
    if (vars) for (const k in vars) s = s.split("{" + k + "}").join(vars[k]);
    return s;
  }

  /* 不用 toFixed 取整：它对 1.005 这类值有二进制误差，返回的又是字符串。
     这里按十进制位数四舍五入，返回数字；显示时再交给 Intl（fmt.num）。 */
  function round(v, d) {
    const n = Number(v);
    if (!isFinite(n)) return null;
    const k = Math.pow(10, d);
    return Math.round((n + Number.EPSILON * Math.sign(n)) * k) / k;
  }
  const show = (v, d) => (typeof fmt !== "undefined" ? fmt.num(v, d) : String(v));

  /* ---------- 单位换算：入参永远是公制 ---------- */
  const U = {
    // 距离：km → km|mi
    dist(km, digits) {
      if (km == null || km === "") return null;
      const d = digits == null ? 1 : digits;
      return round(state.length === "mi" ? km * 0.621371 : km, d);
    },
    distLabel() { return state.length === "mi" ? "mi" : "km"; },

    // 速度：km/h → km/h|mph
    speed(kmh, digits) { return U.dist(kmh, digits == null ? 0 : digits); },
    speedLabel() { return state.length === "mi" ? "mph" : "km/h"; },

    // 温度：°C → °C|°F
    temp(c, digits) {
      if (c == null || c === "") return null;
      const d = digits == null ? 1 : digits;
      return round(state.temp === "F" ? c * 9 / 5 + 32 : c, d);
    },
    tempLabel() { return state.temp === "F" ? "°F" : "°C"; },

    // 胎压：bar → bar|psi|kPa
    press(bar, digits) {
      if (bar == null || bar === "") return null;
      if (state.pressure === "psi") return round(bar * 14.5038, digits == null ? 1 : digits);
      if (state.pressure === "kpa") return Math.round(bar * 100);
      return round(bar, digits == null ? 2 : digits);
    },
    pressLabel() {
      return state.pressure === "psi" ? "psi" : (state.pressure === "kpa" ? "kPa" : "bar");
    },
    /* 胎压显示的固定小数位：bar 两位、psi 一位、kPa 整数 */
    pressDigits() { return state.pressure === "psi" ? 1 : state.pressure === "kpa" ? 0 : 2; },

    // 能耗：Wh/km → Wh/km|Wh/mi
    cons(whkm, digits) {
      if (whkm == null || whkm === "") return null;
      const d = digits == null ? 0 : digits;
      return round(state.length === "mi" ? whkm / 0.621371 : whkm, d);
    },
    consLabel() { return state.length === "mi" ? "Wh/mi" : "Wh/km"; },

    // 带单位的完整字符串（数字分组跟着语言走："44,731 mi"）
    distStr(km, digits) { const v = U.dist(km, digits); return v == null ? "–" : show(v, digits == null ? 1 : digits) + " " + U.distLabel(); },
    tempStr(c, digits) { const v = U.temp(c, digits); return v == null ? "–" : show(v, digits == null ? 1 : digits) + U.tempLabel(); },
    speedStr(kmh) { const v = U.speed(kmh); return v == null ? "–" : show(v, 0) + " " + U.speedLabel(); },
  };

  /* ---------- 应用到页面 ---------- */
  function apply() {
    document.documentElement.lang = state.locale === "en" ? "en" : "zh-CN";
    document.querySelectorAll("[data-i18n]").forEach((el) => {
      el.textContent = t(el.getAttribute("data-i18n"));
    });
    // 含 <b>/<code> 等标记的整段文案：写 innerHTML 而不是 textContent。
    // 字典是我们自己的静态文件，不含用户数据，所以这里没有注入风险。
    document.querySelectorAll("[data-i18n-html]").forEach((el) => {
      el.innerHTML = t(el.getAttribute("data-i18n-html"));
    });
    document.querySelectorAll("[data-i18n-ph]").forEach((el) => {
      el.setAttribute("placeholder", t(el.getAttribute("data-i18n-ph")));
    });
    document.querySelectorAll("[data-i18n-title]").forEach((el) => {
      el.setAttribute("title", t(el.getAttribute("data-i18n-title")));
    });
    // 只给读屏用的名字（没有可见文字的控件，如倍速下拉框）
    document.querySelectorAll("[data-i18n-aria]").forEach((el) => {
      el.setAttribute("aria-label", t(el.getAttribute("data-i18n-aria")));
    });
    window.dispatchEvent(new CustomEvent("omt:locale", { detail: { ...state } }));
  }

  async function loadCatalogs(locale) {
    const want = ["en"];                      // 英文永远加载，作为回退
    if (locale && locale !== "en") want.push(locale);
    await Promise.all(want.map(async (l) => {
      if (Object.keys(DICT[l] || {}).length) return;
      try { DICT[l] = await (await fetch(`/api/prefs/catalog/${l}`)).json(); }
      catch (e) { DICT[l] = DICT[l] || {}; }
    }));
  }

  async function load() {
    try {
      const p = await (await fetch("/api/prefs")).json();
      // 首次访问且后端没存过语言时，按浏览器语言猜一次（只猜语言，不碰单位）
      if (!p.locale_set && !localStorage.getItem("omt_lang_asked")) {
        localStorage.setItem("omt_lang_asked", "1");
        const browserZh = (navigator.language || "").toLowerCase().startsWith("zh");
        if (!browserZh && p.locale === "zh") {
          try { await fetch("/api/prefs", { method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ locale: "en" }) }); p.locale = "en"; } catch (e) {}
        }
      }
      Object.assign(state, {
        locale: p.locale || "zh", length: p.length || "km",
        temp: p.temp || "C", pressure: p.pressure || "bar",
        currency: p.currency || "USD", labels: p.labels || {},
      });
    } catch (e) { /* 后端不可用时用默认值 */ }
    await loadCatalogs(state.locale);
    apply();
    return { ...state };
  }

  async function save(patch) {
    const r = await fetch("/api/prefs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    const p = await r.json();
    Object.assign(state, {
      locale: p.locale, length: p.length, temp: p.temp, pressure: p.pressure,
      currency: p.currency || state.currency,
    });
    await loadCatalogs(state.locale);
    apply();
    return p;
  }

  /* 有没有这个键（不记缺失、不告警）：给"有专门文案就用，没有就用通用模板"的地方。 */
  function has(key) {
    return (DICT[state.locale] || {})[key] !== undefined || DICT.en[key] !== undefined;
  }

  window.i18n = { t, U, state, apply, load, save, has, DICT };
  window.t = t;
  window.U = U;
})();
