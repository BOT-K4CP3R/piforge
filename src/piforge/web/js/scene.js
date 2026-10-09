// PiForge GUI — assembly model: loads scene.json (spec §5.4) + GLBs, builds the node graph
// (column-major matrices, Z up), joints driven by the twin, LED glow, explode, selection,
// overhang heat-map and the print-bed view.
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { mergeVertices, toCreasedNormals } from 'three/addons/utils/BufferGeometryUtils.js';
import { THEMES } from './viewer.js';
import { SplitFlapFaces } from './splitflap3d.js';
import { WireLayer } from './wires.js';

const KIND_STYLE = {
  printed: { roughness: 0.8, metalness: 0.0 },
  pcb: { roughness: 0.62, metalness: 0.05 },
  reference: { roughness: 0.55, metalness: 0.05 },
  fastener: { roughness: 0.32, metalness: 0.8 },
};
/** Wire insulation: PVC/silicone with a slight gloss (clear-coat highlight along the tube). */
const WIRE_STYLE = { roughness: 0.42, metalness: 0.0, clearcoat: 0.55, clearcoatRoughness: 0.28 };
/** Opacity of parts while the wiring is the subject (wire X-ray, a selected wire or net). */
const WIRE_GHOST = 0.16;
const DIM_WIRE = 0.12;
/** Heat-map colours: needs support (red), prints as a bridge (amber). Also used by the legend. */
export const HEAT_HEX = '#ef4444';
export const BRIDGE_HEX = '#f5a524';
/** Bed fillets are painted in the neutral part colour; this swatch only labels them in the legend. */
export const FILLET_HEX = '#9aa3ad';
const HEAT = new THREE.Color(HEAT_HEX);
const BRIDGE = new THREE.Color(BRIDGE_HEX);
const WHITE = new THREE.Color(1, 1, 1);
const IDENTITY = new THREE.Matrix4().toArray();
const _t = new THREE.Matrix4();
const _r = new THREE.Matrix4();

/** Rotation matrix for print rotation angles (deg) about fixed X, then Y, then Z (= Rz·Ry·Rx). */
export function rotationXYZ(rx = 0, ry = 0, rz = 0) {
  const d = THREE.MathUtils.degToRad;
  return new THREE.Matrix4().makeRotationZ(d(rz))
    .multiply(new THREE.Matrix4().makeRotationY(d(ry)))
    .multiply(new THREE.Matrix4().makeRotationX(d(rx)));
}

/** Joint transform: revolute T(o)·R(axis, value°)·T(−o); prismatic T(axis·value mm). */
export function jointMatrix(j, out = new THREE.Matrix4()) {
  const axis = new THREE.Vector3(...(j.axis || [0, 0, 1])).normalize();
  const v = Number(j.value) || 0;
  if (j.type === 'prismatic') return out.makeTranslation(axis.x * v, axis.y * v, axis.z * v);
  const o = j.origin || [0, 0, 0];
  out.makeTranslation(o[0], o[1], o[2]);
  out.multiply(_r.makeRotationAxis(axis, THREE.MathUtils.degToRad(v)));
  return out.multiply(_t.makeTranslation(-o[0], -o[1], -o[2]));
}

/** Twin output value → glow level 0..1 (bools, 0..1 floats, 0..255 values, on/off strings, #rrggbb). */
export function brightnessOf(raw) {
  if (typeof raw === 'boolean') return raw ? 1 : 0;
  if (typeof raw === 'string') {
    const c = colorOf(raw);
    if (c) return Math.max(c.r, c.g, c.b);
    return ['on', 'true', 'high', '1'].includes(raw.toLowerCase()) ? 1 : 0;
  }
  const v = Number(raw);
  if (!Number.isFinite(v)) return 0;
  return v > 1 ? Math.min(1, v / 255) : Math.max(0, v);
}

/** "#rrggbb" (an RGB LED's `color` output) → THREE.Color, otherwise null. */
export function colorOf(raw) {
  return typeof raw === 'string' && /^#[0-9a-f]{6}$/i.test(raw.trim()) ? new THREE.Color(raw.trim()) : null;
}

/**
 * Twin value → joint value: `value·scale + offset`, clamped to [min, max] — except revolute joints
 * spanning a full turn (e.g. a stepper-driven drum, −180…180°), which wrap around instead.
 */
export function drivenJointValue(j, raw) {
  const drv = j.driven_by || {};
  let v = Number(raw) * Number(drv.scale ?? 1) + Number(drv.offset ?? 0);
  if (!Number.isFinite(v)) return null;
  const bound = (x) => (x === null || x === undefined || x === '' ? NaN : Number(x));  // null = unbounded
  const lo = bound(j.min);
  const hi = bound(j.max);
  if (j.type !== 'prismatic' && Number.isFinite(lo) && Number.isFinite(hi) && hi - lo >= 360 - 1e-6) {
    return lo + ((((v - lo) % 360) + 360) % 360);
  }
  // Unbounded revolute joint driven by an ever-growing angle (a split-flap spool, a motor shaft):
  // the same pose, but keep the number small (−180…180°) so it never loses precision.
  if (j.type !== 'prismatic' && !Number.isFinite(lo) && !Number.isFinite(hi)) {
    return ((((v + 180) % 360) + 360) % 360) - 180;
  }
  if (Number.isFinite(lo)) v = Math.max(lo, v);
  if (Number.isFinite(hi)) v = Math.min(hi, v);
  return v;
}

