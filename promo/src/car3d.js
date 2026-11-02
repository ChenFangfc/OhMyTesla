/* car3d.js — the truck on the film's dark stage.
 *
 * The model is the one the app ships: "Tesla Cybertruck 2025" by Nieve5677,
 * CC BY 4.0 (services/hub/app/web/static/models/CREDITS.md). Never point this
 * at an owner's private model: these frames are published.
 *
 * Like the rest of the film, a frame is a pure function of its inputs:
 * CAR3D.render(view) sets camera, lights and the light bar from numbers alone.
 * Model contract (as in the app): +Y up, front toward +Z, driver side +X,
 * wheels on y = 0, metres.
 *
 * The look: brushed stainless under large soft lights, so flat panels carry
 * broad gradients instead of mirror-black; a dim horizon band for the lower
 * body to reflect; a dark glossy floor that mirrors the truck faintly and
 * holds its contact shadow.
 */
import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { MeshoptDecoder } from "three/addons/libs/meshopt_decoder.module.js";

const W = 1920, H = 1080;
const q = new URLSearchParams(location.search);
const canvas = document.createElement("canvas");
canvas.className = "car3d";
document.getElementById("stage").prepend(canvas);

const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, preserveDrawingBuffer: true });
renderer.setPixelRatio(Number(q.get("pr")) || 1.5);          // render.py passes its pixel density: the truck at full output size
renderer.setSize(W, H, false);
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;

const scene = new THREE.Scene();

// Soft light: every source fades at its edges, so steel shows gradients, not flat patches.
function grad(kind, stops) {
  const c = document.createElement("canvas");
  c.width = c.height = 256;
  const g = c.getContext("2d");
  const grd = kind === "radial" ? g.createRadialGradient(128, 128, 0, 128, 128, 128) : g.createLinearGradient(0, 0, 0, 256);
  for (const [at, col] of stops) grd.addColorStop(at, col);
  g.fillStyle = grd;
  g.fillRect(0, 0, 256, 256);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

function studio() {
  // Mid-grey surroundings, so the steel reads silver as it does in daylight;
  // soft boxes on top for the gradients; a darker floor for the lower body.
  // None of this is drawn: the stage behind the truck stays dark.
  const room = new THREE.Scene();
  const GREY = new THREE.Color(0.2, 0.205, 0.215);           // linear, about 50% in sRGB
  room.background = GREY;
  const soft = grad("radial", [[0, "#ffffff"], [0.55, "#bdbdbd"], [1, "#7c7e82"]]);
  const tall = grad("linear", [[0, "#ffffff"], [0.6, "#b0b0b0"], [1, "#7c7e82"]]);
  const lit = (geo, map, color, k, pos, rot) => {
    const m = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ map, color: new THREE.Color(color).multiplyScalar(k), side: THREE.DoubleSide }));
    m.position.set(...pos);
    m.rotation.set(...rot);
    room.add(m);
  };
  lit(new THREE.PlaneGeometry(12, 6), soft, 0xffffff, 1.6, [0, 8, 0], [Math.PI / 2, 0, 0]);          // overhead
  lit(new THREE.PlaneGeometry(9, 5), tall, 0xf3f5ff, 1.9, [9, 2.2, 1.5], [0, -Math.PI / 2, 0]);      // right
  lit(new THREE.PlaneGeometry(9, 5), tall, 0xf3f5ff, 1.3, [-9, 2.2, -1.5], [0, Math.PI / 2, 0]);     // left
  lit(new THREE.PlaneGeometry(10, 1.2), soft, 0xffffff, 2.4, [0, 2.8, -10], [0, 0, 0]);              // rim, behind
  lit(new THREE.PlaneGeometry(10, 1.2), soft, 0xffffff, 1.4, [0, 1.2, 10], [0, Math.PI, 0]);         // low front
  const ground = new THREE.Mesh(new THREE.PlaneGeometry(60, 60).rotateX(-Math.PI / 2),
    new THREE.MeshBasicMaterial({ color: new THREE.Color(0.012, 0.012, 0.014), side: THREE.DoubleSide }));
  ground.position.y = -0.2;
  room.add(ground);
  const pm = new THREE.PMREMGenerator(renderer);
  const tex = pm.fromScene(room, 0.02).texture;
  pm.dispose();
  return tex;
}
scene.environment = studio();

const camera = new THREE.PerspectiveCamera(28, W / H, 0.1, 200);

// One light from above for the contact shadow; the studio does the rest.
const top = new THREE.DirectionalLight(0xffffff, 0);
top.position.set(1.2, 10, 2.5);
top.castShadow = true;
top.shadow.mapSize.set(4096, 4096);
Object.assign(top.shadow.camera, { left: -5, right: 5, top: 5, bottom: -5, near: 1, far: 20 });
top.shadow.bias = -0.0005;
const sweep = new THREE.PointLight(0xffffff, 0, 2.2, 2);   // short reach: the body, never the cabin
scene.add(top, top.target, sweep);

