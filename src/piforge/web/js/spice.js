// PiForge GUI — SPICE tab: pick a bench, edit its inputs (form generated from ParamSpecs), run
// ngspice on the server, plot the traces (uPlot), compare measures with analytic values, findings.
import uPlot from 'uplot';
import { getJSON, postJSON } from './api.js';
import { findingRow } from './checks.js';
import { debounce, empty, h, icon, inputValue, num, parseSI, replace, si, table } from './ui.js';

const PALETTE = ['#4c8dff', '#f2a93b', '#2fbf71', '#e05d9c', '#9d7bff', '#2ec4c4', '#f05252'];
const SI_BASE = new Set(['V', 'A', 's', 'Hz', 'Ω', 'F', 'H', 'W']);

/** Split a trace label "V(out) [V]" → {name: "V(out)", unit: "V"}. */
export function splitLabel(label) {
  const m = String(label).match(/^(.*?)\s*\[([^\]]*)\]\s*$/);
  return m ? { name: m[1], unit: m[2] } : { name: String(label), unit: guessUnit(label) };
}

/** Unit guessed from a name when the bench gives none: v(…) → V, i(…) → A, time → s, frequency → Hz. */
export function guessUnit(name) {
  const n = String(name).toLowerCase();
  if (n === 'time' || n === 't') return 's';
  if (n.startsWith('freq')) return 'Hz';
  if (n.includes('phase')) return 'deg';
  if (n.includes('gain') || n.includes('db')) return 'dB';
  if (n.startsWith('v(') || /^v[_a-z0-9]/.test(n)) return 'V';
  if (n.startsWith('i(') || /^i[_a-z0-9]/.test(n)) return 'A';
  if (n.startsWith('tau') || /^t[_a-z0-9]/.test(n)) return 's';
  if (/^f[_a-z0-9]/.test(n)) return 'Hz';
  if (/^p[_a-z0-9]/.test(n)) return 'W';
  if (/^r[_a-z0-9]/.test(n)) return 'Ω';
  return '';
}

/** Value with unit: SI prefixes for base units (3.90 mA), plain numbers otherwise (−20.0 dB, 45.0°). */
export function fmtVal(v, unit) {
  if (typeof v !== 'number') return v === null || v === undefined ? '—' : String(v);
  if (SI_BASE.has(unit)) return si(v, unit);
  if (unit === 'deg' || unit === '°') return `${num(v)}°`;
  return `${num(v)}${unit ? ` ${unit}` : ''}`;
}

/** Signed percentage with one decimal; |d| < 0.05 % prints as "0.0 %" (never "-0.0 %"). */
function pct(d) {
  return Math.abs(d) < 0.05 ? '0.0 %' : `${d > 0 ? '+' : '−'}${Math.abs(d).toFixed(1)} %`;
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#888';
}

/** SPICE tab. */
export class SpicePanel {
  /** @param {HTMLElement} host */
  constructor(host) {
    this.host = host;
    this.benches = [];
    this.project = [];
    this.available = false;
    this.error = null;
    this.current = null;
    this.inputs = new Map();
    this.abort = null;
    this.plot = null;
    this.loaded = false;
    this.last = null;
    this.select = h('select.bench-select', { 'aria-label': 'Bench', dataset: { testid: 'spice-bench' },
      onchange: () => this._choose(this.select.value) });
    this.desc = h('div.bench-desc');
    this.form = h('div.param-form');
    this.autoBox = h('input', { type: 'checkbox', id: 'spice-auto' });
    this.runBtn = h('button.btn.primary', { dataset: { testid: 'spice-run' }, onclick: () => this.run() }, icon('play', 14), 'Run');
    this.result = h('div.spice-result');
    this.autoRun = debounce(() => { if (this.autoBox.checked) this.run(); }, 600);
    replace(host, h('div.pane-toolbar', {}, this.select), this.desc, this.form,
      h('div.form-actions', {}, this.runBtn,
        h('button.btn', { onclick: () => this._fillDefaults() }, 'Defaults'),
        h('label.check', { for: 'spice-auto', title: 'Re-run automatically when an input changes' }, this.autoBox, 'Auto-run')),
      this.result);
    new ResizeObserver(() => this._resizePlot()).observe(this.result);
  }