let haloTex = null;
function haloTexture() {
  if (haloTex) return haloTex;
  const c = document.createElement('canvas');
  c.width = c.height = 128;
  const g = c.getContext('2d');
  const grad = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  grad.addColorStop(0, 'rgba(255,255,255,1)');
  grad.addColorStop(0.25, 'rgba(255,255,255,0.55)');
  grad.addColorStop(1, 'rgba(255,255,255,0)');
  g.fillStyle = grad;
  g.fillRect(0, 0, 128, 128);
  haloTex = new THREE.CanvasTexture(c);
  haloTex.colorSpace = THREE.SRGBColorSpace;
  return haloTex;
}

/**
 * Colour reader for the per-leaf `baseColorFactor`s of a multi-leaf GLB. glTF defines the factor as
 * LINEAR, but trimesh-written GLBs (piforge.mech.export.export_glb) store sRGB bytes / 255. Pick the
 * interpretation that reproduces the node's scene colour on some leaf (sRGB when undecidable).
 */
function leafColorReader(asset, nodeHex) {
  const factors = (asset?.meshes || []).map((m) => m.factor).filter(Boolean);
  if (!factors.length) return () => null;
  const target = new THREE.Color(nodeHex || '#9aa3ad');
  const near = (c) => Math.max(Math.abs(c.r - target.r), Math.abs(c.g - target.g), Math.abs(c.b - target.b)) < 0.01;
  const srgb = (f) => new THREE.Color().setRGB(f[0], f[1], f[2], THREE.SRGBColorSpace);
  const linear = (f) => new THREE.Color().setRGB(f[0], f[1], f[2], THREE.LinearSRGBColorSpace);
  const read = !factors.some((f) => near(srgb(f))) && factors.some((f) => near(linear(f))) ? linear : srgb;
  return (f) => (f ? read(f) : null);
}

async function runLimited(items, limit, fn) {
  const queue = [...items];
  const workers = Array.from({ length: Math.min(limit, queue.length) }, async () => {
    while (queue.length) await fn(queue.shift());
  });
  await Promise.all(workers);
}

/** The assembly shown in the viewer. */
export class SceneModel {
  /** @param {import('./viewer.js').Viewer} viewer */
  constructor(viewer) {
    this.viewer = viewer;
    this.loader = new GLTFLoader();
    this.assets = new Map();
    this.nodes = new Map();
    this.def = null;
    this.explode = 0;
    this.explodeDist = 100;
    this.hidden = new Set();
    this.colors = new Map();
    this.selected = new Set();
    this.isolated = null;
    this.xray = false;
    this.heat = null;
    this.bedPart = null;
    this.clipping = null;
    this.bedMaterials = [];
    viewer.pickables = () => this.pickables();
    viewer.modelBox = () => (this.bedPart ? new THREE.Box3().setFromObject(this.viewer.bed) : this.visibleBox());
    viewer.onSectionChange = (planes) => this._applyClipping(planes);
    this.flaps = new SplitFlapFaces(viewer);  // live split-flap faces on `display_from` nodes
    // Harness: `kind: "wire"` nodes (tubes in the world frame) + connector pins from scene.json.
    this.wiresOn = true;
    this.wireXray = false;
    this.wireFocus = new Set();  // wire node ids in focus (selected wire / highlighted net)
    this.hoverWire = null;
    this.wires = new WireLayer(this, viewer);
  }

  /** True for harness wire nodes. */
  isWire(n) { return n?.def?.kind === 'wire'; }

  _url(mesh, version) {
    return `/build/${String(mesh).split('/').map(encodeURIComponent).join('/')}?v=${encodeURIComponent(version)}`;
  }

  async _asset(url, smooth = false) {
    if (!this.assets.has(url)) {
      this.assets.set(url, this.loader.loadAsync(url).then((gltf) => {
        const meshes = [];
        let offset = 0;
        const json = gltf.parser.json || {};
        gltf.scene.updateMatrixWorld(true);
        gltf.scene.traverse((o) => {  // depth-first = glTF node order (the server's face order)
          if (!o.isMesh || !o.geometry?.attributes?.position) return;
          const assoc = gltf.parser.associations.get(o) || {};
          const prim = json.meshes?.[assoc.meshes]?.primitives?.[assoc.primitives ?? 0];
          const mat = prim && prim.material !== undefined ? json.materials?.[prim.material] : null;
          const factor = mat ? (mat.pbrMetallicRoughness?.baseColorFactor || [1, 1, 1, 1]) : null;
          let g = o.geometry.clone();
          g.applyMatrix4(o.matrixWorld);
          for (const name of Object.keys(g.attributes)) if (name !== 'position') g.deleteAttribute(name);
          let faceCount;
          if (smooth) {  // wire tubes: smooth round insulation, no feature edges, no heat-map
            faceCount = (g.index ? g.index.count : g.attributes.position.count) / 3;
            g = mergeVertices(g, 1e-4);
            g.computeVertexNormals();
          } else {
            g = toCreasedNormals(g, THREE.MathUtils.degToRad(35)); // non-indexed: face order kept
            faceCount = g.attributes.position.count / 3;
            g.setAttribute('color', new THREE.BufferAttribute(new Float32Array(faceCount * 9).fill(1), 3));
          }
          g.computeBoundingBox();
          g.computeBoundingSphere();
          meshes.push({ geometry: g, edges: smooth ? null : new THREE.EdgesGeometry(g, 28), faceOffset: offset, faceCount, factor });
          offset += faceCount;
        });
        gltf.scene.traverse((o) => {
          o.geometry?.dispose?.();
          for (const m of [o.material].flat()) { m?.map?.dispose?.(); m?.dispose?.(); }
        });
        const box = new THREE.Box3();
        for (const m of meshes) box.union(m.geometry.boundingBox);
        return { url, meshes, faceCount: offset, box };
      }));
    }
    return this.assets.get(url);
  }

