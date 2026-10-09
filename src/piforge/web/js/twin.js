// PiForge GUI — Twin tab: start/stop the digital twin (real firmware on a virtual Pi), device
// cards with inputs and outputs, live GPIO header, console, scenario runner; couples the twin
// state to the 3D view (driven joints, glowing LEDs, split-flap faces). Scenarios run "watched":
// the server streams each scenario's twin state over /ws/twin (tagged `scenario`), so the 3D view,
// the Display and the device cards move while a scenario runs.
import { Socket, getJSON, postJSON } from './api.js';
import { findingRow } from './checks.js';
import { empty, h, icon, num, replace, toast } from './ui.js';
import { gpioHeader, inputWidget, ledColor, outputWidgets } from './twin_widgets.js';
import { displaySection } from './splitflap.js';

const MAX_CONSOLE_LINES = 600;

/** Why the firmware ended (`exit.reason` from the twin runner) → console text. */
const EXIT_REASON = {
  stopped: 'firmware stopped', duration: 'run time limit reached', finished: 'firmware finished (its main script returned)',
  'sys.exit': 'firmware called sys.exit()', interrupted: 'firmware interrupted', crash: 'firmware crashed',
};

// src: Raspberry Pi GPIO alternate functions (BCM2711 ARM Peripherals §5.3; raspberrypi.com GPIO docs):
// I2C1 SDA/SCL = GPIO2/3, I2C0 (HAT ID) = GPIO0/1; SPI0 MISO/MOSI/SCLK = 9/10/11, CE0/CE1 = 8/7;
// SPI1 MISO/MOSI/SCLK = 19/20/21, CE0/1/2 = 18/17/16.
const BUS_PINS = {
  i2c: { 0: [0, 1], 1: [2, 3] },
  spi: { 0: { pins: [9, 10, 11], cs: [8, 7] }, 1: { pins: [19, 20, 21], cs: [18, 17, 16] } },
};

/** BCM pins a device occupies: its own pins plus the bus lines it is attached to. */
export function devicePins(d) {
  const out = Object.values(d.pins || {}).map(Number).filter(Number.isInteger);
  const bus = d.bus;
  if (bus?.kind === 'i2c') out.push(...(BUS_PINS.i2c[bus.bus ?? 1] || []));
  if (bus?.kind === 'spi') {
    const s = BUS_PINS.spi[bus.bus ?? 0];
    if (s) out.push(...s.pins, ...(s.cs[bus.cs ?? 0] !== undefined ? [s.cs[bus.cs ?? 0]] : []));
  }
  return [...new Set(out)];
}

/** Text for an `exit` message. */
function exitText(m) {
  if (m.error) return `■ firmware stopped with an error (exit code ${m.code ?? '?'}):\n${m.error}`;
  return `■ ${EXIT_REASON[m.reason] || 'firmware exited'} (exit code ${m.code ?? 0})`;
}

