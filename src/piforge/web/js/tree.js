// PiForge GUI — left scene tree: nodes grouped by kind, visibility, colour swatch, select, isolate.
import { h, icon, replace } from './ui.js';
import { pinText, wireGroup } from './wires.js';

const KIND_ORDER = ['printed', 'pcb', 'reference', 'fastener', 'wire'];
const KIND_LABEL = { printed: 'Printed parts', pcb: 'Boards', reference: 'Modules & parts', fastener: 'Fasteners',
  wire: 'Wires' };
const short = (e) => (e ? `${e.ref || '?'}${e.pin ? ` ${pinText(e.pin)}` : ''}` : '?');

/**
 * Scene tree. Callbacks: onToggle(id, visible), onToggleKind(ids, visible), onSelect(id, additive),
 * onIsolate(id), onColor(id, hex), onFocus(id).
 */
export class Tree {
  /**
   * @param {HTMLElement} host
   * @param {object} callbacks
   */
  constructor(host, callbacks) {
    this.host = host;
    this.cb = callbacks;
    this.rows = [];
    this.selected = new Set();
    this.isolated = null;
    this.filter = '';
    this.collapsed = new Set();
    this.search = h('input.tree-search', { type: 'search', placeholder: 'Filter parts…', 'aria-label': 'Filter parts',
      oninput: () => { this.filter = this.search.value.trim().toLowerCase(); this._draw(); } });
    this.list = h('div.tree-list', { role: 'tree' });
    replace(host, h('div.tree-tools', {}, this.search), this.list);
  }

  /**
   * Replace the rows.
   * @param {Array<{id,name,kind,color,hidden,meshes,driven,glow,material}>} rows
   */
  render(rows) {
    this.rows = rows;
    this._draw();
  }

  /** Highlight selected ids. */
  setSelected(ids) {
    this.selected = new Set(ids);
    for (const el of this.list.querySelectorAll('.tree-row')) {
      el.classList.toggle('selected', this.selected.has(el.dataset.id));
    }
    const first = this.list.querySelector('.tree-row.selected');
    first?.scrollIntoView({ block: 'nearest' });
  }

  /** Mark the isolated node (or none). */
  setIsolated(id) {
    this.isolated = id;
    this._draw();
  }

  _draw() {
    const groups = new Map();
    for (const r of this.rows) {
      const w = r.wire;
      const text = `${r.name} ${r.id} ${r.material || ''}${w ? ` ${w.id} ${w.net || ''} ${w.cable || ''} ${short(w.from)} ${short(w.to)} ${w.color_name || ''}` : ''}`;
      if (this.filter && !text.toLowerCase().includes(this.filter)) continue;
      const k = KIND_ORDER.includes(r.kind) ? r.kind : 'other';
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(r);
    }
    const kinds = [...KIND_ORDER, 'other'].filter((k) => groups.has(k));
    if (!kinds.length) {
      replace(this.list, h('div.tree-empty', {}, this.rows.length ? 'No match' : 'No scene loaded'));
      return;
    }
    replace(this.list, kinds.map((k) => this._group(k, groups.get(k))));
  }

