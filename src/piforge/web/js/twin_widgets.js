// PiForge GUI — digital-twin widgets: input controls generated from PropSpecs (honouring the
// `widget` hint: momentary, toggle, slider, stepper, file), output indicators (LED lamp, servo
// gauge, shaft dial, OLED canvas, LCD, NeoPixel strip, pills/values), and the live GPIO header.
import { h, num, s, withUnit } from './ui.js';

// src: Raspberry Pi 40-pin GPIO header, physical pin → function (identical on Pi 2B…5 and Zero),
// https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#gpio
export const HEADER = ['3V3', '5V', 'GPIO2', '5V', 'GPIO3', 'GND', 'GPIO4', 'GPIO14', 'GND', 'GPIO15',
  'GPIO17', 'GPIO18', 'GPIO27', 'GND', 'GPIO22', 'GPIO23', '3V3', 'GPIO24', 'GPIO10', 'GND',
  'GPIO9', 'GPIO25', 'GPIO11', 'GPIO8', 'GND', 'GPIO7', 'GPIO0', 'GPIO1', 'GPIO5', 'GND',
  'GPIO6', 'GPIO12', 'GPIO13', 'GND', 'GPIO19', 'GPIO16', 'GPIO26', 'GPIO20', 'GND', 'GPIO21'];

const NAMED = { red: '#ff3b30', green: '#30d158', blue: '#2f80ff', yellow: '#ffd60a', white: '#f4f4f0',
  orange: '#ff9f0a', amber: '#ffbf00', ir: '#a0306a', uv: '#8f5cff', warm_white: '#ffe3b0', pink: '#ff6fae' };

