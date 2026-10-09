// PiForge GUI — Electronics tab: wiring diagram + table, cut list, BOM, pinout, config.txt, power, ERC.
// Harness wires (scene.json `kind: "wire"` nodes) are linked to the 3D view both ways.
import { getJSON } from './api.js';
import { findingRow } from './checks.js';
import { copyText, empty, h, icon, markdown, num, replace, si, table, toast } from './ui.js';
import { endText, wireSpec } from './wires.js';

const VIEWS = [['wiring', 'Wiring'], ['cut', 'Cut list'], ['bom', 'BOM'], ['pinout', 'Pinout'], ['config', 'config.txt'],
  ['power', 'Power'], ['erc', 'ERC']];

function railsOf(power) {
  const rails = power?.rails;
  if (!rails) return [];
  const list = Array.isArray(rails) ? rails : Object.entries(rails).map(([k, v]) => ({ name: k, ...v }));
  return list.map((r) => ({
    name: r.name, voltage: Number(r.voltage), available: Number(r.available_ma), typ: Number(r.typ_ma),
    max: Number(r.max_ma),
    loads: (r.loads || []).map((l) => (Array.isArray(l) ? { name: l[0], typ: Number(l[1]), max: Number(l[2]) }
      : { name: l.name ?? l.ref ?? '', typ: Number(l.typ_ma ?? l.typ), max: Number(l.max_ma ?? l.max) })),
  }));
}

/** Currents below 1 A are shown in mA (0 → "0 mA", never "0 A"). */
function mA(v) {
  if (!Number.isFinite(v)) return '—';
  return Math.abs(v) < 1000 ? `${Number(v.toFixed(v % 1 ? 1 : 0))} mA` : si(v / 1000, 'A');
}

function wireEnd(ref, pin, phys) {
  return h('span.wire-end', {}, h('strong', {}, ref || '?'), ' ', pin || '', phys ? h('span.dim', {}, ` · pin ${phys}`) : null);
}

const ID_KEYS = ['wire_id', 'wire', 'id'];
const sameEnd = (ref, pins, end) => end && String(end.ref) === String(ref)
  && pins.some((p) => p !== undefined && p !== null && p !== '' && [end.pin, end.label].map(String).includes(String(p)));

/** The harness wire (from /api/elec `wires`) that realises a wiring.json row, or null. */
export function wireOfRow(row, wires) {
  const a = [row.a_phys, row.a_pin];
  const b = [row.b_phys, row.b_pin];
  return (wires || []).find((w) => (sameEnd(row.a_ref, a, w.from) && sameEnd(row.b_ref, b, w.to))
    || (sameEnd(row.a_ref, a, w.to) && sameEnd(row.b_ref, b, w.from))) || null;
}

/** Wire id of a cut-list row (column "wire id" / "wire" / "id"). */
export function cutRowId(r) {
  for (const k of ID_KEYS) if (r[k]) return String(r[k]);
  return null;
}

/** Electronics tab (loads `/api/elec` lazily). Callback: onSelectWire(wireId). */
export class ElecPanel {
  /**
   * @param {HTMLElement} host
   * @param {{onSelectWire?: (wireId: string) => void}} [callbacks]
   */
  constructor(host, callbacks = {}) {
    this.host = host;
    this.cb = callbacks;
    this.selWires = new Set();
    this.data = null;
    this.view = 'wiring';
    this.version = '';
    this.nav = h('div.segmented', { role: 'tablist', 'aria-label': 'Electronics views' });
    this.body = h('div.elec-body');
    replace(host, h('div.pane-toolbar', {}, this.nav), this.body);
  }

  /** Fetch data if not loaded yet. */
  async ensure(version = '') {
    if (this.data && this.version === version) return;
    this.version = version;
    replace(this.body, h('div.loading-line', {}, 'Loading electronics…'));
    try {
      this.data = await getJSON('/api/elec');
    } catch (err) {
      replace(this.body, empty('Electronics unavailable', err.message));
      return;
    }
    this._draw();
  }

