// PiForge GUI — split-flap display widget (Twin tab) and the "Amount" control of a `ws_feed`
// device. The widget mirrors the twin's `splitflap` devices (outputs digit, next_digit, flip, hall;
// params position, comma_after): every digit card flips like a real flap — the upper half of the
// current digit falls over the hinge and reveals the next one (CSS 3D transforms).
import { h, icon } from './ui.js';

/**
 * Flip timing (ms): a calm single flip, the quickest flip, and the most the display may lag behind.
 * FLIP_MS stays below one flap period of a module at full speed (850 half-steps/s → 4.15 flaps/s →
 * 240 ms), so a spinning digit plays every flap at its real pace instead of building a backlog.
 */
export const FLIP_MS = 200;
export const MIN_FLIP_MS = 55;
export const MAX_LAG_MS = 300;

const AMOUNT_MAX = 999999.99;

export const isDigit = (v) => Number.isInteger(v) && v >= 0 && v <= 9;
export const mod10 = (v) => ((v % 10) + 10) % 10;

/**
 * The digit a display should head for, from a `splitflap` device's state: `next_digit` as soon as
 * the twin's flap starts to fall (0 < flip < 1, so the display never lags), else `digit`.
 */
export function flapTarget(p) {
  const digit = Number(p?.digit);
  const next = Number(p?.next_digit);
  const flip = Number(p?.flip);
  return flip > 0.02 && flip < 1 && isDigit(next) ? next : digit;
}

/** Half of a card ("top"/"bottom") showing `d`; the full-height glyph is clipped by the half. */
function half(cls, d) {
  const glyph = h('span.sf-glyph', {}, d === null ? '' : String(d));
  return { el: h(`div.${cls}`, { 'aria-hidden': 'true' }, glyph), set: (v) => { glyph.textContent = v === null ? '' : String(v); } };
}

/**
 * One flap digit. `setTarget(d)` makes it flip forward (0 → 1 → … → 9 → 0, like a spool) until it
 * shows `d`. Backlogs are played faster; beyond {@link MAX_LAG_MS} of flips it skips ahead.
 */
export class FlapDigit {
  constructor(pos, id = '') {
    this.pos = pos;
    this.id = id;
    this.shown = null;
    this.target = null;
    this.busy = false;
    this.flips = 0;
    this.timer = 0;
    this.deadline = 0;
    this.settledAt = 0;
    this.top = half('sf-half.sf-top', null);
    this.bottom = half('sf-half.sf-bottom', null);
    this.front = half('sf-flap.sf-front', null);  // upper half of the old digit: falls down
    this.back = half('sf-flap.sf-back', null);    // lower half of the new digit: lands
    this.hall = h('span.sf-hall', { title: 'Hall sensor (magnet under the sensor = home)' });
    this.card = h('div.sf-card', {}, this.top.el, this.bottom.el, this.front.el, this.back.el);
    this.el = h('div.sf-digit', { dataset: { testid: `sf-digit-${pos}`, pos: String(pos), device: id }, title: id },
      this.card, this.hall);
    this.fresh = true;  // shows a placeholder 0 until the twin reports; the first report jumps
    this.top.set(0);
    this.bottom.set(0);
  }

  /** Show `d` at once (no animation). */
  jump(d) {
    clearTimeout(this.timer);
    this.busy = false;
    this.el.classList.remove('flipping');
    this._static(d);
    this.target = d;
    this.settledAt = performance.now();
  }

  _static(d) {
    this.shown = d;
    this.top.set(d);
    this.bottom.set(d);
    this.el.dataset.digit = d === null ? '' : String(d);
  }

  /** Flip towards `d` (0–9); anything else is ignored. */
  setTarget(d) {
    d = Number(d);
    if (!isDigit(d)) return;
    if (d !== this.target) this.deadline = performance.now() + MAX_LAG_MS * FlapDigit.timeScale;  // must show `d` by then
    this.target = d;
    if (this.fresh || this.shown === null) { this.fresh = false; this.jump(d); return; }
    this._pump();
  }

  setHall(on) { this.hall.classList.toggle('on', !!on); }

