# Dashcam UI: what the pages need from the backend

The UI now uses the contracts in `UI_NOTES.md` §1–7. A few things it cannot fix on its own:

## 1. Deep links into the player (`?t=`)

The player reads `?t=` as seconds on the measured timeline: segments end to end, using each
segment's `offset` and `duration` from `GET /event`. Two producers still use other timelines:

- `insights/router.py` (drive → footage, used by Trips) computes `seek = i * 60 + within`. With
  real ~60 s segments this is close. With short or truncated segments it lands in the wrong place,
  or past the end.
- `alerts/router.py` (Security → Watch) computes wall-clock seconds since the event start. That
  matches the timeline for Sentry and Saved events, but not for RecentClips, which have gaps.

The player now also accepts `?at=<naive local ISO>` (for example `at=2026-10-31T09:00:40`, the
car's local time as in the file names). It maps that moment to the timeline itself, and closes up
gaps. Emitting `at` needs no probing on the server, so it is the simplest fix for both producers.
Driving events (`/dashcam` → Watch) already link with `?clip=<path>&seek=<s>`, which is exact.

## 2. `POST /align` 422 text is Chinese only

The UI shows the 422 `detail` as is, as UI_NOTES §5 asks. In an English UI that is a Chinese
sentence. Please write it in the user's locale (`i18n.prefs`), or add a short code the UI can
translate (as `X-Error-Code` does for analytics).

Chrome also logs every 4xx response as a console error ("Failed to load resource … 422"). Page
script cannot suppress that. If console silence matters, `200 {accepted: false, reason}` would
avoid it.

## 3. Other English-only strings the UI translates by pattern

- Job report `warnings[]`. Three are already covered by structured fields (`dropped`, `overlay`).
  The UI matches "TeslaMate database unavailable" itself, and shows any other warning as is.
- teslausb `pi_error` and the `/sync` 502 detail. The UI matches the five current sentences in
  `static/js/pages/settings_dashcam.js` (`PI_ERRORS`).

A `code` next to each sentence would let the UI stop matching English text.

## 4. Event cards guess the last segment's length

`GET /events` reports `end` as `last.start + 60 s` until someone opens the event, because
`fill_durations` only runs in `/event` and `/compose`. An 18-second event shows as 1:12 on its
card until it is first played. Optional fix: include `duration` with `duration_known` per event
once the durations are cached, so the UI can mark estimates.

## 5. Small things

- `GET /teslausb/status` → `settings.notify_token` is always `""`, because `_public(cfg)` is called
  without the token. The UI reads the token from `/teslausb/settings`, so this is harmless. Either
  drop the field or fill it in.
- `GET /telemetry` refuses windows over 4.5 h. For longer RecentClips drives, the players now ask for
  a 4-hour window around the playhead and move it as playback moves. No change is needed; this is
  noted so the cap isn't lowered without updating the pages.
