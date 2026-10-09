"""Split-flap display widget + "Amount" control of the Twin tab (Playwright + headless Chromium).

The twin is a fake session behind the real ``/ws/twin`` bridge: 8 ``splitflap`` devices and one
``ws_feed`` (contract: money-counter spec, "Toolkit additions" §2–3). An ``amount`` input makes the
fake twin show that amount on the flaps, so the whole path GUI → websocket → bridge → session →
state → widget is exercised without the device models. Screenshots go to ``build/_review/gui/``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from ..server.fixture_build import live_server, make_fixture_project, sf_spool_id, sf_window_id

pytestmark = [pytest.mark.gui, pytest.mark.timeout(600)]

READY = "window.piforge && window.piforge.ready === true"
N_DIGITS = 8
FEED = "FEED1"
POSITIONS = [3, 0, 7, 1, 5, 2, 6, 4]  # hello order ≠ display order: the widget sorts by `position`


def flap_id(pos: int) -> str:
    return f"FLAP{pos + 1}"


def digits_of(amount: float) -> list[int]:
    """1234.56 → [0,0,1,2,3,4,5,6] (what the firmware shows on 8 modules)."""
    return [int(c) for c in f"{amount:09.2f}".replace(".", "")]


def text_of(amount: float) -> str:
    s = f"{amount:09.2f}"
    return s.replace(".", ",")


class FlapSession:
    """Fake TwinSession: splitflap + ws_feed devices; ``amount`` → digits shown on the flaps."""

    def __init__(self) -> None:
        self.started = self.stopped = False
        self.inputs: list[tuple] = []
        self._q: list[dict] = []
        self._lock = threading.Lock()
        self.t = 0.0

    def _push(self, msg: dict) -> None:
        with self._lock:
            self._q.append(msg)

    def _flap_spec(self, pos: int) -> dict:
        return {"id": flap_id(pos), "type": "splitflap", "label": "Split-flap module", "inputs": {},
                "outputs": {"angle": {"type": "float", "unit": "deg"}, "digit": {"type": "int", "min": 0, "max": 9},
                            "next_digit": {"type": "int", "min": 0, "max": 9},
                            "flip": {"type": "float", "min": 0, "max": 1}, "hall": {"type": "bool"}},
                "pins": {"hall_pin": 4 + pos}, "bus": None,
                "params": {"position": pos, "flaps": 20, "stepper": f"M{pos + 1}"}}

    def start(self) -> None:
        self.started = True
        devices = [self._flap_spec(p) for p in POSITIONS]
        devices.append({"id": FEED, "type": "ws_feed", "label": "WebSocket feed",
                        "inputs": {"amount": {"type": "float", "min": 0, "max": 999999.99, "default": 0.0},
                                   "online": {"type": "bool", "default": True}},
                        "outputs": {"clients": {"type": "int"}, "port": {"type": "int"}},
                        "pins": {}, "bus": None, "params": {"port": 8765}})
        self._push({"op": "hello", "devices": devices})
        self.show(0.0, clients=1)

    def show(self, amount: float, clients: int = 1, flip: dict[int, tuple[int, float]] | None = None) -> None:
        """Push a state with `amount` on the flaps; `flip` = {pos: (next_digit, progress)}."""
        self.t += 0.1
        devs: dict = {}
        for pos, d in enumerate(digits_of(amount)):
            nxt, fl = (flip or {}).get(pos, ((d + 1) % 10, 0.0))
            devs[flap_id(pos)] = {"digit": d, "next_digit": nxt, "flip": fl, "angle": 18.0 * d, "hall": d == 0}
        devs[FEED] = {"amount": amount, "online": True, "clients": clients, "port": 8765}
        self._push({"op": "state", "t": self.t, "pins": {}, "devices": devs})

    @property
    def running(self) -> bool:
        return self.started and not self.stopped

    def send_input(self, device: str, prop: str, value: object) -> None:
        self.inputs.append((device, prop, value))
        if device == FEED and prop == "amount":
            self.show(float(value))  # type: ignore[arg-type]

    def stop(self, timeout: float = 2.0) -> None:
        if not self.stopped:
            self.stopped = True
            self._push({"op": "exit", "code": 0, "error": None, "reason": "stopped"})

    def poll(self) -> list[dict]:
        with self._lock:
            out, self._q = self._q, []
        return out


@pytest.fixture(scope="module")
def gui(tmp_path_factory: pytest.TempPathFactory, repo_root: Path) -> Iterator[SimpleNamespace]:
    sync_api = pytest.importorskip("playwright.sync_api")
    from piforge.server.app import create_app

    # the scene gets 8 split-flap windows (display_from → FLAP1..8) + spools driven by FLAPi.angle
    root = make_fixture_project(tmp_path_factory.mktemp("gui-flap") / "flap project ~ż", splitflaps=N_DIGITS)
    shots = repo_root / "build" / "_review" / "gui"
    shots.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    app = create_app(root)
    sessions: list[FlapSession] = []

    def factory() -> FlapSession:
        s = FlapSession()
        sessions.append(s)
        return s

    app.state.piforge.twin.session_factory = factory
    with live_server(app) as base, sync_api.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.set_default_timeout(90_000)  # the 8 GB host is often busy with CAD builds
            page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            page.goto(base + "/", timeout=120_000)
            page.wait_for_function(READY, timeout=120_000)
            page.click("[data-testid=tab-twin]")
            page.wait_for_selector("#tab-twin:not([hidden])")
            page.click("[data-testid=twin-start]")
            page.wait_for_function("(() => { const t = window.piforge.debug.twin(); return t.running && t.t !== null; })()",
                                   timeout=30_000)
            yield SimpleNamespace(page=page, base=base, errors=errors, shots=shots, sessions=sessions, root=root, app=app)
        finally:
            browser.close()


def _flap(page) -> dict:
    return page.evaluate("window.piforge.debug.splitflap()")


def _wait_text(page, text: str, timeout: float = 20_000) -> None:
    try:
        page.wait_for_function("(t) => { const s = window.piforge.debug.splitflap(); return s && s.text === t && !s.flipping; }",
                               arg=text, timeout=timeout)
    except Exception as exc:
        state = page.evaluate("[window.piforge.debug.splitflap(), window.piforge.debug.twin(),"
                              " document.querySelector('[data-testid=amount-input]').value]")
        raise AssertionError(f"split-flap never showed {text!r}: {state}") from exc


def _watch_flips(page) -> None:
    """Count every time a digit card gets the ``flipping`` class (flips can be quicker than polling)."""
    page.evaluate("""() => { window.__flipSeen = 0; window.__flipObs?.disconnect();
        window.__flipObs = new MutationObserver((ms) => { for (const m of ms)
          if (m.target.classList?.contains('flipping') && !(m.oldValue || '').includes('flipping')) window.__flipSeen++; });
        window.__flipObs.observe(document.querySelector('[data-testid=splitflap-row]'),
          {subtree: true, attributes: true, attributeFilter: ['class'], attributeOldValue: true}); }""")


def _session(gui) -> FlapSession:
    return gui.sessions[-1]


def test_widget_orders_digits_by_position_with_comma(gui):
    page = gui.page
    assert page.locator("[data-testid=splitflap-row] .sf-digit").count() == N_DIGITS
    ids = page.eval_on_selector_all("[data-testid=splitflap-row] .sf-digit", "els => els.map((e) => e.dataset.device)")
    assert ids == [flap_id(p) for p in range(N_DIGITS)]
    # exactly one comma, after the 6th digit (position 5)
    kids = page.eval_on_selector("[data-testid=splitflap-row]",
                                 "el => [...el.children].map((c) => c.classList.contains('sf-comma') ? ',' : 'd').join('')")
    assert kids == "dddddd,dd"
    _wait_text(page, "000000,00")
    # the Display section sits above the device cards
    order = page.evaluate("""() => { const d = document.querySelector('[data-testid=splitflap]');
        const g = document.querySelector('[data-testid=device-grid]');
        return !!(d.compareDocumentPosition(g) & Node.DOCUMENT_POSITION_FOLLOWING); }""")
    assert order is True
    assert gui.errors == []


def test_amount_input_sends_ws_message_and_flaps_follow(gui):
    page = gui.page
    sess = _session(gui)
    page.fill("[data-testid=amount-input]", "1234.56")
    _watch_flips(page)
    page.click("[data-testid=amount-send]")
    _wait_text(page, "001234,56")
    assert page.evaluate("window.__flipSeen") >= 1  # the .flipping animation class was toggled on…
    assert (FEED, "amount", 1234.56) in sess.inputs
    assert page.locator("[data-testid=splitflap-row] .sf-digit.flipping").count() == 0
    assert page.inner_text("[data-testid=amount-clients]") == "1"
    # digits are rendered in both halves of every card
    assert page.eval_on_selector_all("[data-testid=splitflap-row] .sf-digit",
                                     "els => els.map((e) => e.dataset.digit).join('')") == "00123456"
    # quick buttons add to the current value (2 decimals, no float noise)
    page.click("[data-testid=amount-add-1]")
    page.click("[data-testid=amount-add-0_01]")
    page.click("[data-testid=amount-add-1000]")
    page.click("[data-testid=amount-add-100]")
    _wait_text(page, "002335,57")
    amounts = [v for d, p, v in sess.inputs if d == FEED and p == "amount"]
    assert amounts[-4:] == [1235.56, 1235.57, 2235.57, 2335.57]
    page.click("[data-testid=amount-random]")
    deadline = time.monotonic() + 5
    while len([1 for d, p, _ in sess.inputs if p == "amount"]) < len(amounts) + 1 and time.monotonic() < deadline:
        time.sleep(0.05)
    rnd = [v for d, p, v in sess.inputs if d == FEED and p == "amount"][-1]
    assert 0 <= rnd <= 999999.99 and round(rnd, 2) == rnd
    _wait_text(page, text_of(rnd))
    # out-of-range values are refused client-side
    n = len(sess.inputs)
    page.fill("[data-testid=amount-input]", "1000000")
    page.click("[data-testid=amount-send]")
    assert page.inner_text("[data-testid=amount-error]") != ""
    time.sleep(0.2)
    assert len(sess.inputs) == n
    # online switch → ws_feed.online
    page.click("[data-testid=amount-FEED1] label.toggle")
    deadline = time.monotonic() + 5
    while (FEED, "online", False) not in sess.inputs and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (FEED, "online", False) in sess.inputs
    page.click("[data-testid=amount-FEED1] label.toggle")
    assert gui.errors == []


def test_flip_progress_starts_the_flip_and_rapid_changes_never_lag(gui):
    page = gui.page
    page.fill("[data-testid=amount-input]", "0")
    page.click("[data-testid=amount-send]")
    _wait_text(page, "000000,00")
    # the twin reports the last module's flap falling towards 1 → the widget flips now
    _watch_flips(page)
    flipping = page.evaluate("""() => { const t = window.piforge.panels.twin;
        t._onMessage({op: 'state', t: 50, pins: {}, devices: {FLAP8: {digit: 0, next_digit: 1, flip: 0.4}}});
        return document.querySelector('[data-testid=sf-digit-7]').classList.contains('flipping'); }""")
    assert flipping is True
    _wait_text(page, "000000,01")
    # 12 updates in 120 ms (every digit changes each time) → settles ≤ ~0.3 s after the last one
    elapsed = page.evaluate("""async () => {
        const t = window.piforge.panels.twin;
        const digits = (v) => v.toFixed(2).padStart(9, '0').replace('.', '').split('').map(Number);
        let last = '';
        for (let k = 1; k <= 12; k++) {
          const v = 111111.11 * (k % 9) + k * 0.37, devices = {};
          digits(v).forEach((d, i) => { devices[`FLAP${i + 1}`] = {digit: d, next_digit: (d + 1) % 10, flip: 0}; });
          t._onMessage({op: 'state', t: 60 + k, pins: {}, devices});
          last = v.toFixed(2).padStart(9, '0').replace('.', ',');
          await new Promise((r) => setTimeout(r, 10));
        }
        const tLast = performance.now(), row = t.display.row;
        while (performance.now() - tLast < 5000) {
          const s = window.piforge.debug.splitflap();
          if (s.text === last && !s.flipping) return Math.max(...row.digits.map((d) => d.settledAt)) - tLast;
          await new Promise((r) => setTimeout(r, 5));
        }
        return -1; }""")
    assert 0 <= elapsed < 600, elapsed  # MAX_LAG_MS = 300 (+ late timers on a loaded host)
    assert gui.errors == []


def test_full_speed_spin_plays_every_flap(gui):
    """A module at full speed (850 half-steps/s ≈ 4.15 flaps/s → a flap every 240 ms): the widget
    plays every flap (no skip-ahead), keeps the pace and settles right after the last one."""
    page = gui.page
    res = page.evaluate("""async () => {
        const t = window.piforge.panels.twin, row = t.display.row;
        const dg = row.digits[row.digits.length - 1];
        const send = (d, flip) => t._onMessage({op: 'state', t: 0, pins: {}, devices: {FLAP8: {digit: d,
            next_digit: (d + 1) % 10, flip}}});
        send(0, 0);
        await new Promise((r) => setTimeout(r, 400));
        const f0 = dg.flips, start = performance.now();
        let worst = 0;
        for (let k = 0; k < 15; k++) {                  // the twin: flap k starts to fall, then lands
          send(k % 10, 0.3);
          await new Promise((r) => setTimeout(r, 80));
          send((k + 1) % 10, 0);
          await new Promise((r) => setTimeout(r, 160));
          worst = Math.max(worst, ((k + 1) % 10 - dg.shown + 10) % 10);
        }
        const tLast = performance.now();
        while (performance.now() - tLast < 3000 && (dg.busy || dg.shown !== 5)) await new Promise((r) => setTimeout(r, 5));
        return {flips: dg.flips - f0, shown: dg.shown, settle: performance.now() - tLast, worst,
                total: tLast - start}; }""")
    assert res["shown"] == 5 and res["flips"] == 15, res        # every flap played, none skipped
    assert res["worst"] <= 2 and res["settle"] < 450, res       # never more than ~2 flaps behind
    assert gui.errors == []


def test_screenshots_mid_flip_and_settled(gui):
    page = gui.page
    page.fill("[data-testid=amount-input]", "1234.56")
    page.click("[data-testid=amount-send]")
    _wait_text(page, "001234,56")
    host = page.locator("[data-testid=splitflap]")
    host.screenshot(path=str(gui.shots / "splitflap_settled.png"))
    # slow motion, then freeze every flap: first while the old half falls, then while the new one lands
    page.evaluate("async () => { (await import('/static/js/splitflap.js')).FlapDigit.timeScale = 40; }")
    try:
        page.fill("[data-testid=amount-input]", "2345.67")
        page.click("[data-testid=amount-send]")
        page.wait_for_function("window.piforge.debug.splitflap().flipping >= 6", timeout=20_000)
        for name, frac in (("splitflap_midflip_fall.png", 0.46), ("splitflap_midflip_land.png", 0.62)):
            page.evaluate("""(frac) => { for (const a of document.getAnimations()) {
                const el = a.effect?.target; if (!el?.closest?.('.sf-digit')) continue;
                const dur = parseFloat(getComputedStyle(el.closest('.sf-digit')).getPropertyValue('--flip-ms')) || 200;
                a.pause(); a.currentTime = frac * dur; } }""", frac)
            page.wait_for_timeout(100)
            host.screenshot(path=str(gui.shots / name))
            assert page.locator("[data-testid=splitflap-row] .sf-digit.flipping").count() >= 6
        page.evaluate("() => { for (const a of document.getAnimations()) a.play(); }")
    finally:
        page.evaluate("async () => { (await import('/static/js/splitflap.js')).FlapDigit.timeScale = 1; }")
    _wait_text(page, "002345,67", timeout=30_000)
    page.screenshot(path=str(gui.shots / "splitflap_twin_tab.png"))
    for f in ("splitflap_settled.png", "splitflap_midflip_fall.png", "splitflap_midflip_land.png"):
        assert (gui.shots / f).stat().st_size > 3_000, f
    assert gui.errors == []


def test_fullscreen_display(gui):
    page = gui.page
    page.click("[data-testid=splitflap-fullscreen-btn]")
    page.wait_for_selector("[data-testid=splitflap-fullscreen]")
    big = page.locator("[data-testid=splitflap-row-big]")
    assert big.get_attribute("data-value") == "002345,67"
    assert page.locator("[data-testid=splitflap-row-big] .sf-digit").count() == N_DIGITS
    width = page.evaluate("document.querySelector('[data-testid=splitflap-row-big] .sf-card').getBoundingClientRect().width")
    assert width > 90  # large digits for demos
    # the fullscreen row follows the twin too
    page.evaluate("""() => { const t = window.piforge.panels.twin;
        t._onMessage({op: 'state', t: 99, pins: {}, devices: {FLAP8: {digit: 8, next_digit: 9, flip: 0}}}); }""")
    page.wait_for_function("document.querySelector('[data-testid=splitflap-row-big]').dataset.value === '002345,68'")
    page.wait_for_function("!document.querySelector('[data-testid=splitflap-row-big] .sf-digit.flipping')", timeout=20_000)
    page.screenshot(path=str(gui.shots / "splitflap_fullscreen.png"))
    page.keyboard.press("Escape")
    page.wait_for_selector("[data-testid=splitflap-fullscreen]", state="detached", timeout=5_000)
    assert gui.errors == []


def test_driven_joint_follows_continuous_rotation(gui):
    """A spool joint driven by `splitflap.angle` (grows past 360°) wraps without jumps."""
    res = gui.page.evaluate("""async () => {
        const { drivenJointValue, jointMatrix } = await import('/static/js/scene.js');
        const out = {};
        for (const [name, j] of Object.entries({
            full: {type: 'revolute', min: -180, max: 180, driven_by: {scale: 1, offset: 0}},
            open: {type: 'revolute', min: null, max: null, driven_by: {scale: 1, offset: 0}} })) {
          let prev = null, worst = 0;
          const m = (v) => jointMatrix({...j, axis: [1, 0, 0], value: v}).elements;
          for (let a = 0; a <= 5 * 360; a += 7) {
            const v = drivenJointValue(j, a);
            const ref = m(a), got = m(v);
            worst = Math.max(worst, ...ref.map((x, i) => Math.abs(x - got[i])));
            if (prev !== null) { const d = ((v - prev) % 360 + 360) % 360; if (Math.abs(d - 7) > 1e-6) worst = 99; }
            prev = v;
          }
          out[name] = {worst, big: drivenJointValue(j, 1e7 + 30)};
        }
        return out; }""")
    for name in ("full", "open"):
        assert res[name]["worst"] < 1e-9, name
        assert -180 <= res[name]["big"] <= 180, name


# ------------------------------------------------------------------------------- 3D split-flap faces
def _faces(page) -> list[dict]:
    return page.evaluate("window.__piforge.splitflapFaces()")


def _wait_faces(page, digits: list[int], timeout: float = 20_000) -> None:
    try:
        page.wait_for_function("""(want) => { const f = window.__piforge.splitflapFaces();
            return f.length >= want.length && want.every((d, i) => f[i].shown === d && f[i].top === d
              && f[i].bottom === d && !f[i].busy); }""", arg=digits, timeout=timeout)
    except Exception as exc:
        raise AssertionError(f"3D faces never showed {digits}: {_faces(page)}") from exc


def _frame_row(page, ids: list[str], tilt: tuple[float, float, float] = (0.32, -1.0, 0.3)) -> None:
    """Camera in front of the split-flap windows, a little from the right and above."""
    page.evaluate("""([ids, tilt]) => { const { viewer, scene } = window.piforge;
        const box = scene.boxOf(ids), c = box.getCenter(box.min.clone()), r = box.getSize(box.min.clone()).length() / 2;
        const d = box.min.clone().set(...tilt).normalize().multiplyScalar(r * 2.7);
        viewer.animateTo(c.clone().add(d), c, 0); }""", [ids, list(tilt)])
    page.wait_for_timeout(250)


def test_3d_faces_exist_for_display_from_nodes(gui):
    page = gui.page
    faces = _faces(page)
    assert [f["node"] for f in faces] == [sf_window_id(i) for i in range(N_DIGITS)]
    assert [f["device"] for f in faces] == [flap_id(i) for i in range(N_DIGITS)]
    for f in faces:  # the face covers the window: 40 mm wide, 60 mm high (thin axis = −Y normal)
        assert abs(f["w"] - 40) < 0.01 and abs(f["h"] - 60) < 0.01, f
    # the window's own mesh is hidden (the face replaces it); the face group lives in the node
    res = page.evaluate("""(id) => { const n = window.piforge.scene.nodes.get(id);
        return { meshVisible: n.meshes.some((m) => m.visible), face: !!n.content.getObjectByName(id + ':splitflap') }; }""",
                        sf_window_id(0))
    assert res == {"meshVisible": False, "face": True}
    assert page.evaluate("window.piforge.scene.list().filter((r) => r.display).length") == N_DIGITS
    assert gui.errors == []


def test_3d_faces_and_spools_follow_twin_state(gui):
    page = gui.page
    page.fill("[data-testid=amount-input]", "0")
    page.click("[data-testid=amount-send]")
    _wait_faces(page, digits_of(0))
    before = [f["flips"] for f in _faces(page)]
    page.fill("[data-testid=amount-input]", "1234.56")
    page.click("[data-testid=amount-send]")
    _wait_text(page, "001234,56")
    _wait_faces(page, digits_of(1234.56))
    after = [f["flips"] for f in _faces(page)]
    for i, d in enumerate(digits_of(1234.56)):
        assert after[i] > before[i] if d else after[i] == before[i], (i, before, after)  # flipped, not jumped
    # the spool joints follow splitflap.angle (18° per digit in the fake twin)
    for i, d in enumerate(digits_of(1234.56)):
        jv = page.evaluate("(id) => window.piforge.debug.node(id).jointValue", sf_spool_id(i))
        assert abs(jv - 18.0 * d) < 1e-6, (i, jv)
    # a twin flap that starts to fall starts the 3D flip at once (same rule as the 2D widget)
    busy = page.evaluate("""() => { window.piforge.panels.twin._onMessage({op: 'state', t: 70, pins: {},
        devices: {FLAP8: {digit: 6, next_digit: 7, flip: 0.3}}});
        return window.__piforge.splitflapFaces()[7].busy; }""")
    assert busy is True
    _wait_faces(page, digits_of(1234.57))
    assert gui.errors == []


def test_3d_faces_rapid_updates_never_lag(gui):
    page = gui.page
    elapsed = page.evaluate("""async () => {
        const t = window.piforge.panels.twin;
        const digits = (v) => v.toFixed(2).padStart(9, '0').replace('.', '').split('').map(Number);
        let want = [];
        for (let k = 1; k <= 12; k++) {
          const v = 111111.11 * (k % 9) + k * 0.37, devices = {};
          want = digits(v);
          want.forEach((d, i) => { devices[`FLAP${i + 1}`] = {digit: d, next_digit: (d + 1) % 10, flip: 0}; });
          t._onMessage({op: 'state', t: 80 + k, pins: {}, devices});
          await new Promise((r) => setTimeout(r, 10));
        }
        const t0 = performance.now();
        while (performance.now() - t0 < 5000) {
          const f = window.__piforge.splitflapFaces();
          if (want.every((d, i) => f[i].shown === d && !f[i].busy)) return performance.now() - t0;
          await new Promise((r) => setTimeout(r, 5));
        }
        return -1; }""")
    assert 0 <= elapsed < 600, elapsed  # MAX_LAG_MS = 300 (+ late timers on a loaded host)
    assert gui.errors == []


def test_3d_screenshots_mid_flip_and_settled(gui):
    page = gui.page
    ids = [sf_window_id(i) for i in range(N_DIGITS)]
    page.fill("[data-testid=amount-input]", "1234.56")
    page.click("[data-testid=amount-send]")
    _wait_faces(page, digits_of(1234.56))
    _frame_row(page, ids, (0.0, -1.0, 0.0))
    page.locator("#viewport").screenshot(path=str(gui.shots / "sf3d_settled_front.png"))
    _frame_row(page, ids)
    page.locator("#viewport").screenshot(path=str(gui.shots / "sf3d_settled.png"))
    page.evaluate("async () => { (await import('/static/js/splitflap.js')).FlapDigit.timeScale = 60; }")
    try:
        page.fill("[data-testid=amount-input]", "2345.67")
        page.click("[data-testid=amount-send]")
        page.wait_for_function("window.__piforge.splitflapFaces().filter((f) => f.busy).length >= 6", timeout=20_000)
        for name, frac, tilt in (("sf3d_midflip_fall.png", 0.3, (0.32, -1.0, 0.3)),
                                 ("sf3d_midflip_half.png", 0.5, (0.55, -1.0, 0.1)),
                                 ("sf3d_midflip_land.png", 0.72, (0.32, -1.0, 0.3))):
            page.evaluate("async (p) => { (await import('/static/js/splitflap3d.js')).SplitFlapFaces.freeze = p; }", frac)
            _frame_row(page, ids, tilt)
            angles = [f["angle"] for f in _faces(page) if f["busy"]]
            assert len(angles) >= 6 and all(0 < a < 180 for a in angles), angles
            page.locator("#viewport").screenshot(path=str(gui.shots / name))
    finally:
        page.evaluate("""async () => { (await import('/static/js/splitflap3d.js')).SplitFlapFaces.freeze = null;
            (await import('/static/js/splitflap.js')).FlapDigit.timeScale = 1; }""")
    _wait_faces(page, digits_of(2345.67), timeout=60_000)
    # section plane and explode keep the faces attached to their windows
    moved = page.evaluate("""(id) => { const s = window.piforge.scene, n = s.nodes.get(id);
        s.setExplode(1);
        const g = n.content.getObjectByName(id + ':splitflap'); window.piforge.viewer.scene.updateMatrixWorld(true);
        const out = {y: g.matrixWorld.elements[13], explode: s.explode, offset: n.explodeOffset?.toArray() ?? null,
          frame: n.frame.matrix.elements[13]};
        s.setExplode(0);
        return out; }""", sf_window_id(0))
    assert moved["y"] < -70 - 15, str(moved)  # exploded 20 mm towards −Y with the window
    clip = page.evaluate("""() => { window.piforge.viewer.setSection({enabled: true, axis: 'x', value: 0});
        const n = window.piforge.scene.flaps.materials()[1].clippingPlanes?.length || 0;
        window.piforge.viewer.setSection({enabled: false}); return n; }""")
    assert clip == 1
    for f in ("sf3d_settled.png", "sf3d_midflip_fall.png", "sf3d_midflip_half.png", "sf3d_midflip_land.png"):
        assert (gui.shots / f).stat().st_size > 5_000, f
    assert gui.errors == []


REAL_FW = """\
import json, os, threading, time
import spidev
from websockets.sync.client import connect