/** Twin tab. Options: scene (SceneModel) for the 3D coupling, onStatus(running). */
export class TwinPanel {
  /**
   * @param {HTMLElement} host
   * @param {{scene?: import('./scene.js').SceneModel, onStatus?: Function}} opts
   */
  constructor(host, opts = {}) {
    this.host = host;
    this.scene = opts.scene;
    this.onStatus = opts.onStatus;
    this.running = false;
    this.error = null;
    this.info = null;
    this.devices = [];
    this.cards = new Map();
    this.lastState = null;
    this.startBtn = h('button.btn.primary', { dataset: { testid: 'twin-start' }, onclick: () => this.start() }, icon('play', 14), 'Start');
    this.stopBtn = h('button.btn', { dataset: { testid: 'twin-stop' }, disabled: true, onclick: () => this.stop() }, icon('stop', 13), 'Stop');
    this.pill = h('span.status-pill.idle', { dataset: { testid: 'twin-status' } }, 'stopped');
    this.clock = h('span.twin-clock.dim');
    this.banner = h('div.twin-banner');
    this.displayHost = h('div.twin-display', { dataset: { testid: 'twin-display' } });
    this.display = null;
    this.deviceGrid = h('div.device-grid', { dataset: { testid: 'device-grid' } });
    this.gpioHost = h('div');
    this.consoleEl = h('pre.console', { dataset: { testid: 'twin-console' } });
    this.scenarioHost = h('div.scenarios');
    this.fwLabel = h('span.dim.fw');
    replace(host,
      h('div.pane-toolbar.twin-toolbar', {}, this.startBtn, this.stopBtn, this.pill, this.clock, h('span.spacer'), this.fwLabel),
      this.banner,
      this.displayHost,
      h('h4.section-title', {}, 'Devices'), this.deviceGrid,
      h('div.section-row', {}, h('h4.section-title', {}, 'Console'),
        h('button.btn.small.ghost', { onclick: () => replace(this.consoleEl) }, 'Clear')), this.consoleEl,
      h('h4.section-title', {}, 'GPIO header'), this.gpioHost,
      h('h4.section-title', {}, 'Scenarios'), this.scenarioHost);
    this.sock = new Socket('/ws/twin', { onMessage: (m) => this._onMessage(m), onClose: () => this._setRunning(false, null, true) });
  }

  /** Load the configured devices/scenarios (once per build version). */
  async ensure(version = '') {
    if (this.info && this.version === version) return;
    this.version = version;
    try {
      this.info = await getJSON('/api/twin/devices');
    } catch (err) {
      this.info = { devices: [], scenarios: [], error: err.message };
    }
    this.fwLabel.textContent = this.info.firmware ? `firmware: ${this.info.firmware}` : '';
    if (!this.running || !this.devices.length) this._buildCards(this.info.devices || []);
    this._buildScenarios();
    this._updateBanner();
  }

  /** Forget cached data (after a rebuild). */
  invalidate() { this.info = null; }

  /** Ask the server to (re)start the twin. */
  start() {
    replace(this.consoleEl);
    this._log('▶ starting the twin…', 'sys');
    this.error = null;
    this._exitLogged = false;
    this._updateBanner();
    this.pill.className = 'status-pill busy';
    this.pill.textContent = 'starting';
    if (!this.sock.send({ op: 'start' })) toast('Not connected to the server', 'error');
  }

  /** Ask the server to stop the twin. */
  stop() { this.sock.send({ op: 'stop' }); }

  _send(device, prop, value) {
    if (!this.running) return;
    this.sock.send({ op: 'input', device, prop, value });
  }

  _onMessage(m) {
    if (m.scenario && m.op !== 'scenario') { this._scenarioMessage(m); return; }
    switch (m.op) {
      case 'status':
        if (m.running && m.error) {  // e.g. a rejected input: the twin keeps running
          this._log(`✖ ${m.error}`, 'err');
          toast(m.error, 'warning', 4000);
          this._setRunning(true);
        } else this._setRunning(!!m.running, m.error || null);
        break;
      case 'hello': this._buildCards(this._mergeHello(m.devices || [])); break;
      case 'state': this._state(m); break;
      case 'display': this.cards.get(m.device)?.out.display(m); break;
      case 'log': {
        const level = m.level || (m.stream === 'stderr' ? 'error' : '');
        const kind = level === 'error' ? 'err' : level === 'warning' ? 'warn' : m.stream === 'twin' ? 'twin' : 'out';
        this._log(m.stream === 'twin' ? `[twin] ${m.text ?? ''}` : m.text ?? '', kind);
        break;
      }
      case 'exit':
        this._exitLogged = true;
        this._log(exitText(m), m.error ? 'err' : 'sys');
        break;
      case 'scenario': this._scenarioProgress(m); break;
      default: break;
    }
  }

