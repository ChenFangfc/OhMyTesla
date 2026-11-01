# Dashcam — notes for the UI redesign

The 2026-10-31 dashcam audit fixes changed or added the API surface below. The templates were not
touched, so each item here is something the new UI must show or call. All routes are under
`/api/dashcam` and sit behind the portal's HTTP Basic auth.

## 1. Segments are not 60 seconds long (audit D14)

`GET /event?id=…` now returns the measured length of every segment:

```json
{"duration": 11.888,
 "segments": [{"start": "2026-10-29T08:03:00", "cameras": {…}, "duration": 5.944, "offset": 0.0, "duration_known": true},
              {"start": "2026-10-29T08:04:00", "cameras": {…}, "duration": 5.944, "offset": 5.944, "duration_known": true}]}
```

- `offset` is where the segment starts on the event timeline, in seconds. Segments sit end to end
  on that timeline, so a gap of up to 90 s between RecentClips segments is not shown.
- `duration_known: false` means ffprobe could not read the clip (it is truncated or encrypted).
  The server then uses 60 s.
- **Player and USB page:** every `segIdx * 60` and `segments.length * 60` must use these numbers
  instead (`player.html` lines 212–221, 283, 462; `usb.html` lines 321–328, 387). That covers the
  total length, the seek bar, the TeslaMate HUD lookup (`offset + currentTime`), and the `?t=`
  deep link.
- `start_offset` / `end_offset` sent to `POST /compose` are read on this same timeline. If the UI
  keeps using `segIdx * 60`, a range export no longer matches what the user selected.
- The wall-clock time for a point is `segment.start + currentTime`. Do not compute it from the
  event start, because gaps exist between segments.

## 2. Export jobs carry a report (D8, D9, D14, L3)

`GET /jobs` and `GET /jobs/{id}` now add a `report` object:

| field | meaning |
|---|---|
| `overlay` | `burned`, `unavailable` (this ffmpeg has no libass), `no_telemetry` (nothing in TeslaMate or SEI for the range), or `off` |
| `warnings[]` | One sentence each, ready to display |
| `dropped[]` | `{segment, missing:[cam], unreadable:[{camera, path, reason}]}` — the minutes left out of the export |
| `short_clips[]` | `{segment, camera, duration, expected}` — a camera's clip is shorter than the rest; that tile freezes until the next minute |
| `encoder` | e.g. `h264_videotoolbox (quality)` or `libx264` |
| `duration` / `output_seconds` | Planned length and actual length of the output |
| `error` | The failure reason (`cancelled` when the user cancelled) |

The UI should show:

- The warnings on **done** jobs, not only on failed ones.
- For every `dropped` entry with `unreadable`, an action "export again without the `<camera>`
  camera" (pass `cameras` without that camera). Currently a single truncated back-camera clip
  removes that whole minute from a 4-camera export.
- That the export has no overlay when `overlay == "unavailable"`.

`GET /encoder` adds `overlay_supported` (false means no libass), `mode`, and `tried[]` (which
hardware encoders were probed and why each failed). The Homebrew ffmpeg and the ffmpeg built under
`packaging/macos/build` both lack libass, so every export from the macOS app currently has no
overlay. The UI should say so near the overlay toggle.

`POST /compose` now returns **400** when `start_offset` is past the end of the event, or when every
camera named in `cameras` is unknown.

## 3. Library status (D4, D11)

`GET /status` adds:

- `time_zone: {name, source, utc_offset_now}` — the zone used to read clip file names and how it
  was found (`TESLACAM_TZ`, `TZ setting`, `TZ environment`, `/etc/localtime`, or
  `system localtime rules`). Show it in settings, because a wrong zone shifts every overlay by
  whole hours.
- `scan: {skipped, skipped_items[{path, reason}], shadowed_root}` — files and folders the scanner
  ignored (impossible dates, unreadable folders). `shadowed_root` is set when both `TeslaCam/` and
  the root hold clips; only `TeslaCam/` is shown, so warn the user.
- `probe_timeouts` — clips whose probe timed out, usually a slow network mount. Do not report these
  as unreadable or encrypted.
- `base` — the folder actually in use.

## 4. Evidence package (D1, D6)

`GET /evidence?id=…[&car_id=]` now **streams** the ZIP: it sends no Content-Length and starts
immediately. No page links to it yet. Start the download with a plain link
(`<a href="/api/dashcam/evidence?id=…" download>`) or `location.href`. **Do not use
`fetch()`→`blob()`**: that puts the whole package back in the browser's memory, and a 10-minute HW4
event is about 2 GB. The README in the package now states the offset correctly. `telemetry.csv`
and `drive.csv` both have a `timestamp_utc` column on the same timeline.

## 5. Clock auto-align (D17)

`POST /align` returns **422** with a sentence explaining the problem when the fit is poor:

- RMSE above 6 km/h,
- no clear best offset, or
- a clip with almost constant speed.

In all three cases the stored offset is left unchanged. On success the response is
`{offset, rmse_kmh, median_rmse_kmh, samples, accepted: true}`. Show the 422 text as is; it is not
a server error.