  _disposeAsset(a) {
    for (const m of a.meshes) { m.geometry.dispose(); m.edges?.dispose(); }
  }

  /**
   * Load (or reload after a rebuild) a scene; user state (hidden, colours, selection, explode) is kept.
   * @param {object} sceneDef scene.json
   * @param {{version?: string, onProgress?: Function}} [opts]
   * @returns {Promise<{nodes:number, failed:string[]}>}
   */
  async load(sceneDef, { version = '', onProgress } = {}) {
    const defs = (sceneDef.nodes || []).filter((n) => n && n.id);
    const urls = [...new Set(defs.filter((n) => n.mesh).map((n) => this._url(n.mesh, version)))];
    const smooth = new Set(defs.filter((n) => n.mesh && n.kind === 'wire').map((n) => this._url(n.mesh, version)));
    for (const [u, p] of this.assets) {
      if (!urls.includes(u)) { p.then((a) => this._disposeAsset(a)).catch(() => {}); this.assets.delete(u); }
    }
    const loaded = new Map();
    const failed = [];
    let done = 0;
    onProgress?.(0, urls.length);
    await runLimited(urls, 4, async (u) => {
      try { loaded.set(u, await this._asset(u, smooth.has(u))); } catch (err) {
        failed.push(u);
        this.assets.delete(u);
        console.warn('PiForge: mesh failed to load', u, err);
      }
      onProgress?.(++done, urls.length);
    });
    this._clearNodes();
    this.def = sceneDef;
    for (const d of defs) this._makeNode(d, d.mesh ? loaded.get(this._url(d.mesh, version)) : null);
    for (const n of this.nodes.values()) {
      const parent = n.def.parent && this.nodes.get(n.def.parent);
      (parent ? parent.frame : this.viewer.root).add(n.frame);
    }
    for (const id of [...this.selected]) if (!this.nodes.has(id)) this.selected.delete(id);
    if (this.isolated && !this.nodes.has(this.isolated)) this.isolated = null;
    for (const id of [...this.wireFocus]) if (!this.nodes.has(id)) this.wireFocus.delete(id);
    if (this.hoverWire && !this.nodes.has(this.hoverWire)) this.hoverWire = null;
    this.flaps.rebuild([...this.nodes.values()]);
    this.wires.rebuild(sceneDef);
    this._ghostFlaps();
    this._computeExplode();
    for (const n of this.nodes.values()) { this._updateMatrix(n); this._refresh(n); }
    this._applyVisibility();
    this._applyClipping(this.clipping);
    this.heat = null;
    this.viewer.root.updateMatrixWorld(true);
    this.viewer.requestRender();
    return { nodes: this.nodes.size, failed };
  }

  _clearNodes() {
    for (const n of this.nodes.values()) {
      n.frame.parent?.remove(n.frame);
      for (const m of n.mats) m.dispose();
      n.edgeMat.dispose();
      n.halo?.material.dispose();
    }
    this.nodes.clear();
  }

  _makeNode(d, asset) {
    const frame = new THREE.Group();
    frame.name = d.id;
    frame.matrixAutoUpdate = false;
    const content = new THREE.Group();
    content.name = `${d.id}:content`;
    frame.add(content);
    const override = this.colors.get(d.id);
    const color = new THREE.Color(override || d.color || '#9aa3ad');
    const theme = THEMES[this.viewer.theme];
    const style = KIND_STYLE[d.kind] || KIND_STYLE.reference;
    const wire = d.kind === 'wire';
    const makeMat = (c) => (wire ? new THREE.MeshPhysicalMaterial({ color: c, ...WIRE_STYLE })
      : new THREE.MeshStandardMaterial({ color: c, ...style, side: THREE.DoubleSide,
        polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 }));
    const edgeMat = new THREE.LineBasicMaterial({ color: theme.edge, transparent: true, opacity: theme.edgeOpacity });
    const meshes = [];
    const mats = [];
    const bases = [];
    // scene.json `color` (sRGB hex) is the source of truth; only multi-leaf GLBs (e.g. a board with
    // coloured components) take per-leaf colours from the file.
    const leafColor = !wire && (asset?.meshes?.length || 0) > 1 ? leafColorReader(asset, d.color) : () => null;
    for (const m of asset?.meshes || []) {
      const leaf = leafColor(m.factor);
      const base = !override && leaf ? leaf : color.clone();
      const mat = makeMat(base.clone());
      mats.push(mat);
      bases.push(base);
      const mesh = new THREE.Mesh(m.geometry, mat);
      Object.assign(mesh.userData, { nodeId: d.id, faceOffset: m.faceOffset, faceCount: m.faceCount, wire });
      content.add(mesh);
      if (m.edges) {
        const lines = new THREE.LineSegments(m.edges, edgeMat);
        lines.userData.nodeId = d.id;
        content.add(lines);
      }
      meshes.push(mesh);
    }
    const j = d.joint && typeof d.joint === 'object' ? { ...d.joint } : null;
    if (j) { j.value = Number(j.value) || 0; j.value0 = j.value; }
    if (!mats.length) { mats.push(makeMat(color.clone())); bases.push(color.clone()); }
    const node = { id: d.id, def: d, frame, content, mats, bases, mat: mats[0], edgeMat, color, meshes, asset, joint: j,
      base: new THREE.Matrix4().fromArray(Array.isArray(d.matrix) && d.matrix.length === 16 ? d.matrix : IDENTITY),
      glow: 0, explodeOffset: null };
    if (d.emissive_from) this._addGlow(node);
    this.nodes.set(d.id, node);
  }

