// PiForge GUI — three.js viewport: Z-up camera + orbit controls, mm grid, view gizmo, standard
// views, fit, section plane, measure tool, picking. Renders on demand (dirty flag).
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { ViewHelper } from 'three/addons/helpers/ViewHelper.js';
import { CSS2DRenderer, CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';

const VIEW_DIRS = {
  iso: [1, -1.2, 0.95], front: [0, -1, 0], back: [0, 1, 0], left: [-1, 0, 0], right: [1, 0, 0],
  top: [0, -1e-3, 1], bottom: [0, 1e-3, -1], under: [1, -1.3, -0.55],
};
const GIZMO_VIEWS = { posX: 'right', negX: 'left', posY: 'back', negY: 'front', posZ: 'top', negZ: 'bottom' };
const AXES = { x: new THREE.Vector3(1, 0, 0), y: new THREE.Vector3(0, 1, 0), z: new THREE.Vector3(0, 0, 1) };

/** Colours per GUI theme. */
export const THEMES = {
  dark: { gridMinor: 0x252d38, gridMajor: 0x354152, axisX: 0xb54a46, axisY: 0x4a8f52, edge: 0x05070a,
    edgeOpacity: 0.5, bed: 0x262d37, bedLine: 0x3a4554, accent: 0x4c8dff, heatBase: 0xb8c0cc },
  light: { gridMinor: 0xdde2e9, gridMajor: 0xbcc6d2, axisX: 0xd0453f, axisY: 0x3f9a4c, edge: 0x1d2530,
    edgeOpacity: 0.32, bed: 0xd5dbe3, bedLine: 0xaab4c0, accent: 0x2f6fe4, heatBase: 0xd1d7df },
};

function gridLines(extent, step, color, opacity) {
  const half = extent / 2;
  const pts = [];
  for (let v = -half; v <= half + 1e-6; v += step) pts.push(-half, v, 0, half, v, 0, v, -half, 0, v, half, 0);
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(pts, 3));
  return new THREE.LineSegments(g, new THREE.LineBasicMaterial({ color, transparent: true, opacity, depthWrite: false }));
}

function disposeTree(obj) {
  obj.traverse((o) => {
    o.geometry?.dispose?.();
    const mats = Array.isArray(o.material) ? o.material : [o.material];
    for (const m of mats) { m?.map?.dispose?.(); m?.dispose?.(); }
  });
}

/** Two-click distance measurement with markers and a label (mm). */
class Measure {
  constructor(viewer) {
    this.v = viewer;
    this.group = new THREE.Group();
    this.group.name = 'measure';
    viewer.scene.add(this.group);
    this.points = [];
    this.active = false;
    this.onChange = null;
  }

  setActive(on) {
    this.active = on;
    this.v.canvas.classList.toggle('measuring', on);
    if (!on) this.clear();
  }

  add(point) {
    if (this.points.length >= 2) this.clear();
    this.points.push(point.clone());
    const accent = THEMES[this.v.theme].accent;
    const r = Math.max(this.v.modelRadius * 0.012, 0.3);
    const marker = new THREE.Mesh(new THREE.SphereGeometry(r, 16, 12),
      new THREE.MeshBasicMaterial({ color: accent, depthTest: false, transparent: true }));
    marker.renderOrder = 1000;
    marker.position.copy(point);
    this.group.add(marker);
    let result = null;
    if (this.points.length === 2) {
      const [a, b] = this.points;
      const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints([a, b]),
        new THREE.LineBasicMaterial({ color: accent, depthTest: false, transparent: true }));
      line.renderOrder = 999;
      this.group.add(line);
      const d = b.clone().sub(a);
      result = { distance: d.length(), dx: Math.abs(d.x), dy: Math.abs(d.y), dz: Math.abs(d.z) };
      const el = document.createElement('div');
      el.className = 'measure-label';
      el.textContent = `${result.distance.toFixed(2)} mm`;
      const label = new CSS2DObject(el);
      label.position.copy(a).add(b).multiplyScalar(0.5);
      this.group.add(label);
    }
    this.onChange?.(result, this.points.length);
    this.v.requestRender();
  }

  clear() {
    for (const c of [...this.group.children]) {
      if (c.isCSS2DObject) c.element.remove();
      this.group.remove(c);
      disposeTree(c);
    }
    this.points = [];
    this.onChange?.(null, 0);
    this.v.requestRender();
  }
}

