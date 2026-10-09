// PiForge GUI — live split-flap faces in the 3D view. Every scene node with
// `display_from: {device, kind: "splitflap"}` gets a real two-half flap on the front of its bounding
// box (the window face): two static half panels (current digit) and a falling flap (the upper half of
// the old digit, hinged at the centre line, with the lower half of the new digit on its back). It is
// driven by the twin's `splitflap` state (digit / next_digit / flip) with the same queue and
// skip-ahead rule as the 2D widget (splitflap.js), so it never lags more than MAX_LAG_MS.
import * as THREE from 'three';
import { FLIP_MS, FlapDigit, MAX_LAG_MS, MIN_FLIP_MS, flapTarget, isDigit, mod10 } from './splitflap.js';

const FLAP_BG_TOP = '#26292f';
const FLAP_BG_BOTTOM = '#1b1d22';
const GLYPH = '#f3efe4';
const BACKING = 0x07080a;
const TEX_W = 512;
const FONT = '"Inter", "Helvetica Neue", "Arial Narrow", Arial, system-ui, sans-serif';

const textures = new Map();  // `${digit}:${h}` → CanvasTexture (shared by all faces of that aspect)

/** Full flap card for digit `d` (null = blank) as a texture; halves are picked by UVs. */
function digitTexture(d, aspect, renderer) {
  const hgt = Math.max(128, Math.min(2048, Math.round(TEX_W * aspect / 8) * 8));
  const key = `${d}:${hgt}`;
  if (textures.has(key)) return textures.get(key);
  const c = document.createElement('canvas');
  c.width = TEX_W;
  c.height = hgt;
  const g = c.getContext('2d');
  const grad = g.createLinearGradient(0, 0, 0, hgt);
  grad.addColorStop(0, FLAP_BG_TOP);
  grad.addColorStop(0.5, '#202328');
  grad.addColorStop(1, FLAP_BG_BOTTOM);
  g.fillStyle = grad;
  g.fillRect(0, 0, TEX_W, hgt);
  // a faint inner shadow along the hinge so the two halves read as separate leaves
  const hinge = g.createLinearGradient(0, hgt / 2 - hgt * 0.06, 0, hgt / 2 + hgt * 0.06);
  hinge.addColorStop(0, 'rgba(0,0,0,0)');
  hinge.addColorStop(0.5, 'rgba(0,0,0,0.35)');
  hinge.addColorStop(1, 'rgba(0,0,0,0)');
  g.fillStyle = hinge;
  g.fillRect(0, hgt / 2 - hgt * 0.06, TEX_W, hgt * 0.12);
  if (d !== null && d !== undefined) {
    const size = Math.min(hgt * 0.8, TEX_W * 1.25);
    g.font = `700 ${Math.round(size)}px ${FONT}`;
    g.textAlign = 'center';
    g.textBaseline = 'alphabetic';
    const m = g.measureText(String(d));
    const asc = m.actualBoundingBoxAscent || size * 0.72;
    const desc = m.actualBoundingBoxDescent || 0;
    const y = hgt / 2 + (asc - desc) / 2;  // glyph box centred exactly on the split line
    g.fillStyle = GLYPH;
    g.fillText(String(d), TEX_W / 2, y);
  }
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = renderer?.capabilities?.getMaxAnisotropy?.() || 1;
  tex.generateMipmaps = true;
  tex.minFilter = THREE.LinearMipmapLinearFilter;
  tex.userData.digit = d;
  textures.set(key, tex);
  return tex;
}

/** Rectangle w × h centred at (0, yc) in the XY plane (facing +Z) showing texture rows v0..v1. */
function halfGeometry(w, h, yc, v0, v1, back = false) {
  const g = new THREE.PlaneGeometry(w, h);
  const uv = g.attributes.uv;
  for (let i = 0; i < uv.count; i++) uv.setY(i, uv.getY(i) > 0.5 ? v1 : v0);
  // The back of the falling flap: facing −Z, upright once the flap has turned 180° about X.
  if (back) g.rotateX(Math.PI);
  g.translate(0, yc, 0);
  return g;
}

/** Fall angle (rad, 0 → π) at flip progress p: accelerating fall, small bounce on landing. */
export function fallAngle(p) {
  const q = Math.max(0, Math.min(1, p));
  const LAND = 0.82;
  if (q < LAND) return Math.PI * (q / LAND) ** 2;
  const b = (q - LAND) / (1 - LAND);
  return Math.PI - 0.07 * Math.PI * Math.sin(b * Math.PI) * (1 - b);
}

/**
 * Face frame (x right, y up, z towards the viewer; origin = centre of the front face) inside the
 * node frame, from the node's mesh bounding box. The thinnest box axis is the face normal: −Y
 * (viewer in front, as in the split-flap module frame), +Z (a face lying flat) or −X. `normal`/`up`
 * in `display_from` override it.
 */