  _pump() {
    if (this.busy || this.target === null) return;
    if (this.target === this.shown) { this.settledAt = performance.now(); return; }
    let steps = mod10(this.target - this.shown);
    // Time left until the deadline (self-correcting when timers run late on a busy machine).
    const left = Math.max(0, this.deadline - performance.now()) / FlapDigit.timeScale;
    const keep = Math.max(1, Math.floor(left / MIN_FLIP_MS));
    if (steps > keep) {  // too far behind: skip ahead silently, flip only the last few flaps
      this._static(mod10(this.target - keep));
      steps = keep;
    }
    const ms = Math.round(Math.max(MIN_FLIP_MS, Math.min(FLIP_MS, left / steps)));
    this._flip(this.shown, mod10(this.shown + 1), ms);
  }

  _flip(a, b, ms) {
    ms *= FlapDigit.timeScale;
    this.busy = true;
    this.top.set(b);       // revealed behind the falling flap
    this.bottom.set(a);    // covered when the flap lands
    this.front.set(a);
    this.back.set(b);
    this.el.style.setProperty('--flip-ms', `${ms}ms`);
    this.el.classList.remove('flipping');
    void this.el.offsetWidth;  // restart the CSS animation
    this.el.classList.add('flipping');
    // A timer (not animationend): animations do not run while the tab is hidden.
    this.timer = setTimeout(() => {
      this.busy = false;
      this.flips += 1;
      this.el.classList.remove('flipping');
      this._static(b);
      this.el.dataset.flips = String(this.flips);
      this._pump();
    }, ms);
  }

  destroy() { clearTimeout(this.timer); }
}

/** Slow motion for demos and review screenshots (1 = real time; the lag bound holds only at 1). */
FlapDigit.timeScale = 1;

/** Device params → comma position (index of the digit the comma follows) or null. */
export function commaAfter(devices) {
  for (const d of devices) {
    const c = d.params?.comma_after;
    if (c === null || c === false || c === -1) return null;
    if (Number.isInteger(Number(c)) && c !== undefined && c !== '') return Number(c);
  }
  return devices.length >= 3 ? devices.length - 3 : null;  // 8 digits → "000000,00"
}

/** `splitflap` devices ordered by their `position` param (then by id). */
export function orderFlaps(devices) {
  const pos = (d) => { const p = Number(d.params?.position); return Number.isFinite(p) ? p : Infinity; };
  return devices.filter((d) => d.type === 'splitflap')
    .sort((a, b) => pos(a) - pos(b) || String(a.id).localeCompare(String(b.id), undefined, { numeric: true }));
}

/** A row of flap digits (+ the fixed comma) for the given `splitflap` devices. */
export class FlapRow {
  constructor(devices, { big = false } = {}) {
    this.devices = orderFlaps(devices);
    this.comma = commaAfter(this.devices);
    this.digits = this.devices.map((d, i) => new FlapDigit(i, d.id));
    this.byId = new Map(this.devices.map((d, i) => [d.id, this.digits[i]]));
    const cells = [];
    this.digits.forEach((dg, i) => {
      cells.push(dg.el);
      if (i === this.comma && i < this.digits.length - 1) {
        cells.push(h('div.sf-comma', { 'aria-hidden': 'true' }, h('div.sf-card', {}, h('span.sf-comma-glyph', {}, ','))));
      }
    });
    this.el = h('div.sf-row', { class: big ? 'big' : null, dataset: { testid: big ? 'splitflap-row-big' : 'splitflap-row' },
      role: 'img' }, cells);
    this.el.style.setProperty('--n', String(this.digits.length + (cells.length > this.digits.length ? 0.42 : 0)));
    this.el.style.setProperty('--cells', String(Math.max(0, cells.length - 1)));
    this._label();
  }

  /** Apply a twin `state` message (device id → props). */
  update(devices) {
    let touched = false;
    for (const [id, p] of Object.entries(devices || {})) {
      const dg = this.byId.get(id);
      if (!dg || !p || typeof p !== 'object') continue;
      touched = true;
      dg.setTarget(flapTarget(p));  // the flap has started to fall → play that flip now
      if ('hall' in p) dg.setHall(p.hall);
    }
    if (touched) this._label();
  }