## 6. Decrypting 2026.20+ clips (L9) — new

1. `GET /decrypt/scan?id=<event>` returns
   `{clips, encrypted, encrypted_paths[], key_service, request_preview[], running}`.
   `request_preview` is **exactly** what will be sent to Tesla: VIN, key_id, timestamp, and the
   wrapped and public keys. It contains no video bytes. Show it, so the user can see what leaves
   the machine.
2. The user pastes a dashcam.tesla.com access token. Explain that this token can only fetch
   decryption keys: it cannot control the car or read vehicle data. The token is used once, in
   memory, and is never stored.
3. `POST /decrypt {"event_id", "token", "keep_encrypted": true}` starts the job. It returns
   `{state:"running", total, …}`, `{state:"nothing_to_do"}`, or 409 if a job is already running.
4. Poll `GET /decrypt/status` → `{state: running|done|failed, total, done, decrypted, failed[{path, reason}], error}`.
   When it finishes, reload the event. Decrypted files replace the originals, which are kept as
   `<name>.mp4.enc`. The scanner ignores those, and the user can delete them once happy.

## 7. teslausb (task B) — new settings section

| route | purpose |
|---|---|
| `GET /teslausb/settings` | `{pi_url, username, has_password}`. The password is never returned |
| `POST /teslausb/settings` | `{pi_url?, username?, password?}`. Accepts `192.168.1.60` or `http://host:port`. Returns 422 for credentials in the URL or a non-http scheme. A `null` field is left unchanged; `""` clears it |
| `POST /teslausb/sync` | Calls the Pi's `/cgi-bin/trigger_sync.sh`. Returns 409 when no address is set and 502 with a reason when the Pi is unreachable or wants a password |
| `GET /teslausb/status` | `{configured, pi:{cpu_temp_c, uptime_s, drives_active, snapshots{count,oldest,newest}, disk{total_bytes,free_bytes}, wifi{ssid,strength,ip}, …}, pi_error, archiving, last_archive, runs[], last_sync_request, ingest:{layout, base, archive_markers, transfer_in_progress, scan, last_ingest, watcher}}` |
| `POST /teslausb/notify` | Called by teslausb itself (form field `msg`), not by the UI |

The settings page should show the exact line for the user to paste into
`teslausb_setup_variables.conf`. Fill in the hub's address; the password stays a placeholder:

```
export NOTIFICATION_COMMAND_FINISH='curl -fsS -m 10 -u ohmytesla:<portal password> --data-urlencode "msg=$message" http://<hub>:8800/api/dashcam/teslausb/notify'
```

Show it once for `_START` and once for `_FINISH`. Then show `last_archive`: success or "Error
during archiving", the file count, how long it took, and when. Errors are common on 32-bit Pi OS
since firmware 2025.44 (repeater and pillar clips are dropped). The full guide is
`docs/TESLAUSB.md`.

`GET /watcher` returns `{known, pending[{event_id, since, attempts, next_try}], last_tick}`. Use it
to explain "waiting for the folder to finish copying" or "will retry".

## 8. New-clip alerts (D5, plus the owner's short-push rule)

These notes are for whoever owns `alerts/`:

- The watcher calls
  `engine.fire_custom(car_id, level, title, body, rule="new_sentry_clip", gate="new_sentry_clip", image=<path or None>, url=<relative link>)`.
  - It runs for **SentryClips only**. SavedClips are user-initiated and never trigger it.
  - `level` comes from `RULES["new_sentry_clip"].level`.
  - The title is one line (`Sentry clip saved · <city>`), and so is the body
    (`<reason> · YYYY-MM-DD HH:MM`).
  - When several clips land at once the title is `N Sentry clips saved`. The image is the newest
    clip's thumbnail and `url` is `/dashcam`.
- `url` is **relative** (`/dashcam/play?id=SentryClips/…`). `notifier._absolute()` adds
  `PUBLIC_URL`, and web push uses the relative URL directly.
- `image` is `thumb.png`. When there is none, it is a frame grabbed with ffmpeg and cached in
  `preview/thumbs/`.
- **Contract this relies on:**
  - If `fire_custom` returns False, the event stays pending. It is retried with backoff (1, 2, 4,
    … up to 30 min) and dropped after 24 h.
  - If an alert that is only recorded in-app ever returns **False**, every retry files another
    in-app record. Please keep returning True when the alert was recorded in-app. The current
    working tree does: the live test logged `recorded in-app` and the event was marked delivered
    once.
  - Optional: passing `tag=f"dashcam:{event_id}"` would let devices collapse retried pushes. It
    is not passed today because it was not in the agreed signature.

## 9. `sei.js`

- `omtSei.extract(file, {step})` no longer needs `durationS`. It reads the clip's real length from
  `moov/mvhd`, and `durationS` is only a fallback.
- It decodes proto3 defaults as values: a missing gear is `P` and a missing speed is `0`, not
  `"–"`.
- The function is exported as `omtSei.mp4Duration(arrayBuffer)` for the USB page's timeline.