  /** A message of a watched scenario's own twin session (same ops as the live twin). */
  _scenarioMessage(m) {
    switch (m.op) {
      case 'hello': this._buildCards(this._mergeHello(m.devices || [])); break;
      case 'state':
        this._state(m);
        this._scenarioTick(m.t);
        break;
      case 'display': this.cards.get(m.device)?.out.display(m); break;
      case 'log': {
        const level = m.level || (m.stream === 'stderr' ? 'error' : '');
        const kind = level === 'error' ? 'err' : level === 'warning' ? 'warn' : m.stream === 'twin' ? 'twin' : 'out';
        this._log(`[${m.scenario}] ${m.stream === 'twin' ? '[twin] ' : ''}${m.text ?? ''}`, kind);
        break;
      }
      case 'exit': this._log(`[${m.scenario}] ${exitText(m)}`, m.error ? 'err' : 'sys'); break;
      default: break;
    }
  }

  /** `scenario` progress message (start / end / done) of a watched run. */
  _scenarioProgress(m) {
    const row = this.scenarioRows?.get(m.name);
    if (m.phase === 'start') {
      this.scenarioRun = { name: m.name, duration: Number(m.duration) || 0, index: m.index, total: m.total };
      this.pill.className = 'status-pill busy';
      this.pill.textContent = `scenario ${m.index + 1}/${m.total}`;
      this.clock.textContent = '';
      this._log(`▶ scenario ${m.name} (${m.index + 1}/${m.total}, ${num(m.duration, 1)} s)`, 'sys');
      if (row) { row.set('run', 'RUNNING'); row.progress(0); }
    } else if (m.phase === 'end') {
      if (row) { row.set(m.ok ? 'ok' : 'fail', m.ok ? 'PASS' : 'FAIL'); row.progress(1); }
      this._log(`■ scenario ${m.name}: ${m.ok ? 'PASS' : 'FAIL'}`, m.ok ? 'sys' : 'err');
    } else if (m.phase === 'done') {
      this.scenarioRun = null;
      if (m.error) this._log(`✖ ${m.error}`, 'err');
      this._setRunning(this.running);
    }
  }

  _scenarioTick(t) {
    const run = this.scenarioRun;
    if (!run || typeof t !== 'number') return;
    this.clock.textContent = `${run.name}: t = ${num(t, 1)} / ${num(run.duration, 1)} s`;
    this.scenarioRows?.get(run.name)?.progress(run.duration > 0 ? t / run.duration : 0);
  }

  _mergeHello(devs) {
    const cfg = new Map((this.info?.devices || []).map((d) => [d.id, d]));
    return devs.map((d) => ({ ...(cfg.get(d.id) || {}), ...d }));
  }

  _setRunning(running, error, disconnected = false) {
    const was = this.running;
    this.running = running;
    if (error !== undefined && !disconnected) this.error = error;
    this.startBtn.disabled = false;
    this.startBtn.lastChild.textContent = running ? 'Restart' : 'Start';
    this.stopBtn.disabled = !running;
    this.pill.className = `status-pill ${running ? 'ok' : this.error ? 'fail' : 'idle'}`;
    this.pill.textContent = running ? 'running' : this.error ? 'error' : 'stopped';
    for (const c of this.cards.values()) for (const w of c.inputs) w.setEnabled(running);
    this.display?.setEnabled(running);
    this.displayHost.classList.toggle('stale', !running);
    this.deviceGrid.classList.toggle('stale', !running);
    this.deviceGrid.title = running ? '' : 'Twin stopped — values are from the last run';
    if (was && !running) {
      this.scene?.resetTwin();
      this.gpio?.reset();
      if (!this.error && !this._exitLogged) this._log('■ twin stopped', 'sys');
    }
    if (error && error !== this._lastLoggedError) { this._log(`✖ ${error}`, 'err'); this._lastLoggedError = error; }
    this._updateBanner();
    this.onStatus?.(running);
  }

  _updateBanner() {
    if (this.error) {
      replace(this.banner, h('div.error-box', { dataset: { testid: 'twin-error' } }, icon('error', 15),
        h('div', {}, h('strong', {}, 'Twin stopped with an error'), h('pre.err-text', {}, this.error))));
    } else if (this.info?.error && !this.info?.available) {
      replace(this.banner, h('div.inline-warn', {}, icon('alert', 13), this.info.error));
    } else if (this.info && !this.info.firmware_exists) {
      replace(this.banner, h('div.inline-warn', {}, icon('alert', 13), 'This project has no firmware to run (p.firmware(...) in project.py).'));
    } else replace(this.banner);
  }

