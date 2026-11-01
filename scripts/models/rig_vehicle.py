"""Turn a vehicle model into the rigged GLB the 3D view animates.

Run inside Blender (headless):

    blender -b "<model>.blend" --python scripts/models/rig_vehicle.py -- \
        --profile ct-2024-interior --out data/models/cybertruck.glb

then compress with scripts/models/optimize.sh.

What the viewer needs from a model is a small, fixed contract — see
services/hub/app/web/static/models/README.md:

  * one node per moving part, named canonically (door_fl, frunk, tailgate,
    charge_port, tonneau, wheel_fl, …), whose origin sits **on the hinge**;
  * glTF orientation: +Y up, vehicle front toward +Z, driver (left) side +X,
    wheels on y = 0, centred on x/z, real-world metres.

Source models almost never arrive like that. This one ships with grouping
empties at each part's *centre*, so a door rotated about its own empty spins
in place instead of swinging. The hinge is therefore derived here from each
part's bounding box (front edge for doors, rear-top edge for the frunk,
bottom edge for the tailgate) rather than trusted from the file.
"""

import argparse
import sys

import bpy
from mathutils import Matrix, Vector

# Each profile maps canonical part -> object-name prefixes in the source file,
# plus the hinge rule. Blender axes here: front = -Y, left = +X, up = +Z.
PROFILES = {
    # "Tesla CyberTruck 2024 (with interior)": Spanish part names
    # (PuertoCarga = charge port, Persiana = tonneau blind).
    "ct-2024-interior": {
        "length_m": 5.683,  # Tesla spec, bumper to bumper
        "parts": {
            "frunk": (["HOOD_"], "rear_top"),
            "tailgate": (["TRUNK_"], "bottom_rear"),
            # Hinges come from the door *panel* only: the front doors' light mesh
            # includes the side mirror, which would put the hinge 17 cm outboard.
            "door_fl": (["LF_DOOR_"], "front_edge_left", "_Base_"),
            "door_rl": (["LR_DOOR_"], "front_edge_left", "_Base_"),
            "door_fr": (["RF_DOOR_"], "front_edge_right", "_Base_"),
            "door_rr": (["RR_DOOR_"], "front_edge_right", "_Base_"),
            "charge_port": (["PuertoCarga_"], "front_edge_left", "_Base_"),
            "tonneau": (["Persiana_"], "front_top"),
            "wheel_fl": (["LF_WHEEL_"], "center"),
            "wheel_rl": (["LR_WHEEL_"], "center"),
            "wheel_fr": (["RF_WHEEL_"], "center"),
            "wheel_rr": (["RR_WHEEL_"], "center"),
            "steering_wheel": (["Steering_Wheel_"], "center"),
        },
        # name -> (base color, metallic, roughness, emissive, alpha_mode)
        "materials": {
            "CBTruck_Base": ("CBTruck_Base.png", "CBTruck_Base_met.png", "CBTruck_Base_rog.png", None, "OPAQUE"),
            "CBTruck_Glass": ("CBTruck_Glass.tga", None, None, None, "BLEND"),
            "CBTruck_Interior": ("CBTruck_Interior.png", None, None, None, "OPAQUE"),
            "CBTruck_Lights1": ("CBTruck_Lights.png", None, None, "CBTruck_Lights_ilum3.png", "OPAQUE"),
            "CBTruck_wheels": ("CBTruck_wheels1.png", "CBTruck_Wheels_met.png", "CBTruck_Wheels_rog.png", None, "OPAQUE"),
        },
        # Canonical material names the viewer looks for (paint tint, light glow, glass).
        "rename_materials": {
            "CBTruck_Base": "paint",
            "CBTruck_Glass": "glass",
            "CBTruck_Interior": "interior",
            "CBTruck_Lights1": "lights",
            "CBTruck_wheels": "wheel",
        },
    },
}


def hinge_point(rule, lo, hi):
    c = (lo + hi) / 2
    if rule == "center":
        return c
    if rule == "rear_top":            # frunk lid hinges at the windscreen end
        return Vector((c.x, hi.y, hi.z))
    if rule == "bottom_rear":         # tailgate folds down about its lower edge
        return Vector((c.x, hi.y, lo.z))
    if rule == "front_edge_left":     # swings outward about its leading edge
        return Vector((hi.x, lo.y, c.z))
    if rule == "front_edge_right":
        return Vector((lo.x, lo.y, c.z))
    if rule == "front_top":           # tonneau retracts toward the cab
        return Vector((c.x, lo.y, hi.z))
    raise ValueError(rule)


