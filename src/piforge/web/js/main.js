// PiForge GUI — entry point: loads the project, wires the viewer, tree, tabs, toolbar, rebuild
// events and the twin ↔ 3D coupling. Exposes `window.piforge` for debugging and E2E tests.
import { Socket, getJSON, postJSON } from './api.js';
import { ChecksPanel, findingTargets } from './checks.js';
import { ElecPanel } from './elec.js';
import { PrintPanel } from './print.js';
import { BRIDGE_HEX, FILLET_HEX, HEAT_HEX, SceneModel } from './scene.js';
import { SpicePanel } from './spice.js';
import { Tree } from './tree.js';
import { TwinPanel } from './twin.js';
import { ago, h, icon, num, replace, sizeMM, toast } from './ui.js';
import { Viewer } from './viewer.js';
import { endText, endVia, wireSpec, wireTooltip } from './wires.js';

const $ = (sel) => document.querySelector(sel);
const store = {
  get(k, d) { try { return localStorage.getItem(`piforge.${k}`) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(`piforge.${k}`, v); } catch { /* private mode */ } },
};

const app = { project: null, report: null, parts: [], selected: [], tab: 'checks', version: '', overhang: null, bed: null,
  connector: null };
const theme = store.get('theme', 'dark');
document.documentElement.dataset.theme = theme;

const viewer = new Viewer($('#canvas-host'), { theme });
const scene = new SceneModel(viewer);
const tree = new Tree($('#tree'), {
  onToggle: (id, visible) => { scene.setHidden(id, !visible); refreshTree(); },
  onToggleKind: (ids, visible) => { for (const id of ids) scene.setHidden(id, !visible); refreshTree(); },
  onSelect: (id, additive) => select([id], { additive }),
  onSelectMany: (ids) => select(ids),
  onIsolate: (id) => { const iso = scene.isolate(id); tree.setIsolated(iso); if (iso) viewer.fit(scene.boxOf([iso])); },
  onColor: (id, hex) => scene.setColor(id, hex),
  onFocus: (id) => select([id], { focus: true }),
});
const checks = new ChecksPanel($('#tab-checks'), {
  resolve: (f) => nodesOfFinding(f),
  onSelect: (ids) => select(ids, { focus: true }),
});
const elec = new ElecPanel($('#tab-elec'), { onSelectWire: (wireId) => selectWire(wireId) });
const spice = new SpicePanel($('#tab-spice'));
const twin = new TwinPanel($('#tab-twin'), { scene, onStatus: (running) => $('#tab-btn-twin').classList.toggle('live', running) });
const print = new PrintPanel($('#tab-print'), {
  onSelectPart: (name) => selectPart(name, true),
  onOverhang: (part) => toggleOverhang(part),
  onBed: (part) => toggleBed(part),
});

// ------------------------------------------------------------------------------- selection
function nodesOfPart(name) {
  const part = app.parts.find((p) => p.name === name);
  const ids = part?.node_ids?.filter((id) => scene.nodes.has(id)) || [];
  if (ids.length) return ids;
  return [...scene.nodes.values()].filter((n) => n.id === name || n.def.name === name).map((n) => n.id);
}

/** Scene node ids a finding refers to (printed parts, assembly node pairs, joints). */
function nodesOfFinding(f) {
  const { parts, nodes } = findingTargets(f);
  const ids = new Set();
  for (const p of parts) for (const id of nodesOfPart(p)) ids.add(id);
  for (const id of nodes) if (scene.nodes.has(id)) ids.add(id);
  return [...ids];
}

function selectPart(name, focus = false) {
  if (!name) return;
  const ids = nodesOfPart(name);
  if (!ids.length) { toast(`“${name}” is not in the 3D scene`, 'warning', 2500); return; }
  select(ids, { focus });
}

function select(ids, { additive = false, focus = false } = {}) {
  let next = ids.filter(Boolean);
  if (additive) {
    const cur = new Set(app.selected);
    for (const id of next) { if (cur.has(id)) cur.delete(id); else cur.add(id); }
    next = [...cur];
  }
  app.selected = next;
  app.connector = null;
  scene.wires.focusConnector = null;
  scene.select(next);
  // a selected wire is the subject: other wires dim, parts become ghosts
  scene.setWireFocus(next.filter((id) => scene.isWire(scene.nodes.get(id))));
  tree.setSelected(next);
  syncWireRows();
  showInfo();
  if (focus && next.length) viewer.fit(scene.boxOf(next));
}

