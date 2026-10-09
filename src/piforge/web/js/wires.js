// PiForge GUI — wiring harness layer: wire queries + text, connector pin markers, wire/pin picking.
// scene.json contract: top-level `connectors: [{id, ref, pin, label, node, pos, dir}]` (world mm) and
// `kind: "wire"` nodes `{mesh (tube, world frame), color, wire: {id, from, to, net, signal,
// color_name, gauge_awg, length_mm, cable}}` with `from/to = {ref, pin, label, connector}`.
import * as THREE from 'three';

const PIN_HEX = 0xd4a72c;  // gilded pin marker
const PICK_PX = 6;  // hover/click tolerance around a wire tube (screen pixels)
const PIN_PX = 8;  // … around a connector marker
const _v = new THREE.Vector3();
const _a = new THREE.Vector3();
const _b = new THREE.Vector3();
const _onRay = new THREE.Vector3();
const _inv = new THREE.Matrix4();
const IDENTITY = new THREE.Matrix4();

/**
 * Bounding spheres of runs of 96 triangles (cached on the geometry): a sweep's triangles follow the
 * path, so the ray test skips almost every run of a long wire.
 */
function chunksOf(g) {
  if (g.userData.pickChunks) return g.userData.pickChunks;
  const pos = g.attributes.position;
  const idx = g.index;
  const count = idx ? idx.count : pos.count;
  const out = [];
  const box = new THREE.Box3();
  const step = 96 * 3;
  for (let start = 0; start < count; start += step) {
    const end = Math.min(count, start + step);
    box.makeEmpty();
    for (let i = start; i < end; i++) box.expandByPoint(_v.fromBufferAttribute(pos, idx ? idx.getX(i) : i));
    const sph = box.getBoundingSphere(new THREE.Sphere());
    out.push({ start, end, center: sph.center, radius: sph.radius });
  }
  g.userData.pickChunks = out;
  return out;
}

/** "pin 19" for a numbered pin, the name for a named one ("SER", "GPIO17"). */
export function pinText(pin) {
  const p = String(pin ?? '').trim();
  if (!p) return '';
  return /^\d+$/.test(p) ? `pin ${p}` : p;
}

/**
 * One wire end. A header-style pin (numbered, labelled with its own name) reads
 * "U1 pin 19 (GPIO10 / SPI0 MOSI)"; a named pin reads "U2 SER" (its label then names the contact
 * it is reached through, e.g. a perfboard header, shown in the details instead).
 */
export function endText(end) {
  if (!end) return '?';
  const pin = String(end.pin ?? '').trim();
  const number = String(end.number ?? '').trim();
  const label = String(end.label ?? '').trim();
  const ref = end.ref || '?';
  if (/^\d+$/.test(pin)) return `${ref} pin ${pin}${label && label !== pin ? ` (${label})` : ''}`;
  const own = label && (label === pin || label.startsWith(`${pin} `) || label.startsWith(`${pin}/`));
  if (number && own) return `${ref} pin ${number} (${label})`;
  return `${ref}${pin ? ` ${pin}` : ''}`;
}

/** The contact an end is reached through when its label is not the pin's own name ("JP.1 (U2.SER)"). */
export function endVia(end) {
  const pin = String(end?.pin ?? '').trim();
  const label = String(end?.label ?? '').trim();
  if (!label || label === pin || /^\d+$/.test(pin) || label.startsWith(`${pin} `) || label.startsWith(`${pin}/`)) return '';
  return label;
}

/** "blue 22 AWG". */
export function wireSpec(w) {
  return [w.color_name, w.gauge_awg ? `${w.gauge_awg} AWG` : null].filter(Boolean).join(' ');
}

/** Hover text: "W12 · U1 pin 19 (GPIO10 / SPI0 MOSI) → U2 SER · net MOSI · blue 22 AWG · 85 mm · cable W-SPI". */
export function wireTooltip(w) {
  const len = Number(w.length_mm);
  return [w.id, `${endText(w.from)} → ${endText(w.to)}`, w.net ? `net ${w.net}` : null, wireSpec(w) || null,
    Number.isFinite(len) && len > 0 ? `${Math.round(len)} mm` : null, w.cable ? `cable ${w.cable}` : null]
    .filter(Boolean).join(' · ');
}