  /** Copy what another row shows (no animation) — used when the fullscreen view opens. */
  copyFrom(row) {
    this.digits.forEach((dg, i) => {
      const src = row.digits[i];
      if (!src) return;
      if (src.target !== null) { dg.fresh = false; dg.jump(src.target); }
      dg.setHall(src.hall.classList.contains('on'));
    });
    this._label();
  }

  /** Displayed text: shown digits with the comma, e.g. "001234,56" (blank digits = "·"). */
  text() {
    return this.digits.map((d, i) => `${d.shown === null ? '·' : d.shown}${i === this.comma && i < this.digits.length - 1 ? ',' : ''}`).join('');
  }

  /** Text of the targets (where the display is heading). */
  targetText() {
    return this.digits.map((d, i) => `${d.target === null ? '·' : d.target}${i === this.comma && i < this.digits.length - 1 ? ',' : ''}`).join('');
  }

  _label() {
    const t = this.targetText();
    this.el.setAttribute('aria-label', `Split-flap display: ${t}`);
    this.el.dataset.value = t;
  }

  destroy() { for (const d of this.digits) d.destroy(); }
}

const round2 = (v) => Math.round(Number(v) * 100) / 100;
const clampAmount = (v) => Math.max(0, Math.min(AMOUNT_MAX, round2(v)));

/**
 * "Amount" control for a `ws_feed` device: number + Send, quick buttons, online switch, clients.
 * @param {object} dev device description
 * @param {(dev:string, prop:string, value:any) => void} send
 */
export function amountControl(dev, send) {
  const id = dev.id;
  const input = h('input.num.sf-amount', { type: 'number', min: 0, max: AMOUNT_MAX, step: 0.01, inputmode: 'decimal',
    dataset: { testid: 'amount-input' }, 'aria-label': `${id} amount` });
  input.value = '0.00';
  const err = h('span.field-error.sf-amount-err', { dataset: { testid: 'amount-error' } });
  const current = () => { const v = Number(input.value); return Number.isFinite(v) ? v : 0; };
  const sendAmount = (v) => {
    const raw = Number(v);
    if (!Number.isFinite(raw) || raw < 0 || raw > AMOUNT_MAX) {
      err.textContent = `0 – ${AMOUNT_MAX.toFixed(2)}`;
      return;
    }
    const val = clampAmount(raw);
    err.textContent = '';
    input.value = val.toFixed(2);
    send(id, 'amount', val);
  };
  const sendBtn = h('button.btn.primary.small', { type: 'button', dataset: { testid: 'amount-send' }, onclick: () => sendAmount(input.value) }, 'Send');
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); sendAmount(input.value); } });
  input.addEventListener('change', () => { const v = Number(input.value); if (Number.isFinite(v)) input.value = round2(v).toFixed(2); });
  const quick = [['+0.01', 0.01], ['+1', 1], ['+100', 100], ['+1000', 1000]].map(([label, d]) =>
    h('button.btn.small', { type: 'button', dataset: { testid: `amount-add-${String(d).replace('.', '_')}` },
      onclick: () => sendAmount(clampAmount(current() + d)) }, label));
  // Random: log-uniform so small and large amounts both show up (0.01 … 999 999.99).
  const rnd = h('button.btn.small', { type: 'button', dataset: { testid: 'amount-random' }, title: 'Random amount',
    onclick: () => sendAmount(clampAmount(10 ** (Math.random() * 6) - 1 + Math.random())) }, 'random');
  const online = h('input', { type: 'checkbox', role: 'switch', dataset: { testid: 'amount-online' },
    onchange: () => send(id, 'online', online.checked) });
  online.checked = dev.inputs?.online?.default ?? true;
  const clients = h('span.out-value', { dataset: { testid: 'amount-clients' } }, '—');
  const port = h('span.dim.mono', { dataset: { testid: 'amount-port' } }, dev.params?.port ? `:${dev.params.port}` : '');
  const controls = [input, sendBtn, ...quick, rnd, online];
  const el = h('div.sf-amount-box', { dataset: { testid: `amount-${id}` } },
    h('div.sf-amount-line', {}, h('label.sf-amount-label', {}, 'Amount'), input, sendBtn, err),
    h('div.sf-amount-line', {}, quick, rnd),
    h('div.sf-amount-line.dim', {},
      h('label.toggle', {}, online, h('span.toggle-track'), h('span', {}, 'online')),
      h('span.spacer'),
      h('span', {}, icon('wave', 12), ' ', h('span.dim', {}, `${id} clients `), clients, ' ', port)));
  return {
    el,
    set: (p) => {
      if (!p) return;
      if (typeof p.clients === 'number') clients.textContent = String(Math.round(p.clients));
      if (p.port) port.textContent = `:${p.port}`;
      if (typeof p.online === 'boolean') online.checked = p.online;
      if (typeof p.amount === 'number' && document.activeElement !== input) {
        input.value = round2(p.amount).toFixed(2);
      }
    },
    setEnabled: (on) => { for (const c of controls) c.disabled = !on; },
  };
}

