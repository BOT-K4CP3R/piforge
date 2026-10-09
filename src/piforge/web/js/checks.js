// PiForge GUI — Checks tab: build findings grouped by source, severity filters, click → select part.
import { empty, h, icon, replace, sevIcon } from './ui.js';

const SEV_RANK = { error: 2, warning: 1, info: 0 };
const SOURCE_LABEL = { erc: 'Electrical rules (ERC)', power: 'Power budget', assembly: 'Assembly',
  thermal: 'Thermal', project: 'Project', build: 'Build' };
const PREFIX_LABEL = { print: 'Print', check: 'Check', spice: 'SPICE', twin: 'Twin', thermal: 'Thermal' };

/** Human label for a finding source ("print:lid" → "Print · lid"; case-insensitive). */
export function sourceLabel(src) {
  if (!src) return 'General';
  const low = String(src).toLowerCase();
  if (SOURCE_LABEL[low]) return SOURCE_LABEL[low];
  const i = low.indexOf(':');
  if (i > 0 && PREFIX_LABEL[low.slice(0, i)]) return `${PREFIX_LABEL[low.slice(0, i)]} · ${src.slice(i + 1)}`;
  return src;
}

/**
 * What a finding points at in the 3D scene, as candidate names (resolved against the scene by the
 * caller): subjects "part:<printed part>", "node:<a>/<b>" (assembly interference), "joint:<node>",
 * sources "print:<part>", and data.a / data.b / data.node / data.other node ids.
 * @returns {{parts: string[], nodes: string[]}}
 */
export function findingTargets(f) {
  const parts = [];
  const nodes = [];
  const subj = String(f.subject || '');
  const i = subj.indexOf(':');
  const kind = i > 0 ? subj.slice(0, i).toLowerCase() : '';
  const rest = i > 0 ? subj.slice(i + 1) : '';
  if (kind === 'part') parts.push(rest.split(/[\s,]/)[0]);
  if (kind === 'node') nodes.push(...rest.split('/'));
  if (kind === 'joint') nodes.push(rest);
  const src = String(f.source || '');
  if (src.toLowerCase().startsWith('print:')) parts.push(src.slice(6));
  for (const k of ['a', 'b', 'node', 'other']) if (typeof f.data?.[k] === 'string') nodes.push(f.data[k]);
  return { parts: [...new Set(parts.filter(Boolean))], nodes: [...new Set(nodes.filter(Boolean))] };
}

/**
 * One finding as a row (also used by the Electronics, SPICE and Twin tabs).
 * @param {object} f finding dict
 * @param {Function} [onClick] called with the finding when the row is clicked
 * @param {string[]} [targets] scene node ids the row selects (the row is clickable only with targets)
 */
export function findingRow(f, onClick, targets = []) {
  const sev = String(f.severity || 'info').toLowerCase();
  const clickable = !!(onClick && targets.length);
  return h('div.finding', { class: `sev-${sev}${clickable ? ' clickable' : ''}`, dataset: { code: f.code },
    title: clickable ? `Select ${targets.join(' + ')} in the 3D view` : null,
    onclick: clickable ? () => onClick(f) : null },
  sevIcon(sev, 15),
  h('div.finding-body', {},
    h('div.finding-head', {}, h('span.code', {}, f.code), f.subject ? h('span.subject', {}, f.subject) : null),
    h('div.finding-msg', {}, f.message),
    f.hint ? h('div.finding-hint', {}, h('span.hint-label', {}, 'Fix'), f.hint) : null));
}

/** Checks tab. Callbacks: resolve(finding) → scene node ids, onSelect(ids, finding). */
export class ChecksPanel {
  /**
   * @param {HTMLElement} host
   * @param {{resolve?: Function, onSelect?: Function}} cb
   */
  constructor(host, cb = {}) {
    this.host = host;
    this.cb = cb;
    this.report = { findings: [], counts: { error: 0, warning: 0, info: 0 } };
    this.show = { error: true, warning: true, info: true };
    this.query = '';
    this.collapsed = new Set();
    this.filters = h('div.filter-row');
    this.search = h('input.search', { type: 'search', placeholder: 'Search findings…', 'aria-label': 'Search findings',
      oninput: () => { this.query = this.search.value.trim().toLowerCase(); this._drawList(); } });
    this.summary = h('div.check-summary');
    this.list = h('div.check-list');
    replace(host, h('div.pane-toolbar', {}, this.filters, this.search), this.summary, this.list);
  }

