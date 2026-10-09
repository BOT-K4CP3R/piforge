// PiForge GUI — DOM helpers, number formatting with units, icons, toasts, mini markdown.

const SVG_NS = 'http://www.w3.org/2000/svg';

/**
 * Create an HTML element.
 * @param {string} tag element name, optionally with classes: "div.card.wide"
 * @param {object} [attrs] attributes; `on*` functions become listeners, `style` may be an object,
 *   `dataset` an object, `class` a string; `false`/`null` values are skipped
 * @param {...(Node|string|number|null|undefined|Array)} children
 * @returns {HTMLElement}
 */
export function h(tag, attrs = {}, ...children) {
  const [name, ...classes] = tag.split('.');
  const el = document.createElement(name || 'div');
  if (classes.length) el.className = classes.join(' ');
  applyAttrs(el, attrs);
  append(el, children);
  return el;
}

/**
 * Create an SVG element (same conventions as {@link h}).
 * @returns {SVGElement}
 */
export function s(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  applyAttrs(el, attrs);
  append(el, children);
  return el;
}

function applyAttrs(el, attrs) {
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (k === 'class') el.setAttribute('class', [el.getAttribute('class'), v].filter(Boolean).join(' '));
    else if (k === 'text') el.textContent = v;
    else el.setAttribute(k, v === true ? '' : String(v));
  }
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