// ------------------------------------------------------------------------------- wiring harness
function syncWireRows() {
  const ids = [...scene.wireFocus].map((id) => scene.wires.wireOf(id)?.id).filter(Boolean);
  elec.highlightWires(ids);
}

/** Select a harness wire by its wire id ("W12") — from the Electronics tables. */
function selectWire(wireId) {
  const id = scene.wires.nodeOf(wireId);
  if (!id) { toast(`Wire ${wireId} is not in the 3D scene`, 'warning', 2500); return; }
  if (!scene.wiresOn) setWiresOn(true);
  select([id], { focus: true });
}

/** Click on a connector pin: highlight every wire on its net(s) and list them. */
function selectConnector(cid) {
  const nets = scene.wires.netsAt(cid);
  const wires = nets.length ? scene.wires.wiresOnNets(nets) : scene.wires.wiresAt(cid);
  app.selected = [];
  scene.select([]);
  tree.setSelected([]);
  app.connector = cid;
  scene.wires.focusConnector = cid;
  scene.setWireFocus(wires);
  syncWireRows();
  showInfo();
}

/** Fly the camera to a connector (keeps the viewing direction, ~80 mm away). */
function flyToConnector(cid) {
  const pos = scene.wires.posOf(cid);
  if (!pos) { toast(`Connector ${cid} has no position`, 'warning', 2500); return; }
  const dir = viewer.camera.position.clone().sub(viewer.controls.target).normalize();
  viewer.animateTo(pos.clone().addScaledVector(dir, 80), pos, 450);
  scene.wires.focusConnector = cid;
  scene.wires.refresh();
}

function setWiresOn(on) {
  scene.setWiresVisible(on);
  const b = $('[data-action="wires"]');
  b.classList.toggle('on', on);
  b.setAttribute('aria-pressed', String(on));
  store.set('wires', on ? '1' : '0');
  if (!on && (app.connector || [...app.selected].some((id) => scene.isWire(scene.nodes.get(id))))) select([]);
}

function connectorTitle(c) {
  const pin = c.pin !== undefined && c.pin !== null && c.pin !== '' ? endText({ ref: c.ref, pin: c.pin }) : c.ref || c.id;
  return { title: pin, label: c.label && String(c.label) !== String(c.pin) ? c.label : '' };
}

function endButton(end, testid) {
  const cid = end?.connector;
  const known = cid && scene.wires.connector(cid);
  return h('button.wire-end-btn', { type: 'button', disabled: !known, dataset: { testid, connector: cid || '' },
    title: known ? 'Fly the camera to this end' : 'This end has no connector position',
    onclick: () => flyToConnector(cid) }, icon('target', 12), h('span', {}, endText(end),
    endVia(end) ? h('span.dim', {}, ` · ${endVia(end)}`) : null));
}

function showWireInfo(card, nodeId) {
  const w = scene.wires.wireOf(nodeId);
  const len = Number(w.length_mm);
  const rows = [['net', w.net], ['signal', w.signal && w.signal !== w.net ? w.signal : null], ['wire', wireSpec(w)],
    ['length', Number.isFinite(len) && len > 0 ? `${num(len, 1)} mm` : null], ['cable', w.cable]];
  replace(card,
    h('div.info-title', {}, h('span.wire-dot', { style: { background: w.color || '#888' } }), w.id,
      w.cable ? h('span.badge-mini', {}, w.cable) : null, h('span.dim', {}, nodeId !== w.id ? nodeId : '')),
    h('div.wire-ends', {}, h('span.wire-end-k', {}, 'from'), endButton(w.from, 'wire-end-from'),
      h('span.wire-end-k', {}, 'to'), endButton(w.to, 'wire-end-to')),
    h('dl', {}, rows.filter(([, v]) => v).map(([k, v]) => [h('dt', {}, k), h('dd', {}, v)])),
    h('div.info-actions', {},
      h('button.btn.small', { onclick: () => viewer.fit(scene.boxOf([nodeId])) }, icon('fit', 12), 'Focus'),
      h('button.btn.small', { onclick: () => { showTab('elec'); if (elec.view !== 'wiring' && elec.view !== 'cut') { elec.view = 'wiring'; elec.data && elec._draw(); } syncWireRows(); } },
        icon('list', 12), 'In table'),
      h('button.btn.small', { onclick: () => select([]) }, icon('x', 12), 'Clear')));
  card.dataset.kind = 'wire';
  card.hidden = false;
}