  /** Forget cached data (after a rebuild). */
  invalidate() { this.data = null; }

  _draw() {
    replace(this.nav, VIEWS.map(([k, label]) => h('button', { class: this.view === k ? 'on' : null, role: 'tab',
      'aria-selected': String(this.view === k), onclick: () => { this.view = k; this._draw(); } }, label,
    k === 'erc' && this.data?.erc?.length ? h('span.mini-count', {}, this.data.erc.length) : null)));
    const d = this.data;
    if (!d?.available) {
      replace(this.body, empty('No electronics outputs', 'Build the project to generate the BOM, wiring, pinout and config.txt.'));
      return;
    }
    const draw = { wiring: () => this._wiring(d), cut: () => this._cut(d), bom: () => this._bom(d), pinout: () => this._pinout(d),
      config: () => this._config(d), power: () => this._power(d), erc: () => this._erc(d) }[this.view];
    replace(this.body, draw());
    this._markSelection(false);
  }

  /** Highlight the rows of these wire ids (3D selection → table); scrolls the first into view. */
  highlightWires(ids) {
    this.selWires = new Set(ids || []);
    this._markSelection(true);
  }

  _markSelection(scroll) {
    let first = null;
    for (const tr of this.body.querySelectorAll('tr[data-wire]')) {
      const on = this.selWires.has(tr.dataset.wire);
      tr.classList.toggle('sel', on);
      if (on && !first) first = tr;
    }
    if (scroll && first && !this.host.hidden) first.scrollIntoView({ block: 'nearest' });
  }

  _wireRow(id) {
    if (!id) return {};
    return { dataset: { wire: id, testid: `wire-row-${id}` }, class: 'clickable', title: 'Show this wire in 3D',
      onclick: () => this.cb.onSelectWire?.(id) };
  }

  _fileLinks(pairs) {
    const links = pairs.filter(([, url]) => url).map(([label, url]) =>
      h('a.file-link', { href: url, target: '_blank', rel: 'noopener', download: '' }, icon('download', 13), label));
    return links.length ? h('div.file-links', {}, links) : null;
  }

  _wiring(d) {
    const f = d.files;
    const img = f.wiring_svg || f.wiring_png;
    const diagram = img ? h('a.diagram', { href: img, target: '_blank', rel: 'noopener', title: 'Open full size' },
      h('img', { src: `${img}?v=${encodeURIComponent(this.version)}`, alt: 'Wiring diagram' }),
      h('span.diagram-open', {}, icon('external', 13), 'Full size')) : null;
    const wires = d.wires || [];
    if (wires.length) {
      const total = wires.reduce((s, w) => s + (Number(w.length_mm) || 0), 0);
      const tbl = table([
        { key: 'id', label: 'Wire', render: (w) => h('div.wire-id-cell', {}, h('span.net', {}, h('span.wire-swatch', { style: { background: w.color || 'gray' } }),
          h('strong', {}, w.id)), w.cable ? h('span.badge-mini.keep-case', { title: 'Cable / harness' }, w.cable) : null) },
        { key: 'ends', label: 'From → to', render: (w) => h('div.wire-conn', {}, h('div', {}, endText(w.from)), h('div', {}, h('span.dim', {}, '→ '), endText(w.to))) },
        { key: 'net', label: 'Net', render: (w) => h('span', {}, w.net || '', w.signal && w.signal !== w.net ? h('div.dim', {}, w.signal) : null) },
        { key: 'spec', label: 'Wire', align: 'right', render: (w) => h('span', {}, wireSpec(w),
          Number(w.length_mm) ? h('div.dim', {}, `${num(Number(w.length_mm), 0)} mm`) : null) },
      ], wires, { empty: 'No wires', className: 'compact wires', rowAttrs: (w) => this._wireRow(w.id) });
      return h('div.stack', {}, diagram,
        h('div.section-row', {}, h('h4.section-title', {}, `Wiring · ${wires.length} wires`),
          h('span.dim', {}, `${num(total / 1000, 2)} m total · click a row to see it in 3D`)), tbl,
        this._fileLinks([['wiring.svg', f.wiring_svg], ['wiring.png', f.wiring_png], ['wiring.md', f.wiring_md],
          ['cut_list.csv', f.cut_list_csv], ['netlist.net (KiCad)', f.netlist]]));
    }
    const rows = d.wiring || [];
    const tbl = table([
      { key: 'net', label: 'Net', render: (r) => h('span.net', {}, h('span.wire-swatch', { style: { background: r.color || 'gray' } }), r.net) },
      { key: 'a', label: 'From', render: (r) => wireEnd(r.a_ref, r.a_pin, r.a_phys) },
      { key: 'b', label: 'To', render: (r) => wireEnd(r.b_ref, r.b_pin, r.b_phys) },
      { key: 'signal', label: 'Signal' },
    ], rows, { empty: 'No wires', className: 'compact', rowAttrs: (r) => this._wireRow(wireOfRow(r, wires)?.id) });
    return h('div.stack', {}, diagram, h('h4.section-title', {}, `Wiring table · ${rows.length} wires`), tbl,
      this._fileLinks([['wiring.svg', f.wiring_svg], ['wiring.png', f.wiring_png], ['wiring.md', f.wiring_md],
        ['netlist.net (KiCad)', f.netlist]]));
  }