export function faceFrame(box, def = {}) {
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const vec = (v) => (Array.isArray(v) && v.length === 3 && v.some(Number) ? new THREE.Vector3(...v.map(Number)).normalize() : null);
  let normal = vec(def.normal);
  if (!normal) {
    const dims = [size.x, size.y, size.z];
    const thin = dims.indexOf(Math.min(...dims));
    normal = [new THREE.Vector3(-1, 0, 0), new THREE.Vector3(0, -1, 0), new THREE.Vector3(0, 0, 1)][thin];
  }
  let up = vec(def.up) || (Math.abs(normal.z) > 0.9 ? new THREE.Vector3(0, 1, 0) : new THREE.Vector3(0, 0, 1));
  up = up.sub(normal.clone().multiplyScalar(up.dot(normal))).normalize();
  const right = new THREE.Vector3().crossVectors(up, normal).normalize();
  const extent = (axis) => Math.abs(axis.x) * size.x + Math.abs(axis.y) * size.y + Math.abs(axis.z) * size.z;
  const w = extent(right);
  const h = extent(up);
  const depth = extent(normal);
  const origin = center.clone().addScaledVector(normal, depth / 2);
  const m = new THREE.Matrix4().makeBasis(right, up, normal).setPosition(origin);
  return { matrix: m, w, h };
}

/** One live flap face on a scene node. */
export class FlapFace {
  constructor(node, renderer) {
    this.node = node;
    this.device = node.def.display_from.device;
    this.renderer = renderer;
    this.shown = 0;          // placeholder until the twin reports (the first report jumps)
    this.fresh = true;
    this.target = null;
    this.busy = false;
    this.flips = 0;
    this.deadline = 0;
    this.timer = 0;
    this.flip = null;        // {a, b, t0, ms}
    const { matrix, w, h } = faceFrame(node.asset.box, node.def.display_from);
    this.w = w;
    this.h = h;
    this.aspect = h / Math.max(w, 1e-6);
    const gap = Math.max(0.15, h * 0.012);          // split line between the two leaves
    const inset = Math.min(w, h) * 0.035;            // leaves are a bit smaller than the window
    const lw = w - 2 * inset;
    const lh = (h - 2 * inset - gap) / 2;
    const yc = gap / 2 + lh / 2;
    const vSplit = 0.5 - (gap / 2) / (h - 2 * inset);
    const vTop = 0.5 + (gap / 2) / (h - 2 * inset);
    const e = Math.max(0.03, h * 0.004);            // layer spacing towards the viewer
    const mat = () => new THREE.MeshBasicMaterial({ color: 0xffffff, side: THREE.FrontSide, toneMapped: false });
    this.group = new THREE.Group();
    this.group.name = `${node.id}:splitflap`;
    this.group.matrixAutoUpdate = false;
    this.group.matrix.copy(matrix);
    this.group.userData.splitflap = true;
    const backing = new THREE.Mesh(new THREE.PlaneGeometry(w, h), new THREE.MeshBasicMaterial({ color: BACKING, toneMapped: false }));
    backing.position.z = e * 0.25;
    this.topMat = mat();
    this.bottomMat = mat();
    this.frontMat = mat();
    this.backMat = mat();
    this.top = new THREE.Mesh(halfGeometry(lw, lh, yc, vTop, 1), this.topMat);
    this.bottom = new THREE.Mesh(halfGeometry(lw, lh, -yc, 0, vSplit), this.bottomMat);
    this.top.position.z = this.bottom.position.z = e;
    this.leaf = new THREE.Group();  // the falling flap: hinge = local X axis at the split line
    this.leaf.position.z = e * 2.5;
    const front = new THREE.Mesh(halfGeometry(lw, lh, yc, vTop, 1), this.frontMat);
    const back = new THREE.Mesh(halfGeometry(lw, lh, yc, 0, vSplit, true), this.backMat);
    front.position.z = e * 0.5;
    back.position.z = -e * 0.5;
    this.leaf.add(front, back);
    this.leaf.visible = false;
    for (const m of [front, back, this.top, this.bottom, backing]) m.renderOrder = 2;
    this.group.add(backing, this.top, this.bottom, this.leaf);
    this.materials = [backing.material, this.topMat, this.bottomMat, this.frontMat, this.backMat];
    this._static(0);
  }

  _tex(d) { return digitTexture(d, this.aspect, this.renderer); }

  _setMap(m, d) { if (m.map !== this._tex(d)) { m.map = this._tex(d); m.needsUpdate = true; } }

  _static(d) {
    this.shown = d;
    this._setMap(this.topMat, d);
    this._setMap(this.bottomMat, d);
    this.topMat.color.setScalar(1);
    this.bottomMat.color.setScalar(1);
    this.leaf.visible = false;
    this.flip = null;
  }

  /** Show `d` at once. */
  jump(d) {
    clearTimeout(this.timer);
    this.busy = false;
    this._static(d);
    this.target = d;
  }

  /** Flip forward (like a spool) until `d` shows; anything that is not 0–9 is ignored. */
  setTarget(d) {
    d = Number(d);
    if (!isDigit(d)) return;
    if (d !== this.target) this.deadline = performance.now() + MAX_LAG_MS * FlapDigit.timeScale;
    this.target = d;
    if (this.fresh) { this.fresh = false; this.jump(d); return; }
    this._pump();
  }