function showConnectorInfo(card, cid) {
  const c = scene.wires.connector(cid) || { id: cid };
  const { title, label } = connectorTitle(c);
  const nets = scene.wires.netsAt(cid);
  const wires = [...scene.wireFocus].map((id) => scene.wires.wireOf(id)).filter(Boolean)
    .sort((a, b) => String(a.id).localeCompare(String(b.id), undefined, { numeric: true }));
  const here = new Set(scene.wires.wiresAt(cid));
  replace(card,
    h('div.info-title', {}, h('span.pin-dot'), title, h('span.dim', {}, label)),
    h('div.dim.conn-sub', {}, nets.length ? `net ${nets.join(', ')} · ${wires.length} wire${wires.length === 1 ? '' : 's'}`
      : 'no wire on this pin'),
    h('div.net-wires', {}, wires.map((w) => h('button.net-wire', { type: 'button', class: here.has(w.node) ? 'here' : null,
      dataset: { testid: `net-wire-${w.id}` }, title: wireTooltip(w), onclick: () => select([w.node]) },
    h('span.wire-dot', { style: { background: w.color || '#888' } }), h('strong', {}, w.id),
    h('span.net-wire-ends', {}, `${endText(w.from)} → ${endText(w.to)}`)))),
    h('div.info-actions', {},
      h('button.btn.small', { onclick: () => viewer.fit(scene.boxOf([...scene.wireFocus])) }, icon('fit', 12), 'Focus net'),
      h('button.btn.small', { onclick: () => select([]) }, icon('x', 12), 'Clear')));
  card.dataset.kind = 'connector';
  card.hidden = false;
}

function showInfo() {
  const card = $('#vp-info');
  const ids = app.selected;
  // measuring is modal: its hint sits at the bottom of the viewport where this card would be
  if (viewer.measure.active) { card.hidden = true; return; }
  card.dataset.kind = '';
  if (app.connector) { showConnectorInfo(card, app.connector); return; }
  if (!ids.length) { card.hidden = true; return; }
  if (ids.length === 1 && scene.isWire(scene.nodes.get(ids[0]))) { showWireInfo(card, ids[0]); return; }
  if (ids.length > 1) {
    replace(card, h('div.info-title', {}, `${ids.length} parts selected`), h('div.dim', {}, ids.join(', ')));
    card.hidden = false;
    return;
  }
  const n = scene.nodes.get(ids[0]);
  if (!n) { card.hidden = true; return; }
  const d = n.def;
  const box = scene.boxOf([n.id]);
  const size = box.isEmpty() ? null : box.getSize(box.min.clone()).toArray();
  const rows = [['kind', d.kind], ['material', d.material], ['size (world)', size ? sizeMM(size) : null]];
  if (n.joint) {
    const j = n.joint;
    rows.push(['joint', `${j.type} ${j.axis?.join(',')} · ${num(j.value, 1)}${j.type === 'prismatic' ? ' mm' : '°'} [${j.min} … ${j.max}]`]);
    if (j.driven_by) rows.push(['driven by', `${j.driven_by.device}.${j.driven_by.prop}`]);
  }
  if (d.emissive_from) rows.push(['glows with', `${d.emissive_from.device}.${d.emissive_from.prop}`]);
  replace(card,
    h('div.info-title', {}, h('span.swatch-dot', { style: { background: `#${n.color.getHexString()}` } }), d.name || d.id, h('span.dim', {}, d.id)),
    h('dl', {}, rows.filter(([, v]) => v).map(([k, v]) => [h('dt', {}, k), h('dd', {}, v)])),
    h('div.info-actions', {},
      h('button.btn.small', { onclick: () => viewer.fit(scene.boxOf(ids)) }, icon('fit', 12), 'Focus'),
      h('button.btn.small', { onclick: () => { scene.setHidden(n.id, true); refreshTree(); } }, icon('eyeOff', 12), 'Hide'),
      h('button.btn.small', { onclick: () => select([]) }, icon('x', 12), 'Clear')));
  card.hidden = false;
}