/** Tree/group label of a wire: its cable, else its net. */
export function wireGroup(w) {
  return w?.cable ? `cable ${w.cable}` : w?.net ? `net ${w.net}` : 'loose';
}

/** Connector pin markers + wire picking for a {@link SceneModel}. */
export class WireLayer {
  /**
   * @param {import('./scene.js').SceneModel} scene
   * @param {import('./viewer.js').Viewer} viewer
   */
  constructor(scene, viewer) {
    this.scene = scene;
    this.viewer = viewer;
    this.group = new THREE.Group();
    this.group.name = 'connectors';
    viewer.root.add(this.group);
    this.connectors = new Map();  // id → {def, mesh, pos: Vector3}
    this.focusConnector = null;
    this.hoverConnector = null;
    this._geo = null;
    this._mats = null;
  }

  /** Wire nodes of the scene. */
  wireNodes() { return [...this.scene.nodes.values()].filter((n) => this.scene.isWire(n)); }

  /** The `wire` dict of every wire node (+ `node`: its scene node id, `color`: the node colour). */
  list() {
    return this.wireNodes().map((n) => ({ ...(n.def.wire || {}), id: n.def.wire?.id || n.id, node: n.id,
      color: n.def.color || null }));
  }

  /** Wire node id of a wire id ("W12"), or of a node id. */
  nodeOf(wireId) {
    if (!wireId) return null;
    for (const n of this.wireNodes()) if (n.def.wire?.id === wireId || n.id === wireId) return n.id;
    return null;
  }

  /** Wire dict of a wire node id. */
  wireOf(nodeId) {
    const n = this.scene.nodes.get(nodeId);
    return this.scene.isWire(n) ? { ...(n.def.wire || {}), id: n.def.wire?.id || n.id, node: n.id, color: n.def.color } : null;
  }

  /** Connector def by id. */
  connector(id) { return this.connectors.get(id)?.def || null; }

  /** Ids of the connectors a wire plugs into (from, to). */
  endsOf(w) { return [w?.from?.connector, w?.to?.connector].filter(Boolean); }

  /** Wire node ids plugged into a connector. */
  wiresAt(connectorId) {
    return this.list().filter((w) => this.endsOf(w).includes(connectorId)).map((w) => w.node);
  }

  /** Nets present at a connector (via its wires). */
  netsAt(connectorId) {
    return [...new Set(this.list().filter((w) => this.endsOf(w).includes(connectorId)).map((w) => w.net).filter(Boolean))];
  }

  /** Wire node ids on any of `nets`. */
  wiresOnNets(nets) {
    const set = new Set(nets);
    return this.list().filter((w) => set.has(w.net)).map((w) => w.node);
  }

  /** World position of a connector (or a wire end), as a Vector3. */
  posOf(connectorId) {
    const c = this.connectors.get(connectorId);
    return c ? c.pos.clone() : null;
  }

  // ------------------------------------------------------------------------- markers
  /** (Re)build the pin markers from scene.json `connectors` (tolerates a scene without them). */
  rebuild(sceneDef) {
    for (const c of this.connectors.values()) this.group.remove(c.mesh);
    this.connectors.clear();
    const defs = Array.isArray(sceneDef?.connectors) ? sceneDef.connectors : [];
    if (!this._geo) {
      // pin = a short gilded stub pointing out of the connector, with a round head
      this._geo = new THREE.SphereGeometry(0.75, 14, 10);
      this._mats = { pin: new THREE.MeshStandardMaterial({ color: PIN_HEX, roughness: 0.3, metalness: 0.85 }),
        hot: new THREE.MeshStandardMaterial({ color: PIN_HEX, roughness: 0.3, metalness: 0.6, emissive: 0xffffff, emissiveIntensity: 0.35 }),
        dim: new THREE.MeshStandardMaterial({ color: PIN_HEX, roughness: 0.4, metalness: 0.6, transparent: true, opacity: 0.25, depthWrite: false }) };
    }
    for (const d of defs) {
      if (!d || !d.id || !Array.isArray(d.pos) || d.pos.length !== 3 || d.pos.some((v) => !Number.isFinite(Number(v)))) continue;
      const pos = new THREE.Vector3(...d.pos.map(Number));
      const mesh = new THREE.Mesh(this._geo, this._mats.pin);
      mesh.position.copy(pos);
      mesh.userData = { connectorId: d.id };
      mesh.renderOrder = 2;
      this.group.add(mesh);
      this.connectors.set(d.id, { def: d, mesh, pos });
    }
    this.refresh();
  }