/** Remove all children of `el` and append `children`. */
export function replace(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

// ------------------------------------------------------------------------------------ icons
// 24×24 stroke icons (drawn for PiForge).
const ICONS = {
  play: 'M7 5v14l11-7z',
  stop: 'M6 6h12v12H6z',
  refresh: 'M20 11a8 8 0 1 0-2.3 5.7M20 5v6h-6',
  sun: 'M12 4V2M12 22v-2M4 12H2M22 12h-2M5.6 5.6 4.2 4.2M19.8 19.8l-1.4-1.4M5.6 18.4l-1.4 1.4M19.8 4.2l-1.4 1.4M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z',
  moon: 'M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z',
  eye: 'M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12zM12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z',
  eyeOff: 'M3 3l18 18M10.6 5.1A10 10 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-3.2 4M6.6 6.6C3.7 8.4 2 12 2 12s3.6 7 10 7a9.6 9.6 0 0 0 5.4-1.6M9.9 9.9a3 3 0 0 0 4.2 4.2',
  target: 'M12 3v4M12 17v4M3 12h4M17 12h4M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z',
  fit: 'M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5',
  cube: 'M12 2 3 7v10l9 5 9-5V7zM3 7l9 5 9-5M12 12v10',
  scissors: 'M6 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM8.1 8.1 20 20M8.1 15.9 20 4',
  ruler: 'M3 17 17 3l4 4L7 21zM7 13l2 2M10 10l2 2M13 7l2 2',
  explode: 'M12 2v6M12 16v6M2 12h6M16 12h6M9 9l-4-4M15 9l4-4M9 15l-4 4M15 15l4 4',
  bed: 'M3 18h18M5 18V8h14v10M9 8V5h6v3',
  flame: 'M12 22a7 7 0 0 0 7-7c0-4-3-6-4-10-2 2-3 4-3 6-1-1-2-2-2-4-2 2-5 5-5 8a7 7 0 0 0 7 7z',
  alert: 'M12 3 2 20h20zM12 9v5M12 17v.5',
  error: 'M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zM12 7v6M12 16v.5',
  info: 'M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zM12 11v6M12 7.5V8',
  check: 'M4 12l5 5L20 6',
  x: 'M6 6l12 12M18 6 6 18',
  copy: 'M9 9h11v11H9zM5 15H4V4h11v1',
  download: 'M12 3v12M7 10l5 5 5-5M4 21h16',
  chevron: 'M9 6l6 6-6 6',
  search: 'M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM21 21l-5-5',
  chip: 'M7 7h10v10H7zM10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M3 14h4M17 10h4M17 14h4',
  wave: 'M2 12h3l3-8 4 16 4-12 3 4h3',
  printer: 'M6 9V3h12v6M6 18H4v-7h16v7h-2M7 14h10v7H7z',
  list: 'M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01',
  grid: 'M4 4h16v16H4zM4 9.3h16M4 14.7h16M9.3 4v16M14.7 4v16',
  xray: 'M12 3 4 7.5v9L12 21l8-4.5v-9zM4 7.5l8 4.5 8-4.5M12 12v9M8 5.3l8 4.4',
  panelLeft: 'M3 4h18v16H3zM9 4v16',
  panelRight: 'M3 4h18v16H3zM15 4v16',
  external: 'M14 4h6v6M20 4l-9 9M18 14v6H4V6h6',
  cable: 'M4 3v4M8 3v4M3 7h6v4a3 3 0 0 0 6 0V9a3 3 0 0 1 6 0v12',
};

/**
 * An inline SVG icon.
 * @param {string} name key of the built-in icon set
 * @param {number} [size=16] pixel size
 * @returns {SVGElement}
 */
export function icon(name, size = 16) {
  return s('svg', { viewBox: '0 0 24 24', width: size, height: size, class: `icon icon-${name}`,
    fill: name === 'play' || name === 'stop' ? 'currentColor' : 'none', stroke: 'currentColor',
    'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true' },
  s('path', { d: ICONS[name] || ICONS.info }));
}

/** Severity → icon element. */
export function sevIcon(sev, size = 14) {
  const name = { error: 'error', warning: 'alert', info: 'info' }[sev] || 'info';
  const el = icon(name, size);
  el.classList.add(`sev-${sev}`);
  return el;
}

// ------------------------------------------------------------------------------------ numbers
const PREFIXES = [[1e9, 'G'], [1e6, 'M'], [1e3, 'k'], [1, ''], [1e-3, 'm'], [1e-6, 'µ'], [1e-9, 'n'], [1e-12, 'p'], [1e-15, 'f']];
const SI_UNITS = new Set(['V', 'A', 'Ω', 'F', 'H', 's', 'Hz', 'W', 'ohm']);

/**
 * Engineering notation with an SI prefix, e.g. si(0.0039, 'A') → "3.90 mA".
 * @param {number} v value
 * @param {string} [unit] unit symbol
 * @param {number} [digits=3] significant digits
 */
export function si(v, unit = '', digits = 3) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—';
  v = Number(v);
  if (!Number.isFinite(v)) return v > 0 ? '∞' : '−∞';
  if (v === 0) return `0 ${unit}`.trim();
  const a = Math.abs(v);
  for (let k = 0; k < PREFIXES.length; k++) {
    const [f, p] = PREFIXES[k];
    if (a < f) continue;
    const str = (v / f).toPrecision(digits);
    if (Math.abs(Number(str)) >= 1000 && k > 0) {  // 999.96 → "1.00 k", not "1.00e+3"
      const [f2, p2] = PREFIXES[k - 1];
      return `${(v / f2).toPrecision(digits)} ${p2}${unit}`.trim();
    }
    return `${str} ${p}${unit}`.trim();
  }
  return `${v.toExponential(digits - 1)} ${unit}`.trim();
}

/**
 * Format a value with its unit: electrical units get SI prefixes, others fixed decimals.
 * @param {number} v
 * @param {string} [unit]
 */
export function withUnit(v, unit = '') {
  if (v === null || v === undefined || v === '') return '—';
  if (typeof v === 'boolean') return v ? 'on' : 'off';
  if (typeof v !== 'number') return `${v}${unit ? ' ' + unit : ''}`;
  if (SI_UNITS.has(unit)) return si(v, unit === 'ohm' ? 'Ω' : unit);
  return `${num(v)}${unit ? (unit === '%' || unit === '°' ? '' : ' ') + unit : ''}`;
}

/** Compact fixed formatting: 3 significant digits for small, no excess decimals for large. */
export function num(v, digits) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—';
  v = Number(v);
  if (digits !== undefined) return v.toFixed(digits);
  const a = Math.abs(v);
  if (a !== 0 && (a < 0.01 || a >= 1e6)) return v.toExponential(2);
  if (a >= 100) return v.toFixed(0);
  if (a >= 10) return v.toFixed(1);
  return v.toFixed(2);
}