/** The 3D viewport. Callbacks: onPick(nodeId|null, event), onHover(nodeId|null), onDoublePick(nodeId). */
export class Viewer {
  /**
   * @param {HTMLElement} host element that receives the canvas (sized by CSS)
   * @param {{theme?: 'dark'|'light'}} [opts]
   */
  constructor(host, opts = {}) {
    this.host = host;
    this.theme = opts.theme || 'dark';
    this.modelRadius = 100;
    this.pickables = () => [];
    /** Optional `(clientX, clientY, raycaster, partHit) => hit|null` for things picked with a screen
     * tolerance (wires, connector pins); null falls back to the part hit. Not used while measuring. */
    this.pickExtra = null;
    this.modelBox = () => null;
    this.onPick = null;
    this.onHover = null;
    this.onDoublePick = null;
    this.onSectionChange = null;
    this._dirty = true;
    this._anim = null;
    this._timer = new THREE.Timer();
    this._timer.connect(document);
    this.frames = 0;
    /** Per-frame callbacks `(now) => boolean`; returning true asks for a redraw (animations). */
    this.tickers = new Set();

    const r = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    r.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    r.setClearColor(0x000000, 0);
    r.localClippingEnabled = true;
    r.autoClear = false;
    this.renderer = r;
    this.canvas = r.domElement;
    host.append(this.canvas);

    this.labels = new CSS2DRenderer();
    this.labels.domElement.className = 'label-layer';
    host.append(this.labels.domElement);

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(35, 1, 0.5, 20000);
    this.camera.up.set(0, 0, 1); // Z-up world (spec §5.4) — the model is never rotated
    this.camera.position.set(240, -280, 220);
    this.scene.add(this.camera);

    this.controls = new OrbitControls(this.camera, this.canvas);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.14;
    this.controls.zoomToCursor = true;
    this.controls.addEventListener('change', () => this.requestRender());

    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x5a6270, 1.6));
    const key = new THREE.DirectionalLight(0xffffff, 1.9);
    key.position.set(0.6, 0.9, 1.0);
    this.camera.add(key); // headlight: the visible side is always lit
    this.camera.add(key.target);
    const fill = new THREE.DirectionalLight(0xffffff, 0.55);
    fill.position.set(-1, -0.6, 0.8);
    this.scene.add(fill);

    this.root = new THREE.Group();
    this.root.name = 'assembly';
    this.bed = new THREE.Group();
    this.bed.name = 'print-bed';
    this.bed.visible = false;
    this.helpers = new THREE.Group();
    this.helpers.name = 'helpers';
    this.scene.add(this.helpers, this.root, this.bed);
    this.grid = null;
    this._gridOn = true;
    this.buildGrid(null);

    this.gizmo = new ViewHelper(this.camera, this.canvas);
    this.gizmo.setLabels('X', 'Y', 'Z');
    this._gizmoCam = new THREE.OrthographicCamera(-2, 2, 2, -2, 0, 4);
    this._gizmoCam.position.set(0, 0, 2);
    this._gizmoCam.updateMatrixWorld();

    this.section = { enabled: false, axis: 'z', value: 0, flip: false };
    this.sectionPlane = new THREE.Plane(new THREE.Vector3(0, 0, -1), 0);
    this.sectionOutline = null;
    this.measure = new Measure(this);

    this._raycaster = new THREE.Raycaster();
    this._raycaster.params.Line.threshold = 0;
    this._bindEvents();
    new ResizeObserver(() => this.resize()).observe(host);
    this.resize();
    this._loop = this._loop.bind(this);
    requestAnimationFrame(this._loop);
  }

  /** Ask for a redraw on the next animation frame. */
  requestRender() { this._dirty = true; }

  resize() {
    const w = Math.max(1, this.host.clientWidth);
    const hgt = Math.max(1, this.host.clientHeight);
    this.renderer.setSize(w, hgt);
    this.labels.setSize(w, hgt);
    this.camera.aspect = w / hgt;
    this.camera.updateProjectionMatrix();
    this.requestRender();
  }

  _loop() {
    requestAnimationFrame(this._loop);
    this._timer.update();
    const dt = this._timer.getDelta();
    if (this._anim) this._stepAnim();
    if (this.controls.update(dt)) this._dirty = true;
    if (this.tickers.size) {
      const now = performance.now();
      for (const f of this.tickers) if (f(now)) this._dirty = true;
    }
    if (!this._dirty) return;
    this._dirty = false;
    this.frames += 1;
    this.gizmo.center.copy(this.controls.target);
    // The floor grid would be drawn over the model when looking up from below (overhang views).
    if (this.grid) this.grid.visible = this._gridOn && this.camera.position.z >= this.grid.position.z;
    this.renderer.clear();
    this.renderer.render(this.scene, this.camera);
    this.labels.render(this.scene, this.camera);
    this.gizmo.render(this.renderer);
  }

  // --------------------------------------------------------------------------- theme + grid
  /** Switch grid/edge colours ('dark' | 'light'). */
  setTheme(theme) {
    this.theme = theme;
    this.buildGrid(this._gridBox);
    this.requestRender();
  }

  /**
   * (Re)build the millimetre grid on the XY plane to cover `box` (10 mm minor, 50 mm major lines).
   * @param {THREE.Box3|null} box
   */
  buildGrid(box) {
    this._gridBox = box;
    if (this.grid) { this.helpers.remove(this.grid); disposeTree(this.grid); }
    const c = THEMES[this.theme];
    let reach = 100;
    if (box && !box.isEmpty()) {
      reach = Math.max(Math.abs(box.min.x), Math.abs(box.max.x), Math.abs(box.min.y), Math.abs(box.max.y));
    }
    const extent = Math.max(200, Math.ceil((reach * 2.6) / 50) * 50);
    const g = new THREE.Group();
    g.name = 'grid';
    g.add(gridLines(extent, 10, c.gridMinor, 0.75), gridLines(extent, 50, c.gridMajor, 0.95));
    const half = extent / 2;
    const axis = (pts, color) => new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.9, depthWrite: false }));
    g.add(axis([new THREE.Vector3(0, 0, 0), new THREE.Vector3(half, 0, 0)], c.axisX),
      axis([new THREE.Vector3(0, 0, 0), new THREE.Vector3(0, half, 0)], c.axisY));
    g.position.z = (box && !box.isEmpty() ? Math.min(0, box.min.z) : 0) - 0.05;
    g.renderOrder = -1;
    g.visible = this._gridOn;
    this.grid = g;
    this.gridExtent = extent;
    this.helpers.add(g);
    this.requestRender();
  }

  /** Show/hide the floor grid (it is also hidden while the camera looks up from below it). */
  setGridVisible(on) { this._gridOn = !!on; if (this.grid) this.grid.visible = this._gridOn; this.requestRender(); }

  /** Whether the user wants the grid (independent of the camera hiding it from below). */
  get gridOn() { return this._gridOn; }

  // --------------------------------------------------------------------------- camera
  _stepAnim() {
    const a = this._anim;
    const k = Math.min(1, (performance.now() - a.t0) / a.ms);
    const e = k < 0.5 ? 2 * k * k : 1 - (-2 * k + 2) ** 2 / 2;
    this.camera.position.lerpVectors(a.from, a.to, e);
    this.controls.target.lerpVectors(a.tFrom, a.tTo, e);
    this._dirty = true;
    if (k >= 1) this._anim = null;
  }

  /** Move the camera to `position` looking at `target` (animated unless ms = 0). */
  animateTo(position, target, ms = 380) {
    if (!ms) {
      this.camera.position.copy(position);
      this.controls.target.copy(target);
      this.controls.update();
      this.requestRender();
      return;
    }
    this._anim = { from: this.camera.position.clone(), to: position.clone(), tFrom: this.controls.target.clone(),
      tTo: target.clone(), t0: performance.now(), ms };
  }

  /**
   * Frame `box` from a named direction (iso|front|back|left|right|top|bottom) or the current one.
   * @param {THREE.Box3} box
   * @param {string} [view]
   * @param {number} [ms]
   */
  fit(box, view, ms = 380) {
    if (!box || box.isEmpty()) return;
    const sphere = box.getBoundingSphere(new THREE.Sphere());
    const r = Math.max(sphere.radius, 2);
    const vfov = THREE.MathUtils.degToRad(this.camera.fov);
    const hfov = 2 * Math.atan(Math.tan(vfov / 2) * this.camera.aspect);
    const dist = (r / Math.sin(Math.min(vfov, hfov) / 2)) * 1.06;
    const dir = view ? new THREE.Vector3(...VIEW_DIRS[view]).normalize()
      : this.camera.position.clone().sub(this.controls.target).normalize();
    this.camera.near = Math.max(0.05, r / 200);
    this.camera.far = dist + r * 60;
    this.camera.updateProjectionMatrix();
    this.controls.maxDistance = dist * 12;
    this.animateTo(sphere.center.clone().addScaledVector(dir, dist), sphere.center, ms);
  }

  /** Standard view of the current model (or bed). */
  setView(view, ms = 380) {
    this.fit(this.modelBox(), view, ms);
  }

  /** Current camera position and target (to restore after a reload). */
  cameraState() {
    return { position: this.camera.position.clone(), target: this.controls.target.clone() };
  }

  // --------------------------------------------------------------------------- section plane
  /**
   * Configure the section (clipping) plane: keeps the side where coordinate ≤ value (or ≥ when flipped).
   * @param {{enabled?:boolean, axis?:'x'|'y'|'z', value?:number, flip?:boolean}} opts
   */
  setSection(opts) {
    Object.assign(this.section, opts);
    const { axis, value, flip, enabled } = this.section;
    const n = AXES[axis].clone().multiplyScalar(flip ? 1 : -1);
    this.sectionPlane.normal.copy(n);
    this.sectionPlane.constant = flip ? -value : value;
    this._updateSectionOutline();
    this.onSectionChange?.(enabled ? [this.sectionPlane] : null);
    this.requestRender();
  }

  _updateSectionOutline() {
    if (this.sectionOutline) { this.scene.remove(this.sectionOutline); disposeTree(this.sectionOutline); }
    this.sectionOutline = null;
    const box = this.modelBox();
    if (!this.section.enabled || !box || box.isEmpty()) return;
    const { axis, value } = this.section;
    const lo = box.min.clone().subScalar(4);
    const hi = box.max.clone().addScalar(4);
    const corners = axis === 'x' ? [[value, lo.y, lo.z], [value, hi.y, lo.z], [value, hi.y, hi.z], [value, lo.y, hi.z]]
      : axis === 'y' ? [[lo.x, value, lo.z], [hi.x, value, lo.z], [hi.x, value, hi.z], [lo.x, value, hi.z]]
        : [[lo.x, lo.y, value], [hi.x, lo.y, value], [hi.x, hi.y, value], [lo.x, hi.y, value]];
    const pts = corners.map((p) => new THREE.Vector3(...p));
    const loop = new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineDashedMaterial({ color: THEMES[this.theme].accent, dashSize: 4, gapSize: 3, transparent: true, opacity: 0.9 }));
    loop.computeLineDistances();
    this.sectionOutline = loop;
    this.scene.add(loop);
  }

  // --------------------------------------------------------------------------- picking
  _ndc(clientX, clientY) {
    const rect = this.canvas.getBoundingClientRect();
    return new THREE.Vector2(((clientX - rect.left) / rect.width) * 2 - 1, -((clientY - rect.top) / rect.height) * 2 + 1);
  }

  /**
   * First visible, unclipped surface hit under a screen point.
   * @returns {{object: THREE.Object3D, point: THREE.Vector3, face: object, nodeId: string}|null}
   */
  pickAt(clientX, clientY) {
    this._raycaster.setFromCamera(this._ndc(clientX, clientY), this.camera);
    const hits = this._raycaster.intersectObjects(this.pickables(), false);
    let part = null;
    for (const hit of hits) {
      if (!this._visible(hit.object)) continue;
      if (this.section.enabled && this.sectionPlane.distanceToPoint(hit.point) < 0) continue;
      part = { object: hit.object, point: hit.point, face: hit.face, nodeId: hit.object.userData.nodeId, distance: hit.distance };
      break;
    }
    if (this.pickExtra && !this.measure.active) {
      const extra = this.pickExtra(clientX, clientY, this._raycaster, part);
      if (extra && !(this.section.enabled && this.sectionPlane.distanceToPoint(extra.point) < 0)) return extra;
    }
    return part;
  }

  _visible(o) {
    for (let p = o; p; p = p.parent) if (!p.visible) return false;
    return true;
  }

  _snap(hit, clientX, clientY) {
    // Snap to the nearest triangle corner within 10 px (measuring corner to corner).
    const pos = hit.object.geometry.attributes.position;
    if (!hit.face || !pos) return hit.point;
    const rect = this.canvas.getBoundingClientRect();
    let best = null;
    let bestD = 10;
    for (const idx of [hit.face.a, hit.face.b, hit.face.c]) {
      const v = new THREE.Vector3().fromBufferAttribute(pos, idx).applyMatrix4(hit.object.matrixWorld);
      const p = v.clone().project(this.camera);
      const sx = rect.left + ((p.x + 1) / 2) * rect.width;
      const sy = rect.top + ((1 - p.y) / 2) * rect.height;
      const d = Math.hypot(sx - clientX, sy - clientY);
      if (d < bestD) { bestD = d; best = v; }
    }
    return best || hit.point;
  }

  _gizmoView(ev) {
    const rect = this.canvas.getBoundingClientRect();
    const dim = 128;
    const x0 = rect.left + rect.width - dim - this.gizmo.location.right;
    const y0 = rect.top + rect.height - dim - this.gizmo.location.bottom;
    const mx = ((ev.clientX - x0) / dim) * 2 - 1;
    const my = -((ev.clientY - y0) / dim) * 2 + 1;
    if (Math.abs(mx) > 1 || Math.abs(my) > 1) return null;
    this._raycaster.setFromCamera(new THREE.Vector2(mx, my), this._gizmoCam);
    const hits = this._raycaster.intersectObjects(this.gizmo.children.filter((o) => o.isSprite), false);
    return hits.length ? GIZMO_VIEWS[hits[0].object.userData.type] : null;
  }

  _bindEvents() {
    const el = this.canvas;
    let down = null;
    el.addEventListener('pointerdown', (e) => { down = { x: e.clientX, y: e.clientY, b: e.button }; });
    el.addEventListener('pointerup', (e) => {
      if (!down) return;
      const moved = Math.hypot(e.clientX - down.x, e.clientY - down.y);
      const button = down.b;
      down = null;
      if (moved > 4 || button !== 0) return;
      const view = this._gizmoView(e);
      if (view) { this.setView(view); return; }
      const hit = this.pickAt(e.clientX, e.clientY);
      if (this.measure.active) {
        if (hit) this.measure.add(this._snap(hit, e.clientX, e.clientY));
        return;
      }
      this.onPick?.(hit ? hit.nodeId : null, e, hit);
    });
    el.addEventListener('dblclick', (e) => {
      const hit = this.pickAt(e.clientX, e.clientY);
      if (hit && hit.nodeId && !this.measure.active) this.onDoublePick?.(hit.nodeId, hit);
    });
    let hoverQueued = false;
    let last = null;
    el.addEventListener('pointermove', (e) => {
      last = e;
      if (down || hoverQueued || !this.onHover) return;
      hoverQueued = true;
      setTimeout(() => {
        hoverQueued = false;
        if (!last || down) return;
        const hit = this.pickAt(last.clientX, last.clientY);
        el.classList.toggle('hovering', !!hit);
        this.onHover?.(hit ? hit.nodeId : null, last, hit);
      }, 60);
    });
    el.addEventListener('pointerleave', () => { last = null; el.classList.remove('hovering'); this.onHover?.(null); });
  }
}
