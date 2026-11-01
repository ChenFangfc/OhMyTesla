/* Settings → Dashcam: the library folder and its time zone, and teslausb.
 *
 * The Pi never gets the portal password: it calls /teslausb/notify with its
 * own token, which can only ask for a rescan. The token lives here, can be
 * copied straight into teslausb_setup_variables.conf, and can be replaced.
 */
(function () {
  const $ = (id) => document.getElementById(id);
  if (!$("dashcam")) return;

  let settings = null;       // GET /teslausb/settings
  let status = null;         // GET /teslausb/status
  let watcher = null;        // GET /watcher
  let library = null;        // GET /dashcam/status

  const ago = (epochS) => (epochS ? fmt.ago(Date.now() / 1000 - epochS) : "");
  const tn = (key, n, vars) => t(n === 1 ? key + "_one" : key, { n, ...vars });     // "1 file", "2 files"
  const code = (s) => `<code>${esc(s)}</code>`;
  /* The hub reports Pi problems in English; translate the ones it knows. */
  const PI_ERRORS = [
    [/^timed out talking to the Pi/i, "sdc.pi.err_timeout"],
    [/^could not connect to the Pi/i, "sdc.pi.err_connect"],
    [/asked for a password/i, "sdc.pi.err_auth"],
    [/did not return JSON/i, "sdc.pi.err_json"],
  ];
  function piError(msg) {
    const m = String(msg || "");
    const hit = PI_ERRORS.find(([re]) => re.test(m));
    if (hit) return t(hit[1]);
    const http = /^Pi answered HTTP (\d+)/i.exec(m);
    return http ? t("sdc.pi.err_http", { code: http[1] }) : m;
  }
  async function send(method, url, body) {
    const r = await fetch(url, { method, headers: { "Content-Type": "application/json" },
                                 body: body === undefined ? undefined : JSON.stringify(body) });
    let j = {};
    try { j = await r.json(); } catch (e) { /* empty */ }
    if (!r.ok) {
      const err = new Error(typeof j.detail === "string" ? j.detail : r.statusText);
      err.status = r.status;
      throw err;
    }
    return j;
  }

  /* ------------------------------------------------------------ library */
  function paintLibrary() {
    const box = $("sdc-library");
    if (!library) { box.innerHTML = ""; return; }
    const tz = library.time_zone || {};
    const off = /^[+-]\d{4}$/.test(tz.utc_offset_now || "") ? `UTC${tz.utc_offset_now.slice(0, 3)}:${tz.utc_offset_now.slice(3)}` : "";
    const rows = [];
    rows.push(`<div class="sdc-row"><div class="k">${esc(t("sdc.folder"))}</div><div class="v">${
      library.base ? code(library.base) : `<span class="sdc-warn">${esc(library.root_exists ? t("dashcam.status.awaiting") : t("dashcam.status.no_folder"))}</span><small>${code(library.root || "")}</small>`}</div></div>`);
    rows.push(`<div class="sdc-row"><div class="k">${esc(t("sdc.tz"))}</div><div class="v">${esc([tz.name, off].filter(Boolean).join(" · "))}
      <small>${esc(t("sdc.tz_source", { source: tz.source || "?" }))} ${esc(t("sdc.tz_why"))}</small></div></div>`);
    const scan = library.scan || {};
    if (scan.shadowed_root || scan.skipped) {
      const bits = [];
      if (scan.shadowed_root) bits.push(t("dashcam.note.shadowed_title"));
      if (scan.skipped) bits.push(tn("dashcam.note.skipped_title", scan.skipped));
      rows.push(`<div class="sdc-row"><div class="k">${esc(t("sdc.scan"))}</div><div class="v sdc-warn">${esc(bits.join(" · "))}
        <small><a href="/dashcam" style="text-decoration:underline">${esc(t("sdc.scan_see"))}</a></small></div></div>`);
    }
    box.innerHTML = rows.join("");
  }

  /* ------------------------------------------------------------- status */
  function runText(r) {
    if (!r) return "";
    if (r.kind === "start") return r.files_total != null ? tn("sdc.run.start_n", r.files_total) : t("sdc.run.start");
    if (r.kind === "finish") {
      const what = r.files != null && r.took ? tn("sdc.run.files_took", r.files, { took: r.took })
        : r.files != null ? tn("sdc.run.files", r.files) : "";
      return [r.ok ? t("sdc.run.ok") : t("sdc.run.error"), what].filter(Boolean).join(" · ");
    }
    return r.message ? r.message : t("sdc.run.other");
  }

  function paintStatus() {
    const box = $("sdc-status");
    if (!status) { box.innerHTML = `<div class="body-sm">${esc(t("common.checking"))}</div>`; return; }
    const last = status.last_archive;
    let html;
    if (last) {
      const ok = last.ok !== false;
      html = `<div class="sdc-state ${ok ? "ok" : "bad"}"><div class="ic">${icon(ok ? "check" : "warn", 18)}</div><div class="grow">
        <div class="tt">${esc(runText(last))}${status.archiving ? `<span class="sdc-pill">${esc(t("sdc.archiving"))}</span>` : ""}</div>
        <div class="sb">${esc(t("sdc.last_archive", { ago: ago(last.at) }))}</div>
        ${ok ? "" : `<div class="hint">${esc(t("sdc.error_hint"))}</div>`}`;
    } else {
      html = `<div class="sdc-state"><div class="ic">${icon("clock", 18)}</div><div class="grow">
        <div class="tt">${esc(t(status.archiving ? "sdc.archiving_first" : "sdc.never"))}${status.archiving ? `<span class="sdc-pill">${esc(t("sdc.archiving"))}</span>` : ""}</div>
        <div class="sb">${esc(t("sdc.never_sub"))}</div>`;
    }
    html += waitingHtml() + "</div></div>";
    box.innerHTML = html;

    const v = $("v-dashcam");
    v.textContent = last ? (last.ok === false ? t("sdc.v_error", { ago: ago(last.at) }) : t("sdc.v_ok", { ago: ago(last.at) }))
      : library && library.time_zone ? library.time_zone.name : "";
    paintPi();
    paintRuns();
  }

  /* New Sentry clips wait for their folder to finish copying, and alerts that
     couldn't be delivered are retried with backoff. Say which is which. */
  function waitingHtml() {
    const ing = (status && status.ingest) || {};
    const pend = (watcher && watcher.pending) || [];
    const lines = [];
    if (ing.transfer_in_progress) lines.push(t("sdc.copying_now"));
    for (const p of pend.slice(0, 6)) {
      const name = (p.event_id || "").split("/").pop();
      lines.push(p.attempts ? t("sdc.pending.retry", { event: name, when: p.next_try ? fmtIn(p.next_try) : "" })
                            : t("sdc.pending.copying", { event: name }));
    }
    const li = ing.last_ingest || {};
    const pickup = li.error ? t("sdc.pickup_error", { message: li.error })
      : li.at ? tn("sdc.pickup", li.new_events ?? 0, { ago: ago(li.at) }) : "";
    if (!lines.length && !pickup) return "";
    return `<div class="sdc-waiting">${pickup ? `<div>${esc(pickup)}</div>` : ""}${lines.length ? `<ul>${lines.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>` : ""}</div>`;
  }
  function fmtIn(epochS) {
    const s = Math.max(0, Math.round(epochS - Date.now() / 1000));
    try {
      const rtf = new Intl.RelativeTimeFormat(localeTag(), { numeric: "auto" });
      return s >= 3600 ? rtf.format(Math.round(s / 3600), "hour") : s >= 60 ? rtf.format(Math.round(s / 60), "minute") : rtf.format(s, "second");
    } catch (e) { return ""; }
  }

  /* ---------------------------------------------------------- the hook */
  function hubOrigin() { return `${location.protocol}//${location.host}`; }
  function hookLines() {
    const tok = (settings && settings.notify_token) || "<token>";
    const cmd = `curl -fsS -m 10 -H "Authorization: Bearer ${tok}" --data-urlencode "msg=$message" ${hubOrigin()}/api/dashcam/teslausb/notify`;
    return [`export NOTIFICATION_COMMAND_START='${cmd}'`, `export NOTIFICATION_COMMAND_FINISH='${cmd}'`].join("\n");
  }
  function paintHook() {
    $("sdc-lines").textContent = hookLines();
    // The Pi can't reach "localhost": that is the Pi itself
    const local = /^(localhost|127\.\d+\.\d+\.\d+|\[::1\]|::1)$/i.test(location.hostname);
    $("sdc-local").hidden = !local;
    if (local) $("sdc-local").innerHTML = esc(t("sdc.hook.localhost")) + " " + code(location.host);
  }

  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return;
    }
    // Plain http on a LAN address has no async clipboard API
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;top:0;left:0;opacity:0;pointer-events:none";
    document.body.append(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    if (!ok) throw new Error("copy");
  }

  $("sdc-copy").addEventListener("click", async () => {
    try {
      await copyText(hookLines());
      toast(t("sdc.hook.copied"), "ok");
    } catch (e) {
      // last resort: select the lines so ⌘C / Ctrl+C works
      const r = document.createRange();
      r.selectNodeContents($("sdc-lines"));
      const sel = getSelection();
      sel.removeAllRanges(); sel.addRange(r);
      toast(t("sdc.hook.copy_manual"), "err");
    }
  });

  $("sdc-rotate").addEventListener("click", async () => {
    const yes = await Sheet.open({
      title: t("sdc.hook.rotate_title"),
      lead: esc(t("sdc.hook.rotate_lead")),
      actions: [{ label: t("common.cancel"), value: false }, { label: t("sdc.hook.rotate"), kind: "danger", value: true }],
    });
    if (!yes) return;
    try {
      const r = await send("POST", "/api/dashcam/teslausb/notify-token");
      settings = { ...(settings || {}), notify_token: r.notify_token };
      paintHook();
      toast(t("sdc.hook.rotated"), "ok");
    } catch (e) { toast(e.message, "err"); }
  });

  /* ------------------------------------------------------------ the Pi */
  function paintForm() {
    if (!settings) return;
    $("sdc-url").value = settings.pi_url || "";
    $("sdc-user").value = settings.username || "";
    $("sdc-pass").value = "";
    $("sdc-pass").placeholder = settings.has_password ? t("sdc.pi.password_saved") : t("sdc.pi.password_ph");
    $("sdc-pass-clear").hidden = !settings.has_password;
  }

  function bytes(n) {
    if (n == null) return "–";
    const gb = n / 1e9;
    return (gb >= 100 ? fmt.int(gb) : fmt.fixed(gb, 1)) + " GB";
  }
  function paintPi() {
    const box = $("sdc-pi");
    if (!status || !status.configured) { box.innerHTML = ""; return; }
    if (status.pi_error) { box.innerHTML = `<span class="bad">${icon("warn", 15)}${esc(piError(status.pi_error))}</span>`; return; }
    const pi = status.pi;
    if (!pi) { box.innerHTML = ""; return; }
    const parts = [];
    parts.push(`<span>${icon("check", 15)}${esc(t("sdc.pi.reachable"))}</span>`);
    if (pi.cpu_temp_c != null) parts.push(`<span>${icon("thermo", 15)}${esc(U.tempStr(pi.cpu_temp_c, 0))}</span>`);
    if (pi.uptime_s != null) {
      const d = Math.floor(pi.uptime_s / 86400);
      parts.push(`<span>${icon("clock", 15)}${esc(d >= 1 ? tn("sdc.pi.up_days", d) : t("sdc.pi.up_hours", { n: Math.max(1, Math.round(pi.uptime_s / 3600)) }))}</span>`);
    }
    const disk = pi.disk || {};
    if (disk.free_bytes != null) parts.push(`<span>${icon("box", 15)}${esc(t("sdc.pi.disk", { free: bytes(disk.free_bytes), total: bytes(disk.total_bytes) }))}</span>`);
    const wifi = pi.wifi || {};
    if (wifi.ssid || wifi.ip) parts.push(`<span>${icon("wind", 15)}${esc([wifi.ssid, wifi.strength, wifi.ip].filter(Boolean).join(" · "))}</span>`);
    const snaps = pi.snapshots || {};
    if (snaps.count != null) parts.push(`<span>${icon("film", 15)}${esc(t("sdc.pi.snapshots", { n: snaps.count }))}</span>`);
    parts.push(`<span>${icon("drive", 15)}${esc(t(pi.drives_active ? "sdc.pi.drive_on" : "sdc.pi.drive_off"))}</span>`);
    box.innerHTML = parts.join("");
  }

  $("sdc-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("sdc-msg");
    const pass = $("sdc-pass").value;
    try {
      settings = await send("POST", "/api/dashcam/teslausb/settings", {
        pi_url: $("sdc-url").value.trim(),
        username: $("sdc-user").value.trim(),
        password: pass ? pass : null,          // empty field = keep the saved password
      });
      paintForm(); paintHook();
      msg.textContent = t("sdc.pi.saved");
      msg.className = "body-sm";
      loadStatus();
    } catch (err) {
      msg.textContent = err.message;
      msg.className = "body-sm sdc-warn";
    }
  });

  $("sdc-pass-clear").addEventListener("click", async () => {
    try {
      settings = await send("POST", "/api/dashcam/teslausb/settings", { password: "" });
      paintForm();
      $("sdc-msg").textContent = t("sdc.pi.password_removed");
    } catch (err) { $("sdc-msg").textContent = err.message; }
  });

  $("sdc-sync").addEventListener("click", async () => {
    const btn = $("sdc-sync"), msg = $("sdc-msg");
    btn.disabled = true;
    msg.className = "body-sm";
    msg.textContent = t("sdc.pi.syncing");
    try {
      await send("POST", "/api/dashcam/teslausb/sync");
      msg.textContent = t("sdc.pi.sync_ok");
    } catch (err) {
      msg.className = "body-sm sdc-warn";
      msg.textContent = err.status === 409 ? t("sdc.pi.sync_no_address") : t("sdc.pi.sync_failed", { message: piError(err.message) });
    }
    btn.disabled = false;
    loadStatus();
  });

  /* ---------------------------------------------------------- activity */
  function paintRuns() {
    const runs = (status && status.runs) || [];
    const box = $("sdc-runs");
    $("v-sdc-activity").textContent = runs.length ? ago(runs[0].at) : "";
    const rows = runs.map((r) => `<div class="sdc-run"><span class="w">${esc(ago(r.at))}</span>
      <span class="m"><b>${esc(runText(r))}</b>${r.message && r.kind !== "other" ? `<br>${esc(r.message)}` : ""}</span></div>`);
    const sync = status && status.last_sync_request;
    if (sync) rows.push(`<div class="sdc-run"><span class="w">${esc(ago(sync.at))}</span><span class="m"><b>${esc(t("sdc.sync_request"))}</b> · ${
      esc(sync.ok ? t("sdc.sync_request_ok") : sync.error ? piError(sync.error) : "HTTP " + (sync.status_code || "?"))}</span></div>`);
    const markers = (status && status.ingest && status.ingest.archive_markers) || {};
    for (const [name, mtime] of Object.entries(markers)) {
      rows.push(`<div class="sdc-run"><span class="w">${esc(ago(mtime))}</span><span class="m">${code(name)}</span></div>`);
    }
    box.innerHTML = rows.length ? rows.join("") : `<div class="body-sm">${esc(t("sdc.no_runs"))}</div>`;
  }

  /* --------------------------------------------------------------- load */
  async function loadStatus() {
    try { [status, watcher] = await Promise.all([apiGet("/api/dashcam/teslausb/status"), apiGet("/api/dashcam/watcher").catch(() => null)]); }
    catch (e) { status = null; $("sdc-status").innerHTML = `<div class="body-sm sdc-warn">${esc(e.message)}</div>`; return; }
    paintStatus();
  }

  async function boot() {
    await i18nReady.catch(() => {});
    paintStatus();
    try { settings = await apiGet("/api/dashcam/teslausb/settings"); } catch (e) { settings = null; }
    paintForm(); paintHook();
    try { library = await apiGet("/api/dashcam/status"); } catch (e) { library = null; }
    paintLibrary();
    await loadStatus();
  }

  window.addEventListener("omt:locale", () => {
    if (!settings) return;
    paintLibrary(); paintForm(); paintHook();
    if (status) paintStatus();
  });
  boot();
})();
