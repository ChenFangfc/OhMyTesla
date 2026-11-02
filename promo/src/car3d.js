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
renderer.setPixelRatio(Number(q.get("pr")) || 1.5);
renderer.setSize(W, H, false);
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;

const scene = new THREE.Scene();

// A black studio with a few long softboxes: stainless steel is a mirror, so
// it looks like whatever it reflects. Dark room → dark body with sharp light
// lines along its edges, the look of a car launch. Turning this environment
// (view.env) slides those lines over the body.
function studio() {
  const room = new THREE.Scene();
  room.background = new THREE.Color(0x000000);
  const box = (w, h, x, y, z, ry, rx, color, k) => {
    const m = new THREE.Mesh(new THREE.PlaneGeometry(w, h),
      new THREE.MeshBasicMaterial({ color: new THREE.Color(color).multiplyScalar(k), side: THREE.DoubleSide }));
    m.position.set(x, y, z);
    m.rotation.set(rx, ry, 0);
    room.add(m);
  };
  box(10, 1.6, 0, 7, 0, 0, Math.PI / 2, 0xffffff, 3.2);       // overhead strip
  box(0.7, 5.5, 8, 2.2, 1, -Math.PI / 2, 0, 0xf2f5ff, 2.6);   // right strip
  box(0.7, 5.5, -8, 2.2, -1, Math.PI / 2, 0, 0xf2f5ff, 2.0);  // left strip
  box(8, 0.5, 0, 0.9, 9, Math.PI, 0, 0xffffff, 1.6);          // low front strip
  box(9, 1.4, 0, 2.4, -9, 0, 0, 0xff2440, 2.4);               // red rim behind
  box(24, 10, 0, 3, 13, Math.PI, 0, 0x8a919c, 0.22);          // dim fill, so the sides read
  box(24, 10, 13, 3, 0, -Math.PI / 2, 0, 0x8a919c, 0.3);
  const pm = new THREE.PMREMGenerator(renderer);
  const tex = pm.fromScene(room, 0).texture;
  pm.dispose();
  return tex;
}
scene.environment = studio();

const camera = new THREE.PerspectiveCamera(28, W / H, 0.1, 200);

// A key light from above the front, a red rim from behind, and a small bright
// light that travels along the body: the "sweep" that reveals the steel.
const key = new THREE.SpotLight(0xffffff, 0, 30, Math.PI / 6, 0.7, 1.4);
key.position.set(3.5, 7, 6);
key.castShadow = true;
key.shadow.mapSize.set(2048, 2048);
key.shadow.bias = -0.0004;
key.shadow.radius = 6;
const rim = new THREE.DirectionalLight(0xffffff, 0);
rim.position.set(-4, 5, -7);
const sweep = new THREE.PointLight(0xffffff, 0, 2.2, 2);   // short reach: the body, never the cabin
scene.add(key, key.target, rim, sweep);

// The floor: a soft pool of light that fades out in every direction, so the
// truck stands on something without a visible edge.
function radial(size = 512) {
  const c = document.createElement("canvas");
  c.width = c.height = size;
  const g = c.getContext("2d");
  const grd = g.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  grd.addColorStop(0, "#ffffff");
  grd.addColorStop(0.35, "#9a9a9a");
  grd.addColorStop(0.8, "#1a1a1a");
  grd.addColorStop(1, "#000000");
  g.fillStyle = grd;
  g.fillRect(0, 0, size, size);
  return new THREE.CanvasTexture(c);
}
const floor = new THREE.Mesh(
  new THREE.PlaneGeometry(26, 26).rotateX(-Math.PI / 2),
  new THREE.MeshStandardMaterial({ color: 0x0d0e10, roughness: 0.5, metalness: 0.2, envMapIntensity: 0.12,
                                   transparent: true, alphaMap: radial() }));
floor.receiveShadow = true;
scene.add(floor);

const heads = [], tails = [];
let bar = new THREE.Vector3(0, 1.1, 2.9);

const ready = new Promise((resolve, reject) => {
  const loader = new GLTFLoader();
  loader.setMeshoptDecoder(MeshoptDecoder);
  loader.load("assets/cybertruck.glb", (gltf) => {
    const root = gltf.scene;
    root.traverse((o) => {
      if (!o.isMesh) return;
      o.castShadow = true;
      o.frustumCulled = false;
      const mats = Array.isArray(o.material) ? o.material : [o.material];
      for (const m of mats) {
        const n = m.name || "";
        if (n === "exterior_steel" || n === "bedcover") {
          m.color = new THREE.Color(0xc9cdd3); m.metalness = 1; m.roughness = 0.2; m.envMapIntensity = 1;
        } else if (n === "glass") {
          m.transparent = true; m.opacity = 0.88; m.color = new THREE.Color(0x0b0d10);
          m.metalness = 0; m.roughness = 0.04; m.depthWrite = false;
        } else if (n === "headlight") {
          m.emissive = new THREE.Color(0xf2f6ff); m.emissiveIntensity = 0; heads.push(m);
        } else if (n === "taillight") {
          m.emissive = new THREE.Color(0xff1f35); m.emissiveIntensity = 0; tails.push(m);
        } else if (n === "tire") {
          m.color = new THREE.Color(0x0c0d0f); m.roughness = 0.9; m.metalness = 0; m.envMapIntensity = 0.25;
        } else if (n === "rim") {
          m.color = new THREE.Color(0x2a2d31); m.roughness = 0.45; m.metalness = 0.8;
        }
      }
    });
    // stand the wheels on the floor, centred under the camera's target
    const box = new THREE.Box3().setFromObject(root);
    const c = box.getCenter(new THREE.Vector3());
    root.position.x -= c.x;
    root.position.z -= c.z;
    root.position.y -= box.min.y;
    scene.add(root);
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

/* view: { cam:[x,y,z], at:[x,y,z], fov, env (rad), envI, key, rim, sweep:[x,y,z], sweepI, lights, exposure }
   Returns where the light bar lands on the 1920×1080 stage, for the CSS glow. */
function render(v) {
  camera.position.copy(v3(v.cam));
  camera.fov = v.fov || 28;
  camera.updateProjectionMatrix();
  camera.lookAt(v3(v.at || [0, 0.9, 0]));
  scene.environmentRotation.set(0, v.env || 0, 0);
  scene.environmentIntensity = v.envI ?? 1;
  renderer.toneMappingExposure = v.exposure ?? 1;
  key.intensity = 140 * (v.key ?? 1);
  key.target.position.set(0, 0.6, 0);
  rim.intensity = 1.2 * (v.rim ?? 1);
  sweep.position.copy(v3(v.sweep || [0, 2.4, 3.2]));
  sweep.intensity = 30 * (v.sweepI ?? 0);
  for (const m of heads) m.emissiveIntensity = 4.5 * (v.lights ?? 0);
  for (const m of tails) m.emissiveIntensity = 3 * (v.lights ?? 0);
  renderer.render(scene, camera);
  const p = bar.clone().project(camera);
  return { bar: { x: (p.x + 1) / 2 * W, y: (1 - p.y) / 2 * H, front: p.z < 1 } };
}

window.CAR3D = { ready, render, canvas };
window.dispatchEvent(new Event("car3d"));
