/* 迷你 SVG 图表库：折线/散点 + (堆叠)柱状，自带图例与悬浮提示。
 * 规则: 单一 y 轴；≥2 个系列必有图例；网格线只画横向淡色；文字用墨色而非系列色。 */
(function () {
  const NS = "http://www.w3.org/2000/svg";
  const PAD = { l: 46, r: 12, t: 10, b: 26 };

  function el(tag, attrs) {
    const n = document.createElementNS(NS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    return n;
  }
  function css(name) {
    return getComputedStyle(document.body).getPropertyValue(name).trim() || name;
  }
  function niceTicks(min, max, n = 4) {
    if (min === max) { max = min + 1; }
    const span = max - min;
    const step0 = span / n;
    const mag = Math.pow(10, Math.floor(Math.log10(step0)));
    let step = mag;
    for (const m of [1, 2, 2.5, 5, 10]) if (step0 / mag <= m) { step = m * mag; break; }
    const lo = Math.floor(min / step) * step;
    const hi = Math.ceil(max / step) * step;
    const ticks = [];
    for (let v = lo; v <= hi + step / 1e6; v += step) ticks.push(+v.toFixed(10));
    return ticks;
  }
  const defFmt = (v) => {
    if (Math.abs(v) >= 10000) return (v / 1000).toFixed(0) + "k";
    return +v.toFixed(2) + "";
  };

  function makeTooltip(wrap) {
    const tt = document.createElement("div");
    tt.className = "tooltip";
    wrap.appendChild(tt);
    return {
      show(html, x, y) {
        tt.innerHTML = html;
        tt.style.display = "block";
        const w = wrap.clientWidth;
        const tw = tt.offsetWidth;
        tt.style.left = Math.min(Math.max(x + 12, 0), w - tw - 4) + "px";
        tt.style.top = Math.max(y - tt.offsetHeight - 10, 0) + "px";
      },
      hide() { tt.style.display = "none"; },
    };
  }

  function legend(wrap, series) {
    if (series.length < 2) return;
    const lg = document.createElement("div");
    lg.className = "legend";
    for (const s of series) {
      const item = document.createElement("span");
      item.className = "item";
      item.innerHTML = `<span class="swatch" style="background:${css(s.color)}"></span>${s.name}`;
      lg.appendChild(item);
    }
    wrap.appendChild(lg);
  }

  function frame(wrap, height) {
    wrap.classList.add("chart");
    wrap.innerHTML = "";
    const W = Math.max(wrap.clientWidth || 640, 320);
    const H = height || 240;
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H });
    svg.style.width = "100%";
    wrap.appendChild(svg);
    return { svg, W, H, iw: W - PAD.l - PAD.r, ih: H - PAD.t - PAD.b };
  }

  function drawYAxis(svg, ticks, sy, W, yFmt) {
    for (const t of ticks) {
      const y = sy(t);
      svg.appendChild(el("line", { x1: PAD.l, x2: W - PAD.r, y1: y, y2: y, stroke: css("--grid"), "stroke-width": 1 }));
      const lbl = el("text", { x: PAD.l - 8, y: y + 4, "text-anchor": "end", "font-size": 11, fill: css("--muted") });
      lbl.textContent = (yFmt || defFmt)(t);
      svg.appendChild(lbl);
    }
  }

  /* ---------------- 折线 / 散点 ---------------- */
  function line(wrap, opts) {
    const { svg, W, H, iw, ih } = frame(wrap, opts.height);
    const series = opts.series.filter((s) => s.data && s.data.length);
    if (!series.length) { wrap.innerHTML = `<div class="empty small">${typeof t === "function" ? t("common.no_data") : "No data yet"}</div>`; return; }

    let xs = [], ys = [];
    for (const s of series) for (const [x, y] of s.data) { if (y != null) { xs.push(x); ys.push(y); } }
    // 上面只按 data.length 过滤，一条全是 null 的序列（比如还没有能耗数据的
    // Wh/km）能通过。那样 xs/ys 为空 → Math.min(...[]) 得 Infinity → 所有坐标
    // 变成 NaN → 画出一张什么都没有的空 SVG，而不是"暂无数据"。
    if (!ys.length) { wrap.innerHTML = `<div class="empty small">${typeof t === "function" ? t("common.no_data") : "No data yet"}</div>`; return; }
    const xmin = Math.min(...xs), xmax = Math.max(...xs);
    let ymin = Math.min(...ys), ymax = Math.max(...ys);
    if (opts.zeroBase) ymin = Math.min(0, ymin);
    const yticks = niceTicks(ymin, ymax);
    ymin = yticks[0]; ymax = yticks[yticks.length - 1];

    const sx = (x) => PAD.l + (xmax === xmin ? 0.5 : (x - xmin) / (xmax - xmin)) * iw;
    const sy = (y) => PAD.t + (1 - (y - ymin) / (ymax - ymin)) * ih;

    drawYAxis(svg, yticks, sy, W, opts.yFmt);
    // x 轴刻度（首/中/尾）
    // 日期格式必须跟用户语言走。写死 zh-CN 的话英文界面上的图表
    // x 轴会出现 "8/15" 这种中式写法（localeTag() 在 util.js）。
    const xFmt = opts.xFmt || ((x) => opts.xTime
      ? new Date(x).toLocaleDateString(
          typeof localeTag === "function" ? localeTag() : undefined,
          { month: "numeric", day: "numeric" })
      : defFmt(x));
    for (const fx of [xmin, (xmin + xmax) / 2, xmax]) {
      const lbl = el("text", { x: sx(fx), y: H - 8, "text-anchor": "middle", "font-size": 11, fill: css("--muted") });
      lbl.textContent = xFmt(fx);
      svg.appendChild(lbl);
    }

    for (const s of series) {
      const color = css(s.color);
      const pts = s.data.filter((d) => d[1] != null);
      if (s.line !== false) {
        let d = "";
        for (let i = 0; i < pts.length; i++) d += (i ? "L" : "M") + sx(pts[i][0]).toFixed(1) + " " + sy(pts[i][1]).toFixed(1);
        svg.appendChild(el("path", { d, fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
      }
      if (s.points) {
        for (const [x, y] of pts) {
          svg.appendChild(el("circle", { cx: sx(x), cy: sy(y), r: 3, fill: color, stroke: css("--surface"), "stroke-width": 1.5 }));
        }
      }
    }

    // 悬浮: 十字线 + tooltip
    const tt = makeTooltip(wrap);
    const cross = el("line", { y1: PAD.t, y2: H - PAD.b, stroke: css("--baseline"), "stroke-width": 1, "stroke-dasharray": "3 3", visibility: "hidden" });
    svg.appendChild(cross);
    const dots = series.map((s) => {
      const c = el("circle", { r: 4, fill: css(s.color), stroke: css("--surface"), "stroke-width": 2, visibility: "hidden" });
      svg.appendChild(c);
      return c;
    });
    svg.addEventListener("mousemove", (e) => {
      const rect = svg.getBoundingClientRect();
      const px = ((e.clientX - rect.left) / rect.width) * W;
      const xv = xmin + ((px - PAD.l) / iw) * (xmax - xmin);
      let html = `<div class="tt-title">${xFmt(xv)}</div>`;
      let anyY = null;
      series.forEach((s, i) => {
        let best = null, bd = Infinity;
        for (const d of s.data) {
          if (d[1] == null) continue;
          const dist = Math.abs(d[0] - xv);
          if (dist < bd) { bd = dist; best = d; }
        }
        if (!best) { dots[i].setAttribute("visibility", "hidden"); return; }
        dots[i].setAttribute("cx", sx(best[0]));
        dots[i].setAttribute("cy", sy(best[1]));
        dots[i].setAttribute("visibility", "visible");
        anyY = sy(best[1]);
        html += `<div class="tt-row"><span class="dot" style="background:${css(s.color)}"></span>${s.name}<span class="v">${(opts.yFmt || defFmt)(best[1])}</span></div>`;
      });
      cross.setAttribute("x1", px); cross.setAttribute("x2", px);
      cross.setAttribute("visibility", "visible");
      tt.show(html, (px / W) * rect.width, anyY != null ? (anyY / H) * rect.height : 40);
    });
    svg.addEventListener("mouseleave", () => {
      tt.hide();
      cross.setAttribute("visibility", "hidden");
      dots.forEach((d) => d.setAttribute("visibility", "hidden"));
    });

    legend(wrap, series);
  }

  /* ---------------- (堆叠)柱状 ---------------- */
  function bars(wrap, opts) {
    const { svg, W, H, iw, ih } = frame(wrap, opts.height);
    const labels = opts.labels || [];
    const series = opts.series.filter((s) => s.values && s.values.length);
    if (!labels.length || !series.length) { wrap.innerHTML = `<div class="empty small">${typeof t === "function" ? t("common.no_data") : "No data yet"}</div>`; return; }

    const totals = labels.map((_, i) =>
      opts.stacked
        ? series.reduce((a, s) => a + (s.values[i] || 0), 0)
        : Math.max(...series.map((s) => s.values[i] || 0))
    );
    const yticks = niceTicks(0, Math.max(...totals, 1));
    const ymax = yticks[yticks.length - 1];
    const sy = (v) => PAD.t + (1 - v / ymax) * ih;
    drawYAxis(svg, yticks, sy, W, opts.yFmt);

    const n = labels.length;
    const slot = iw / n;
    const groupW = Math.min(slot * 0.62, 46);
    const tt = makeTooltip(wrap);

    labels.forEach((lab, i) => {
      const cx = PAD.l + slot * (i + 0.5);
      let acc = 0;
      const perBarW = opts.stacked ? groupW : groupW / series.length;
      series.forEach((s, si) => {
        const v = s.values[i] || 0;
        if (v <= 0) { acc += 0; return; }
        let x, y, h;
        if (opts.stacked) {
          x = cx - groupW / 2;
          y = sy(acc + v);
          h = sy(acc) - sy(acc + v) - (acc > 0 ? 2 : 0); /* 段间 2px 间隔 */
          acc += v;
        } else {
          x = cx - groupW / 2 + si * perBarW;
          y = sy(v);
          h = sy(0) - sy(v);
        }
        if (h <= 0) return;
        const r = el("rect", {
          x, y, width: opts.stacked ? groupW : perBarW - 2, height: h,
          rx: 2, fill: css(s.color),
        });
        r.addEventListener("mousemove", (e) => {
          const rect = wrap.getBoundingClientRect();
          let html = `<div class="tt-title">${lab}</div>`;
          for (const s2 of series) {
            const v2 = s2.values[i];
            if (v2 == null) continue;
            html += `<div class="tt-row"><span class="dot" style="background:${css(s2.color)}"></span>${s2.name}<span class="v">${(opts.yFmt || defFmt)(v2)}</span></div>`;
          }
          tt.show(html, e.clientX - rect.left, e.clientY - rect.top);
        });
        r.addEventListener("mouseleave", () => tt.hide());
        svg.appendChild(r);
      });
      if (n <= 18 || i % Math.ceil(n / 12) === 0) {
        const lbl = el("text", { x: cx, y: H - 8, "text-anchor": "middle", "font-size": 11, fill: css("--muted") });
        lbl.textContent = lab;
        svg.appendChild(lbl);
      }
    });

    svg.appendChild(el("line", { x1: PAD.l, x2: W - PAD.r, y1: sy(0), y2: sy(0), stroke: css("--baseline"), "stroke-width": 1 }));
    legend(wrap, series);
  }

  /* 每个容器只记最后一次的画法。以前是无脑 push，而 energy 页每次切时间段
     / 切车都会重跑 render()，于是同一个元素被登记 N 次 —— 窗口一改大小就把
     N 份陈旧的配置依次重画一遍。用 Map 按元素覆盖，天然去重。 */
  const registry = new Map();

  /* 折叠的 <details> 里 clientWidth 是 0，frame() 会退回 640 画完，
     再被 `width:100%` 拉伸到容器真实宽度（常常是 1200+），于是文字和
     线宽整体变形近两倍。窗口 resize 监听救不了这种情况 —— 展开 <details>
     不触发 resize。ResizeObserver 盯的是元素自己的尺寸，展开、
     侧栏收放、窗口缩放三种情况一并覆盖。 */
  let rt;
  const ro = typeof ResizeObserver !== "undefined"
    ? new ResizeObserver((entries) => {
        clearTimeout(rt);
        rt = setTimeout(() => {
          for (const e of entries) {
            const fn = registry.get(e.target);
            // 宽度为 0 说明还是不可见，画了也是白画，等下一次
            if (fn && e.target.clientWidth > 0) fn();
          }
        }, 120);
      })
    : null;

  function register(elm, draw) {
    const first = !registry.has(elm);
    registry.set(elm, draw);
    if (ro && first) ro.observe(elm);
    draw();
  }

  window.omtChart = {
    line(elm, opts) { register(elm, () => line(elm, opts)); },
    bars(elm, opts) { register(elm, () => bars(elm, opts)); },
  };

  // 没有 ResizeObserver 的老浏览器退回窗口监听，至少缩放能重画
  if (!ro) {
    window.addEventListener("resize", () => {
      clearTimeout(rt);
      rt = setTimeout(() => registry.forEach((fn) => fn()), 200);
    });
  }
})();