/**
 * The whole "Display" section: split-flap row(s), amount control(s), fullscreen view.
 * @param {object[]} devices twin device descriptions
 * @param {(dev:string, prop:string, value:any) => void} send
 * @returns {null | {el, update, setEnabled, row, openFullscreen, closeFullscreen, destroy}}
 */
export function displaySection(devices, send) {
  const flaps = orderFlaps(devices);
  const feeds = devices.filter((d) => d.type === 'ws_feed');
  if (!flaps.length && !feeds.length) return null;
  const row = flaps.length ? new FlapRow(flaps) : null;
  const amounts = feeds.map((d) => amountControl(d, send));
  let big = null;
  let overlay = null;
  const onKey = (e) => { if (e.key === 'Escape') closeFullscreen(); };
  const onFs = () => { if (!document.fullscreenElement && overlay?.dataset.fs === '1') closeFullscreen(); };
  function closeFullscreen() {
    if (!overlay) return;
    document.removeEventListener('keydown', onKey);
    document.removeEventListener('fullscreenchange', onFs);
    if (document.fullscreenElement === overlay) document.exitFullscreen?.().catch(() => {});
    big?.destroy();
    overlay.remove();
    overlay = null;
    big = null;
  }
  function openFullscreen() {
    if (!row || overlay) return;
    big = new FlapRow(flaps, { big: true });
    big.copyFrom(row);
    overlay = h('div.sf-overlay', { dataset: { testid: 'splitflap-fullscreen' }, role: 'dialog', 'aria-label': 'Split-flap display' },
      h('button.btn.icon-only.sf-close', { type: 'button', title: 'Close (Esc)', 'aria-label': 'Close', dataset: { testid: 'splitflap-close' },
        onclick: closeFullscreen }, icon('x', 16)),
      h('div.sf-stage', {}, big.el));
    document.body.append(overlay);
    document.addEventListener('keydown', onKey);
    document.addEventListener('fullscreenchange', onFs);
    try {
      const p = overlay.requestFullscreen?.();
      p?.then(() => { if (overlay) overlay.dataset.fs = '1'; }).catch(() => {});
    } catch { /* fullscreen not allowed: the overlay alone fills the window */ }
  }
  const fsBtn = row ? h('button.btn.small', { type: 'button', dataset: { testid: 'splitflap-fullscreen-btn' }, title: 'Show only the display, full screen',
    onclick: openFullscreen }, icon('fit', 12), 'Fullscreen display') : null;
  const el = h('section.sf-section', { dataset: { testid: 'splitflap' } },
    h('div.section-row', {}, h('h4.section-title', {}, 'Display'), fsBtn),
    row ? h('div.sf-frame', {}, row.el) : null,
    amounts.map((a) => a.el));
  return {
    el, row,
    get big() { return big; },
    update: (devs) => {
      row?.update(devs);
      big?.update(devs);
      feeds.forEach((d, i) => amounts[i].set(devs?.[d.id]));
    },
    setEnabled: (on) => { for (const a of amounts) a.setEnabled(on); },
    openFullscreen, closeFullscreen,
    destroy: () => { closeFullscreen(); row?.destroy(); },
  };
}