/** Millimetre size triple → "124 × 72 × 32 mm", "22 × 6 × 1.6 mm" (0.1 mm below 100 mm, no trailing .0). */
export function sizeMM(size) {
  if (!Array.isArray(size)) return '—';
  const fmt = (x) => (Math.abs(x) >= 100 ? Number(x).toFixed(0) : String(Number(Number(x).toFixed(1))));
  return `${size.map(fmt).join(' × ')} mm`;
}

/** Hours → "2 h 10 min". */
export function duration(hours) {
  if (hours === null || hours === undefined || !Number.isFinite(Number(hours))) return '—';
  const mins = Math.round(Number(hours) * 60);
  if (mins < 1) return Number(hours) > 0 ? '< 1 min' : '0 min';
  if (mins < 60) return `${mins} min`;
  return `${Math.floor(mins / 60)} h ${String(mins % 60).padStart(2, '0')} min`;
}

const PREFIX_VALUE = { G: 1e9, M: 1e6, k: 1e3, K: 1e3, m: 1e-3, u: 1e-6, 'µ': 1e-6, n: 1e-9, p: 1e-12, f: 1e-15 };

/**
 * Parse numbers typed by a user: "4.7k", "4k7", "100n", "2.2µF", "1e-6", "3,3" → number (NaN if invalid).
 * @param {string|number} text
 */
