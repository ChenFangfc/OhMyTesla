"""3D vehicle models: bundled with the app, or supplied by the owner.

Two places, checked in this order:

* ``$DATA_DIR/models/<key>.glb`` — the owner's own file (uploaded in
  Settings, or produced by ``scripts/models/rig_vehicle.py``). Never part of
  the repository or the DMG: these are often commercial models whose licence
  allows personal use only.
* ``static/models/<key>.glb`` — shipped with the app. Only models whose
  licence allows redistribution go here, with their credits beside them.

Each GLB may have a ``<key>.json`` manifest next to it telling the viewer
which materials are paint / glass / lights and how to normalise the model.
Without one, names are guessed (most exporters use recognisable words).
"""

from __future__ import annotations

import contextlib
import json
import re
import struct
from pathlib import Path

from ..config import settings

BUNDLED_DIR = Path(__file__).resolve().parent.parent / "web" / "static" / "models"
MAX_UPLOAD = 60 * 1024 * 1024
RIG_PARTS = ("door_fl", "door_fr", "door_rl", "door_rr", "frunk", "tailgate",
             "charge_port", "tonneau", "wheel_fl", "wheel_fr", "wheel_rl", "wheel_rr")
_KEY = re.compile(r"^[a-z0-9_-]{2,32}$")


def user_dir() -> Path:
    p = Path(settings.data_dir) / "models"
    p.mkdir(parents=True, exist_ok=True)
    return p


def valid_key(key: str) -> bool:
    return bool(_KEY.match(key or ""))


def inspect_glb(data: bytes) -> dict:
    """Structural check of a binary glTF without loading geometry.

    Rejects anything that isn't glTF 2.0 binary with a JSON chunk; reports the
    node and material names so the caller can tell a rigged model (doors that
    open) from a static one before the browser ever downloads it.
    """
    if len(data) < 20 or data[:4] != b"glTF":
        raise ValueError("not a binary glTF (.glb) file")
    version, length = struct.unpack_from("<II", data, 4)
    if version != 2:
        raise ValueError(f"glTF version {version} — only 2.0 is supported")
    if length != len(data):
        raise ValueError("truncated or padded .glb (header length mismatch)")
    clen, ctype = struct.unpack_from("<II", data, 12)
    if ctype != 0x4E4F534A:          # 'JSON'
        raise ValueError("first chunk is not JSON")
    doc = json.loads(data[20:20 + clen].decode("utf-8"))
    nodes = [n.get("name", "") for n in doc.get("nodes", [])]
    mats = [m.get("name", "") for m in doc.get("materials", [])]
    exts = doc.get("extensionsRequired", []) or []
    unsupported = [e for e in exts if e not in ("EXT_meshopt_compression", "KHR_mesh_quantization",
                                                 "KHR_texture_transform", "KHR_materials_emissive_strength")]
    if unsupported:
        raise ValueError(f"requires unsupported glTF extensions: {', '.join(unsupported)}")
    return {
        "nodes": len(nodes),
        "materials": mats,
        "rig": [p for p in RIG_PARTS if p in nodes],
        "bytes": len(data),
    }


def _guess_materials(names: list[str]) -> dict:
    def pick(*words, exclude=()):
        out = []
        for n in names:
            low = n.lower()
            if any(w in low for w in words) and not any(x in low for x in exclude):
                out.append(n)
        return out
    return {
        "paint": pick("paint", "body", "exterior", "steel", "carpaint", exclude=("glass", "light", "lamp")),
        "glass": pick("glass", "window", "windshield"),
        "lights": pick("lights", exclude=("head", "tail")),
        "headlights": pick("headlight", "head_light", "headlamp"),
        "taillights": pick("taillight", "tail_light", "taillamp", "rearlight"),
        "tonneau": pick("bedcover", "tonneau"),
    }


def _manifest(glb: Path) -> dict:
    side = glb.with_suffix(".json")
    if side.is_file():
        with contextlib.suppress(ValueError, OSError):   # a broken manifest falls back to guessing
            return json.loads(side.read_text("utf-8"))
    try:
        info = inspect_glb(glb.read_bytes())
    except Exception:  # noqa: BLE001
        return {}
    return {"materials": _guess_materials(info["materials"]), "rigged": bool(info["rig"])}


def resolve(key: str | None) -> dict | None:
    """The model the viewer should load for this model line, or None."""
    if not key or not valid_key(key):
        return None
    for source, base, url in (
        ("user", user_dir(), f"/models/{key}.glb"),
        ("bundled", BUNDLED_DIR, f"/static/models/{key}.glb"),
    ):
        glb = base / f"{key}.glb"
        if glb.is_file():
            m = _manifest(glb)
            # mtime in the URL: replacing the file must not be masked by the
            # browser's cache of the old one.
            m["url"] = f"{url}?v={int(glb.stat().st_mtime)}"
            m["source"] = source
            m.setdefault("rigged", False)
            return m
    return None


def available() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for source, base in (("bundled", BUNDLED_DIR), ("user", user_dir())):
        for glb in sorted(base.glob("*.glb")):
            m = _manifest(glb)
            out.setdefault(glb.stem, {})[source] = {
                "rigged": bool(m.get("rigged")),
                "bytes": glb.stat().st_size,
                "credits": m.get("credits"),
                "license": m.get("license"),
            }
    return out


def save_user_model(key: str, data: bytes) -> dict:
    if not valid_key(key):
        raise ValueError("bad model key")
    if len(data) > MAX_UPLOAD:
        raise ValueError(f"file too large (max {MAX_UPLOAD // (1024 * 1024)} MB)")
    info = inspect_glb(data)
    d = user_dir()
    tmp = d / f".{key}.glb.part"
    tmp.write_bytes(data)
    tmp.replace(d / f"{key}.glb")
    manifest = {"materials": _guess_materials(info["materials"]), "rigged": bool(info["rig"]),
                "license": "personal use (owner-supplied)"}
    (d / f"{key}.json").write_text(json.dumps(manifest, indent=2), "utf-8")
    return {**info, "manifest": manifest}


def delete_user_model(key: str) -> bool:
    if not valid_key(key):
        return False
    gone = False
    for suffix in (".glb", ".json"):
        p = user_dir() / f"{key}{suffix}"
        if p.is_file():
            p.unlink()
            gone = True
    return gone
