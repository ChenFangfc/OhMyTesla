/* The vehicle, in 3D.
 *
 * One component renders whatever model the backend resolved for this car
 * (/api/vehicle/model) and keeps it in step with live state: doors, frunk,
 * tailgate, tonneau and charge port move; lights glow; sentry and charging
 * read as light on the ground.
 *
 * Model contract (see static/models/README.md): +Y up, front toward +Z, left
 * (driver) side +X, wheels on y = 0, metres. Moving parts are nodes named
 * door_fl/door_fr/door_rl/door_rr/frunk/tailgate/charge_port/tonneau/
 * wheel_fl/... whose origin is the hinge. A model without those nodes still
 * renders — it just doesn't open.
 *
 * Rendering is on demand: a frame is drawn only while something moves or the
 * user is dragging. A static scene costs nothing, which matters on a phone
 * left open on the dashboard.
 */
import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { MeshoptDecoder } from "three/addons/libs/meshopt_decoder.module.js";

// Opening poses, radians (or scale for the tonneau). Signs follow from the
// axis convention above: left-side parts swing toward +X about −Y, the frunk
// lifts its front edge about −X, the tailgate folds rearward about −X.
const POSE = {
  door_fl: { prop: "rotation", axis: "y", open: -1.05 },
  door_rl: { prop: "rotation", axis: "y", open: -1.05 },
  door_fr: { prop: "rotation", axis: "y", open: 1.05 },
  door_rr: { prop: "rotation", axis: "y", open: 1.05 },
  frunk: { prop: "rotation", axis: "x", open: -0.95 },
  tailgate: { prop: "rotation", axis: "x", open: -1.52 },
  charge_port: { prop: "rotation", axis: "y", open: -1.35 },
  tonneau: { prop: "scale", axis: "z", open: 0.06 },
};
const WHEELS = ["wheel_fl", "wheel_fr", "wheel_rl", "wheel_rr"];

// Camera presets: [azimuth°, elevation°, distance in vehicle lengths].
const VIEWS = {
  hero: [38, 14, 1.42],
  side: [90, 6, 1.32],
  front: [0, 10, 1.25],
  rear: [180, 16, 1.3],
  top: [60, 62, 1.35],
  charge: [118, 10, 0.95],
  bed: [165, 34, 1.15],
};

const ease = (t) => 1 - Math.pow(1 - t, 3);   // ease-out: motion starts immediately

export function webglAvailable() {
  try {
    const c = document.createElement("canvas");
    return !!(window.WebGL2RenderingContext && c.getContext("webgl2"));
  } catch (_) { return false; }
}

export class VehicleView {
  constructor(container, opts = {}) {
    this.el = container;
    this.opts = { interactive: true, autoRotate: false, onPick: null, ...opts };
    this.parts = {};          // canonical name -> Object3D (hinge node)
    this.anim = {};           // name -> {from, to, t0, dur}
    this.state = {};
    this.lightMats = [];
    this.paintMats = [];
    this._dirty = true;
    this._raf = 0;
    this._spin = 0;
    this._pulse = null;
  }

  async load(desc) {
    this.desc = desc;
    this._setupRenderer();
    const gltf = await this._loadGLTF(desc.url);
    this._installModel(gltf.scene, desc);
    this._frame(desc.view || "hero", true);
    this._observe();
    this.render();
    return this;
  }