export function parseSI(text) {
  if (typeof text === 'number') return text;
  let t = String(text ?? '').trim().replace(/\s+/g, '').replace(',', '.');
  if (!t) return NaN;
  const n = Number(t);
  if (!Number.isNaN(n)) return n;
  let m = t.match(/^([+-]?\d+)([GMkKmuµnpf])(\d+)$/);
  if (m) return Number(`${m[1]}.${m[3]}`) * PREFIX_VALUE[m[2]];
  t = t.replace(/(Ω|ohm|Ohm|F|H|V|A|Hz|s|W|°C|%)$/, '');
  m = t.match(/^([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)([GMkKmuµnpf])?$/);
  if (!m) return NaN;
  return Number(m[1]) * (m[2] ? PREFIX_VALUE[m[2]] : 1);
}

/** A number formatted for an input box (engineering notation for electrical units). */
export function inputValue(v, unit = '') {
  if (typeof v !== 'number') return String(v ?? '');
  if (SI_UNITS.has(unit) && v !== 0 && (Math.abs(v) < 0.1 || Math.abs(v) >= 1e4)) {
    const [mant, prefix = ''] = si(v, '', 4).split(' ');
    return `${Number(mant)}${prefix}`;  // "1.000 µ" → "1µ", "4.700 k" → "4.7k"
  }
  return String(Number(v.toPrecision(6)));
}

/** Relative time: "5 min ago". */
export function ago(iso) {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '';
  const sec = Math.max(0, (Date.now() - t) / 1000);
  if (sec < 60) return 'just now';
  if (sec < 3600) return `${Math.floor(sec / 60)} min ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} h ago`;
  return new Date(t).toLocaleDateString();
}

// ------------------------------------------------------------------------------------ misc
/** Debounce `fn` by `ms` milliseconds. */
export function debounce(fn, ms = 250) {
  let t = 0;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

/** Copy text to the clipboard (falls back to a hidden textarea). */
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = h('textarea', { style: { position: 'fixed', opacity: '0' } }, text);
    document.body.append(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  }
}

/**
 * Show a transient notification in the bottom-right corner.
 * @param {string} message
 * @param {'info'|'ok'|'warning'|'error'} [kind]
 * @param {number} [ms] auto-dismiss delay (0 = sticky)
 */
export function toast(message, kind = 'info', ms = 4500) {
  const host = document.getElementById('toasts');
  if (!host) return;
  const iconName = { ok: 'check', warning: 'alert', error: 'error' }[kind] || 'info';
  const el = h('div.toast', { class: `toast-${kind}`, role: 'status' }, icon(iconName, 16),
    h('span.toast-text', {}, message),
    h('button.toast-close', { title: 'Dismiss', onclick: () => el.remove() }, icon('x', 14)));
  host.append(el);
  if (ms) setTimeout(() => el.remove(), ms);
}

/**
 * A data table.
 * @param {Array<{key:string,label:string,align?:string,render?:Function}>} columns
 * @param {object[]} rows
 * @param {object} [opts] {className, empty}
 */
export function table(columns, rows, opts = {}) {
  const head = h('tr', {}, columns.map((c) => h('th', { class: c.align === 'right' ? 'num' : null }, c.label)));
  const body = rows.map((r) => h('tr', opts.rowAttrs ? opts.rowAttrs(r) : {}, columns.map((c) => {
    const v = c.render ? c.render(r) : r[c.key];
    return h('td', { class: c.align === 'right' ? 'num' : null }, v ?? '');
  })));
  if (!rows.length) body.push(h('tr', {}, h('td.empty', { colspan: columns.length }, opts.empty || 'Nothing to show')));
  return h('div.table-wrap', {}, h('table.data', { class: opts.className }, h('thead', {}, head), h('tbody', {}, body)));
}

/** A labelled empty-state block. */
export function empty(title, detail = '') {
  return h('div.empty-state', {}, h('div.empty-title', {}, title), detail ? h('div.empty-detail', {}, detail) : null);
}

// ------------------------------------------------------------------------------------ markdown
function inline(text) {
  const out = [];
  const re = /(`[^`]+`|\*\*[^*]+\*\*)/g;
  let last = 0;
  for (const m of text.matchAll(re)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const tok = m[0];
    out.push(tok.startsWith('`') ? h('code', {}, tok.slice(1, -1)) : h('strong', {}, tok.slice(2, -2)));
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function splitRow(line) {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
}

/**
 * Render a small, safe subset of Markdown (headings, tables, lists, code fences, paragraphs).
 * @param {string} md
 * @returns {HTMLElement}
 */
export function markdown(md) {
  const root = h('div.md');
  const lines = String(md || '').replace(/\r/g, '').split('\n');
  for (let i = 0; i < lines.length;) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    if (line.startsWith('```')) {
      const buf = [];
      for (i++; i < lines.length && !lines[i].startsWith('```'); i++) buf.push(lines[i]);
      root.append(h('pre.code', {}, buf.join('\n')));
      i++;
      continue;
    }
    const hm = line.match(/^(#{1,4})\s+(.*)$/);
    if (hm) { root.append(h(`h${Math.min(6, hm[1].length + 2)}`, {}, inline(hm[2]))); i++; continue; }
    if (line.trim().startsWith('|') && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      const header = splitRow(line);
      const aligns = splitRow(lines[i + 1]).map((c) => (c.endsWith(':') ? 'right' : 'left'));
      const rows = [];
      for (i += 2; i < lines.length && lines[i].trim().startsWith('|'); i++) rows.push(splitRow(lines[i]));
      root.append(h('div.table-wrap', {}, h('table.data', {},
        h('thead', {}, h('tr', {}, header.map((c, k) => h('th', { class: aligns[k] === 'right' ? 'num' : null }, inline(c))))),
        h('tbody', {}, rows.map((r) => h('tr', {}, r.map((c, k) => h('td', { class: aligns[k] === 'right' ? 'num' : null }, inline(c)))))))));
      continue;
    }
    if (/^\s*[-*]\s+/.test(line)) {
      const ul = h('ul');
      for (; i < lines.length && /^\s*[-*]\s+/.test(lines[i]); i++) ul.append(h('li', {}, inline(lines[i].replace(/^\s*[-*]\s+/, ''))));
      root.append(ul);
      continue;
    }
    const para = [];
    for (; i < lines.length && lines[i].trim() && !/^(#|\||```|\s*[-*]\s)/.test(lines[i]); i++) para.push(lines[i].trim());
    if (!para.length) { para.push(line); i++; }
    root.append(h('p', {}, inline(para.join(' '))));
  }
  return root;
}
