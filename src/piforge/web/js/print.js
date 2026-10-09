// PiForge GUI — Print tab: printed parts with size, mass, time, cost, printability status,
// downloads (STL/3MF/STEP), overhang heat-map and print-bed view toggles.
import { duration, empty, h, icon, num, replace, sizeMM } from './ui.js';

const STATUS = { ok: ['ok', 'OK'], warning: ['warn', 'Warnings'], error: ['fail', 'Errors'] };
const FORMATS = [['stl', 'STL'], ['3mf', '3MF'], ['step', 'STEP']];

function est(part) {
  const a = part.analysis || {};
  const e = a.estimate || {};
  return { mass: e.mass_g ?? a.mass_g, time: e.time_h ?? a.time_h, cost: e.cost ?? a.cost,
    size: a.size_mm, overhang: a.overhang_area_mm2, supports: a.needs_supports, fits: a.fits_bed,
    bridges: a.bridge_count, bridgeArea: a.bridge_area_mm2 };
}

/** Print tab. Callbacks: onOverhang(part|null), onBed(part|null), onSelectPart(name). */
export class PrintPanel {
  /**
   * @param {HTMLElement} host
   * @param {object} cb
   */
  constructor(host, cb = {}) {
    this.host = host;
    this.cb = cb;
    this.parts = [];
    this.project = null;
    this.active = { overhang: null, bed: null };
  }

  /** Show the parts of `/api/parts` with the project info (printer, material, currency). */
  setData(parts, project) {
    this.parts = parts || [];
    this.project = project;
    this._draw();
  }

  /** Reflect which part has the heat-map / bed view on. */
  setActive(active) {
    Object.assign(this.active, active);
    this._draw();
  }

  _draw() {
    const p = this.project || {};
    const cur = p.currency || 'PLN';
    if (!this.parts.length) {
      replace(this.host, empty('No printed parts', 'Add printed parts with p.add_printed(...) and rebuild.'));
      return;
    }
    let mass = 0; let time = 0; let cost = 0; let count = 0;
    for (const part of this.parts) {
      const q = Number(part.quantity) || 1;
      const e = est(part);
      mass += (Number(e.mass) || 0) * q;
      time += (Number(e.time) || 0) * q;
      cost += (Number(e.cost) || 0) * q;
      count += q;
    }
    const prof = p.printer_profile;
    const summary = h('div.print-summary', {},
      h('div.kv', {}, h('span', {}, 'Printer'), h('strong', {}, p.printer || 'generic'),
        prof ? h('span.dim', {}, `${prof.build_x} × ${prof.build_y} × ${prof.build_z} mm · overhang ≤ ${prof.max_overhang_deg}°`) : null),
      h('div.kv', {}, h('span', {}, 'Material'), h('strong', {}, p.material || '—')),
      h('div.totals', {},
        h('div.stat', {}, h('span.stat-val', {}, String(count)), h('span.stat-label', {}, 'parts')),
        h('div.stat', {}, h('span.stat-val', {}, `${num(mass, 1)} g`), h('span.stat-label', {}, 'filament')),
        h('div.stat', {}, h('span.stat-val', {}, duration(time)), h('span.stat-label', {}, 'print time')),
        h('div.stat', {}, h('span.stat-val', {}, `${num(cost, 2)} ${cur}`), h('span.stat-label', {}, 'material cost'))));
    replace(this.host, summary, h('div.part-list', {}, this.parts.map((part) => this._card(part, cur))));
  }

  _card(part, cur) {
    const e = est(part);
    const [cls, label] = STATUS[part.status] || STATUS.ok;
    const thumb = (this.project?.renders || []).find((u) => u.endsWith(`/part_${encodeURIComponent(part.name)}.png`));
    const files = FORMATS.map(([k, lbl]) => (part.urls?.[k]
      ? h('a.file-chip', { href: part.urls[k], download: '', title: `Download ${lbl}` }, icon('download', 12), lbl)
      : h('span.file-chip.disabled', { title: `${lbl} not exported` }, lbl)));
    const ohOn = this.active.overhang === part.name;
    const bedOn = this.active.bed === part.name;
    // Overhang (needs support) warns; bridges print without supports, so they are only noted.
    const notes = [];
    if (Number(e.overhang) > 0) {
      notes.push(h('span', { class: e.supports ? 'warn' : null },
        `overhang ${num(e.overhang, 0)} mm²${e.supports ? ' · supports needed' : ''}`));
    }
    if (Number(e.bridgeArea) > 0) {
      const n = Number(e.bridges) || 1;
      notes.push(h('span', {}, `${n} bridge${n === 1 ? '' : 's'} ${num(e.bridgeArea, 0)} mm² (no supports)`));
    }
    if (e.fits === false) notes.push(h('span.err', {}, 'does not fit the bed'));
    const rot = Array.isArray(part.print_rotation) ? part.print_rotation : null;
    if (rot) notes.push(h('span', {}, `print rotation ${rot.map((v) => `${num(v, 0)}°`).join(' / ')}`));
    return h('section.part-card', { class: `status-${cls}`, dataset: { part: part.name, testid: `part-${part.name}` } },
      h('header', { onclick: () => this.cb.onSelectPart?.(part.name), title: 'Select in the 3D view' },
        thumb ? h('img.thumb', { src: thumb, alt: '' }) : h('span.swatch-dot', { style: { background: part.color || '#888' } }),
        h('strong', {}, part.name), Number(part.quantity) > 1 ? h('span.qty', {}, `×${part.quantity}`) : null,
        h('span.dim', {}, part.material || ''), h('span.status-pill', { class: cls }, label)),
      h('div.part-stats', {},
        h('span', {}, sizeMM(e.size)), h('span', {}, e.mass !== undefined ? `${num(e.mass, 1)} g` : '—'),
        h('span', {}, duration(e.time)), h('span', {}, e.cost !== undefined ? `${num(e.cost, 2)} ${cur}` : '—')),
      notes.length ? h('div.part-notes', {}, notes.flatMap((n, i) => (i ? [' · ', n] : [n]))) : null,
      h('div.part-actions', {}, h('div.files', {}, files),
        h('div.toggles', {},
          h('button.btn.small', { class: ohOn ? 'on' : null, 'aria-pressed': String(ohOn), dataset: { testid: `overhang-${part.name}` },
            title: 'Paint overhang faces red (in print orientation)', onclick: () => this.cb.onOverhang?.(ohOn ? null : part) },
          icon('flame', 13), 'Overhang'),
          h('button.btn.small', { class: bedOn ? 'on' : null, 'aria-pressed': String(bedOn), dataset: { testid: `bed-${part.name}` },
            title: 'Show the part alone on the print bed', onclick: () => this.cb.onBed?.(bedOn ? null : part) },
          icon('bed', 13), 'Bed view'))));
  }
}