  /** Load benches and the project's stored runs (once per build version). */
  async ensure(version = '') {
    if (this.loaded && this.version === version) return;
    this.version = version;
    this.loaded = true;
    const [b, p] = await Promise.all([
      getJSON('/api/spice/benches').catch((e) => ({ available: false, error: e.message, benches: [] })),
      getJSON('/api/spice/project').catch(() => [])]);
    this.available = !!b.available;
    this.error = b.error;
    this.benches = b.benches || [];
    this.project = p || [];
    replace(this.select,
      this.project.length ? h('optgroup', { label: 'This project' }, this.project.map((r) =>
        h('option', { value: `run:${r.label}` }, `${r.label} — ${r.bench}${r.ok === false ? ' (fails)' : ''}`))) : null,
      this.benches.length ? h('optgroup', { label: 'All benches' }, this.benches.map((x) =>
        h('option', { value: `bench:${x.key}` }, x.title || x.key))) : null);
    if (!this.project.length && !this.benches.length) {
      replace(this.result, empty('No SPICE benches', this.error || 'The SPICE package is not available.'));
      this.runBtn.disabled = true;
      return;
    }
    const keep = this.current && [...this.select.options].some((o) => o.value === this.current.value);
    this._choose(keep ? this.current.value : this.select.options[0].value);
  }

  /** Forget data (after a rebuild). */
  invalidate() { this.loaded = false; }

  /** Re-draw the last result with the current theme colours. */
  refreshTheme() { if (this.last) this._showResult(...this.last); }

  _choose(value) {
    this.select.value = value;
    const i = value.indexOf(':');
    const kind = value.slice(0, i);
    const id = value.slice(i + 1);
    const run = kind === 'run' ? this.project.find((r) => r.label === id) : null;
    const bench = this.benches.find((b) => b.key === (run ? run.bench : id)) || null;
    this.current = { value, bench, run };
    replace(this.desc, bench ? bench.description || '' : run ? `Stored run of bench “${run.bench}”.` : '',
      !this.available ? h('div.inline-warn', {}, icon('alert', 13), this.error || 'Live SPICE runs are unavailable.') : null);
    this._buildForm(bench, run?.params || {});
    this.runBtn.disabled = !this.available || !bench;
    if (run?.result_url) this._showStored(run);
    else replace(this.result, h('div.hint-box', {}, 'Set the inputs and press Run — the circuit is simulated with ngspice on this computer.'));
  }

  _buildForm(bench, values) {
    this.inputs.clear();
    if (!bench) { replace(this.form); return; }
    const rows = Object.entries(bench.params || {}).map(([name, spec]) => {
      const type = spec.type || (typeof spec.default === 'boolean' ? 'bool' : spec.choices?.length ? 'choice' : typeof spec.default === 'number' ? 'number' : 'text');
      const v = values[name] ?? spec.default;
      let input;
      if (type === 'bool') {
        input = h('input', { type: 'checkbox', name });
        input.checked = !!v;
      } else if (type === 'choice') {
        input = h('select', { name }, spec.choices.map((c, k) => h('option', { value: String(k) },
          typeof c === 'number' ? fmtVal(c, spec.unit) : String(c))));
        const k = spec.choices.findIndex((c) => (typeof c === 'number' ? Math.abs(c - Number(v)) < 1e-12 : String(c) === String(v)));
        input.value = String(Math.max(0, k));
      } else {
        input = h('input', { type: 'text', inputmode: type === 'number' ? 'decimal' : null, name, spellcheck: 'false',
          value: type === 'number' ? inputValue(Number(v), spec.unit) : String(v ?? '') });
      }
      input.addEventListener('input', () => { this._validate(name); this.autoRun(); });
      input.addEventListener('change', () => { this._validate(name); this.autoRun(); });
      input.addEventListener('keydown', (e) => { if (e.key === 'Enter') this.run(); });
      const bounded = type === 'number' && [spec.min, spec.max].some((x) => x !== null && x !== undefined);
      const rangeText = bounded ? [spec.min, spec.max].map((x) => (x === null || x === undefined ? '…' : fmtVal(x, spec.unit))).join(' – ') : '';
      const err = h('div.field-error');
      this.inputs.set(name, { input, spec, type, err });
      return h('div.param-row', { dataset: { param: name } },
        h('label', { title: spec.help ? `${name}: ${spec.help}` : name }, spec.label || name),
        h('div.param-input', { class: type === 'bool' ? 'is-bool' : null }, input, type !== 'bool' ? h('span.unit', {}, spec.unit || '') : null),
        h('span.range', { title: bounded ? `allowed range ${rangeText}` : null }, rangeText), err);
    });
    replace(this.form, rows.length ? rows : h('div.dim', {}, 'This bench has no inputs.'));
  }