  _cut(d) {
    const rows = d.cut_list || [];
    const f = d.files;
    if (!rows.length) {
      return h('div.stack', {}, empty('No cut list', 'The build writes elec/cut_list.csv when the project routes its wires in 3D.'));
    }
    const all = (d.cut_list_columns || Object.keys(rows[0])).filter(Boolean);
    // the panel is narrow: the essentials (connector ids, route length… stay in the CSV)
    const pref = ['wire_id', 'wire', 'id', 'from', 'to', 'net', 'colour', 'color', 'gauge', 'gauge_awg', 'awg', 'length_mm'];
    const picked = pref.filter((c) => all.includes(c));
    const cols = picked.length >= 4 ? picked.filter((c, i) => !ID_KEYS.includes(c) || picked.findIndex((x) => ID_KEYS.includes(x)) === i) : all;
    const color = new Map((d.wires || []).map((w) => [w.id, w.color]));
    const isLen = (c) => /length|mm$/.test(c);
    const isNum = (c) => isLen(c) || /gauge|awg/.test(c);
    const label = (c) => ({ wire_id: 'Wire', length_mm: 'mm', colour: 'Colour', color: 'Colour', gauge: 'Gauge',
      gauge_awg: 'AWG' }[c] || c.replace(/_/g, ' ').replace(/^./, (x) => x.toUpperCase()));
    const lenKey = cols.find(isLen);
    const lead = (r) => /^(true|1|yes)$/i.test(String(r.factory_lead ?? ''));
    const leads = rows.filter(lead).length;
    const total = lenKey ? rows.reduce((s, r) => s + (Number(r[lenKey]) || 0), 0) : 0;
    const tbl = table(cols.map((c) => ({ key: c, label: label(c), align: isNum(c) ? 'right' : null,
      render: ID_KEYS.includes(c) ? (r) => h('span.net', { title: r.cable ? `cable ${r.cable}` : null },
        h('span.wire-swatch', { style: { background: color.get(cutRowId(r)) || 'gray' } }), h('strong', {}, r[c]))
        : isLen(c) ? (r) => (r[c] === '' ? '' : h('span', {}, num(Number(r[c]), Number(r[c]) % 1 ? 1 : 0),
          lead(r) ? h('span.lead-mark', { title: 'Factory lead: comes with the part, nothing to cut' }, '*') : null)) : null })),
    rows, { className: 'compact wires', rowAttrs: (r) => this._wireRow(cutRowId(r)) });
    return h('div.stack', {},
      h('div', {}, h('strong', {}, `${rows.length} wires${lenKey ? ` · ${num(total / 1000, 2)} m of wire` : ''}`),
        h('div.dim', {}, `Cut, strip and label each wire${leads ? ` (* = ${leads} factory leads, not cut)` : ''}. Click a row to see it in 3D.`)),
      tbl, this._fileLinks([['cut_list.csv', f.cut_list_csv], ['cut_list.md', f.cut_list_md]]));
  }

