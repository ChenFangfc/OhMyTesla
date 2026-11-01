# Vehicle 3D models

The home screen draws the car with `static/js/vehicle3d.js`. Which file it loads is decided by
`app/vehicle/models3d.py`, per model line (`cybertruck`, `model3`, `modely`, `models`, `modelx`):

1. **Your own model** — `$DATA_DIR/models/<key>.glb` (Docker: `data/hub/models/`). Upload it in
   **Settings → Vehicles → 3D model**, or copy it there. It wins over the bundled one and is never
   committed or shipped: commercial models are usually licensed for personal use only.
2. **Bundled** — `static/models/<key>.glb`, only for models whose licence allows redistribution
   (see `CREDITS.md`).
3. Neither → the page falls back to a 2D render.

## The contract a model has to meet

| | |
|---|---|
| Format | glTF 2.0 binary (`.glb`); Meshopt compression and WebP textures are fine |
| Axes | +Y up, vehicle front toward **+Z**, driver (left) side toward **+X** |
| Scale | metres; the viewer rescales to the real length anyway |
| Ground | wheels touching y = 0 (the viewer re-grounds, but hinges are cleaner this way) |

Doors, frunk, tailgate and so on only move if the model has **one node per moving part, named
canonically, with its origin on the hinge**:

`door_fl` `door_fr` `door_rl` `door_rr` `frunk` `tailgate` `charge_port` `tonneau`
`wheel_fl` `wheel_fr` `wheel_rl` `wheel_rr`

A model without them still renders; it just doesn't open. A model with a separate bed-cover
material (`bedcover`/`tonneau`) gets a retracting tonneau even without a `tonneau` node.

### Optional manifest

`<key>.json` next to the `.glb` tells the viewer which materials are which:

```json
{
  "rigged": true,
  "length_m": 5.683,
  "materials": {
    "paint": ["paint"],
    "glass": ["glass"],
    "lights": ["lights"],
    "headlights": [], "taillights": [], "tonneau": []
  },
  "credits": "…", "license": "…"
}
```

Without it, material names are guessed (`paint`/`body`/`exterior`, `glass`/`window`, `light`…).

## Turning a source model into one

`scripts/models/rig_vehicle.py` runs inside Blender: it bakes every transform into vertices,
re-pivots each moving part on its real hinge (derived from the part's geometry, because source
files usually put pivots at part centres), names nodes canonically, wires PBR textures, and
exports. `scripts/models/optimize.sh` then compresses it (2K WebP + Meshopt, typically 25 MB → <1 MB).

```bash
blender -b model.blend --python scripts/models/rig_vehicle.py -- \
    --profile ct-2024-interior --out /tmp/cybertruck-raw.glb
scripts/models/optimize.sh /tmp/cybertruck-raw.glb data/hub/models/cybertruck.glb
```

A new source model needs a profile in `rig_vehicle.py` mapping its object names to the canonical
parts — usually a dozen lines.