function radial(stops, size = 512) {
  const c = document.createElement("canvas");
  c.width = c.height = size;
  const g = c.getContext("2d");
  const grd = g.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  for (const [at, col] of stops) grd.addColorStop(at, col);
  g.fillStyle = grd;
  g.fillRect(0, 0, size, size);
  return new THREE.CanvasTexture(c);
}
// The floor: dark and slightly see-through near the truck (the mirrored copy
// below shows as a faint reflection), fading out at the edges into the page.
const floor = new THREE.Mesh(new THREE.PlaneGeometry(30, 30).rotateX(-Math.PI / 2),
  new THREE.MeshBasicMaterial({ color: 0x0b0c0e, transparent: true, depthWrite: false,
    alphaMap: radial([[0, "#c8c8c8"], [0.22, "#e2e2e2"], [0.5, "#f4f4f4"], [0.8, "#808080"], [1, "#000000"]]) }));
floor.renderOrder = 1;
// a soft pool of light under the truck, so its dark underside separates from the floor
const pool = new THREE.Mesh(new THREE.PlaneGeometry(16, 12).rotateX(-Math.PI / 2),
  new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    opacity: 0.07, alphaMap: radial([[0, "#ffffff"], [0.5, "#5a5a5a"], [1, "#000000"]]) }));
pool.position.y = 0.002;
pool.renderOrder = 2;
const shade = new THREE.Mesh(new THREE.PlaneGeometry(14, 14).rotateX(-Math.PI / 2), new THREE.ShadowMaterial({ opacity: 0.6 }));
shade.position.y = 0.003;
shade.receiveShadow = true;
shade.renderOrder = 3;
scene.add(floor, pool, shade);

const heads = [], tails = [];
let bar = new THREE.Vector3(0, 1.1, 2.9);

function finish(m) {
  const n = m.name || "";
  if (n === "exterior_steel" || n === "bedcover") {
    m.color = new THREE.Color(0xc8cdd3); m.metalness = 1; m.roughness = 0.3; m.envMapIntensity = 1;
  } else if (n === "glass") {
    // dark tinted glass: it reflects the studio, it does not show the cabin
    m.transparent = false; m.opacity = 1; m.color = new THREE.Color(0x050608);
    m.metalness = 0; m.roughness = 0.04; m.envMapIntensity = 1.3;
  } else if (n === "headlight") {
    m.emissive = new THREE.Color(0xf2f6ff); m.emissiveIntensity = 0; heads.push(m);
  } else if (n === "taillight") {
    m.emissive = new THREE.Color(0xff1f35); m.emissiveIntensity = 0; tails.push(m);
  } else if (n === "tire") {
    m.color = new THREE.Color(0x0c0d0f); m.roughness = 0.9; m.metalness = 0; m.envMapIntensity = 0.3;
  } else if (n === "rim") {
    m.color = new THREE.Color(0x2b2e33); m.roughness = 0.4; m.metalness = 0.85;
  }
}

/* The model groups faces by material, so small parts hide inside big meshes.
   Split a mesh into connected parts (vertices merged by position) and call
   `pick(box)` on each: "drop" removes the part, "red" moves it to a second
   material. Positions are in the truck's own frame (wheels on y = 0). */
const REFLECTOR = new THREE.MeshStandardMaterial({ color: 0x9a0f1d, metalness: 0, roughness: 0.25, envMapIntensity: 1.2 });
function regroup(o, pick) {
  const g = o.geometry, pos = g.attributes.position;
  const index = g.index ? Array.from(g.index.array) : [...Array(pos.count).keys()];
  const v = new THREE.Vector3(), ids = new Int32Array(pos.count), seen = new Map(), parent = [];
  const find = (x) => { while (parent[x] !== x) { parent[x] = parent[parent[x]]; x = parent[x]; } return x; };
  for (let i = 0; i < pos.count; i++) {
    v.fromBufferAttribute(pos, i).applyMatrix4(o.matrixWorld);
    const k = `${v.x.toFixed(3)},${v.y.toFixed(3)},${v.z.toFixed(3)}`;
    if (!seen.has(k)) { seen.set(k, parent.length); parent.push(parent.length); }
    ids[i] = seen.get(k);
  }
  const tris = index.length / 3;
  for (let t = 0; t < tris; t++) {
    const a = find(ids[index[3 * t]]);
    parent[find(ids[index[3 * t + 1]])] = a;
    parent[find(ids[index[3 * t + 2]])] = a;
  }
  const boxes = new Map();
  for (let t = 0; t < tris; t++) {
    const r = find(ids[index[3 * t]]);
    if (!boxes.has(r)) boxes.set(r, new THREE.Box3());
    for (let j = 0; j < 3; j++) boxes.get(r).expandByPoint(v.fromBufferAttribute(pos, index[3 * t + j]).applyMatrix4(o.matrixWorld));
  }
  const verdict = new Map([...boxes].map(([r, b]) => [r, pick(b)]));
  const keep = [], red = [];
  for (let t = 0; t < tris; t++) {
    const what = verdict.get(find(ids[index[3 * t]]));
    if (what === "drop") continue;
    (what === "red" ? red : keep).push(index[3 * t], index[3 * t + 1], index[3 * t + 2]);
  }
  g.setIndex([...keep, ...red]);
  g.clearGroups();
  g.addGroup(0, keep.length, 0);
  if (red.length) {
    g.addGroup(keep.length, red.length, 1);
    o.material = [[].concat(o.material)[0], REFLECTOR];
  }
}
const inside = (b, [x0, x1], [y0, y1], [z0, z1]) =>
  b.min.x >= x0 && b.max.x <= x1 && b.min.y >= y0 && b.max.y <= y1 && b.min.z >= z0 && b.max.z <= z1;