function refreshTree() { tree.render(scene.list()); tree.setSelected(app.selected); }

// ------------------------------------------------------------------------------- printability views
async function toggleOverhang(part) {
  if (!part) {
    scene.clearOverhang();
    app.overhang = null;
    updateLegend();
    print.setActive({ overhang: null });
    $('[data-action="overhang"]').classList.remove('on');
    return;
  }
  try {
    const data = await getJSON(`/api/parts/${encodeURIComponent(part.name)}/overhang`);
    if (!scene.setOverhang(part.name, data)) { toast(`Overhang mask does not match the mesh of ${part.name}`, 'error'); return; }
    app.overhang = { part: part.name, data };
    print.setActive({ overhang: part.name });
    $('[data-action="overhang"]').classList.add('on');
    updateLegend();
    if (!scene.bedPart) select(nodesOfPart(part.name));
    // Overhang and bridge faces point down: look at the part from below so the painted faces show.
    const under = data.overhang_count || data.bridge_count;
    viewer.fit(scene.bedPart ? scene.bedFocusBox() : scene.boxOf(nodesOfPart(part.name)), under ? 'under' : 'iso');
  } catch (err) {
    toast(`Overhang analysis failed: ${err.message}`, 'error');
  }
}

function toggleBed(part) {
  if (!part) {
    scene.hideBed();
    app.bed = null;
    viewer.setView('iso');
  } else {
    if (!scene.showBed(part, app.project?.printer_profile)) { toast(`No mesh for ${part.name}`, 'warning'); return; }
    app.bed = part.name;
    const d = app.overhang?.part === part.name ? app.overhang.data : null;
    viewer.fit(scene.bedFocusBox(), d && (d.overhang_count || d.bridge_count) ? 'under' : 'iso');
  }
  $('[data-action="bed"]').classList.toggle('on', !!app.bed);
  print.setActive({ bed: app.bed });
  updateLegend();
  updateSectionRange();
}

function updateLegend() {
  const el = $('#vp-legend');
  const parts = [];
  if (app.bed) {
    const prof = app.project?.printer_profile;
    parts.push(h('div.legend-row', {}, icon('bed', 13), h('strong', {}, app.bed), h('span.dim', {}, 'on the bed of'),
      h('span', {}, `${app.project?.printer || 'generic'}${prof ? ` (${prof.build_x}×${prof.build_y} mm)` : ''}`)));
  }
  if (app.overhang) {
    const d = app.overhang.data;
    const faces = (n) => `${n} face${n === 1 ? '' : 's'}`;
    // Each colour row zooms the camera onto the faces it describes (port roofs are small).
    const row = (kind, color, label, count, area) => h('button.legend-row.zoomable', { type: 'button',
      dataset: { testid: `legend-${kind}` }, disabled: !count, title: count ? 'Zoom to these faces' : null,
      onclick: () => zoomHeat(kind) },
    h('span.legend-swatch', { style: { background: color } }), h('span', {}, label),
    h('span.legend-num', {}, count ? `${num(area, 1)} mm² · ${faces(count)}` : 'none'));
    parts.push(h('div.legend-row.legend-title', {}, icon('flame', 13), h('strong', {}, app.overhang.part),
      h('span.dim', {}, 'in print orientation')));
    parts.push(row('overhang', HEAT_HEX, `Needs support (> ${num(d.max_overhang_deg, 0)}° from vertical)`,
      d.overhang_count, d.overhang_area_mm2));
    if (d.bridges_excluded) {
      parts.push(row('bridge', BRIDGE_HEX, `Bridge, prints without support (span ≤ ${num(d.max_bridge_mm, 0)} mm)`,
        d.bridge_count, d.bridge_area_mm2));
    }
    if (d.fillet_count) {
      parts.push(row('fillet', FILLET_HEX, 'Bed fillet, prints without support', d.fillet_count, d.fillet_area_mm2));
    }
  }
  replace(el, parts);
  el.hidden = !parts.length;
}

