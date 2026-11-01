/* 公共工具 */
async function apiGet(url) {
  const r = await fetch(url);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (e) { /* ignore */ }
    const err = new Error(msg);
    err.status = r.status;      // 调用方需要区分 428(待确认) / 429(太快) / 503
    throw err;
  }
  return r.json();
}
async function apiPost(url, body, method = "POST") {
  const r = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (e) { /* ignore */ }
    const err = new Error(msg);
    err.status = r.status;      // 调用方需要区分 428(待确认) / 429(太快) / 503
    throw err;
  }
  return r.json();
}

/* HTML 转义——所有来自数据的字符串进 innerHTML 前必须过这里
   （地理围栏名/OSM 地名/车名/event.json 字段都可能含特殊字符） */
/* 推送文案里的前导 emoji 在通知栏有用，在单色界面里没有。
   目录保持原值不动，界面渲染时剥掉。 */
const LEAD_EMOJI = /^[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{FE0F}\s]+/u;
function plain(text) { return String(text || "").replace(LEAD_EMOJI, "").trim(); }

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

const DATE_ONLY = /^(\d{4})-(\d{2})-(\d{2})$/;
function localeTag() {
  return (window.i18n && i18n.state.locale === "en") ? "en-US" : "zh-CN";
}

/* 数字一律走 Intl.NumberFormat，不用 toFixed：
   toFixed 不分组（44731 而不是 44,731）、不认语言，而且四舍五入有二进制误差
   （1.005.toFixed(2) === "1.00"）。格式器按 语言+选项 缓存，热路径上不反复构造。 */