  _addGlow(n) {
    const ef = n.def.emissive_from;
    n.glowColor = new THREE.Color(ef.color || '#ff3b30');
    const box = n.asset?.box && !n.asset.box.isEmpty() ? n.asset.box : new THREE.Box3(new THREE.Vector3(-2, -2, 0), new THREE.Vector3(2, 2, 4));
    const center = box.getCenter(new THREE.Vector3());
    const size = Math.max(...box.getSize(new THREE.Vector3()).toArray(), 1);
    const halo = new THREE.Sprite(new THREE.SpriteMaterial({ map: haloTexture(), color: n.glowColor,
      transparent: true, opacity: 0, depthWrite: false, blending: THREE.AdditiveBlending }));
    halo.position.copy(center);
    halo.scale.setScalar(size * 3.4);
    halo.visible = false;
    halo.renderOrder = 10;
    const light = new THREE.PointLight(n.glowColor, 0, size * 14, 2);
    light.position.copy(center);
    n.content.add(halo, light);
    n.halo = halo;
    n.light = light;
  }

  _setGlow(n, b, color = null) {
    n.glow = b;
    if (!n.glowColor) return;
    if (color) n.glowColor.copy(color);  // RGB LED: the twin reports the mixed colour
    for (const m of n.mats) { m.emissive.copy(n.glowColor); m.emissiveIntensity = b * 1.6; }
    n.halo.material.color.copy(n.glowColor);
    n.halo.material.opacity = Math.min(1, b);
    n.halo.visible = b > 0.01;
    n.light.color.copy(n.glowColor);
    n.light.intensity = b * 350;
  }

  _computeExplode() {
    const saved = this.explode;
    this.explode = 0;
    for (const n of this.nodes.values()) this._updateMatrix(n);
    this.viewer.root.updateMatrixWorld(true);
    const all = new THREE.Box3();
    const boxes = new Map();
    for (const n of this.nodes.values()) {
      if (this.isWire(n)) continue;  // wires are routed in the rest pose; they never fly apart
      const b = new THREE.Box3().expandByObject(n.content);
      boxes.set(n.id, b);
      if (!b.isEmpty()) all.union(b);
    }
    const center = all.isEmpty() ? new THREE.Vector3() : all.getCenter(new THREE.Vector3());
    this.explodeDist = all.isEmpty() ? 100 : all.getBoundingSphere(new THREE.Sphere()).radius;
    const explicit = (e) => Array.isArray(e) && e.length === 3 && e.some((v) => Number(v));
    // `explode` = offset in mm at full explode, in the parent frame. When the designer gave none
    // at all, top-level parts fly apart radially so the slider still does something useful.
    const anyExplicit = [...this.nodes.values()].some((n) => explicit(n.def.explode));
    for (const n of this.nodes.values()) {
      const e = n.def.explode;
      if (this.isWire(n)) n.explodeOffset = null;
      else if (explicit(e)) n.explodeOffset = new THREE.Vector3(...e.map(Number));
      else if (!anyExplicit && !n.def.parent && !boxes.get(n.id).isEmpty()) {
        const dir = boxes.get(n.id).getCenter(new THREE.Vector3()).sub(center);
        n.explodeOffset = (dir.length() < 1e-3 ? new THREE.Vector3(0, 0, 1) : dir.normalize()).multiplyScalar(this.explodeDist * 0.5);
      } else n.explodeOffset = null;
    }
    this.explode = saved;
  }

  _updateMatrix(n) {
    const m = n.frame.matrix.copy(n.base);  // world = parent_world · matrix · J(value)
    if (n.joint) m.multiply(jointMatrix(n.joint, new THREE.Matrix4()));
    if (this.explode > 0 && n.explodeOffset) {
      const o = n.explodeOffset.clone().multiplyScalar(this.explode);
      m.premultiply(new THREE.Matrix4().makeTranslation(o.x, o.y, o.z));
    }
    n.frame.matrixWorldNeedsUpdate = true;
  }

  /** Parts become ghosts (and other wires dim) while the wiring is the subject. */
  get wireMode() { return this.wiresOn && (this.wireXray || this.wireFocus.size > 0); }