/** Frame the faces the heat-map paints as `kind` ('overhang' | 'bridge'), seen from below. */
function zoomHeat(kind) {
  const box = scene.heatBox(kind);
  if (!box) return;
  const diag = box.getSize(box.min.clone()).length();
  viewer.fit(box.expandByScalar(Math.max(4, Math.min(20, diag * 0.3))), 'under');
}

// ------------------------------------------------------------------------------- toolbar
function updateSectionRange(reset = false) {
  const box = viewer.modelBox();
  const slider = $('#section-slider');
  const axis = viewer.section.axis;
  if (!box || box.isEmpty()) return;
  const lo = box.min[axis] - 1;
  const hi = box.max[axis] + 1;
  slider.min = String(lo);
  slider.max = String(hi);
  slider.step = String(Math.max(0.1, (hi - lo) / 400));
  if (reset || viewer.section.value < lo || viewer.section.value > hi) slider.value = String((lo + hi) / 2);
  applySection();
}

function applySection() {
  const value = Number($('#section-slider').value);
  viewer.setSection({ value });
  $('#section-value').textContent = `${viewer.section.axis} = ${num(value, 1)} mm`;
}

function bindToolbar() {
  const on = (sel, fn) => $(sel).addEventListener('click', fn);
  on('[data-action="fit"]', () => viewer.setView(null));
  for (const v of ['iso', 'front', 'top', 'right']) on(`[data-action="view-${v}"]`, () => viewer.setView(v));
  on('[data-action="section"]', (e) => {
    const enabled = !viewer.section.enabled;
    e.currentTarget.classList.toggle('on', enabled);
    $('#section-panel').hidden = !enabled;
    viewer.setSection({ enabled });
    if (enabled) updateSectionRange(true);
  });
  for (const ax of ['x', 'y', 'z']) {
    on(`[data-axis="${ax}"]`, () => {
      viewer.setSection({ axis: ax });
      document.querySelectorAll('[data-axis]').forEach((b) => b.classList.toggle('on', b.dataset.axis === ax));
      updateSectionRange(true);
    });
  }
  on('[data-action="section-flip"]', (e) => {
    viewer.setSection({ flip: !viewer.section.flip });
    e.currentTarget.classList.toggle('on', viewer.section.flip);
    applySection();
  });
  $('#section-slider').addEventListener('input', applySection);
  on('[data-action="measure"]', (e) => {
    const active = !viewer.measure.active;
    viewer.measure.setActive(active);
    e.currentTarget.classList.toggle('on', active);
    $('#vp-measure').hidden = !active;
    showInfo();
    $('#vp-measure').textContent = 'Click two points on the model (corners snap) · Esc to finish';
  });
  viewer.measure.onChange = (res, count) => {
    if (!viewer.measure.active) return;
    const el = $('#vp-measure');
    if (res) {
      replace(el, h('strong', {}, `${res.distance.toFixed(2)} mm`),
        h('span.dim', {}, `Δx ${res.dx.toFixed(2)} · Δy ${res.dy.toFixed(2)} · Δz ${res.dz.toFixed(2)} mm`));
    } else el.textContent = count === 1 ? 'Click the second point' : 'Click two points on the model (corners snap) · Esc to finish';
  };
  const explode = $('#explode');
  explode.addEventListener('input', () => {
    scene.setExplode(Number(explode.value) / 100);
    $('#explode-value').textContent = `${explode.value} %`;
  });
  on('[data-action="bed"]', () => {
    if (app.bed) { toggleBed(null); return; }
    const part = selectedPrintedPart() || app.parts[0];
    if (part) toggleBed(part); else toast('No printed parts in this project', 'warning');
  });
  on('[data-action="overhang"]', () => {
    if (app.overhang) { toggleOverhang(null); return; }
    const part = selectedPrintedPart() || (app.bed && app.parts.find((p) => p.name === app.bed));
    if (!part) { toast('Select a printed part first (tree, 3D view or Print tab)', 'info', 3000); return; }
    toggleOverhang(part);
  });
  on('[data-action="grid"]', (e) => {
    const visible = !viewer.gridOn;
    viewer.setGridVisible(visible);
    e.currentTarget.classList.toggle('on', visible);
  });
  on('[data-action="xray"]', (e) => {
    scene.setXray(!scene.xray);
    e.currentTarget.classList.toggle('on', scene.xray);
    e.currentTarget.setAttribute('aria-pressed', String(scene.xray));
    if (scene.xray && !app.selected.length) toast('X-ray shows the selected parts through the others — select a part', 'info', 3000);
  });
  on('[data-action="wires"]', () => setWiresOn(!scene.wiresOn));
  on('[data-action="wire-xray"]', (e) => {
    scene.setWireXray(!scene.wireXray);
    e.currentTarget.classList.toggle('on', scene.wireXray);
    e.currentTarget.setAttribute('aria-pressed', String(scene.wireXray));
    if (scene.wireXray && !scene.wiresOn) setWiresOn(true);
  });
  viewer.pickExtra = (x, y, raycaster, part) => scene.wires.pick(x, y, raycaster, part);
  viewer.onPick = (id, e, hit) => {
    if (hit?.connector) { selectConnector(hit.connector); return; }
    if (id) select([id], { additive: e?.shiftKey || e?.metaKey || e?.ctrlKey }); else select([]);
  };
  viewer.onDoublePick = (id) => select([id], { focus: true });
  const hover = $('#vp-hover');
  viewer.onHover = (id, e, hit) => {
    const n = id && scene.nodes.get(id);
    const conn = hit?.connector ? scene.wires.connector(hit.connector) : null;
    scene.setHoverWire(scene.isWire(n) ? id : null);
    scene.wires.setHoverConnector(conn ? hit.connector : null);
    if ((!n && !conn) || !e) { hover.hidden = true; hover.classList.remove('wire-tip'); return; }
    const rect = $('#viewport').getBoundingClientRect();
    if (conn) {
      const { title, label } = connectorTitle(conn);
      const k = scene.wires.wiresAt(hit.connector).length;
      const part = conn.node && scene.nodes.get(conn.node);
      hover.textContent = [title, label, part ? `on ${part.def.name || part.id}` : null,
        k ? `${k} wire${k === 1 ? '' : 's'}` : 'not wired'].filter(Boolean).join(' · ');
    } else hover.textContent = scene.isWire(n) ? wireTooltip(scene.wires.wireOf(id)) : n.def.name || n.id;
    hover.classList.toggle('wire-tip', !!conn || scene.isWire(n));
    hover.dataset.testid = 'vp-hover';
    hover.style.left = `${e.clientX - rect.left + 14}px`;
    hover.style.top = `${e.clientY - rect.top + 12}px`;
    hover.hidden = false;
  };
}

