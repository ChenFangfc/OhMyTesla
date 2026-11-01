"""Which vehicle is this — model line, paint, wheels — from whatever we have.

Every surface that draws or describes the car (3D view, 2D fallback, labels,
command availability) asks this module instead of guessing on its own.
Signals, most to least reliable:

1. VIN position 4 is the model line on every Tesla VIN in production
   (5YJ3…, 7SAY…, LRWY…, XP7Y…, 7G2C… → 3, Y, Y, Y, C). It never changes
   and is present from the first sync.
2. TeslaMate's ``cars.model``: "S"/"3"/"X"/"Y", and "Cybertruck" since
   TeslaMate 4.2 — but only after the next wake-up, and NULL before that.
3. ``vehicle_config.car_type`` when a live API payload is at hand
   (models/models2/lychee = S, modelx/tamarind = X, cybertruck).

Paint comes from ``exterior_color``; the API prefixes some values with
"ExteriorColor" and spells others plainly, so names are normalised before
lookup. Cybertruck paint is stainless whatever the field says — a wrap is
the owner's business, and the view tints only when one is set explicitly.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

# key -> display name, bumper-to-bumper length (m), body type
MODELS: dict[str, dict] = {
    "cybertruck": {"name": "Cybertruck", "length_m": 5.683, "body": "truck"},
    "model3": {"name": "Model 3", "length_m": 4.720, "body": "sedan"},
    "modely": {"name": "Model Y", "length_m": 4.790, "body": "suv"},
    "models": {"name": "Model S", "length_m": 4.970, "body": "sedan"},
    "modelx": {"name": "Model X", "length_m": 5.057, "body": "suv"},
}

_VIN4 = {"C": "cybertruck", "3": "model3", "Y": "modely", "S": "models", "X": "modelx"}
_TM_MODEL = {"cybertruck": "cybertruck", "3": "model3", "y": "modely", "s": "models", "x": "modelx"}
_CAR_TYPE = {
    "cybertruck": "cybertruck",
    "model3": "model3",
    "modely": "modely",
    "models": "models", "models2": "models", "lychee": "models",
    "modelx": "modelx", "tamarind": "modelx",
}

# Normalised paint name -> (display name, sRGB hex used to tint a neutral model).
PAINTS: dict[str, tuple[str, str]] = {
    "quicksilver": ("Quicksilver", "#c9ccd1"),        # Cybertruck: bare stainless
    "stainless": ("Stainless Steel", "#c9ccd1"),
    "pearlwhite": ("Pearl White", "#f1f1ee"),
    "pearlwhitemulticoat": ("Pearl White", "#f1f1ee"),
    "solidblack": ("Solid Black", "#121314"),
    "black": ("Solid Black", "#121314"),
    "obsidianblack": ("Obsidian Black", "#17181b"),
    "diamondblack": ("Diamond Black", "#141518"),
    "midnightsilver": ("Midnight Silver", "#5b5f66"),
    "midnightsilvermetallic": ("Midnight Silver", "#5b5f66"),
    "stealthgrey": ("Stealth Grey", "#4a4d52"),
    "steelgrey": ("Steel Grey", "#6c7076"),
    "silvermetallic": ("Silver", "#a9adb3"),
    "silver": ("Silver", "#a9adb3"),
    "deepblue": ("Deep Blue", "#1d3561"),
    "deepbluemetallic": ("Deep Blue", "#1d3561"),
    "redmulticoat": ("Red Multi-Coat", "#8f1018"),
    "red": ("Red Multi-Coat", "#8f1018"),
    "ultrared": ("Ultra Red", "#a3121b"),
    "glacierblue": ("Glacier Blue", "#9db6c9"),
}


def _norm(value: str | None) -> str:
    s = (value or "").strip()
    for prefix in ("ExteriorColor", "PaintColor", "WheelType"):
        s = s.removeprefix(prefix)
    return s.replace("_", "").replace(" ", "").lower()


@dataclass
class Identity:
    key: str | None            # cybertruck / model3 / modely / models / modelx, or None
    name: str                  # "Cybertruck", "Model Y", … or "Tesla" when unknown
    variant: str | None        # trim badging, e.g. "Cyberbeast", "LR AWD"
    paint: str | None          # display name
    paint_hex: str | None
    wheel: str | None
    length_m: float | None
    source: str                # which signal decided the model line

    def as_dict(self) -> dict:
        return asdict(self)


def identify(vin: str | None = None, model: str | None = None, car_type: str | None = None,
             trim_badging: str | None = None, marketing_name: str | None = None,
             exterior_color: str | None = None, wheel_type: str | None = None) -> Identity:
    key, source = None, "unknown"
    v = (vin or "").strip().upper()
    if len(v) >= 4 and v[3] in _VIN4:
        key, source = _VIN4[v[3]], "vin"
    elif car_type and _CAR_TYPE.get(car_type.strip().lower()):
        key, source = _CAR_TYPE[car_type.strip().lower()], "car_type"
    elif model and _TM_MODEL.get(model.strip().lower()):
        key, source = _TM_MODEL[model.strip().lower()], "teslamate"

    meta = MODELS.get(key or "", {})
    paint_name = paint_hex = None
    p = _norm(exterior_color)
    if key == "cybertruck":
        paint_name, paint_hex = PAINTS["stainless"]
    elif p in PAINTS:
        paint_name, paint_hex = PAINTS[p]
    elif exterior_color:
        paint_name = exterior_color

    variant = marketing_name or None
    tb = (trim_badging or "").strip()
    if not variant and tb and tb.upper() not in ("DEFAULT", "NONE"):
        variant = "Cyberbeast" if tb.upper() == "CYBERBEAST" else tb.upper()

    wheel = _norm(wheel_type) or None
    return Identity(
        key=key,
        name=meta.get("name", "Tesla"),
        variant=variant,
        paint=paint_name,
        paint_hex=paint_hex,
        wheel=wheel_type.removeprefix("WheelType") if wheel_type else wheel,
        length_m=meta.get("length_m"),
        source=source,
    )