  /** Split-flap faces (opaque overlay planes) turn translucent with the parts in wire mode. */
  _ghostFlaps() {
    const ghost = this.wireMode && !this.heat;
    for (const f of this.flaps.faces || []) {
      f.group?.traverse((o) => {
        const m = o.material;
        if (!m || Array.isArray(m)) return;
        if (m.userData.wireGhost === undefined) m.userData.wireGhost = { transparent: m.transparent, opacity: m.opacity };
        const orig = m.userData.wireGhost;
        const t = ghost ? true : orig.transparent;
        if (m.transparent !== t) { m.transparent = t; m.needsUpdate = true; }
        m.opacity = ghost ? WIRE_GHOST : orig.opacity;
        m.depthWrite = !ghost;
      });
    }
  }

  _refreshWire(n) {
    const sel = this.selected.has(n.id) || this.wireFocus.has(n.id);
    const dim = this.wireFocus.size > 0 && !this.wireFocus.has(n.id) && !this.selected.has(n.id);
    const hover = this.hoverWire === n.id;
    // In wire mode the parts are depth-less ghosts: wires join the transparent pass AFTER them
    // (renderOrder), so stacked housing walls never darken the harness.
    const late = this.wireMode;
    for (const mesh of n.meshes) mesh.renderOrder = late ? (dim ? 9 : 10) : 0;
    n.mats.forEach((m, i) => {
      const tr = dim || late;
      if (m.transparent !== tr) { m.transparent = tr; m.needsUpdate = true; }
      m.color.copy(n.bases[i]);
      // glow in the wire's own colour (lifted towards white so black/brown wires light up too):
      // the insulation colour stays readable, unlike an accent tint
      if (sel || hover) {
        const b = n.bases[i];
        const dark = Math.max(b.r, b.g, b.b) < 0.2;  // linear: black, brown, navy
        m.emissive.copy(b).lerp(WHITE, dark ? (sel ? 0.3 : 0.4) : hover ? 0.25 : 0);
      } else m.emissive.set(0x000000);
      m.emissiveIntensity = sel ? 0.5 : hover ? 0.4 : 1;
      m.opacity = dim ? DIM_WIRE : 1;
      m.depthWrite = !dim;
    });
  }

  _refresh(n) {
    if (this.isWire(n)) { this._refreshWire(n); return; }
    const heat = !!(this.heat && this.heat.nodeIds.has(n.id));
    const t = THEMES[this.viewer.theme];
    const sel = this.selected.has(n.id);
    const wireGhost = !this.heat && this.wireMode;
    // While a heat-map is shown the other parts become ghosts so the painted part stays visible;
    // X-ray does the same for everything but the selection (parts hidden inside an enclosure).
    const ghost = (!!this.heat && !heat) || (!this.heat && this.xray && this.selected.size > 0 && !sel) || wireGhost;
    n.mats.forEach((m, i) => {
      if (m.vertexColors !== heat || m.transparent !== ghost) { m.vertexColors = heat; m.transparent = ghost; m.needsUpdate = true; }
      m.color.copy(heat ? WHITE : n.bases[i]);
      if (!n.glowColor) {  // selection = accent edges + a faint accent glow; the part keeps its hue
        m.emissive.set(sel && !heat ? t.accent : 0x000000);
        m.emissiveIntensity = sel && !heat ? 0.22 : 1;
      }
      m.opacity = ghost ? (wireGhost && !this.xray ? WIRE_GHOST : 0.13) : 1;
      m.depthWrite = !ghost;
    });
    n.edgeMat.color.set(sel ? t.accent : t.edge);
    n.edgeMat.opacity = ghost ? (wireGhost ? 0.1 : 0.06) : sel ? 1 : t.edgeOpacity;
  }

  _applyVisibility() {
    for (const n of this.nodes.values()) {
      n.content.visible = !this.hidden.has(n.id) && (!this.isolated || this.isolated === n.id)
        && (this.wiresOn || !this.isWire(n));
    }
    this.wires.group.visible = this.wiresOn && !this.isolated;
    this.viewer.root.visible = !this.bedPart;
    this.viewer.requestRender();
  }

  _applyClipping(planes) {
    this.clipping = planes;
    const mats = [...this.nodes.values()].flatMap((n) => [...n.mats, n.edgeMat])
      .concat(this.bedMaterials, this.flaps.materials(), this.wires.materials());
    for (const m of mats) { m.clippingPlanes = planes; m.needsUpdate = true; }
    this.viewer.requestRender();
  }

  // --------------------------------------------------------------------------- queries
  /** Meshes the viewer may pick. */
  pickables() {
    const out = [];
    if (this.bedPart) this.viewer.bed.traverse((o) => { if (o.isMesh && o.userData.nodeId) out.push(o); });
    else for (const n of this.nodes.values()) if (!this.isWire(n)) out.push(...n.meshes);  // wires: WireLayer.pick
    return out;
  }

  /** World bounding box of the visible nodes. */
  visibleBox() {
    this.viewer.root.updateMatrixWorld(true);
    const box = new THREE.Box3();
    for (const n of this.nodes.values()) {
      if (n.content.visible && n.meshes.length) box.expandByObject(n.content);
    }
    return box;
  }

  /** World bounding box of the given node ids. */
  boxOf(ids) {
    this.viewer.root.updateMatrixWorld(true);
    const box = new THREE.Box3();
    for (const id of ids) { const n = this.nodes.get(id); if (n?.meshes.length) box.expandByObject(n.content); }
    return box;
  }