  _group(kind, rows) {
    const visibleCount = rows.filter((r) => !r.hidden).length;
    const allOn = visibleCount === rows.length;
    const collapsed = this.collapsed.has(kind);
    const box = h('input', { type: 'checkbox', title: allOn ? 'Hide all' : 'Show all', 'aria-label': `Toggle ${kind}`,
      onclick: (e) => e.stopPropagation(), onchange: () => this.cb.onToggleKind?.(rows.map((r) => r.id), box.checked) });
    box.checked = allOn;
    box.indeterminate = visibleCount > 0 && !allOn;
    const head = h('div.tree-group-head', { onclick: () => { if (collapsed) this.collapsed.delete(kind); else this.collapsed.add(kind); this._draw(); } },
      h('span.chev', { class: collapsed ? null : 'open' }, icon('chevron', 12)), box,
      h('span.tree-group-name', {}, KIND_LABEL[kind] || 'Other'), h('span.count', {}, rows.length));
    let body = null;
    if (!collapsed && kind === 'wire') {
      // by cable (a 28BYJ-48 lead, a ribbon…), loose wires by net
      const subs = new Map();
      for (const r of rows) {
        const g = wireGroup(r.wire);
        if (!subs.has(g)) subs.set(g, []);
        subs.get(g).push(r);
      }
      const names = [...subs.keys()].sort((a, b) => (a.startsWith('cable') === b.startsWith('cable') ? a.localeCompare(b, undefined, { numeric: true }) : a.startsWith('cable') ? -1 : 1));
      body = names.map((g) => {
        const sub = subs.get(g);
        const on = sub.filter((r) => !r.hidden).length;
        const cb = h('input', { type: 'checkbox', 'aria-label': `Toggle ${g}`, onclick: (e) => e.stopPropagation(),
          onchange: () => this.cb.onToggleKind?.(sub.map((r) => r.id), cb.checked) });
        cb.checked = on === sub.length;
        cb.indeterminate = on > 0 && on < sub.length;
        return h('div.tree-sub', { dataset: { group: g } },
          h('div.tree-sub-head', { title: `Select every wire of ${g}`, onclick: () => this.cb.onSelectMany?.(sub.map((r) => r.id)) },
            cb, h('span.tree-name', {}, g), h('span.count', {}, sub.length)),
          sub.map((r) => this._row(r)));
      });
    } else if (!collapsed) body = rows.map((r) => this._row(r));
    return h('div.tree-group', { dataset: { kind } }, head, body);
  }

  _row(r) {
    const vis = h('input', { type: 'checkbox', 'aria-label': `Show ${r.name}`, dataset: { testid: `tree-vis-${r.id}` },
      onchange: () => this.cb.onToggle?.(r.id, vis.checked), onclick: (e) => e.stopPropagation() });
    vis.checked = !r.hidden;
    const picker = h('input.swatch-input', { type: 'color', value: r.color, 'aria-label': `Colour of ${r.name}`,
      oninput: () => { sw.style.background = picker.value; this.cb.onColor?.(r.id, picker.value); },
      onclick: (e) => e.stopPropagation() });
    const sw = h('label.swatch', { title: 'Display colour', style: { background: r.color } }, picker);
    const badges = [];
    if (r.driven) badges.push(h('span.badge-mini', { title: 'Joint driven by the digital twin' }, 'joint'));
    if (r.glow) badges.push(h('span.badge-mini', { title: 'Glows with a twin output' }, 'led'));
    if (!r.meshes) badges.push(h('span.badge-mini.warn', { title: 'Mesh missing' }, 'no mesh'));
    const w = r.wire;
    const label = w ? [h('span.tree-wire-id', {}, w.id), h('span.tree-wire-ends', {}, `${short(w.from)} → ${short(w.to)}`)]
      : r.name;
    const iso = h('button.icon-btn.iso', { title: this.isolated === r.id ? 'Show all again' : 'Isolate (show only this)',
      'aria-label': `Isolate ${r.name}`, class: this.isolated === r.id ? 'active' : null,
      onclick: (e) => { e.stopPropagation(); this.cb.onIsolate?.(r.id); } }, icon('target', 14));
    return h('div.tree-row', {
      class: [this.selected.has(r.id) ? 'selected' : '', r.hidden ? 'is-hidden' : '', w ? 'wire-row' : ''].join(' '),
      role: 'treeitem', tabindex: 0, dataset: { id: r.id, testid: `tree-item-${r.id}` },
      title: w ? `${w.id}: ${short(w.from)} → ${short(w.to)}${w.net ? ` · net ${w.net}` : ''}` : `${r.name} (${r.id})`,
      onclick: (e) => this.cb.onSelect?.(r.id, e.shiftKey || e.metaKey || e.ctrlKey),
      ondblclick: () => this.cb.onFocus?.(r.id),
      onkeydown: (e) => { if (e.key === 'Enter') this.cb.onSelect?.(r.id, false); if (e.key === ' ') { e.preventDefault(); vis.click(); } },
    }, vis, sw, h('span.tree-name', {}, label), badges, iso);
  }
}