  _validate(name) {
    const { input, spec, type, err } = this.inputs.get(name);
    let msg = '';
    let value;
    if (type === 'bool') value = input.checked;
    else if (type === 'choice') value = spec.choices[Number(input.value)];
    else if (type === 'text') value = input.value;
    else {
      value = parseSI(input.value);
      if (!Number.isFinite(value)) msg = 'Enter a number (e.g. 4.7k, 100n, 1e-6)';
      else if (spec.min !== null && spec.min !== undefined && value < spec.min) msg = `Minimum ${fmtVal(spec.min, spec.unit)}`;
      else if (spec.max !== null && spec.max !== undefined && value > spec.max) msg = `Maximum ${fmtVal(spec.max, spec.unit)}`;
    }
    err.textContent = msg;
    input.classList.toggle('invalid', !!msg);
    return { ok: !msg, value };
  }

  _fillDefaults() {
    const b = this.current?.bench;
    if (!b) return;
    this._buildForm(b, {});
    this.autoRun();
  }

  /** Validate the form and run the bench on the server. */
  async run() {
    const bench = this.current?.bench;
    if (!bench || !this.available) return;
    const params = {};
    let ok = true;
    for (const name of this.inputs.keys()) {
      const r = this._validate(name);
      ok = ok && r.ok;
      params[name] = r.value;
    }
    if (!ok) return;
    this.abort?.abort();
    this.abort = new AbortController();
    const signal = this.abort.signal;
    this.runBtn.classList.add('busy');
    this.result.classList.add('stale');
    try {
      const res = await postJSON('/api/spice/run', { key: bench.key, params }, signal);
      this._showResult(res, 'live');
    } catch (err) {
      if (err.name === 'AbortError') return;
      this.last = null;
      replace(this.result, h('div.error-box', { dataset: { testid: 'spice-error' } }, icon('error', 15),
        h('div', {}, h('strong', {}, err.status === 400 ? 'Invalid input' : 'Simulation failed'), h('pre.err-text', {}, err.detail || err.message))));
    } finally {
      if (!signal.aborted) { this.runBtn.classList.remove('busy'); this.result.classList.remove('stale'); }
    }
  }

  async _showStored(run) {
    try {
      const res = await getJSON(`${run.result_url}?v=${encodeURIComponent(this.version)}`);
      if (this.current?.run === run) this._showResult({ ...res, key: res.key || run.bench }, 'stored', run);
    } catch (err) {
      replace(this.result, empty('Stored result unavailable', err.message));
    }
  }

  _showResult(res, mode, run) {
    this.last = [res, mode, run];
    const rep = res.report || {};
    const ok = rep.ok !== false && !(rep.counts?.error > 0);
    const head = h('div.result-head', {},
      h('span.status-pill', { class: ok ? 'ok' : 'fail' }, icon(ok ? 'check' : 'error', 13), ok ? 'PASS' : 'FAIL'),
      h('span.dim', {}, mode === 'stored' ? `stored run “${run.label}” from the last build` : `simulated in ${num(res.elapsed_s ?? 0)} s`),
      run?.plot_url ? h('a.file-link', { href: run.plot_url, target: '_blank', rel: 'noopener' }, icon('external', 12), 'PNG') : null);
    const chart = h('div.chart', { dataset: { testid: 'spice-chart' } });
    const findings = (rep.findings || []).map((f) => findingRow(f));
    const netlist = res.netlist ? h('details.netlist', {}, h('summary', {}, 'Netlist'), h('pre.code', {}, res.netlist)) : null;
    replace(this.result, head, chart, this._measures(res), findings.length ? h('div.stack', {}, findings) : null, netlist);
    this._plot(chart, res);
  }