  /** Tree rows: id, name, kind, colour, hidden, joint/glow flags. */
  list() {
    return [...this.nodes.values()].map((n) => ({ id: n.id, name: n.def.name || n.id, kind: n.def.kind || 'other',
      color: `#${n.color.getHexString()}`, hidden: this.hidden.has(n.id), meshes: n.meshes.length,
      joint: !!n.joint, driven: !!n.joint?.driven_by, glow: !!n.def.emissive_from, display: !!n.flapFace,
      material: n.def.material, wire: this.isWire(n) ? (n.def.wire || { id: n.id }) : null }));
  }

  // --------------------------------------------------------------------------- user actions
  /** Hide or show a node. */
  setHidden(id, hidden) {
    if (hidden) this.hidden.add(id); else this.hidden.delete(id);
    this._applyVisibility();
  }

  /** Show only `id` (null restores the normal view). */
  isolate(id) {
    this.isolated = id && this.isolated !== id ? id : null;
    this._applyVisibility();
    return this.isolated;
  }

  /** Override the display colour of a node. */
  setColor(id, hex) {
    const n = this.nodes.get(id);
    if (!n) return;
    this.colors.set(id, hex);
    n.color.set(hex);
    for (const b of n.bases) b.set(hex);
    this._refresh(n);
    this.viewer.requestRender();
  }

  /** Highlight the given node ids. */
  select(ids) {
    this.selected = new Set(ids.filter((id) => this.nodes.has(id)));
    for (const n of this.nodes.values()) this._refresh(n);
    this.viewer.requestRender();
  }

  /** Show/hide the whole wiring layer (wires + connector pins). */
  setWiresVisible(on) {
    this.wiresOn = !!on;
    if (!this.wiresOn) this.hoverWire = null;
    this._applyVisibility();
    for (const n of this.nodes.values()) this._refresh(n);
    this._ghostFlaps();
    this.wires.refresh();
  }

  /** Wire X-ray: every part becomes a faint ghost, wires stay solid (harness inside a closed box). */
  setWireXray(on) {
    this.wireXray = !!on;
    for (const n of this.nodes.values()) this._refresh(n);
    this._ghostFlaps();
    this.viewer.requestRender();
  }

  /** Put wire node ids in focus (others dim, parts ghost); [] ends it. */
  setWireFocus(ids) {
    this.wireFocus = new Set((ids || []).filter((id) => this.isWire(this.nodes.get(id))));
    for (const n of this.nodes.values()) this._refresh(n);
    this._ghostFlaps();
    this.wires.refresh();
    this.viewer.requestRender();
  }

  /** Hover highlight of one wire node (null clears). */
  setHoverWire(id) {
    const next = id && this.isWire(this.nodes.get(id)) ? id : null;
    if (next === this.hoverWire) return;
    const prev = this.hoverWire;
    this.hoverWire = next;
    for (const k of [prev, next]) if (k) this._refresh(this.nodes.get(k));
    this.viewer.requestRender();
  }

  /** X-ray: every part except the selection becomes a faint ghost (on/off). */
  setXray(on) {
    this.xray = !!on;
    for (const n of this.nodes.values()) this._refresh(n);
    this.viewer.requestRender();
  }

  /** Explode factor 0..1 (fraction of the assembly radius along each node's explode vector). */
  setExplode(f) {
    this.explode = Math.max(0, Math.min(1, Number(f) || 0));
    for (const n of this.nodes.values()) this._updateMatrix(n);
    this.viewer.requestRender();
  }

  /**
   * Couple a twin `state` message to the 3D view: joints with `driven_by` follow
   * `value·scale + offset` (clamped to min/max; full-turn revolute joints wrap), nodes with
   * `emissive_from` glow by the brightness (an RGB LED's "#rrggbb" also sets the glow colour),
   * `display_from` split-flap faces flip to the device's digit.
   * @param {object} state twin state message (`devices: {id: {prop: value}}`)
   */
  applyTwinState(state) {
    const devs = state?.devices || {};
    let changed = false;
    for (const n of this.nodes.values()) {
      const j = n.joint;
      const drv = j?.driven_by;
      if (drv) {
        const raw = devs[drv.device]?.[drv.prop];
        const v = raw === undefined || raw === null || typeof raw === 'boolean' ? null : drivenJointValue(j, raw);
        if (v !== null && Math.abs(v - j.value) > 1e-6) { j.value = v; this._updateMatrix(n); changed = true; }
      }
      const ef = n.def.emissive_from;
      if (ef) {
        const raw = devs[ef.device]?.[ef.prop];
        if (raw !== undefined && raw !== null) {
          const b = brightnessOf(raw);
          const c = colorOf(raw);
          if (Math.abs(b - n.glow) > 1e-4 || (c && !c.equals(n.glowColor))) { this._setGlow(n, b, c); changed = true; }
        }
      }
    }
    this.flaps.update(devs);
    if (changed) this.viewer.requestRender();
  }

  /** Back to the scene's joint values, LEDs off. */
  resetTwin() {
    for (const n of this.nodes.values()) {
      if (n.joint) { n.joint.value = n.joint.value0; this._updateMatrix(n); }
      if (n.glowColor) this._setGlow(n, 0, new THREE.Color(n.def.emissive_from.color || '#ff3b30'));
    }
    this.viewer.requestRender();
  }

