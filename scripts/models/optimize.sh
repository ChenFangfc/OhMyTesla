#!/usr/bin/env bash
# Compress a rigged vehicle GLB for the web viewer.
#
#   scripts/models/optimize.sh in.glb out.glb
#
# 4K textures and raw float geometry make a ~25 MB file, which is a long wait on a
# phone over LAN. 2K WebP is indistinguishable at the sizes the viewer renders,
# and meshopt is lossless for geometry (the viewer bundles its decoder).
#
# `--flatten false` / `--join false` matter: flattening or joining would merge
# the hinge nodes the viewer animates into the body, and doors would stop opening.
set -euo pipefail
in="${1:?usage: optimize.sh in.glb out.glb}"
out="${2:?usage: optimize.sh in.glb out.glb}"
npx --yes @gltf-transform/cli@4 optimize "$in" "$out" \
  --compress meshopt \
  --texture-compress webp \
  --texture-size 2048 \
  --flatten false \
  --join false \
  --instance false \
  --palette false \
  --simplify false
ls -lh "$out"