HALF = [0b0001, 0b0011, 0b0010, 0b0110, 0b0100, 0b1100, 0b1000, 0b1001]
N, SPR, FLAPS = 3, 4096, 20
spi = spidev.SpiDev()
spi.open(0, 0)
spi.max_speed_hz = 1_000_000
phase, steps, target = [0] * N, [0] * N, [0] * N
lock = threading.Lock()


def write(active):
    word = 0
    for i in active:
        word |= HALF[phase[i]] << (4 * i)
    spi.xfer2(list(word.to_bytes(2, "big")))


def listen():
    while True:
        try:
            with connect(os.environ["MONEY_COUNTER_URL"], open_timeout=1) as ws:
                for msg in ws:
                    cents = int(round(float(json.loads(msg)["amount"]) * 100)) % 1000
                    with lock:
                        target[:] = [cents // 100, cents // 10 % 10, cents % 10]
                    print("TARGET", target, flush=True)
        except Exception:
            time.sleep(0.2)


write(range(N))                                # energise phase 0: the steppers' reference
threading.Thread(target=listen, daemon=True).start()
idle = False
while True:
    with lock:
        moving = [i for i in range(N) if (steps[i] * FLAPS // SPR) % 10 != target[i]]
    if not moving:
        if not idle:
            write([])
            print("SHOW", target, flush=True)
        idle = True
        time.sleep(0.01)
        continue
    idle = False
    for i in moving:
        phase[i] = (phase[i] + 1) % 8
        steps[i] += 1
    write(moving)
    time.sleep(0.0012)
"""


def test_real_twin_devices_drive_the_widget(gui):
    """With the real `splitflap`/`ws_feed` device models (when installed): Amount → ws_feed → firmware
    → 74HC595 → steppers → splitflap digits → widget."""
    import importlib

    devices = importlib.import_module("piforge.twin.devices")
    if not {"splitflap", "ws_feed", "shift_register_74hc595"} <= set(getattr(devices, "DEVICE_TYPES", {})):
        pytest.skip("splitflap/ws_feed twin devices not available yet")
    pytest.importorskip("websockets.sync.client")
    from piforge.twin.config import DeviceConfig, TwinConfig

    cfg = TwinConfig(board="rpizero2w", devices=[
        DeviceConfig("SR1", "shift_register_74hc595", bus={"kind": "spi", "bus": 0, "cs": 0}, params={"length": 2}),
        *[DeviceConfig(f"M{i + 1}", "stepper_28byj48",
                       params={"coil_source": {"device": "SR1", "bits": [4 * i + k for k in range(4)]}})
          for i in range(3)],
        *[DeviceConfig(f"F{i + 1}", "splitflap", params={"stepper": f"M{i + 1}", "position": i, "start_angle": 0.0})
          for i in (2, 0, 1)],
        DeviceConfig(FEED, "ws_feed", params={"port": 0})])
    build = gui.root / "build"
    cfg_path, fw_path = build / "twin" / "config.json", gui.root / "firmware" / "main.py"
    old_cfg, old_fw = cfg_path.read_text(encoding="utf-8"), fw_path.read_text(encoding="utf-8")
    bridge = gui.app.state.piforge.twin
    fake_factory = bridge.session_factory
    page = gui.page
    try:
        cfg_path.write_text(cfg.to_json(), encoding="utf-8")
        fw_path.write_text(REAL_FW, encoding="utf-8")
        bridge.session_factory = bridge.default_factory
        page.click("[data-testid=twin-stop]")
        page.wait_for_function("!window.piforge.debug.twin().running", timeout=15_000)
        page.evaluate("async () => { const t = window.piforge.panels.twin; t.invalidate(); await t.ensure('real'); }")
        assert page.locator("[data-testid=splitflap-row] .sf-digit").count() == 3
        page.click("[data-testid=twin-start]")
        page.wait_for_function("(() => { const t = window.piforge.debug.twin(); return t.running && t.t !== null; })()",
                               timeout=60_000)
        page.wait_for_function("document.querySelector('[data-testid=amount-clients]').textContent === '1'",
                               timeout=30_000)  # the firmware connected to the twin's WebSocket server
        _wait_text(page, "0,00", timeout=20_000)
        _watch_flips(page)
        page.fill("[data-testid=amount-input]", "1.23")
        page.click("[data-testid=amount-send]")
        page.wait_for_function("document.querySelector('[data-testid=twin-console]').textContent.includes('SHOW [1, 2, 3]')",
                               timeout=30_000)
        _wait_text(page, "1,23", timeout=10_000)
        assert page.evaluate("window.__flipSeen") >= 6  # 1 + 2 + 3 flaps, each animated
        page.screenshot(path=str(gui.shots / "splitflap_real_twin.png"))
        page.click("[data-testid=twin-stop]")
        page.wait_for_function("!window.piforge.debug.twin().running", timeout=15_000)
    finally:
        cfg_path.write_text(old_cfg, encoding="utf-8")
        fw_path.write_text(old_fw, encoding="utf-8")
        bridge.session_factory = fake_factory
    assert gui.errors == []


def test_scenario_run_from_gui_is_watchable(gui):
    """Run a scenario from the Twin tab: its live state reaches the widget and the 3D faces while it
    runs (real splitflap/ws_feed devices + REAL_FW), the row shows RUNNING → PASS."""
    import importlib
    import json

    devices = importlib.import_module("piforge.twin.devices")
    if not {"splitflap", "ws_feed", "shift_register_74hc595"} <= set(getattr(devices, "DEVICE_TYPES", {})):
        pytest.skip("splitflap/ws_feed twin devices not available yet")
    pytest.importorskip("websockets.sync.client")
    from piforge.twin.config import DeviceConfig, TwinConfig

    # FLAP1..3 = the scene's first three windows (display_from) and spools (driven_by FLAPi.angle)
    cfg = TwinConfig(board="rpizero2w", devices=[
        DeviceConfig("SR1", "shift_register_74hc595", bus={"kind": "spi", "bus": 0, "cs": 0}, params={"length": 2}),
        *[DeviceConfig(f"M{i + 1}", "stepper_28byj48",
                       params={"coil_source": {"device": "SR1", "bits": [4 * i + k for k in range(4)]}})
          for i in range(3)],
        *[DeviceConfig(flap_id(i), "splitflap", params={"stepper": f"M{i + 1}", "position": i, "start_angle": 0.0})
          for i in range(3)],
        DeviceConfig(FEED, "ws_feed", params={"port": 0})])
    scenario = {"name": "show_1_23", "duration": 7.0, "settle": 4.5, "steps": [
        {"at": 1.5, "action": "input", "device": FEED, "prop": "amount", "value": 1.23},
        {"at": 2.0, "action": "expect", "device": flap_id(2), "prop": "digit", "value": 3, "op": "=="},
        {"at": 2.0, "action": "expect_log", "pattern": r"SHOW \[1, 2, 3\]"}]}
    build = gui.root / "build"
    cfg_path, fw_path, sc_path = build / "twin" / "config.json", gui.root / "firmware" / "main.py", build / "twin" / "scenarios.json"
    old = {p: p.read_text(encoding="utf-8") for p in (cfg_path, fw_path, sc_path)}
    page = gui.page
    try:
        cfg_path.write_text(cfg.to_json(), encoding="utf-8")
        fw_path.write_text(REAL_FW, encoding="utf-8")
        sc_path.write_text(json.dumps([scenario]), encoding="utf-8")
        page.evaluate("async () => { const t = window.piforge.panels.twin; t.invalidate(); await t.ensure('scenario'); }")
        page.wait_for_selector("[data-testid=scenario-run-show_1_23]")
        flips0 = sum(f["flips"] for f in _faces(page)[:3])
        page.evaluate("""() => { window.__sfTexts = []; window.__faceDigits = [];
            window.__sfPoll = setInterval(() => {
              const s = window.piforge.debug.splitflap(); if (s) window.__sfTexts.push(s.text);
              window.__faceDigits.push(window.__piforge.splitflapFaces().slice(0, 3).map((f) => f.shown).join('')); }, 20); }""")
        page.click("[data-testid=scenario-run-show_1_23]")
        page.wait_for_function("document.querySelector('[data-testid=scenario-status-show_1_23]').textContent === 'RUNNING'",
                               timeout=30_000)
        page.wait_for_function("window.piforge.debug.splitflap()?.text === '1,23'", timeout=60_000)
        page.wait_for_function("window.__piforge.splitflapFaces().slice(0, 3).every((f, i) => f.shown === i + 1 && !f.busy)",
                               timeout=20_000)
        _frame_row(page, [sf_window_id(i) for i in range(3)])
        page.screenshot(path=str(gui.shots / "sf3d_scenario_running.png"))
        page.wait_for_function("document.querySelector('[data-testid=scenario-status-show_1_23]').textContent === 'PASS'",
                               timeout=60_000)
        texts = page.evaluate("clearInterval(window.__sfPoll), [...new Set(window.__sfTexts)]")
        face_seq = page.evaluate("[...new Set(window.__faceDigits)]")
        assert "1,23" in texts and len(texts) >= 3, texts  # it counted up while the scenario ran
        assert face_seq[-1] == "123" and len(face_seq) >= 3, face_seq
        assert sum(f["flips"] for f in _faces(page)[:3]) - flips0 >= 6  # 1 + 2 + 3 animated flips
        jv = page.evaluate("(id) => window.piforge.debug.node(id).jointValue", sf_spool_id(2))
        assert jv != 0  # the spool turned with the stepper
        console = page.inner_text("[data-testid=twin-console]")
        assert "scenario show_1_23" in console and "PASS" in console
        assert "[show_1_23] SHOW [1, 2, 3]" in console
        assert page.locator("[data-testid=scenario-results] .error-box").count() == 0
        assert not page.evaluate("window.piforge.debug.twin().running")
    finally:
        for p, text in old.items():
            p.write_text(text, encoding="utf-8")
    assert gui.errors == []