  // --------------------------------------------------------------------------- printability views
  /**
   * Paint a part's overhang faces red and its bridge faces amber (masks from
   * /api/parts/{name}/overhang, in GLB face order).
   * @returns {boolean} false when the mask does not match the mesh
   */
  setOverhang(part, data) {
    this.clearOverhang();
    const ids = (data.node_ids?.length ? data.node_ids : [part]).filter((id) => this.nodes.has(id));
    const base = new THREE.Color(THEMES[this.viewer.theme].heatBase);
    const bridge = Array.isArray(data.bridge_mask) && data.bridge_mask.length === data.mask.length ? data.bridge_mask : null;
    const painted = new Set();
    for (const id of ids) {
      const a = this.nodes.get(id).asset;
      if (!a || painted.has(a)) continue;
      if (a.faceCount !== data.mask.length) return false;
      for (const m of a.meshes) {
        const col = m.geometry.attributes.color;
        for (let f = 0; f < m.faceCount; f++) {
          const i = m.faceOffset + f;
          const c = data.mask[i] ? HEAT : bridge?.[i] ? BRIDGE : base;
          for (let k = 0; k < 3; k++) col.setXYZ(f * 3 + k, c.r, c.g, c.b);
        }
        col.needsUpdate = true;
      }
      painted.add(a);
    }
    this.heat = { part, nodeIds: new Set(ids), data };
    for (const n of this.nodes.values()) this._refresh(n);
    for (const m of this.bedMaterials) if (m.userData.part === part) { m.vertexColors = true; m.color.copy(WHITE); m.needsUpdate = true; }
    this.viewer.requestRender();
    return true;
  }

  /**
   * World bounding box of the faces the heat-map paints as `kind` ('overhang' | 'bridge'), in the
   * assembly or on the print bed (whichever shows the part); null when there are none.
   */
  heatBox(kind) {
    const heat = this.heat;
    const mask = heat && (kind === 'bridge' ? heat.data.bridge_mask : kind === 'fillet' ? heat.data.fillet_mask : heat.data.mask);
    if (!Array.isArray(mask)) return null;
    const meshes = this.bedPart === heat.part ? this.bedMeshes || []
      : [...heat.nodeIds].flatMap((id) => (this.nodes.get(id)?.content.visible ? this.nodes.get(id).meshes : []));
    this.viewer.scene.updateMatrixWorld(true);
    const box = new THREE.Box3();
    const v = new THREE.Vector3();
    for (const mesh of meshes) {
      const pos = mesh.geometry.attributes.position;  // non-indexed: face f = vertices 3f..3f+2
      const { faceOffset = 0, faceCount = 0 } = mesh.userData;
      for (let f = 0; f < faceCount; f++) {
        if (!mask[faceOffset + f]) continue;
        for (let k = 0; k < 3; k++) box.expandByPoint(v.fromBufferAttribute(pos, f * 3 + k).applyMatrix4(mesh.matrixWorld));
      }
    }
    return box.isEmpty() ? null : box;
  }

  /** Remove the overhang heat-map. */
  clearOverhang() {
    const part = this.heat?.part;
    this.heat = null;
    for (const n of this.nodes.values()) this._refresh(n);
    for (const m of this.bedMaterials) {
      if (m.userData.part === part && m.vertexColors) { m.vertexColors = false; m.color.copy(m.userData.color); m.needsUpdate = true; }
    }
    this.viewer.requestRender();
  }