const _nf = new Map();
function numberFormat(opts) {
  const key = localeTag() + JSON.stringify(opts);
  let f = _nf.get(key);
  if (!f) { f = new Intl.NumberFormat(localeTag(), opts); _nf.set(key, f); }
  return f;
}
const DASH = "–";
const fmt = {
  /* 最多 d 位小数（默认 1）："51" / "3.5" / "44,731.3" */
  num(v, d = 1) {
    if (v == null || v === "" || !isFinite(Number(v))) return DASH;
    return numberFormat({ maximumFractionDigits: d }).format(Number(v));
  },
  /* 固定 d 位小数：列里对齐用，"3.50" */
  fixed(v, d = 1) {
    if (v == null || v === "" || !isFinite(Number(v))) return DASH;
    return numberFormat({ minimumFractionDigits: d, maximumFractionDigits: d }).format(Number(v));
  },
  int(v) { return fmt.num(v, 0); },
  /* 百分比：输入是 0–100 的数，不是 0–1 */
  pct(v, d = 0) {
    if (v == null || v === "" || !isFinite(Number(v))) return DASH;
    return numberFormat({ style: "percent", maximumFractionDigits: d }).format(Number(v) / 100);
  },
  dt(iso) {
    if (!iso) return DASH;
    return new Date(iso).toLocaleString(localeTag(), { year: "numeric", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
  },
  /* 只要时刻："14:02" */
  time(ms) {
    if (ms == null || !isFinite(ms)) return DASH;
    return new Date(ms).toLocaleTimeString(localeTag(), { hour: "2-digit", minute: "2-digit" });
  },
  // 纯日期串按字面解析——new Date("2026-08-01") 会当 UTC 解析，西半球时区显示会差一天
  date(iso) {
    if (!iso) return DASH;
    const m = DATE_ONLY.exec(iso);
    if (m) return `${m[1]}/${+m[2]}/${+m[3]}`;
    return new Date(iso).toLocaleDateString(localeTag());
  },
  ts(sec) {
    const s = Math.max(0, Math.floor(sec));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  },
  /* 时长："2 小时 10 分" / "2 h 10 min" */
  dur(sec) {
    const m = Math.max(0, Math.round(sec / 60));
    if (m < 60) return t("time.min", { n: m });
    const h = Math.floor(m / 60), r = m % 60;
    if (h < 48) return r ? t("time.h_min", { h, m: r }) : t("time.h", { n: h });
    return t("time.days", { n: Math.round(h / 24) });
  },
  /* 货币跟着地区走。各页面之前各拼各的字符串，还有硬编码 CNY 的。 */
  money(v, digits) {
    if (v == null) return DASH;
    const cur = (window.i18n && i18n.state && i18n.state.currency) || "USD";
    const d = digits == null ? 2 : digits;
    try {
      return numberFormat({ style: "currency", currency: cur, minimumFractionDigits: d, maximumFractionDigits: d }).format(v);
    } catch (e) {
      return fmt.fixed(v, d) + " " + cur;
    }
  },
  /* "2 hours ago" / "2 小时前"。用 Intl.RelativeTimeFormat，两种语言的
     语序、单复数都由浏览器处理，不需要我们自己拼。 */
  ago(sec) {
    const s = Math.max(0, Math.floor(sec));
    const rtf = new Intl.RelativeTimeFormat(localeTag(), { numeric: "auto" });
    for (const [unit, size] of [["day", 86400], ["hour", 3600], ["minute", 60]]) {
      if (s >= size) return rtf.format(-Math.round(s / size), unit);
    }
    return rtf.format(-s, "second");
  },
  /* 之前输出 "26/8"，两种语言下都不像话。交给 Intl 处理月份写法。 */
  month(iso) {
    const m = DATE_ONLY.exec(iso || "");
    const d = m ? new Date(+m[1], +m[2] - 1, 1) : new Date(iso);
    if (isNaN(d)) return DASH;
    const sameYear = d.getFullYear() === new Date().getFullYear();
    return d.toLocaleDateString(localeTag(),
      sameYear ? { month: "short" } : { month: "short", year: "numeric" });
  },
};

const STATE_CLS = {
  online: "good", asleep: "plain", offline: "serious", driving: "good",
  charging: "good", suspended: "plain", updating: "warn", start: "plain",
};
function stateChip(state) {
  const cls = STATE_CLS[state] || "plain";
  const key = "state." + state;
  const txt = (typeof t === "function" && t(key) !== key) ? t(key) : (state || "—");
  return `<span class="chip ${cls}">${esc(txt)}</span>`;
}

/* SSE 实时状态: onUpdate(cars, msg) 在快照与每次状态变化时回调；
   带 type 的消息（{"type": "alert"}）是事件不是状态，交给 onEvent(msg)。 */
function liveConnect(onUpdate, onEvent) {
  const cars = {};
  /* ?nolive=1 跳过 SSE。长连接会让无头浏览器永远等不到 load 事件，
     没有它就只能对着打桩数据截图，而打桩数据会骗人。 */
  if (new URLSearchParams(location.search).has("nolive")) return cars;
  const es = new EventSource("/api/live/stream");
  es.onmessage = (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch (err) { return; }
    if (msg.type === "snapshot") {
      Object.assign(cars, msg.cars);
    } else if (msg.type) {
      if (onEvent) onEvent(msg);
      return;
    } else if (msg.car_id) {
      (cars[msg.car_id] = cars[msg.car_id] || {})[msg.key] = msg.value;
    }
    onUpdate(cars, msg);
  };
  es.onerror = () => { /* EventSource 自动重连 */ };
  return cars;
}

/* 车辆选择器（多车时显示） */
async function initCarPicker(sel, onChange) {
  // Inside the app shell the top bar already chooses the car; follow it
  // instead of showing a second picker.
  if (typeof Car !== "undefined" && document.querySelector("[data-carswitch]")) {
    const own = document.querySelector(sel);
    if (own) own.hidden = true;
    if (!Car.list.length) await Car.load();
    Car.onChange((id) => onChange(Number(id)));
    return Car.current ?? 1;
  }
  let cars = [];
  try { cars = await apiGet("/api/cars"); } catch (e) { /* DB 未就绪 */ }
  const el = document.querySelector(sel);
  if (!el) return cars[0] ? cars[0].id : 1;
  if (cars.length <= 1) { el.style.display = "none"; return cars[0] ? cars[0].id : 1; }
  // energy/trips 的 <select> 标签上写了 hidden，只改 style.display 是盖不住
  // 浏览器默认的 [hidden]{display:none} 的 —— 多车账号会一个选择器都看不见。
  el.removeAttribute("hidden");
  el.style.display = "";
  el.innerHTML = cars.map((c) => `<option value="${c.id}">${esc(c.name || c.model || "Car " + c.id)}</option>`).join("");
  el.addEventListener("change", () => onChange(Number(el.value)));
  return Number(el.value);
}