const ready = new Promise((resolve, reject) => {
  const loader = new GLTFLoader();
  loader.setMeshoptDecoder(MeshoptDecoder);
  loader.load("assets/cybertruck.glb", (gltf) => {
    const root = gltf.scene;
    const seen = new Set();
    root.traverse((o) => {
      if (!o.isMesh) return;
      o.castShadow = true;
      o.frustumCulled = false;
      for (const m of [].concat(o.material)) if (!seen.has(m)) { seen.add(m); finish(m); }
    });
    // stand the wheels on the floor, centred under the camera's target
    const box = new THREE.Box3().setFromObject(root);
    const c = box.getCenter(new THREE.Vector3());
    root.position.set(-c.x, -box.min.y, -c.z);
    root.updateMatrixWorld(true);
    root.traverse((o) => {
      if (!o.isMesh) return;
      const n = [].concat(o.material)[0].name;
      // the front number-plate frame, its plate and two bolts: not part of the truck's design
      if (n === "trim") regroup(o, (b) => (inside(b, [-0.3, 0.3], [0.49, 0.64], [2.72, 2.9]) ? "drop" : "keep"));
      // the rear side reflectors, modelled in steel: red, and lit only by what they reflect
      if (n === "exterior_steel") regroup(o, (b) => (inside(b, [-0.95, -0.78], [0.58, 0.72], [-2.56, -2.2]) ||
                                                      inside(b, [0.78, 0.95], [0.58, 0.72], [-2.56, -2.2]) ? "red" : "keep"));
    });
    scene.add(root);
    // the reflection: the same truck upside down under the floor
    const mirror = root.clone(true);
    mirror.scale.y = -1;
    mirror.position.y = box.min.y;
    mirror.traverse((o) => { if (o.isMesh) o.castShadow = false; });
    scene.add(mirror);
    // where the light bar is, for the glow drawn over it in CSS
    const hb = new THREE.Box3();
    root.updateMatrixWorld(true);
    root.traverse((o) => {
      if (o.isMesh && [].concat(o.material).some((m) => m.name === "headlight")) hb.expandByObject(o);
    });
    if (!hb.isEmpty()) bar = hb.getCenter(new THREE.Vector3());
    resolve();
  }, undefined, reject);
});

const v3 = (a) => new THREE.Vector3(a[0], a[1], a[2]);
const toStage = (p) => {
  const s = p.clone().project(camera);
  return { x: (s.x + 1) / 2 * W, y: (1 - s.y) / 2 * H, front: s.z < 1 };
};

/* view: { cam, at, fov, env (rad), envI, top, sweep, sweepI, lights, exposure, pts: [[x,y,z], …] }
   Returns where the light bar and each of `pts` land on the 1920×1080 stage. */
function render(v) {
  camera.position.copy(v3(v.cam));
  camera.fov = v.fov || 28;
  camera.updateProjectionMatrix();
  camera.lookAt(v3(v.at || [0, 0.9, 0]));
  scene.environmentRotation.set(0, v.env || 0, 0);
  scene.environmentIntensity = v.envI ?? 1;
  renderer.toneMappingExposure = v.exposure ?? 1;
  top.intensity = 1.6 * (v.top ?? 1);
  shade.material.opacity = 0.6 * Math.min(1, v.top ?? 1);
  pool.material.opacity = 0.07 * (v.envI ?? 1);
  sweep.position.copy(v3(v.sweep || [2.1, 1.5, 3.2]));
  sweep.intensity = 30 * (v.sweepI ?? 0);
  for (const m of heads) m.emissiveIntensity = 4.5 * (v.lights ?? 0);
  for (const m of tails) m.emissiveIntensity = 3 * (v.lights ?? 0);
  renderer.render(scene, camera);
  return { bar: toStage(bar), pts: (v.pts || []).map((p) => toStage(v3(p))) };
}

window.CAR3D = { ready, render, canvas };
window.dispatchEvent(new Event("car3d"));