  /**
   * Show one printed part alone in its print orientation on the printer bed.
   * @param {object} part entry of /api/parts
   * @param {object|null} printer printer profile (build_x/y/z in mm)
   */
  showBed(part, printer) {
    this.hideBed();
    const ids = part.node_ids?.length ? part.node_ids : [part.name];
    const n = ids.map((id) => this.nodes.get(id)).find((x) => x?.asset);
    if (!n) return false;
    const t = THEMES[this.viewer.theme];
    const bx = Number(printer?.build_x) || 220;
    const by = Number(printer?.build_y) || 220;
    const bz = Number(printer?.build_z) || 250;
    const g = this.viewer.bed;
    // Translucent plate: overhang faces point down, so the part must stay visible from below.
    const plateMat = new THREE.MeshStandardMaterial({ color: t.bed, roughness: 0.92, metalness: 0,
      transparent: true, opacity: 0.72 });
    const plate = new THREE.Mesh(new THREE.BoxGeometry(bx, by, 2), plateMat);
    plate.position.z = -1.01;
    const pts = [];
    for (let x = -bx / 2; x <= bx / 2 + 1e-6; x += 10) pts.push(x, -by / 2, 0, x, by / 2, 0);
    for (let y = -by / 2; y <= by / 2 + 1e-6; y += 10) pts.push(-bx / 2, y, 0, bx / 2, y, 0);
    const lineGeo = new THREE.BufferGeometry();
    lineGeo.setAttribute('position', new THREE.Float32BufferAttribute(pts, 3));
    const lines = new THREE.LineSegments(lineGeo, new THREE.LineBasicMaterial({ color: t.bedLine, transparent: true, opacity: 0.6 }));
    const vol = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(bx, by, bz)),
      new THREE.LineBasicMaterial({ color: t.bedLine, transparent: true, opacity: 0.45 }));
    vol.position.z = bz / 2;
    const heat = this.heat?.part === part.name;
    const edgeMat = new THREE.LineBasicMaterial({ color: t.edge, transparent: true, opacity: t.edgeOpacity });
    const holder = new THREE.Group();
    const partMats = [];
    const bedMeshes = [];
    n.asset.meshes.forEach((m, i) => {
      const base = n.bases[i] || n.color;
      const mat = new THREE.MeshStandardMaterial({ color: heat ? WHITE : base.clone(), vertexColors: heat,
        ...(KIND_STYLE.printed), side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 });
      mat.userData = { part: part.name, color: base.clone() };
      partMats.push(mat);
      const mesh = new THREE.Mesh(m.geometry, mat);
      mesh.userData = { nodeId: n.id, shared: true, faceOffset: m.faceOffset, faceCount: m.faceCount };
      bedMeshes.push(mesh);
      const edges = new THREE.LineSegments(m.edges, edgeMat);
      edges.userData.shared = true;
      holder.add(mesh, edges);
    });
    const rot = Array.isArray(part.print_rotation) ? part.print_rotation : [0, 0, 0];
    holder.applyMatrix4(rotationXYZ(...rot));
    holder.updateMatrixWorld(true);
    const box = new THREE.Box3().setFromObject(holder);
    const c = box.getCenter(new THREE.Vector3());
    holder.position.add(new THREE.Vector3(-c.x, -c.y, -box.min.z));
    g.add(plate, lines, vol, holder);
    this.bedMaterials = [...partMats, edgeMat, plateMat];
    this.bedMeshes = bedMeshes;
    this._applyClipping(this.clipping);
    this.bedPart = part.name;
    this.bedSize = box.getSize(new THREE.Vector3()).toArray();
    g.visible = true;
    if (!this.bedPart) this._gridBefore = this.viewer.gridOn;  // restored by hideBed()
    this.viewer.setGridVisible(false);
    this._applyVisibility();
    return true;
  }

  /** Region to frame when entering the bed view: the part plus ≥ ±60 mm of bed around it. */
  bedFocusBox() {
    const box = new THREE.Box3().setFromObject(this.viewer.bed.children.at(-1) || this.viewer.bed);
    return box.union(new THREE.Box3(new THREE.Vector3(-60, -60, 0), new THREE.Vector3(60, 60, 10)));
  }

  /** Leave the print-bed view. */
  hideBed() {
    const g = this.viewer.bed;
    for (const c of [...g.children]) {
      g.remove(c);
      c.traverse((o) => {  // part geometry is shared with the assembly; helpers are not
        if (!o.userData.shared) { o.geometry?.dispose?.(); o.material?.dispose?.(); }
      });
    }
    for (const m of this.bedMaterials) m.dispose();
    this.bedMaterials = [];
    this.bedMeshes = [];
    g.visible = false;
    if (this.bedPart) this.viewer.setGridVisible(this._gridBefore ?? true);
    this.bedPart = null;
    this._applyVisibility();
  }

  /** Re-apply theme colours (edges, heat-map base). */
  setTheme() {
    for (const n of this.nodes.values()) this._refresh(n);
    if (this.heat) this.setOverhang(this.heat.part, this.heat.data);
    this.viewer.requestRender();
  }

  // --------------------------------------------------------------------------- test/debug hooks
  /** Plain-data snapshot of a node for tests and debugging. */
  debugNode(id) {
    const n = this.nodes.get(id);
    if (!n) return null;
    this.viewer.scene.updateMatrixWorld(true);
    let visible = true;
    for (let p = n.content; p; p = p.parent) if (!p.visible) visible = false;
    const pos = new THREE.Vector3().setFromMatrixPosition(n.frame.matrixWorld);
    const e = n.mat.emissive;
    let heat = null;  // faces painted by the overhang heat-map
    if (n.mat.vertexColors && n.asset) {
      heat = { overhang: 0, bridge: 0 };
      const near = (c, r, g, b) => Math.abs(c.r - r) + Math.abs(c.g - g) + Math.abs(c.b - b) < 0.02;
      for (const m of n.asset.meshes) {
        const col = m.geometry.attributes.color;
        for (let f = 0; f < m.faceCount; f++) {
          const [r, g, b] = [col.getX(f * 3), col.getY(f * 3), col.getZ(f * 3)];
          if (near(HEAT, r, g, b)) heat.overhang += 1; else if (near(BRIDGE, r, g, b)) heat.bridge += 1;
        }
      }
    }
    return { id, visible, meshes: n.meshes.length, glow: n.glow, glowColor: n.glowColor ? `#${n.glowColor.getHexString()}` : null,
      emissive: n.mat.emissiveIntensity * Math.max(e.r, e.g, e.b), jointValue: n.joint ? n.joint.value : null,
      position: pos.toArray(), world: [...n.frame.matrixWorld.elements], vertexColors: n.mat.vertexColors,
      opacity: n.mat.opacity, heat, colors: n.mats.map((m) => `#${m.color.getHexString()}`),
      emissiveHex: `#${e.getHexString()}`, transparent: n.mat.transparent, kind: n.def.kind || null };
  }
}