function selectedPrintedPart() {
  for (const id of app.selected) {
    const p = app.parts.find((x) => (x.node_ids || [x.name]).includes(id));
    if (p) return p;
  }
  return null;
}

// ------------------------------------------------------------------------------- tabs, layout, theme
function showTab(name) {
  app.tab = name;
  store.set('tab', name);
  for (const b of document.querySelectorAll('#tabs [role="tab"]')) b.setAttribute('aria-selected', String(b.dataset.tab === name));
  for (const p of document.querySelectorAll('.tab-pane')) p.hidden = p.id !== `tab-${name}`;
  ensureTab(name);
}

function ensureTab(name) {
  const v = app.version;
  if (name === 'elec') elec.ensure(v);
  if (name === 'spice') spice.ensure(v).catch((err) => toast(err.message, 'error'));
  if (name === 'twin') twin.ensure(v);
}

function bindSplitters() {
  for (const sp of document.querySelectorAll('.splitter')) {
    const side = sp.dataset.side;
    const varName = `--${side}-w`;
    const saved = store.get(`${side}-w`, null);
    if (saved) document.documentElement.style.setProperty(varName, saved);
    sp.addEventListener('pointerdown', (e) => {
      sp.setPointerCapture(e.pointerId);
      sp.classList.add('dragging');
      const move = (ev) => {
        const w = side === 'left' ? ev.clientX : window.innerWidth - ev.clientX;
        const px = `${Math.round(Math.max(180, Math.min(window.innerWidth * 0.5, w)))}px`;
        document.documentElement.style.setProperty(varName, px);
        store.set(`${side}-w`, px);
      };
      const up = () => { sp.classList.remove('dragging'); sp.removeEventListener('pointermove', move); sp.removeEventListener('pointerup', up); };
      sp.addEventListener('pointermove', move);
      sp.addEventListener('pointerup', up);
    });
  }
}