  /** Materials (for the section plane). */
  materials() { return this._mats ? Object.values(this._mats) : []; }

  /** Restyle markers: the focused net's pins glow, others dim while wires are in focus. */
  refresh() {
    if (!this._mats) return;
    const focus = this.scene.wireFocus;
    const hot = new Set();
    for (const id of focus) for (const c of this.endsOf(this.wireOf(id))) hot.add(c);
    if (this.focusConnector) hot.add(this.focusConnector);
    if (this.hoverConnector) hot.add(this.hoverConnector);
    const late = this.scene.wireMode;
    for (const m of [this._mats.pin, this._mats.hot]) if (m.transparent !== late) { m.transparent = late; m.needsUpdate = true; }
    for (const [id, c] of this.connectors) {
      c.mesh.renderOrder = late ? 11 : 2;  // after the ghost parts (see SceneModel._refreshWire)
      c.mesh.material = hot.has(id) ? this._mats.hot : focus.size ? this._mats.dim : this._mats.pin;
      c.mesh.scale.setScalar(hot.has(id) ? 1.6 : 1);
    }
    this.viewer.requestRender();
  }

  /** Hover highlight of a connector marker (null clears). */
  setHoverConnector(id) {
    if (id === this.hoverConnector) return;
    this.hoverConnector = id;
    this.refresh();
  }

  // ------------------------------------------------------------------------- picking
  _pxPerMm(dist) {
    const cam = this.viewer.camera;
    const h = this.viewer.canvas.clientHeight || 1;
    return h / (2 * Math.max(dist, 1e-3) * Math.tan(THREE.MathUtils.degToRad(cam.fov) / 2));
  }

  /**
   * Wire or connector under a screen point (tolerance in pixels), unless a solid part is in front.
   * @param {THREE.Raycaster} raycaster already set from the camera for this point
   * @param {{distance:number}|null} partHit first visible part hit (blocks wires behind it)
   * @returns {{nodeId:string|null, connector?:string, point:THREE.Vector3, distance:number}|null}
   */
  pick(clientX, clientY, raycaster, partHit) {
    if (!this.scene.wiresOn || this.scene.bedPart || this.scene.isolated) return null;
    const ray = raycaster.ray;
    const ghosts = this.scene.wireMode || this.scene.xray;  // parts are see-through: they do not block
    const blockAt = !ghosts && partHit ? partHit.distance : Infinity;
    // connector pins: nearest projected marker within PIN_PX
    const rect = this.viewer.canvas.getBoundingClientRect();
    let best = null;
    if (this.group.visible) {
      for (const [id, c] of this.connectors) {
        const p = _v.copy(c.pos).project(this.viewer.camera);
        if (p.z > 1) continue;
        const d = Math.hypot(rect.left + ((p.x + 1) / 2) * rect.width - clientX, rect.top + ((1 - p.y) / 2) * rect.height - clientY);
        const dist = c.pos.distanceTo(ray.origin);
        if (d <= PIN_PX && dist <= blockAt + 2.5 && (!best || d < best.px)) best = { px: d, connector: id, nodeId: null, point: c.pos.clone(), distance: dist };
      }
    }
    // wires: ray ↔ triangle-edge distance (tubes are thin: an exact ray hit is too fiddly)
    let bestWire = null;
    const worldRay = raycaster.ray;
    for (const n of this.wireNodes()) {
      if (!n.content.visible) continue;
      for (const mesh of n.meshes) {
        const g = mesh.geometry;
        // tubes are written in the world frame (identity matrix); honour a matrix anyway
        const ray = mesh.matrixWorld.equals(IDENTITY) ? worldRay : worldRay.clone().applyMatrix4(_inv.copy(mesh.matrixWorld).invert());
        const sph = g.boundingSphere;
        const centerDist = sph.center.distanceTo(ray.origin);
        const tol = PICK_PX / this._pxPerMm(centerDist);
        if (ray.distanceSqToPoint(sph.center) > (sph.radius + tol) ** 2) continue;
        const pos = g.attributes.position;
        const idx = g.index;
        for (const ch of chunksOf(g)) {
          const cd = ch.center.distanceTo(ray.origin);
          const ctol = PICK_PX / this._pxPerMm(cd);
          if (ray.distanceSqToPoint(ch.center) > (ch.radius + ctol) ** 2) continue;
          for (let i = ch.start; i < ch.end; i += 3) {
            for (let k = 0; k < 3; k++) {
              const ia = idx ? idx.getX(i + k) : i + k;
              const ib = idx ? idx.getX(i + ((k + 1) % 3)) : i + ((k + 1) % 3);
              _a.fromBufferAttribute(pos, ia);
              _b.fromBufferAttribute(pos, ib);
              const d2 = ray.distanceSqToSegment(_a, _b, _onRay);
              const t = _onRay.distanceTo(ray.origin);
              const tolHere = PICK_PX / this._pxPerMm(t);
              if (d2 > tolHere * tolHere || t > blockAt + 1.0) continue;
              const score = Math.sqrt(d2) * this._pxPerMm(t) + t * 1e-4;  // pixels, then depth
              if (!bestWire || score < bestWire.score) bestWire = { score, nodeId: n.id, point: _onRay.clone(), distance: t };
            }
          }
        }
      }
    }
    // a pin wins unless the cursor is clearly nearer a wire (pins sit at wire ends)
    if (best && (!bestWire || best.px <= bestWire.score + 2)) return best;
    return bestWire ? { nodeId: bestWire.nodeId, point: bestWire.point, distance: bestWire.distance } : null;
  }