  _bom(d) {
    const cols = (d.bom_columns || []).filter((c) => c !== 'key');
    const order = ['qty', 'refs', 'name', 'value', 'notes'];
    cols.sort((a, b) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99));
    const total = d.bom.reduce((s, r) => s + (Number(r.qty) || 0), 0);
    return h('div.stack', {}, h('div.dim', {}, `${d.bom.length} lines · ${total} parts`),
      table(cols.map((c) => ({ key: c, label: c === 'qty' ? 'Qty' : c[0].toUpperCase() + c.slice(1),
        align: c === 'qty' ? 'right' : null })), d.bom, { empty: 'Empty BOM', className: 'compact' }),
      this._fileLinks([['bom.csv', d.files.bom_csv], ['bom.md', d.files.bom_md]]));
  }

  _pinout(d) {
    return d.pinout_md ? h('div.stack', {}, markdown(d.pinout_md), this._fileLinks([['pinout.md', d.files.pinout_md]]))
      : empty('No pinout');
  }

  _config(d) {
    const text = d.config_txt || '';
    const copy = h('button.btn.small', { dataset: { testid: 'copy-config' }, onclick: async () => {
      toast((await copyText(text)) ? 'config.txt copied to the clipboard' : 'Copy failed', 'ok', 2500);
    } }, icon('copy', 14), 'Copy');
    return h('div.stack', {},
      h('div.row-between', {}, h('span.dim', {}, 'Append to /boot/firmware/config.txt on the Pi, then reboot.'), copy),
      h('pre.code.config', {}, text || '# nothing to add'), this._fileLinks([['config.txt', d.files.config_txt]]));
  }

  _power(d) {
    const rails = railsOf(d.power);
    if (!rails.length) return empty('No power budget');
    const blocks = rails.map((r) => {
      const pctMax = r.available > 0 ? (r.max / r.available) * 100 : 0;
      const pctTyp = r.available > 0 ? (r.typ / r.available) * 100 : 0;
      const level = pctMax > 100 ? 'err' : pctMax > 80 ? 'warn' : 'ok';
      return h('div.rail', { dataset: { rail: r.name } },
        h('div.rail-head', {}, h('strong', {}, `${r.name} rail`), h('span.dim', {}, Number.isFinite(r.voltage) ? `${r.voltage} V` : ''),
          h('span.rail-avail', {}, `available ${mA(r.available)}`)),
        h('div.bar', { class: `bar-${level}`, title: `typical ${mA(r.typ)}, worst case ${mA(r.max)}` },
          h('div.bar-max', { style: { width: `${Math.min(100, pctMax)}%` } }),
          h('div.bar-typ', { style: { width: `${Math.min(100, pctTyp)}%` } })),
        h('div.rail-nums', {}, !r.loads.length && !r.typ && !r.max ? h('span.dim', {}, 'no loads')
          : h('span', {}, `typ ${mA(r.typ)}`), r.loads.length || r.typ || r.max ? h('span', {}, `max ${mA(r.max)}`) : null,
          h('span', { class: `pct-${level}` }, `${pctMax.toFixed(0)} % of supply`)),
        r.loads.length ? table([{ key: 'name', label: 'Load' },
          { key: 'typ', label: 'Typ', align: 'right', render: (l) => mA(l.typ) },
          { key: 'max', label: 'Max', align: 'right', render: (l) => mA(l.max) }], r.loads, { className: 'compact' }) : null);
    });
    return h('div.stack', {}, d.power?.psu ? h('div.dim', {}, `Supply: ${d.power.psu}`) : null, blocks,
      (d.power_findings || []).map((f) => findingRow(f)));
  }

  _erc(d) {
    const fs = d.erc || [];
    return fs.length ? h('div.stack', {}, fs.map((f) => findingRow(f)))
      : empty('ERC clean', 'No electrical rule findings.');
  }
}