/** CSS colour from an LED colour name/hex (fallback when unknown). */
export function cssColor(c, fallback = '#ff3b30') {
  const k = String(c || '').toLowerCase().trim();
  if (NAMED[k]) return NAMED[k];
  if (/^#[0-9a-f]{3,8}$/i.test(k)) return k;
  return fallback;
}

/** LED lamp colour of a device description (params.color, else the scene's emissive colour). */
export function ledColor(device, fallback = '#ff3b30') {
  return cssColor(device?.params?.color, fallback);
}

const unitLabel = (u) => (u === 'deg' ? '°' : u || '');
const show = (v, unit) => (unit === 'deg' ? `${num(v, 1)}°` : withUnit(v, unit));
/** A value for its PropSpec: ints as whole numbers ("3", not "3.00"), others with units. */
const showSpec = (v, spec) => (spec.type === 'int' && Number.isFinite(Number(v))
  ? `${Math.round(Number(v))}${spec.unit ? ` ${unitLabel(spec.unit)}` : ''}` : show(v, spec.unit));

function step(spec) {
  if (spec.type === 'int') return 1;
  const span = Number(spec.max) - Number(spec.min);
  if (!Number.isFinite(span) || span <= 0) return 'any';
  const raw = span / 400;
  const p = 10 ** Math.floor(Math.log10(raw));
  return [1, 2, 5, 10].map((k) => k * p).find((v) => v >= raw) || raw;
}

function bounded(spec) {
  return spec.min !== null && spec.min !== undefined && spec.max !== null && spec.max !== undefined
    && Number.isFinite(Number(spec.min)) && Number.isFinite(Number(spec.max));
}

function widgetKind(prop, spec, deviceType) {
  if (spec.widget) return spec.widget;
  const t = spec.type || 'float';
  if (t === 'bool') return prop === 'pressed' && deviceType === 'button' ? 'momentary' : 'toggle';
  if (t === 'enum' || spec.choices?.length) return 'select';
  if (t === 'image') return 'file';
  if (t === 'text') return 'text';
  return bounded(spec) ? 'slider' : 'number';
}

/**
 * Build an input widget for one device input.
 * @param {string} dev device id
 * @param {string} prop property name
 * @param {object} spec PropSpec {type,min,max,unit,default,choices,label,widget}
 * @param {(dev:string, prop:string, value:any) => void} send
 * @param {string} [deviceType]
 * @returns {{el: HTMLElement, set: Function, setEnabled: Function}}
 */
export function inputWidget(dev, prop, spec, send, deviceType = '') {
  const label = spec.label || prop;
  const tid = `in-${dev}-${prop}`;
  const kind = widgetKind(prop, spec, deviceType);
  if (kind === 'momentary') {
    let latched = false;
    // A button caption is an action: "Pressed" (the property's label) reads "Press".
    const caption = /^pressed$/i.test(label) ? 'Press' : label;
    const btn = h('button.momentary', { type: 'button', dataset: { testid: `btn-${dev}-${prop}` },
      title: 'Press and hold (Space/Enter work too)' }, caption);
    const press = (v) => { btn.classList.toggle('down', v); send(dev, prop, v); };
    btn.addEventListener('pointerdown', (e) => {
      if (latched || btn.disabled) return;
      btn.setPointerCapture?.(e.pointerId);
      press(true);
    });
    const release = () => { if (!latched && btn.classList.contains('down')) press(false); };
    btn.addEventListener('pointerup', release);
    btn.addEventListener('pointercancel', release);
    btn.addEventListener('lostpointercapture', release);
    btn.addEventListener('keydown', (e) => {
      if ((e.key === ' ' || e.key === 'Enter') && !e.repeat && !latched) { e.preventDefault(); press(true); }
    });
    btn.addEventListener('keyup', (e) => { if (e.key === ' ' || e.key === 'Enter') release(); });
    const latch = h('button.icon-btn.latch', { type: 'button', title: 'Latch: keep it pressed', 'aria-pressed': 'false',
      onclick: () => {
        latched = !latched;
        latch.classList.toggle('active', latched);
        latch.setAttribute('aria-pressed', String(latched));
        press(latched);
      } }, 'hold');
    return { el: h('div.in-row', {}, btn, latch),
      set: (v) => { if (!latched) btn.classList.toggle('down', !!v); },
      setEnabled: (on) => { btn.disabled = !on; latch.disabled = !on; } };
  }
  if (kind === 'toggle') {
    const box = h('input', { type: 'checkbox', role: 'switch', dataset: { testid: tid }, onchange: () => send(dev, prop, box.checked) });
    box.checked = !!spec.default;
    return { el: h('label.toggle', {}, box, h('span.toggle-track'), h('span', {}, label)),
      set: (v) => { box.checked = !!v; }, setEnabled: (on) => { box.disabled = !on; } };
  }
  if (kind === 'stepper') {
    const mk = (d) => h('button.btn.small', { type: 'button', dataset: { testid: `${tid}-${d > 0 ? 'inc' : 'dec'}${Math.abs(d)}` },
      onclick: () => send(dev, prop, d) }, d > 0 ? `+${d}` : `−${Math.abs(d)}`);
    const btns = [mk(-10), mk(-1), mk(1), mk(10)];
    return { el: h('div.in-field', {}, h('span', {}, label), h('div.stepper', {}, btns)),
      set: () => {}, setEnabled: (on) => { for (const b of btns) b.disabled = !on; } };
  }
  if (kind === 'select') {
    const sel = h('select', { dataset: { testid: tid }, onchange: () => send(dev, prop, sel.value) },
      (spec.choices || []).map((c) => h('option', { value: String(c) }, String(c))));
    if (spec.default !== undefined && spec.default !== null) sel.value = String(spec.default);
    return { el: h('label.in-field', {}, h('span', {}, label), sel),
      set: (v) => { if (document.activeElement !== sel) sel.value = String(v); },
      setEnabled: (on) => { sel.disabled = !on; } };
  }
  if (kind === 'file' || kind === 'text') {
    const inp = h('input', { type: 'text', dataset: { testid: tid }, value: spec.default ?? '',
      placeholder: kind === 'file' ? 'path to an image (empty = test pattern)' : '',
      onchange: () => send(dev, prop, inp.value) });
    return { el: h('label.in-field', {}, h('span', {}, label), inp),
      set: (v) => { if (document.activeElement !== inp) inp.value = v ?? ''; },
      setEnabled: (on) => { inp.disabled = !on; } };
  }
  const isInt = spec.type === 'int';
  const cast = (v) => (isInt ? Math.round(Number(v)) : Number(v));
  const def = Number(spec.default ?? (bounded(spec) ? spec.min : 0));
  const value = h('input.num', { type: 'number', step: step(spec), dataset: { testid: `${tid}-value` },
    'aria-label': `${dev} ${label}` });
  value.value = String(def);
  let slider = null;
  let last = 0;
  let timer = 0;
  const push = (v, force = false) => {  // ≤ 20 messages/s while dragging; the final value always goes out
    clearTimeout(timer);
    const now = performance.now();
    if (force || now - last > 50) { last = now; send(dev, prop, v); return; }
    timer = setTimeout(() => { last = performance.now(); send(dev, prop, v); }, 60);
  };
  value.addEventListener('change', () => {
    const v = cast(value.value);
    if (!Number.isFinite(v)) return;
    if (slider) slider.value = String(v);
    push(v, true);
  });
  if (kind === 'slider' && bounded(spec)) {
    slider = h('input.slider', { type: 'range', min: spec.min, max: spec.max, step: step(spec), dataset: { testid: tid },
      'aria-label': `${dev} ${label}` });
    slider.value = String(def);
    slider.addEventListener('input', () => { value.value = slider.value; push(cast(slider.value)); });
    slider.addEventListener('change', () => push(cast(slider.value), true));
  }
  const u = unitLabel(spec.unit);
  return {
    el: h('div.in-slider', {}, h('div.in-label', {}, h('span', {}, label), h('span.in-value', {}, value, h('span.unit', {}, u))),
      slider, slider ? h('div.in-range', {}, h('span', {}, show(Number(spec.min), spec.unit)), h('span', {}, show(Number(spec.max), spec.unit))) : null),
    set: (v) => {
      if (document.activeElement === value || document.activeElement === slider || typeof v !== 'number') return;
      value.value = String(v);
      if (slider) slider.value = String(v);
    },
    setEnabled: (on) => { value.disabled = !on; if (slider) slider.disabled = !on; },
  };
}

// ---------------------------------------------------------------------------------------- outputs
function semiGauge(min, max) {
  const needle = s('line', { x1: 50, y1: 50, x2: 50, y2: 14, class: 'gauge-needle' });
  const ticks = [];
  for (let k = 0; k <= 4; k++) {
    const a = Math.PI * (1 - k / 4);
    ticks.push(s('line', { x1: 50 + 34 * Math.cos(a), y1: 50 - 34 * Math.sin(a), x2: 50 + 41 * Math.cos(a), y2: 50 - 41 * Math.sin(a), class: 'gauge-tick' }));
  }
  const svg = s('svg', { viewBox: '0 0 100 56', class: 'gauge' }, s('path', { d: 'M 9 50 A 41 41 0 0 1 91 50', class: 'gauge-arc' }),
    ticks, needle, s('circle', { cx: 50, cy: 50, r: 3.5, class: 'gauge-hub' }));
  return { svg, set: (v) => {
    const k = Math.max(0, Math.min(1, (Number(v) - min) / (max - min || 1)));
    const a = Math.PI * (1 - k);
    needle.setAttribute('x2', String(50 + 36 * Math.cos(a)));
    needle.setAttribute('y2', String(50 - 36 * Math.sin(a)));
  } };
}

function dial() {
  const hand = s('line', { x1: 30, y1: 30, x2: 30, y2: 8, class: 'gauge-needle' });
  const svg = s('svg', { viewBox: '0 0 60 60', class: 'dial' }, s('circle', { cx: 30, cy: 30, r: 25, class: 'dial-face' }),
    s('line', { x1: 30, y1: 3, x2: 30, y2: 8, class: 'gauge-tick' }), hand, s('circle', { cx: 30, cy: 30, r: 3, class: 'gauge-hub' }));
  return { svg, set: (deg) => { hand.setAttribute('transform', `rotate(${Number(deg) % 360} 30 30)`); } };
}

function valueRow(dev, name, spec) {
  const val = h('span.out-value', { dataset: { testid: `out-${dev.id}-${name}` } }, '—');
  const isBool = spec.type === 'bool';
  const row = h('div.out-row', {}, isBool ? h('span.pill-dot') : null, h('span.out-label', {}, spec.label || name), val);
  return { el: row, update: (p) => {
    if (!(name in p)) return;
    const v = p[name];
    if (isBool) { val.textContent = v ? 'ON' : 'off'; row.classList.toggle('on', !!v); return; }
    if (spec.type === 'text') { val.textContent = String(v); val.title = String(v); return; }
    val.textContent = typeof v === 'number' ? showSpec(v, spec) : String(v);
  } };
}

/**
 * Output indicators for one device.
 * @param {object} dev device description {id,type,outputs,params,display}
 * @param {string} lampColor default lamp colour for LEDs
 * @returns {{el: HTMLElement, update: (props: object) => void, display: (msg: object) => void}}
 */
export function outputWidgets(dev, lampColor) {
  const outs = dev.outputs || {};
  const parts = [];
  const updaters = [];
  const claimed = new Set();
  let canvas = null;
  let lcdBox = null;

  if (dev.type === 'led' || dev.type === 'rgb_led' || 'brightness' in outs) {
    const lamp = h('div.lamp', { dataset: { testid: `led-${dev.id}` }, style: { '--lamp': lampColor } });
    const txt = h('span.out-value', {}, 'off');
    parts.push(h('div.out-row.lamp-row', {}, lamp, h('span.out-label', {}, dev.type === 'rgb_led' ? 'colour' : 'brightness'), txt));
    for (const k of ['brightness', 'on', 'color', 'red', 'green', 'blue']) claimed.add(k);
    // State messages arrive at up to 30 Hz: touch the DOM only when something changed (rewriting
    // the inline custom properties every message keeps restarting the lamp's CSS transition).
    let shown = { b: null, color: null, text: null };
    updaters.push((p) => {
      let b = p.brightness;
      if (['red', 'green', 'blue'].every((k) => typeof p[k] === 'number')) b = Math.max(p.red, p.green, p.blue);
      if (b === undefined && typeof p.on === 'boolean') b = p.on ? 1 : 0;
      const color = typeof p.color === 'string' && p.color ? cssColor(p.color, lampColor) : shown.color;
      if (color && color !== shown.color) lamp.style.setProperty('--lamp', color);
      if (b === undefined) { shown = { ...shown, color }; return; }
      b = Math.max(0, Math.min(1, Number(b) || 0));
      const text = b > 0.02 ? (dev.type === 'rgb_led' && p.color ? `${p.color}` : `${Math.round(b * 100)} %`) : 'off';
      if (b !== shown.b) {
        lamp.style.setProperty('--b', String(b));
        lamp.dataset.brightness = String(b);
        lamp.classList.toggle('lit', b > 0.02);
      }
      if (text !== shown.text) txt.textContent = text;
      shown = { b, color, text };
    });
  }
  if (dev.type === 'servo' && 'angle' in outs) {
    const min = Number(dev.params?.min_angle ?? -90);
    const max = Number(dev.params?.max_angle ?? 90);
    const g = semiGauge(min, max);
    const val = h('span.out-value.big', {}, '—');
    parts.push(h('div.out-gauge', { dataset: { testid: `gauge-${dev.id}` } },
      h('div.gauge-box', {}, g.svg, h('div.gauge-scale', {}, h('span', {}, `${num(min, 0)}°`), h('span', {}, `${num(max, 0)}°`))),
      h('div', {}, h('span.out-label', {}, 'angle'), val)));
    claimed.add('angle');
    updaters.push((p) => { if (typeof p.angle === 'number') { g.set(p.angle); val.textContent = `${num(p.angle, 1)}°`; } });
  } else if ('angle' in outs && ('rpm' in outs || dev.type === 'stepper_28byj48' || dev.type === 'dc_motor')) {
    // Motors: a turning shaft dial, the speed in rpm (big) and the shaft angle within one turn.
    const d = dial();
    const rpm = 'rpm' in outs ? h('span.out-value.big', { dataset: { testid: `rpm-${dev.id}` } }, '—') : null;
    const ang = h('span.out-value', { dataset: { testid: `angle-${dev.id}` } }, '—');
    parts.push(h('div.out-gauge', { dataset: { testid: `gauge-${dev.id}` } }, d.svg,
      h('div', {}, rpm ? h('span.out-label', {}, 'speed') : null, rpm,
        h('span.out-sub', {}, h('span.out-label', {}, 'shaft'), ang))));
    claimed.add('angle');
    if (rpm) claimed.add('rpm');
    updaters.push((p) => {
      if (typeof p.angle === 'number') {
        d.set(p.angle);
        ang.textContent = `${num(((p.angle % 360) + 360) % 360, 1)}°`;
        ang.title = `${num(p.angle, 1)}° since start`;
      }
      if (rpm && typeof p.rpm === 'number') rpm.textContent = `${num(p.rpm, 1)} rpm`;
    });
  }
  if (dev.display || dev.type === 'ssd1306') {
    const w = Number(dev.params?.width ?? 128);
    const hgt = Number(dev.params?.height ?? (dev.type === 'ssd1306' ? 64 : 32));
    canvas = h('canvas.oled', { width: w, height: hgt, dataset: { testid: `display-${dev.id}` } });
    parts.push(h('div.out-display', { class: dev.type === 'ssd1306' ? null : 'lcd-frame' }, canvas));
    if (dev.type !== 'ssd1306') canvas.hidden = true;
  }
  if (dev.type === 'lcd1602_pcf8574' && 'text' in outs) {
    lcdBox = h('pre.lcd', { dataset: { testid: `lcd-${dev.id}` } }, `${' '.repeat(16)}\n${' '.repeat(16)}`);
    parts.push(lcdBox);
    claimed.add('text');
    updaters.push((p) => {
      if (typeof p.text === 'string') lcdBox.textContent = p.text.split('\n').slice(0, 4).map((l) => l.padEnd(16).slice(0, 20)).join('\n');
      if ('backlight' in p) lcdBox.classList.toggle('dark', !p.backlight);
    });
  }
  if (dev.type === 'neopixel' && 'pixels' in outs) {
    const strip = h('div.pixels', { dataset: { testid: `pixels-${dev.id}` } });
    parts.push(strip);
    claimed.add('pixels');
    updaters.push((p) => {
      if (p.pixels === undefined) return;
      const list = Array.isArray(p.pixels) ? p.pixels : String(p.pixels).split(/[\s,;]+/).filter(Boolean);
      strip.replaceChildren(...list.slice(0, 64).map((c) => h('span.px', { style: { background: cssColor(c, '#000') } })));
    });
  }
  if (dev.type === 'dc_motor' && 'speed' in outs) {
    // `speed` is the H-bridge drive command (−1…1); the measured shaft speed is `rpm` above.
    const bar = h('div.speed-bar', { title: 'H-bridge drive: reverse ← 0 → forward' }, h('div.speed-fill'));
    const pct = h('span.out-value', { dataset: { testid: `drive-${dev.id}` } }, '—');
    parts.push(h('div.out-row', {}, h('span.out-label', {}, 'drive'), bar, pct));
    claimed.add('speed');
    updaters.push((p) => {
      if (typeof p.speed !== 'number') return;
      const f = bar.firstChild;
      const v = Math.max(-1, Math.min(1, p.speed));
      f.style.left = `${50 + Math.min(0, v) * 50}%`;
      f.style.width = `${Math.abs(v) * 50}%`;
      pct.textContent = `${Math.round(v * 100)} %`;
    });
  }
  for (const [name, spec] of Object.entries(outs)) {
    if (claimed.has(name)) continue;
    const r = valueRow(dev, name, spec || {});
    parts.push(r.el);
    updaters.push(r.update);
  }
  const el = h('div.outputs', {}, parts.length ? parts : h('span.dim', {}, 'no outputs'));
  return {
    el,
    update: (props) => { for (const u of updaters) u(props || {}); },
    display: (msg) => {
      if (!canvas) {
        canvas = h('canvas.oled', { width: msg.w || 128, height: msg.h || 64, dataset: { testid: `display-${dev.id}` } });
        el.append(h('div.out-display', {}, canvas));
      }
      const img = new Image();
      img.onload = () => {
        canvas.width = img.width;
        canvas.height = img.height;
        canvas.getContext('2d').drawImage(img, 0, 0);
        canvas.hidden = false;
        if (lcdBox) lcdBox.hidden = true;
      };
      img.src = `data:image/png;base64,${msg.png_b64}`;
    },
  };
}

/**
 * The live 40-pin header (levels from twin `state.pins`: 0/1, PWM pins report their duty).
 * @param {Map<number,string>} owners BCM → device id(s) wired to it
 * @returns {{el: HTMLElement, update: (pins: object) => void, reset: Function}}
 */
export function gpioHeader(owners) {
  const cells = new Map();
  const rows = [];
  for (let r = 0; r < 20; r++) {
    const mk = (fn, phys, side) => {
      const bcm = fn.startsWith('GPIO') ? Number(fn.slice(4)) : null;
      const kind = bcm === null ? fn.toLowerCase() : 'gpio';
      const owner = bcm !== null ? owners.get(bcm) : null;
      const dot = h('span.pin', { class: `pin-${kind}${owner ? ' used' : ''}`, title: `pin ${phys} · ${fn}${owner ? ` · ${owner}` : ''}` }, String(phys));
      if (bcm !== null) cells.set(bcm, dot);
      const label = h('span.pin-label', { class: `${side}${owner ? ' used' : ''}` }, fn);
      const tag = h('span.pin-owner', { class: side, title: owner || '' }, owner || '');
      return side === 'left' ? [tag, label, dot] : [dot, label, tag];
    };
    rows.push(h('div.hdr-row', {}, mk(HEADER[2 * r], 2 * r + 1, 'left'), mk(HEADER[2 * r + 1], 2 * r + 2, 'right')));
  }
  return {
    el: h('div.gpio-header', { dataset: { testid: 'gpio-header' } }, rows),
    update: (pins) => {
      for (const [k, v] of Object.entries(pins || {})) {
        const cell = cells.get(Number(k));
        if (!cell) continue;
        const level = Number(typeof v === 'object' && v !== null ? v.level : v);
        const pwm = level > 0 && level < 1;
        cell.classList.toggle('high', level >= 1);
        cell.classList.toggle('low', level === 0);
        cell.classList.toggle('pwm', pwm);
        cell.style.setProperty('--duty', pwm ? String(level) : '');
        cell.dataset.level = String(level);
      }
    },
    reset: () => { for (const c of cells.values()) { c.classList.remove('high', 'low', 'pwm'); delete c.dataset.level; } },
  };
}