function setTheme(t) {
  document.documentElement.dataset.theme = t;
  store.set('theme', t);
  viewer.setTheme(t);
  scene.setTheme();
  replace($('#btn-theme'), icon(t === 'dark' ? 'sun' : 'moon', 16));
  spice.refreshTheme?.();
}

// ------------------------------------------------------------------------------- data loading
function renderTopbar() {
  const p = app.project;
  $('#proj-name').textContent = p.name;
  $('#proj-desc').textContent = p.description || '';
  $('#proj-desc').title = p.description || '';
  document.title = `${p.name} — PiForge`;
  replace($('#proj-chips'), [['board', p.board], ['printer', p.printer], ['material', p.material]]
    .filter(([, v]) => v).map(([k, v]) => h('span.chip', { title: k }, h('span.chip-k', {}, k), v)));
  const c = app.report?.counts || p.counts || {};
  for (const sev of ['error', 'warning', 'info']) {
    const el = $(`#count-${sev}`);
    el.querySelector('.n').textContent = String(c[sev] || 0);
    el.classList.toggle('zero', !c[sev]);
  }
  $('#tab-btn-checks .badge').textContent = String((c.error || 0) + (c.warning || 0) || '');
  $('#tab-btn-checks .badge').className = `badge ${c.error ? 'sev-error' : c.warning ? 'sev-warning' : ''}`;
  $('#build-info').textContent = p.has_build ? `built ${ago(p.built_at)}` : 'not built yet';
  $('#build-info').title = p.built_at || '';
}

async function loadAll({ first = false } = {}) {
  const [project, sceneDef, report, parts] = await Promise.all([getJSON('/api/project'), getJSON('/api/scene'),
    getJSON('/api/report'), getJSON('/api/parts')]);
  Object.assign(app, { project, report, parts, version: sceneDef.version || project.version || '' });
  renderTopbar();
  checks.setReport(report);
  print.setData(parts, project);
  if (app.bed) toggleBed(null);
  if (app.overhang) { scene.clearOverhang(); app.overhang = null; updateLegend(); }
  $('#vp-empty').hidden = !!(sceneDef.nodes || []).length;
  const loading = $('#vp-loading');
  loading.hidden = !(sceneDef.nodes || []).length;
  const res = await scene.load(sceneDef, { version: app.version,
    onProgress: (done, total) => { loading.textContent = `Loading meshes ${done}/${total}…`; } });
  loading.hidden = true;
  if (res.failed.length) toast(`${res.failed.length} mesh file(s) failed to load`, 'warning');
  refreshTree();
  checks.refresh();  // findings become clickable once their parts are in the scene
  $('#wire-tools').hidden = !scene.wires.wireNodes().length && !scene.wires.connectors.size;
  const conn = app.connector && scene.wires.connector(app.connector) ? app.connector : null;
  select(app.selected.filter((id) => scene.nodes.has(id)));
  if (conn) selectConnector(conn);
  const box = scene.visibleBox();
  viewer.modelRadius = box.isEmpty() ? 100 : box.getSize(box.min.clone()).length() / 2;
  viewer.buildGrid(box.isEmpty() ? null : box);
  if (first && !box.isEmpty()) viewer.fit(box, 'iso', 0);
  if (viewer.section.enabled) updateSectionRange();
  elec.invalidate();
  spice.invalidate();
  twin.invalidate();
  ensureTab(app.tab);
}