  _buildCards(devices) {
    this.devices = devices;
    this.cards.clear();
    const owners = new Map();
    const glowColors = new Map();
    for (const n of this.scene?.nodes?.values?.() || []) {
      const ef = n.def.emissive_from;
      if (ef?.device) glowColors.set(ef.device, ef.color);
    }
    const cards = devices.map((d) => {
      for (const bcm of devicePins(d)) owners.set(bcm, [owners.get(bcm), d.id].filter(Boolean).join(', '));
      // A ws_feed's amount is set from the Display section's Amount control (2 decimals, quick buttons).
      const specs = Object.fromEntries(Object.entries(d.inputs || {}).filter(([p]) => !(d.type === 'ws_feed' && p === 'amount')));
      const inputs = Object.entries(specs).map(([p, spec]) => inputWidget(d.id, p, spec || {}, (dev, prop, v) => this._send(dev, prop, v), d.type));
      for (const w of inputs) w.setEnabled(this.running);
      const out = outputWidgets(d, ledColor(d, glowColors.get(d.id) || '#ff3b30'));
      this.cards.set(d.id, { inputs, out, specs });
      const pins = Object.entries(d.pins || {}).map(([role, bcm]) => `${role === 'pin' ? '' : `${role} `}GPIO${bcm}`).join(' · ');
      const addr = d.bus?.address !== undefined && d.bus?.address !== null ? ` @ 0x${Number(d.bus.address).toString(16).padStart(2, '0')}` : '';
      const bus = d.bus ? `${(d.bus.kind || 'bus').toUpperCase()}${d.bus.bus ?? ''}${addr}${d.bus.cs !== undefined ? ` CE${d.bus.cs}` : ''}` : '';
      return h('section.device-card', { dataset: { device: d.id, testid: `device-${d.id}` } },
        h('header', {}, h('strong', {}, d.id), h('span.dev-type', { title: d.type || '' }, d.label || d.type || '')),
        pins || bus ? h('div.dev-pins', {}, [pins, bus].filter(Boolean).join(' · ')) : null,
        inputs.length ? h('div.inputs', {}, inputs.map((w) => w.el)) : null,
        out.el);
    });
    replace(this.deviceGrid, cards.length ? cards
      : empty('No twin devices', this.info?.error || 'Build a project with firmware and twin-capable parts.'));
    this.gpio = gpioHeader(owners);
    replace(this.gpioHost, this.gpio.el);
    this.display?.destroy();
    this.display = displaySection(devices, (dev, prop, v) => this._send(dev, prop, v));
    this.display?.setEnabled(this.running);
    replace(this.displayHost, this.display?.el);
    if (this.lastState && this.running) this._state(this.lastState);
  }

  _state(m) {
    this.lastState = m;
    if (typeof m.t === 'number') this.clock.textContent = `t = ${num(m.t, 1)} s`;
    for (const [id, props] of Object.entries(m.devices || {})) {
      const c = this.cards.get(id);
      if (!c || !props || typeof props !== 'object') continue;
      c.out.update(props);
      const names = Object.keys(c.specs);
      c.inputs.forEach((w, i) => { if (names[i] in props) w.set(props[names[i]]); });
    }
    this.display?.update(m.devices);
    this.gpio?.update(m.pins);
    this.scene?.applyTwinState(m);
  }

  _log(text, kind = 'out') {
    const atBottom = this.consoleEl.scrollHeight - this.consoleEl.scrollTop - this.consoleEl.clientHeight < 24;
    const line = h('span.line', { class: `log-${kind}` }, text.endsWith('\n') ? text : `${text}\n`);
    this.consoleEl.append(line);
    while (this.consoleEl.childNodes.length > MAX_CONSOLE_LINES) this.consoleEl.firstChild.remove();
    if (atBottom) this.consoleEl.scrollTop = this.consoleEl.scrollHeight;
  }

