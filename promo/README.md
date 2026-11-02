# The film

A 32-second launch film, written as code. Each frame is a pure function of time
(`FILM.seek(t)` in `src/film.js`), and a headless Chromium plus ffmpeg turn
the frames into an MP4. There is no editing timeline and no stock footage:
the screens are the real UI running on synthetic data, and the truck is the
app's own 3D model, lit live in WebGL (`src/car3d.js`).

Picture and score (`scripts/music.py`) share one grid: 120 BPM, a bar is 2 s.

| Time | Scene | Words (en) |
|---|---|---|
| 0:00 | three words on the beat | Your Tesla. Your data. Your machine. |
| 0:04 | the drop: the light bar comes on, the studio lights find the truck | Everything your Tesla knows. On your own machine. |
| 0:08 | two pushes land on a phone | Sentry triggers. You know in seconds. ≈5 s |
| 0:12 | driving, charging, battery as panels in depth | Every drive. Every charge. 935 · 294 · 97% |
| 0:16 | the footprint, tilted like a table | Everywhere you've been. |
| 0:19 | the camera moves in on a confirm sheet; a tap | Controls that ask first. |
| 0:22 | the Mac menu bar | A native Mac app. |
| 0:25 | stop-time, one word a second | Self-hosted. Open source. Free. |
| 0:28 | the truck in profile, the mark, the address | Download for Mac · Runs anywhere with Docker |

Every claim is true of the product: pushes follow the push policy, ≈5 s is the
README's typical Sentry delay, the CPU figure is the Mac app's measured idle
total (see `packaging/macos/README.md`), and the numbers are the demo's
synthetic year, the same as in the screens.

`src/vendor` and `src/assets/cybertruck.glb` are links to the app's own three.js
and model (CC BY 4.0, credited on the end card). Never point the film at an
owner's private model: these frames are published.

## Preview

```bash
python3 -m http.server -d promo/src 8000   # http://localhost:8000/film.html?lang=zh&t=12  (?still: paused)
```

Space pauses; ← → seek one second (Shift: a tenth).

## Build

```bash
python3 promo/scripts/capture.py <demo-site>     # screens → promo/src/shots/{en,zh}/
python3 promo/scripts/music.py                   # → promo/out/soundtrack.wav (needs numpy, scipy)
python3 promo/scripts/render.py --lang en --webp # → promo/out/ohmytesla-film.en.mp4 (+ .webp)
python3 promo/scripts/render.py --lang zh --webp
```

- `<demo-site>` is a static build of the UI on synthetic data, using the bundled CC BY 4.0
  model. Never point it at real data or a private model: these frames are published.
- The Mac panel images (`src/shots/mac/panel-{en,zh}.png`) come from the panel's own SwiftUI
  view, rendered off-screen with synthetic data.
- Requirements: Python 3.10+ with `playwright` (Chromium) and `Pillow`, and `ffmpeg` with
  libx264. On a Mac the truck renders on the GPU (Metal); a render takes a few minutes per language.
- The README uses the WebP. The MP4s are attached to each GitHub release.
