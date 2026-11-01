# The film

A 31-second film, written as code. Each frame is a pure function of time
(`FILM.seek(t)` in `src/film.js`), and a headless Chromium plus ffmpeg turn
the frames into an MP4. There is no editing timeline and no stock footage:
the screens are the real UI, running on synthetic data.

| Time | Scene | Words (en) |
|---|---|---|
| 0:00 | the idea | Your Tesla. Your data. Your machine. |
| 0:05 | Overview | Your car, at a glance. |
| 0:09 | Sentry, a push lands on a phone | Sentry triggers. Your phone knows. |
| 0:14 | Driving, charging, battery | Every drive. Every charge. |
| 0:18 | Footprint | Everywhere you've been. |
| 0:21 | a risky command asks first | Controls that ask first. |
| 0:25 | the Mac menu bar | A native Mac app. No Docker. |
| 0:28 | end card | Open source · Self-hosted · Free |

Every claim is true of the product: pushes follow the push policy, and the CPU
figure is the Mac app's measured idle total (see `packaging/macos/README.md`).

## Preview

```bash
python3 -m http.server -d promo/src 8000   # http://localhost:8000/film.html?lang=zh&t=12
```

Space pauses; ← → seek one second (Shift: a tenth).

## Build

```bash
python3 promo/scripts/capture.py <demo-site>     # screens → promo/src/shots/{en,zh}/
python3 promo/scripts/render.py --lang en --webp # → promo/out/ohmytesla-film.en.mp4 (+ .webp)
python3 promo/scripts/render.py --lang zh --webp
```

- `<demo-site>` is a static build of the UI on synthetic data, using the bundled CC BY 4.0
  model. Never point it at real data or a private model: these frames are published.
- The Mac panel images (`src/shots/mac/panel-{en,zh}.png`) come from the panel's own SwiftUI
  view, rendered off-screen with synthetic data.
- Requirements: Python 3.10+ with `playwright` (Chromium) and `Pillow`, and `ffmpeg` with
  libx264. A render takes about three minutes per language.
- The README uses the WebP. The MP4s are attached to each GitHub release.