  _setupRenderer() {
    const r = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "high-performance" });
    r.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    r.outputColorSpace = THREE.SRGBColorSpace;
    r.toneMapping = THREE.NeutralToneMapping;
    r.toneMappingExposure = 1.15;
    r.shadowMap.enabled = false;           // contact shadow is baked separately
    this.renderer = r;
    this.el.appendChild(r.domElement);
    r.domElement.classList.add("v3d-canvas");

    this.scene = new THREE.Scene();
    this.scene.environment = this._studioEnvironment(r);

    this.camera = new THREE.PerspectiveCamera(30, 1, 0.05, 200);

    if (this.opts.interactive) {
      const c = new OrbitControls(this.camera, r.domElement);
      c.enableDamping = true;
      c.dampingFactor = 0.08;
      c.enablePan = false;
      c.rotateSpeed = 0.6;
      c.zoomSpeed = 0.7;
      c.minPolarAngle = 0.25;
      c.maxPolarAngle = Math.PI / 2 - 0.04;   // never look up through the floor
      c.addEventListener("change", () => this.invalidate());
      c.addEventListener("start", () => { this._userDragging = true; this.el.classList.add("dragging"); });
      c.addEventListener("end", () => { this._userDragging = false; this.el.classList.remove("dragging"); });
      this.controls = c;
      this._bindPicking();
    }
    this._resize = () => this.resize();
    window.addEventListener("resize", this._resize);
    this.resize();
  }

  /* A dark room with three soft-box strips. Brushed stainless reads as the
   * room it reflects: the default "room" environment is a grey box, which
   * flattens the truck's planes into one tone. Softboxes give each facet its
   * own edge highlight, the way a studio shoot does. */
  _studioEnvironment(renderer) {
    const env = new THREE.Scene();
    // Gradient dome: bright ceiling, mid-grey horizon, dark floor. Mirror-like
    // steel shows mostly the horizon band, so that band sets its tone.
    const dome = new THREE.Mesh(
      new THREE.SphereGeometry(20, 48, 24),
      new THREE.ShaderMaterial({
        side: THREE.BackSide,
        depthWrite: false,
        vertexShader: "varying vec3 vP; void main(){ vP = normalize(position); gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }",
        fragmentShader: `varying vec3 vP;
          void main(){
            float y = vP.y;
            vec3 top = vec3(0.86, 0.88, 0.92), hor = vec3(0.46, 0.47, 0.50), bot = vec3(0.09, 0.09, 0.10);
            vec3 c = y > 0.0 ? mix(hor, top, smoothstep(0.0, 0.85, y)) : mix(hor, bot, smoothstep(0.0, 0.35, -y));
            gl_FragColor = vec4(c, 1.0);
          }`,
      }),
    );
    env.add(dome);
    const box = (w, h, x, y, z, ry, intensity, color = 0xffffff) => {
      const m = new THREE.Mesh(
        new THREE.PlaneGeometry(w, h),
        new THREE.MeshBasicMaterial({ color: new THREE.Color(color).multiplyScalar(intensity), side: THREE.DoubleSide }),
      );
      m.position.set(x, y, z);
      m.rotation.y = ry;
      env.add(m);
      return m;
    };
    // Soft boxes: one overhead, two long strips at the sides, warm/cool kickers.
    const top = box(9, 3.2, 0, 7, 0, 0, 4.2);
    top.rotation.x = Math.PI / 2;
    box(2.2, 6, -8, 3, 1, Math.PI / 2, 3.0);
    box(2.2, 6, 8, 3, -1, -Math.PI / 2, 2.4);
    box(10, 1.2, 0, 1.6, -9, 0, 1.6, 0xdfe6ff);
    box(10, 1.2, 0, 1.6, 9, Math.PI, 1.3, 0xfff1e0);
    const pm = new THREE.PMREMGenerator(renderer);
    const rt = pm.fromScene(env, 0.02);
    pm.dispose();
    return rt.texture;
  }

  _loadGLTF(url) {
    const loader = new GLTFLoader();
    loader.setMeshoptDecoder(MeshoptDecoder);
    return new Promise((res, rej) => loader.load(url, res, undefined, rej));
  }

  _installModel(root, desc) {
    const m = desc.materials || {};
    const isIn = (name, list) => (list || []).includes(name);
    const removals = [];

    root.traverse((o) => {
      if (POSE[o.name] || WHEELS.includes(o.name)) this.parts[o.name] = o;
      if (!o.isMesh) return;
      o.frustumCulled = false;
      const mats = Array.isArray(o.material) ? o.material : [o.material];
      for (const mat of mats) {
        const n = mat.name || "";
        if (isIn(n, m.paint)) { this.paintMats.push(mat); mat.envMapIntensity = 1.35; }
        if (isIn(n, m.lights)) { mat.userData.kind = "lights"; this.lightMats.push(mat); }
        if (isIn(n, m.headlights)) { mat.userData.kind = "head"; mat.emissive = new THREE.Color(0xf4f7ff); this.lightMats.push(mat); }
        if (isIn(n, m.taillights)) { mat.userData.kind = "tail"; mat.emissive = new THREE.Color(0xff2a2a); this.lightMats.push(mat); }
        if (isIn(n, m.glass)) {
          mat.transparent = true;
          mat.opacity = Math.min(mat.opacity ?? 1, 0.82);
          mat.roughness = 0.04;
          mat.metalness = 0;
          mat.color?.multiplyScalar(0.55);
          mat.depthWrite = false;
        }
        if (mat.isMeshStandardMaterial && !isIn(n, m.paint)) mat.envMapIntensity = 1;
      }
      // Stray geometry some exports carry (e.g. three loose polyhedra floating
      // above the roof of the CC-BY Cybertruck).
      if (desc.dropTinyMeshes && o.geometry?.attributes?.position?.count <= desc.dropTinyMeshes) removals.push(o);
    });
    removals.forEach((o) => o.parent && o.parent.remove(o));
    for (const mat of this.lightMats) mat.emissiveIntensity = 0;

    // A model without a tonneau node but with a separate bed-cover material can
    // still retract it: wrap those meshes in a hinge at their front edge.
    if (!this.parts.tonneau && m.tonneau) this._synthesizeTonneau(root, m.tonneau);

    if (desc.rotateY) root.rotation.y = desc.rotateY;
    // Normalise to real length and stand the wheels on y = 0.
    const box = new THREE.Box3().setFromObject(root);
    const size = box.getSize(new THREE.Vector3());
    const length = Math.max(size.x, size.z);
    const k = (desc.length_m || length) / length;
    root.scale.multiplyScalar(k);
    if (desc.widthScale) root.scale.x *= desc.widthScale;
    box.setFromObject(root);
    const c = box.getCenter(new THREE.Vector3());
    root.position.x -= c.x;
    root.position.z -= c.z;
    root.position.y -= box.min.y;

    for (const [name, node] of Object.entries(this.parts)) {
      node.userData.rest = { rx: node.rotation.x, ry: node.rotation.y, rz: node.rotation.z, sz: node.scale.z };
      node.userData.part = name;
    }
    this.model = root;
    this.scene.add(root);
    this.size = new THREE.Box3().setFromObject(root).getSize(new THREE.Vector3());
    this._addGround();
    this._addGlows();
  }

  _synthesizeTonneau(root, matNames) {
    const meshes = [];
    root.traverse((o) => {
      if (!o.isMesh) return;
      const mats = Array.isArray(o.material) ? o.material : [o.material];
      if (mats.some((mm) => matNames.includes(mm.name))) meshes.push(o);
    });
    if (!meshes.length) return;
    root.updateMatrixWorld(true);
    const box = new THREE.Box3();
    meshes.forEach((o) => box.expandByObject(o));
    const pivot = new THREE.Group();
    pivot.name = "tonneau";
    // front edge of the cover = its max z (front is +Z), at the top
    pivot.position.set((box.min.x + box.max.x) / 2, box.max.y, box.max.z);
    root.add(pivot);
    pivot.updateMatrixWorld(true);
    for (const o of meshes) pivot.attach(o);
    this.parts.tonneau = pivot;
  }

  /* Contact shadow: the floor darkens where the truck nearly touches it.
   * Rendered once from below into a small target and blurred — the cheap,
   * convincing version of ambient occlusion for one object on one plane. */
  _addGround() {
    const w = this.size.x * 1.9, d = this.size.z * 1.45;
    const rt = new THREE.WebGLRenderTarget(512, 512);
    const shadowCam = new THREE.OrthographicCamera(-w / 2, w / 2, d / 2, -d / 2, 0, 1.6);
    shadowCam.rotation.x = Math.PI / 2;     // look straight up from the floor
    shadowCam.position.y = 0;
    const depthMat = new THREE.MeshDepthMaterial();
    depthMat.onBeforeCompile = (shader) => {
      shader.fragmentShader = shader.fragmentShader.replace(
        "gl_FragColor = vec4( vec3( 1.0 - fragCoordZ ), opacity );",
        "gl_FragColor = vec4( vec3( 0.0 ), ( 1.0 - fragCoordZ ) * 1.6 );",
      );
    };
    const bg = this.scene.background;
    const env = this.scene.environment;
    this.scene.background = null;
    this.scene.environment = null;
    this.scene.overrideMaterial = depthMat;
    this.renderer.setRenderTarget(rt);
    this.renderer.setClearColor(0x000000, 0);
    this.renderer.clear();
    this.renderer.render(this.scene, shadowCam);
    this.scene.overrideMaterial = null;
    this.scene.background = bg;
    this.scene.environment = env;
    // two blur passes
    const blurred = this._blur(rt, 4.0);
    this._blur(blurred, 1.5, blurred);
    this.renderer.setRenderTarget(null);

    const plane = new THREE.Mesh(
      new THREE.PlaneGeometry(w, d).rotateX(-Math.PI / 2),
      new THREE.MeshBasicMaterial({ map: blurred.texture, transparent: true, opacity: 0.92, depthWrite: false }),
    );
    plane.scale.z = -1;  // the camera looked up, so the image is mirrored front-to-back
    plane.position.y = 0.002;
    plane.renderOrder = -1;
    this.scene.add(plane);
    this.ground = plane;
    rt.dispose();
  }

  _blur(src, amount, dst) {
    const size = 512;
    const tmp = new THREE.WebGLRenderTarget(size, size);
    const out = dst || new THREE.WebGLRenderTarget(size, size);
    const mat = new THREE.ShaderMaterial({
      uniforms: { tex: { value: null }, dir: { value: new THREE.Vector2() } },
      vertexShader: "varying vec2 vUv; void main(){ vUv = uv; gl_Position = vec4(position.xy, 0.0, 1.0); }",
      fragmentShader: `uniform sampler2D tex; uniform vec2 dir; varying vec2 vUv;
        void main(){ vec4 s = vec4(0.0);
          s += texture2D(tex, vUv - 4.0*dir) * 0.051; s += texture2D(tex, vUv - 3.0*dir) * 0.0918;
          s += texture2D(tex, vUv - 2.0*dir) * 0.12245; s += texture2D(tex, vUv - dir) * 0.1531;
          s += texture2D(tex, vUv) * 0.1633;
          s += texture2D(tex, vUv + dir) * 0.1531; s += texture2D(tex, vUv + 2.0*dir) * 0.12245;
          s += texture2D(tex, vUv + 3.0*dir) * 0.0918; s += texture2D(tex, vUv + 4.0*dir) * 0.051;
          gl_FragColor = s; }`,
      depthTest: false,
      transparent: true,
    });
    const quad = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), mat);
    const sc = new THREE.Scene();
    sc.add(quad);
    const cam = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);
    const pass = (from, to, dx, dy) => {
      mat.uniforms.tex.value = from.texture;
      mat.uniforms.dir.value.set(dx / size, dy / size);
      this.renderer.setRenderTarget(to);
      this.renderer.clear();
      this.renderer.render(sc, cam);
    };
    pass(src, tmp, amount, 0);
    pass(tmp, out, 0, amount);
    tmp.dispose();
    mat.dispose();
    return out;
  }

  /* State as light on the floor — charging green, sentry red, climate blue. */
  _addGlows() {
    const tex = (() => {
      const c = document.createElement("canvas");
      c.width = c.height = 128;
      const g = c.getContext("2d");
      const gr = g.createRadialGradient(64, 64, 0, 64, 64, 64);
      gr.addColorStop(0, "rgba(255,255,255,1)");
      gr.addColorStop(1, "rgba(255,255,255,0)");
      g.fillStyle = gr;
      g.fillRect(0, 0, 128, 128);
      const t = new THREE.CanvasTexture(c);
      t.colorSpace = THREE.SRGBColorSpace;
      return t;
    })();
    const make = (w, d) => {
      const m = new THREE.Mesh(
        new THREE.PlaneGeometry(w, d).rotateX(-Math.PI / 2),
        new THREE.MeshBasicMaterial({ map: tex, transparent: true, opacity: 0, depthWrite: false, blending: THREE.AdditiveBlending, color: 0xffffff }),
      );
      m.position.y = 0.004;
      m.renderOrder = 0;
      this.scene.add(m);
      return m;
    };
    this.glowFloor = make(this.size.x * 2.6, this.size.z * 1.5);
    // a tight pool under the charge port (left rear)
    this.glowPort = make(1.4, 1.4);
    this.glowPort.position.set(this.size.x * 0.62, 0.005, -this.size.z * 0.33);
  }

  resize() {
    const w = this.el.clientWidth || 1, h = this.el.clientHeight || 1;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    // Screen-space shift (fraction of width, + = car moves right) so overlays
    // on one side — the battery readout — don't sit on top of the car.
    const val = (o) => (typeof o === "function" ? o(w, h) : (o || 0));
    const sx = val(this.opts.shiftX), sy = val(this.opts.shiftY);
    if (sx || sy) this.camera.setViewOffset(w, h, -sx * w, sy * h, w, h);
    else this.camera.clearViewOffset();
    this.camera.updateProjectionMatrix();
    this.invalidate();
  }

  _frame(view, instant) {
    const [az, el, dist] = VIEWS[view] || VIEWS.hero;
    const L = this.size ? this.size.z : 5.7;
    const target = new THREE.Vector3(0, (this.size ? this.size.y : 1.8) * 0.42, 0);
    // Portrait screens are width-limited: pull back until the length fits.
    const r = L * dist * (this.camera.aspect < 1 ? Math.min(3.2, 1.42 / Math.max(0.42, this.camera.aspect)) : 1);
    const a = THREE.MathUtils.degToRad(az), e = THREE.MathUtils.degToRad(el);
    const pos = new THREE.Vector3(
      r * Math.cos(e) * Math.sin(a), target.y + r * Math.sin(e), r * Math.cos(e) * Math.cos(a));
    if (this.controls) {
      this.controls.minDistance = L * 0.65;
      this.controls.maxDistance = L * 2.6;
    }
    if (instant) {
      this.camera.position.copy(pos);
      this.controls?.target.copy(target);
      this.camera.lookAt(target);
      this.controls?.update();
    } else {
      this._camTween = { from: this.camera.position.clone(), to: pos, tFrom: (this.controls?.target || target).clone(), tTo: target, t0: performance.now(), dur: 900 };
    }
    this.invalidate();
  }

  view(name) { this._frame(name, false); }

  /* Live state in, motion out. Unknown keys are ignored; missing keys keep
   * their last value, so callers can send partial updates. */
  setState(s) {
    const prev = this.state;
    this.state = { ...prev, ...s };
    const st = this.state;
    const want = {
      door_fl: !!st.door_fl, door_fr: !!st.door_fr, door_rl: !!st.door_rl, door_rr: !!st.door_rr,
      frunk: !!st.frunk, tailgate: !!st.tailgate, charge_port: !!st.charge_port,
      tonneau: st.tonneau == null ? 0 : st.tonneau,       // 0 closed .. 1 open
    };
    for (const [name, v] of Object.entries(want)) {
      if (!this.parts[name]) continue;
      const target = typeof v === "number" ? v : v ? 1 : 0;
      const cur = this.parts[name].userData.open ?? 0;
      if (Math.abs(cur - target) > 1e-3) this._tween(name, cur, target, name === "tonneau" ? 1600 : 900);
    }
    this._paintLights();
    this._paintGlows();
    if (st.paint && st.paint !== prev.paint) this.setPaint(st.paint);
    this.invalidate();
  }

  setPaint(hex) {
    const c = new THREE.Color(hex);
    for (const m of this.paintMats) m.color.copy(c);
    this.invalidate();
  }

  flash() {
    // Lights flash twice — the visible echo of the "flash lights" command.
    this._flashUntil = performance.now() + 1400;
    this.invalidate();
  }

  _tween(name, from, to, dur) {
    this.anim[name] = { from, to, t0: performance.now(), dur };
    this.invalidate();
  }

  _applyPart(name, v) {
    const node = this.parts[name], p = POSE[name];
    if (!node || !p) return;
    node.userData.open = v;
    const rest = node.userData.rest;
    if (p.prop === "rotation") {
      const base = { x: rest.rx, y: rest.ry, z: rest.rz }[p.axis];
      node.rotation[p.axis] = base + p.open * v;
    } else {
      node.scale.z = rest.sz * (1 - (1 - p.open) * v);
    }
  }

  _paintLights() {
    const st = this.state;
    const now = performance.now();
    let k = st.awake ? 0.45 : 0;
    if (st.lights) k = 2.2;
    if (this._flashUntil && now < this._flashUntil) {
      const phase = ((this._flashUntil - now) / 350) | 0;
      k = phase % 2 ? 2.6 : 0.1;
    }
    for (const m of this.lightMats) m.emissiveIntensity = k;
  }

  _paintGlows() {
    const st = this.state;
    const sentryHot = st.sentry === "aware" || st.sentry === "panic";
    let color = null, base = 0;
    if (sentryHot) { color = 0xff2b2b; base = st.sentry === "panic" ? 0.9 : 0.6; }
    else if (st.charging) { color = 0x30d158; base = 0.4; }
    else if (st.sentry === "armed") { color = 0xff3b30; base = 0.18; }
    else if (st.climate) { color = 0x3e6ae1; base = 0.32; }
    if (this.glowFloor) {
      if (color != null) this.glowFloor.material.color.set(color);
      this.glowFloor.userData.base = color == null ? 0 : base;
      this.glowFloor.userData.pulse = sentryHot || st.charging;
    }
    if (this.glowPort) {
      this.glowPort.material.color.set(0x30d158);
      this.glowPort.userData.base = st.charging ? 0.9 : 0;
      this.glowPort.userData.pulse = !!st.charging;
    }
  }

  invalidate() {
    this._dirty = true;
    if (!this._raf && this._visible !== false) this._raf = requestAnimationFrame((t) => this._tick(t));
  }

  _tick(now) {
    this._raf = 0;
    let busy = false;

    for (const [name, a] of Object.entries(this.anim)) {
      const t = Math.min(1, (now - a.t0) / a.dur);
      this._applyPart(name, a.from + (a.to - a.from) * ease(t));
      if (t < 1) busy = true; else delete this.anim[name];
    }

    if (this._camTween) {
      const c = this._camTween;
      const t = Math.min(1, (now - c.t0) / c.dur), e = ease(t);
      this.camera.position.lerpVectors(c.from, c.to, e);
      this.controls?.target.lerpVectors(c.tFrom, c.tTo, e);
      if (t >= 1) this._camTween = null; else busy = true;
    }

    const speed = this.state.speed_mps || 0;
    if (speed > 0.1) {
      const dt = this._lastNow ? (now - this._lastNow) / 1000 : 0;
      this._spin += (speed / 0.44) * dt;    // ω = v / r, r ≈ 0.44 m
      for (const w of WHEELS) if (this.parts[w]) this.parts[w].rotation.x = this._spin;
      busy = true;
    }

    // glows breathe while something is ongoing
    for (const g of [this.glowFloor, this.glowPort]) {
      if (!g) continue;
      const base = g.userData.base || 0;
      let op = base;
      if (g.userData.pulse && base > 0 && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
        op = base * (0.62 + 0.38 * Math.sin(now / 520));
        busy = true;
      }
      const cur = g.material.opacity;
      g.material.opacity = cur + (op - cur) * 0.12;
      if (Math.abs(g.material.opacity - op) > 0.003) busy = true;
    }

    if (this._flashUntil && now < this._flashUntil + 50) { this._paintLights(); busy = true; }

    if (this.opts.autoRotate && !this._userDragging && this.controls) {
      this.controls.autoRotate = true;
      this.controls.autoRotateSpeed = 0.35;
      busy = true;
    }
    if (this.controls) busy = this.controls.update() || busy || this._userDragging;

    this._lastNow = now;
    this.renderer.render(this.scene, this.camera);
    this._dirty = false;
    if (busy) this.invalidate();
  }

  render() { this.invalidate(); }

  /* Taps on the body resolve to the moving part under the finger, so the
   * truck itself is the control surface — tap the frunk to open the frunk. */
  _bindPicking() {
    const ray = new THREE.Raycaster();
    const ndc = new THREE.Vector2();
    let down = null;
    const el = this.renderer.domElement;
    el.addEventListener("pointerdown", (e) => { down = { x: e.clientX, y: e.clientY, t: performance.now() }; });
    el.addEventListener("pointerup", (e) => {
      if (!down || !this.opts.onPick) return;
      const moved = Math.hypot(e.clientX - down.x, e.clientY - down.y);
      const quick = performance.now() - down.t < 350;
      down = null;
      if (moved > 6 || !quick) return;           // that was a drag, not a tap
      const r = el.getBoundingClientRect();
      ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
      ray.setFromCamera(ndc, this.camera);
      const hit = ray.intersectObject(this.model, true)[0];
      if (!hit) return;
      let o = hit.object;
      while (o && !o.userData.part) o = o.parent;
      this.opts.onPick(o ? o.userData.part : "body", hit.point);
    });
  }

  _observe() {
    this._io = new IntersectionObserver((entries) => {
      this._visible = entries[0].isIntersecting;
      if (this._visible) this.invalidate();
    });
    this._io.observe(this.el);
    this._onVis = () => { this._visible = !document.hidden; if (this._visible) this.invalidate(); };
    document.addEventListener("visibilitychange", this._onVis);
  }

  /* Project a part's hinge to screen space — for floating labels (tyre
   * pressures at each wheel, "open" chips over the frunk). */
  screenPoint(name, offset = [0, 0, 0]) {
    const node = this.parts[name];
    if (!node) return null;
    const v = new THREE.Vector3(...offset);
    node.localToWorld(v);
    v.project(this.camera);
    const r = this.renderer.domElement.getBoundingClientRect();
    return { x: (v.x + 1) / 2 * r.width, y: (1 - v.y) / 2 * r.height, behind: v.z > 1 };
  }

  has(part) { return !!this.parts[part]; }

  dispose() {
    cancelAnimationFrame(this._raf);
    window.removeEventListener("resize", this._resize);
    document.removeEventListener("visibilitychange", this._onVis);
    this._io?.disconnect();
    this.controls?.dispose();
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }
}