  _pump() {
    if (this.busy || this.target === null || this.target === this.shown) return;
    let steps = mod10(this.target - this.shown);
    const left = Math.max(0, this.deadline - performance.now()) / FlapDigit.timeScale;
    const keep = Math.max(1, Math.floor(left / MIN_FLIP_MS));
    if (steps > keep) {  // too far behind: skip ahead silently, flip only the last few flaps
      this._static(mod10(this.target - keep));
      steps = keep;
    }
    const ms = Math.round(Math.max(MIN_FLIP_MS, Math.min(FLIP_MS, left / steps)));
    this._flip(this.shown, mod10(this.shown + 1), ms * FlapDigit.timeScale);
  }

  _flip(a, b, ms) {
    this.busy = true;
    this._setMap(this.topMat, b);     // revealed behind the falling flap
    this._setMap(this.bottomMat, a);  // covered when the flap lands
    this._setMap(this.frontMat, a);
    this._setMap(this.backMat, b);
    this.flip = { a, b, t0: performance.now(), ms };
    this.leaf.visible = true;
    this.tick(performance.now());
    // A timer (not the render loop) ends the flip: frames do not run while the tab is hidden.
    this.timer = setTimeout(() => {
      this.busy = false;
      this.flips += 1;
      this._static(b);
      this._pump();
    }, ms);
  }

  /** Pose the falling flap for time `now`; true while animating. */
  tick(now) {
    const f = this.flip;
    if (!f) return false;
    const frozen = SplitFlapFaces.freeze;
    const p = typeof frozen === 'number' ? frozen : Math.min(1, (now - f.t0) / Math.max(1, f.ms));
    const th = fallAngle(p);
    this.leaf.rotation.x = th;
    const s = Math.sin(Math.min(th, Math.PI / 2));
    this.frontMat.color.setScalar(1 - 0.55 * s);                          // turns away from the light
    this.backMat.color.setScalar(th > Math.PI / 2 ? 0.55 + 0.45 * Math.sin(Math.min(th, Math.PI) - Math.PI / 2) : 0.55);
    this.topMat.color.setScalar(0.62 + 0.38 * Math.min(1, p * 1.6));      // revealed half comes out of the shade
    return true;
  }

  /** Plain-data state for tests. */
  debug() {
    return { node: this.node.id, device: this.device, shown: this.shown, target: this.target, busy: this.busy,
      flips: this.flips, angle: this.flip ? THREE.MathUtils.radToDeg(this.leaf.rotation.x) : 0,
      top: this.topMat.map?.userData?.digit ?? null, bottom: this.bottomMat.map?.userData?.digit ?? null,
      w: this.w, h: this.h };
  }

  dispose() {
    clearTimeout(this.timer);
    this.group.parent?.remove(this.group);
    for (const m of this.materials) m.dispose();
    this.group.traverse((o) => o.geometry?.dispose?.());
  }
}

/** All split-flap faces of a SceneModel. */
export class SplitFlapFaces {
  constructor(viewer) {
    this.viewer = viewer;
    this.faces = [];
    this._tick = (now) => {
      let anim = false;
      for (const f of this.faces) if (f.tick(now)) anim = true;
      return anim;
    };
    viewer.tickers?.add(this._tick);
  }

  /** (Re)create faces for the scene's nodes with `display_from.kind == "splitflap"`. */
  rebuild(nodes) {
    const old = new Map(this.faces.map((f) => [f.node.id, f]));
    for (const f of this.faces) f.dispose();
    this.faces = [];
    for (const n of nodes) {
      const df = n.def.display_from;
      if (!df || df.kind !== 'splitflap' || !df.device) continue;
      if (!n.asset?.box || n.asset.box.isEmpty()) {
        console.warn('PiForge: display_from node without a mesh', n.id);
        continue;
      }
      const face = new FlapFace(n, this.viewer.renderer);
      const prev = old.get(n.id);
      if (prev && !prev.fresh && prev.target !== null) { face.fresh = false; face.jump(prev.target); }
      for (const m of n.meshes) m.visible = false;  // the face replaces the window's own mesh
      n.content.traverse((o) => { if (o.isLineSegments) o.visible = false; });
      n.content.add(face.group);
      n.flapFace = face;
      this.faces.push(face);
    }
    this.viewer.requestRender();
  }

  /** Twin state devices → faces. */
  update(devices) {
    for (const f of this.faces) {
      const p = devices?.[f.device];
      if (p && typeof p === 'object' && ('digit' in p || 'next_digit' in p)) f.setTarget(flapTarget(p));
    }
    if (this.faces.some((f) => f.busy)) this.viewer.requestRender();
  }

  /** Materials (for section clipping). */
  materials() { return this.faces.flatMap((f) => f.materials); }

  debug() { return this.faces.map((f) => f.debug()); }
}

/** Review/test hook: a number 0..1 pins every running flip at that progress (null = real time). */
SplitFlapFaces.freeze = null;