  /** Show a report (`/api/report`). */
  setReport(report) {
    this.report = report || this.report;
    this._drawFilters();
    this._drawList();
  }

  /** Re-draw (e.g. after the 3D scene loaded: rows become clickable when their parts exist). */
  refresh() { this._drawList(); }

  /** Show only one severity (null = all). */
  focusSeverity(sev) {
    for (const k of Object.keys(this.show)) this.show[k] = !sev || k === sev;
    this._drawFilters();
    this._drawList();
  }

  _drawFilters() {
    const c = this.report.counts || {};
    const chip = (sev, label) => h('button.chip-toggle', { class: `sev-${sev}${this.show[sev] ? ' on' : ''}`,
      'aria-pressed': String(this.show[sev]), dataset: { testid: `filter-${sev}` },
      onclick: () => { this.show[sev] = !this.show[sev]; this._drawFilters(); this._drawList(); } },
    sevIcon(sev, 13), `${c[sev] || 0} ${label}`);
    const plural = (n, one, many) => (n === 1 ? one : many);
    replace(this.filters, chip('error', plural(c.error, 'error', 'errors')),
      chip('warning', plural(c.warning, 'warning', 'warnings')), chip('info', 'info'));
    const ok = !(c.error > 0);
    replace(this.summary, h('span.status-pill', { class: ok ? 'ok' : 'fail' }, icon(ok ? 'check' : 'error', 13), ok ? 'PASS' : 'FAIL'),
      h('span.dim', {}, ok ? 'No errors — the design builds.' : `${c.error} error${c.error === 1 ? '' : 's'} must be fixed.`));
  }

  _drawList() {
    const findings = (this.report.findings || []).filter((f) => {
      const sev = String(f.severity || 'info').toLowerCase();
      if (!this.show[sev]) return false;
      if (!this.query) return true;
      return `${f.code} ${f.message} ${f.subject} ${f.source} ${f.hint}`.toLowerCase().includes(this.query);
    });
    if (!(this.report.findings || []).length) {
      replace(this.list, empty('No findings', 'Build the project to run ERC, power, printability, assembly and simulation checks.'));
      return;
    }
    if (!findings.length) { replace(this.list, empty('Nothing matches the filter')); return; }
    const groups = new Map();
    for (const f of findings) {
      const k = f.source || 'General';
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(f);
    }
    const worst = (fs) => Math.max(...fs.map((f) => SEV_RANK[String(f.severity).toLowerCase()] ?? 0));
    const ordered = [...groups.entries()].sort((a, b) => worst(b[1]) - worst(a[1]) || a[0].localeCompare(b[0]));
    replace(this.list, ordered.map(([src, fs]) => this._group(src, fs)));
  }

  _group(src, fs) {
    fs.sort((a, b) => (SEV_RANK[String(b.severity)] ?? 0) - (SEV_RANK[String(a.severity)] ?? 0));
    const counts = { error: 0, warning: 0, info: 0 };
    for (const f of fs) counts[String(f.severity).toLowerCase()] = (counts[String(f.severity).toLowerCase()] || 0) + 1;
    const collapsed = this.collapsed.has(src);
    const head = h('button.group-head', { 'aria-expanded': String(!collapsed),
      onclick: () => { if (collapsed) this.collapsed.delete(src); else this.collapsed.add(src); this._drawList(); } },
    h('span.chev', { class: collapsed ? null : 'open' }, icon('chevron', 12)),
    h('span.group-name', {}, sourceLabel(src)),
    h('span.group-counts', {}, ['error', 'warning', 'info'].filter((s) => counts[s]).map((s) =>
      h('span.count-dot', { class: `sev-${s}` }, counts[s]))));
    return h('section.check-group', { dataset: { source: src } }, head,
      collapsed ? null : fs.map((f) => {
        const ids = this.cb.resolve?.(f) || [];
        return findingRow(f, () => this.cb.onSelect?.(ids, f), ids);
      }));
  }
}