  _buildScenarios() {
    const list = this.info?.scenarios || [];
    this.scenarioRows = new Map();
    if (!list.length) {
      replace(this.scenarioHost, h('div.dim', {}, 'No scenarios defined (p.scenario(...) in project.py).'));
      return;
    }
    const results = h('div.scenario-results', { dataset: { testid: 'scenario-results' } });
    const buttons = [];
    const rows = list.map((s) => {
      const pill = h('span.status-pill.idle', { dataset: { testid: `scenario-status-${s.name}` } }, '—');
      const bar = h('span.scenario-bar', {}, h('span.scenario-bar-fill'));
      const runOne = h('button.btn.small.ghost.icon-only', { type: 'button', title: `Run ${s.name} (watch it in the 3D view)`,
        'aria-label': `Run ${s.name}`, dataset: { testid: `scenario-run-${s.name}` }, onclick: () => run([s.name]) }, icon('play', 12));
      buttons.push(runOne);
      const el = h('div.scenario-row', { dataset: { scenario: s.name } }, pill, h('strong', {}, s.name),
        h('span.dim', {}, s.duration ? `${num(s.duration, 1)} s` : ''), bar, h('span.spacer'), runOne);
      this.scenarioRows.set(s.name, {
        el,
        set: (cls, text) => { pill.className = `status-pill ${cls}`; pill.textContent = text; },
        progress: (f) => { bar.firstChild.style.width = `${Math.round(Math.max(0, Math.min(1, f)) * 100)}%`; },
      });
      return el;
    });
    const showResults = (rep, note) => {
      for (const s of rep.scenarios || []) this.scenarioRows.get(s.name)?.set(s.ok ? 'ok' : 'fail', s.ok ? 'PASS' : 'FAIL');
      replace(results, h('div.dim.result-note', {}, note),
        (rep.scenarios || []).map((s) => (s.counts && (s.counts.error || s.counts.warning) ? h('div.dim', {}, h('strong', {}, s.name), ': ',
          ['error', 'warning'].filter((k) => s.counts[k]).map((k) => `${s.counts[k]} ${k}${s.counts[k] === 1 ? '' : 's'}`).join(' · ')) : null)),
        h('div.stack', {}, (rep.findings || []).filter((f) => f.severity !== 'info' || !rep.ok).map((f) => findingRow(f))));
    };
    const run = async (names) => {
      for (const b of buttons) { b.disabled = true; }
      btn.classList.add('busy');
      for (const n of names || list.map((s) => s.name)) { const r = this.scenarioRows.get(n); r?.set('idle', 'queued'); r?.progress(0); }
      replace(results, h('div.loading-line', {}, 'Running — watch the 3D view and the Display…'));
      const t0 = performance.now();
      try {
        const rep = await postJSON('/api/twin/scenarios/run', names ? { names, watch: true } : { watch: true });
        showResults(rep, `Ran just now in ${num((performance.now() - t0) / 1000, 1)} s`);
      } catch (err) {
        replace(results, h('div.error-box', {}, icon('error', 15), h('div', {}, err.detail || err.message)));
      } finally {
        btn.classList.remove('busy');
        for (const b of buttons) b.disabled = false;
      }
    };
    const btn = h('button.btn', { dataset: { testid: 'scenarios-run' }, title: 'Run every scenario; the twin state is shown live',
      onclick: () => run(null) }, icon('play', 13), 'Run all');
    buttons.push(btn);
    if (this.info?.results && Array.isArray(this.info.results.scenarios)) showResults(this.info.results, 'Results from the last build');
    replace(this.scenarioHost,
      h('div.row-between', {}, h('span.dim', {}, `${list.length} scenario${list.length === 1 ? '' : 's'}`), btn),
      h('div.scenario-list', { dataset: { testid: 'scenario-list' } }, rows),
      results);
  }
}