  _measures(res) {
    const units = res.units || {};
    let rows = Array.isArray(res.table) ? res.table.map((r) => ({ k: r.name, m: r.value, a: r.analytic, d: r.error_pct, unit: r.unit || guessUnit(r.name) })) : null;
    if (!rows) {
      const keys = [...new Set([...Object.keys(res.measures || {}), ...Object.keys(res.analytic || {})])];
      rows = keys.map((k) => {
        const m = res.measures?.[k];
        const a = res.analytic?.[k];
        const d = typeof m === 'number' && typeof a === 'number' && a !== 0 ? ((m - a) / Math.abs(a)) * 100 : null;
        return { k, m, a, d, unit: units[k] || guessUnit(k) };
      });
    }
    if (!rows.length) return null;
    return table([
      { key: 'k', label: 'Quantity', render: (r) => h('code', {}, r.k) },
      { key: 'm', label: 'Simulated', align: 'right', render: (r) => fmtVal(r.m, r.unit) },
      { key: 'a', label: 'Analytic', align: 'right', render: (r) => (r.a === undefined || r.a === null ? '—' : fmtVal(r.a, r.unit)) },
      { key: 'd', label: 'Δ', align: 'right', render: (r) => (r.d === null || r.d === undefined ? '—'
        : h('span', { class: Math.abs(r.d) < 2 ? 'pct-ok' : Math.abs(r.d) < 10 ? 'pct-warn' : 'pct-err' }, pct(r.d))) },
    ], rows, { className: 'compact measures' });
  }

  _plot(el, res) {
    this.plot?.destroy();
    this.plot = null;
    const traces = res.traces || {};
    const names = Object.keys(traces);
    if (names.length < 2) { replace(el, h('div.dim', {}, 'No traces')); return; }
    const xName = names.includes(res.x_label) ? res.x_label : names[0];
    const ys = names.filter((n) => n !== xName);
    const xs = splitLabel(xName);
    const logX = res.x_scale === 'log' || xs.name.toLowerCase().startsWith('freq');
    const units = [...new Set(ys.map((n) => splitLabel(n).unit))];
    const axisColor = cssVar('--text-dim');
    const grid = cssVar('--plot-grid');
    const scaleOf = (n) => (units.indexOf(splitLabel(n).unit) >= 1 ? 'y2' : 'y');
    const tick = (unit) => (u, vals) => vals.map((v) => (v === null ? '' : SI_BASE.has(unit) ? si(v, unit, 3) : fmtVal(v, unit)));
    const axes = [
      { stroke: axisColor, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid }, values: tick(xs.unit),
        label: xs.name, labelSize: 18, size: 36, font: '11px system-ui', labelFont: '11px system-ui' },
      { scale: 'y', stroke: axisColor, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid }, values: tick(units[0] || ''),
        size: 64, font: '11px system-ui' }];
    if (units.length > 1) {
      axes.push({ scale: 'y2', side: 1, stroke: axisColor, grid: { show: false }, values: tick(units.slice(1).join('/')),
        size: 64, font: '11px system-ui' });
    }
    const opts = {
      width: Math.max(260, el.clientWidth - 8 || 380), height: 240,
      scales: { x: { time: false, distr: logX ? 3 : 1, log: 10 } },
      axes,
      series: [{ label: xs.name, value: (u, v) => (v === null ? '—' : fmtVal(v, xs.unit)) },
        ...ys.map((n, i) => ({ label: n, scale: scaleOf(n), stroke: PALETTE[i % PALETTE.length], width: 1.6,
          value: (u, v) => (v === null ? '—' : fmtVal(v, splitLabel(n).unit)) }))],
      cursor: { drag: { x: true, y: false } },
      legend: { live: true },
    };
    this.plot = new uPlot(opts, [traces[xName], ...ys.map((n) => traces[n])], el);
  }

  _resizePlot() {
    const el = this.plot?.root?.parentElement;
    if (this.plot && el && el.clientWidth) this.plot.setSize({ width: Math.max(260, el.clientWidth - 8), height: 240 });
  }
}