def rebuild_material(mat, spec, base_dir):
    base, metal, rough, emissive, alpha_mode = spec
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    def tex(name, non_color=False):
        node = nt.nodes.new("ShaderNodeTexImage")
        node.image = bpy.data.images.load(f"{base_dir}/{name}", check_existing=True)
        if non_color:
            node.image.colorspace_settings.name = "Non-Color"
        return node

    b = tex(base)
    nt.links.new(b.outputs["Color"], bsdf.inputs["Base Color"])
    is_glass = alpha_mode == "BLEND"
    if is_glass:
        nt.links.new(b.outputs["Alpha"], bsdf.inputs["Alpha"])
        bsdf.inputs["Metallic"].default_value = 0.0
        bsdf.inputs["Roughness"].default_value = 0.04
    if metal:
        nt.links.new(tex(metal, True).outputs["Color"], bsdf.inputs["Metallic"])
    elif not is_glass:
        # The source marked every material fully metallic, which renders seats and
        # lenses as dark mirrors. Only the body and rims are metal.
        bsdf.inputs["Metallic"].default_value = 0.0
    if rough:
        nt.links.new(tex(rough, True).outputs["Color"], bsdf.inputs["Roughness"])
    elif not is_glass:
        bsdf.inputs["Roughness"].default_value = 0.6
    if emissive:
        e = tex(emissive)
        nt.links.new(e.outputs["Color"], bsdf.inputs["Emission Color"])
        # Exported as the emissive map at strength 1; the viewer scales
        # emissiveIntensity to switch the lights on and off.
        bsdf.inputs["Emission Strength"].default_value = 1.0
    if hasattr(mat, "surface_render_method"):
        mat.surface_render_method = "BLENDED" if is_glass else "DITHERED"
    mat.blend_method = "BLEND" if is_glass else "OPAQUE"


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True, choices=sorted(PROFILES))
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    prof = PROFILES[args.profile]
    base_dir = bpy.path.abspath("//").rstrip("/")

    for name, spec in prof["materials"].items():
        if name in bpy.data.materials:
            rebuild_material(bpy.data.materials[name], spec, base_dir)

    meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    # Bake every object's world transform into its vertices, then drop all
    # object-level transforms and parents. Source files arrive with chains like
    # object scale 0.001 x mesh scale 1817 plus stale parent-inverse matrices;
    # re-parenting through matrix_world inherits that mess (it put the doors
    # hundreds of metres away). With world-space vertices and identity objects
    # there is nothing left to go wrong.
    world = {o.name: o.matrix_world.copy() for o in meshes}
    # Source files often carry keyframed actions on every object. Evaluation
    # re-applies the keyed loc/scale over anything set here, so the transforms
    # below would silently revert. Drop all animation before touching them.
    for o in bpy.data.objects:
        o.animation_data_clear()
    for o in meshes:
        if o.data.users > 1:             # instanced meshes (the four brake discs)
            o.data = o.data.copy()
    for o in meshes:
        o.data.transform(world[o.name])
        o.parent = None
        # Assign, don't mutate: matrix_basis is computed from loc/rot/scale and
        # returns a copy, so `.identity()` on it silently does nothing.
        o.matrix_parent_inverse = Matrix.Identity(4)
        o.matrix_basis = Matrix.Identity(4)
        o.delta_location = (0, 0, 0)
        o.delta_rotation_euler = (0, 0, 0)
        o.delta_scale = (1, 1, 1)
    for o in [o for o in bpy.data.objects if o.type != "MESH"]:
        bpy.data.objects.remove(o, do_unlink=True)

    def data_bbox(objs):
        pts = [v.co for o in objs for v in o.data.vertices]
        lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
        hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
        return lo, hi

    # Real-world size, centred on x/y, wheels on z = 0.
    lo, hi = data_bbox(meshes)
    scale = prof["length_m"] / (hi.y - lo.y)
    centre = Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, lo.z))
    to_canonical = Matrix.Scale(scale, 4) @ Matrix.Translation(-centre)
    for o in meshes:
        o.data.transform(to_canonical)

    root = bpy.data.objects.new("vehicle", None)
    bpy.context.scene.collection.objects.link(root)
    claimed = set()
    for part, (prefixes, rule, *ref) in prof["parts"].items():
        objs = [o for o in meshes if any(o.name.startswith(p) for p in prefixes)]
        if not objs:
            print(f"WARN no objects for {part}")
            continue
        # Optional third field: only these meshes define the hinge geometry.
        ref_objs = [o for o in objs if ref and ref[0] in o.name] or objs
        hinge = hinge_point(rule, *data_bbox(ref_objs))
        pivot = bpy.data.objects.new(part, None)
        bpy.context.scene.collection.objects.link(pivot)
        pivot.parent = root
        pivot.location = hinge
        # Express the part's vertices relative to its hinge, so the mesh node
        # sits at identity under the pivot and rotating the pivot swings it.
        for o in objs:
            o.data.transform(Matrix.Translation(-hinge))
            o.parent = pivot
            claimed.add(o.name)
        print(f"PART {part:15s} hinge={[round(v, 3) for v in hinge]} meshes={len(objs)}")

    body = bpy.data.objects.new("body", None)
    bpy.context.scene.collection.objects.link(body)
    body.parent = root
    for o in meshes:
        if o.name not in claimed:
            o.parent = body

    for old, new in prof["rename_materials"].items():
        if old in bpy.data.materials:
            bpy.data.materials[old].name = new

    bpy.ops.export_scene.gltf(
        filepath=args.out,
        export_format="GLB",
        export_yup=True,
        export_apply=True,
        export_animations=False,
        export_cameras=False,
        export_lights=False,
        export_extras=False,
        export_image_format="AUTO",
    )
    print("EXPORTED", args.out, "scale", round(scale, 4))


main()