function bindBuildEvents() {
  const btn = $('#btn-rebuild');
  btn.addEventListener('click', async () => {
    try {
      const r = await postJSON('/api/build');
      if (!r.started) toast('A rebuild is already running', 'info', 2500);
    } catch (err) { toast(`Rebuild failed to start: ${err.message}`, 'error'); }
  });
  const status = $('#build-info');
  new Socket('/ws/events', { onMessage: async (m) => {
    if (m.op === 'build_progress') { status.textContent = `building: ${m.message}`; return; }
    if (m.op !== 'build') return;
    btn.classList.toggle('busy', m.state === 'started');
    btn.disabled = m.state === 'started';
    if (m.state === 'started') status.textContent = 'building…';
    if (m.state === 'done') {
      toast('Rebuild finished — reloading the model', 'ok', 3000);
      await loadAll({ first: false }).catch((err) => toast(`Reload failed: ${err.message}`, 'error'));
    }
    if (m.state === 'failed') {
      status.textContent = 'build failed';
      toast(`Rebuild failed: ${m.error || 'unknown error'}`, 'error', 0);
    }
  } });
}

function bindKeys() {
  window.addEventListener('keydown', (e) => {
    if (e.target.closest?.('input, select, textarea, [contenteditable]')) return;
    if (e.key === 'f' || e.key === 'F') viewer.setView(null);
    if ((e.key === 'w' || e.key === 'W') && !$('#wire-tools').hidden) setWiresOn(!scene.wiresOn);
    if (e.key === 'Escape') {
      if (viewer.measure.active) $('[data-action="measure"]').click();
      else select([]);
    }
  });
}

// ------------------------------------------------------------------------------- boot
async function boot() {
  for (const el of document.querySelectorAll('i[data-icon]')) el.replaceWith(icon(el.dataset.icon, Number(el.dataset.size) || 15));
  replace($('#btn-theme'), icon(theme === 'dark' ? 'sun' : 'moon', 16));
  $('#btn-theme').addEventListener('click', () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'));
  for (const b of document.querySelectorAll('#tabs [role="tab"]')) b.addEventListener('click', () => showTab(b.dataset.tab));
  for (const sev of ['error', 'warning', 'info']) {
    $(`#count-${sev}`).addEventListener('click', () => { showTab('checks'); checks.focusSeverity(sev); });
  }
  if (store.get('wires', '1') === '0') setWiresOn(false);
  bindToolbar();
  bindSplitters();
  bindKeys();
  bindBuildEvents();
  showTab(store.get('tab', 'checks'));
  try {
    await loadAll({ first: true });
  } catch (err) {
    toast(`Could not load the project: ${err.message}`, 'error', 0);
  }
  window.piforge.ready = true;
  document.body.dataset.ready = 'true';
}

window.piforge = {
  ready: false, app, viewer, scene,
  panels: { checks, elec, spice, twin, print, tree },
  debug: {
    nodeIds: () => [...scene.nodes.keys()],
    meshNodes: () => [...scene.nodes.values()].filter((n) => n.meshes.length).map((n) => n.id),
    node: (id) => scene.debugNode(id),
    explode: () => scene.explode,
    selected: () => [...scene.selected],
    xray: () => scene.xray,
    heat: () => scene.heat?.part ?? null,
    bed: () => scene.bedPart,
    frames: () => viewer.frames,
    twin: () => ({ running: twin.running, error: twin.error, devices: twin.devices.map((d) => d.id),
      t: twin.lastState?.t ?? null }),
    splitflap: () => (twin.display?.row ? { text: twin.display.row.text(), target: twin.display.row.targetText(),
      flipping: twin.display.row.digits.filter((d) => d.busy).length, flips: twin.display.row.digits.map((d) => d.flips) } : null),
    section: () => ({ ...viewer.section }),
    wires: () => ({ ...scene.wires.debug(), connector: app.connector }),
    wireScreen: (q) => scene.wires.screenOf(q),
    splitflapFaces: () => scene.flaps.debug(),
  },
};
// Short alias for E2E tests: the shown digit of every 3D split-flap face.
window.__piforge = { splitflapFaces: () => scene.flaps.debug() };
boot();