  /** Plain-data snapshot for tests: connectors (screen-independent) and wire nodes. */
  debug() {
    return {
      on: this.scene.wiresOn, xray: this.scene.wireXray, focus: [...this.scene.wireFocus],
      hover: this.scene.hoverWire, focusConnector: this.focusConnector,
      connectors: [...this.connectors.keys()], markersVisible: this.group.visible && this.scene.viewer.root.visible,
      wires: this.list().map((w) => w.id),
    };
  }

  /** Screen position (client px) of a connector or of the middle vertex of a wire node. */
  screenOf({ connector, node }) {
    let p = null;
    if (connector) p = this.posOf(connector);
    if (node) {
      const n = this.scene.nodes.get(node);
      const g = n?.meshes[0]?.geometry;
      if (g) {
        // where the tube crosses the middle plane of its longest horizontal extent (on its surface)
        const pos = g.attributes.position;
        const idx = g.index;
        const box = g.boundingBox;
        const size = box.getSize(new THREE.Vector3());
        // horizontal extent first: the vertical legs end at connector pins
        const axis = Math.max(size.x, size.y) > 4 ? (size.x >= size.y ? 'x' : 'y') : 'z';
        const mid = (box.min[axis] + box.max[axis]) / 2;
        const count = idx ? idx.count : pos.count;
        let bestZ = -Infinity;
        for (let i = 0; i < count; i += 3) {
          for (let k = 0; k < 3; k++) {
            _a.fromBufferAttribute(pos, idx ? idx.getX(i + k) : i + k);
            _b.fromBufferAttribute(pos, idx ? idx.getX(i + ((k + 1) % 3)) : i + ((k + 1) % 3));
            const da = _a[axis] - mid;
            const db = _b[axis] - mid;
            if (da * db > 0 || da === db) continue;
            const q = _a.clone().lerp(_b, da / (da - db));
            if (q.z > bestZ) { bestZ = q.z; p = q; }  // the top of the tube: visible from above
          }
        }
        if (p) p.applyMatrix4(n.meshes[0].matrixWorld);
      }
    }
    if (!p) return null;
    this.viewer.scene.updateMatrixWorld(true);
    const q = p.project(this.viewer.camera);
    const rect = this.viewer.canvas.getBoundingClientRect();
    return { x: rect.left + ((q.x + 1) / 2) * rect.width, y: rect.top + ((1 - q.y) / 2) * rect.height };
  }
}
