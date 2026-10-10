from __future__ import annotations

import math

import pytest
from conftest import FakeRig, make_motion, run_until

from moneycounter.app import Controller
from moneycounter.config import Config


def digits_on(rig: FakeRig) -> list[int]:
    out = []
    for i in range(rig.n):
        f = rig.flap_shown(i)
        assert abs(f - round(f)) < 0.02, f"module {i} stopped between flaps ({f:.3f})"
        out.append(round(f) % 20 % 10)
    return out


class SimClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def homed(rig, **kw):
    clock = SimClock()
    logs: list[str] = []
    m = make_motion(rig, clock=clock, log=logs.append, **kw)
    m.start_homing()
    t = run_until(m, m.settled)
    return m, clock, logs, t


# -- ramp math --------------------------------------------------------------------------------------
def test_trapezoid_profile_starts_slow_cruises_and_stops_exactly():
    m = make_motion(FakeRig())
    v, speeds, remaining = 0.0, [], 2000
    while remaining > 0:
        v = m.next_speed(v, remaining, 850.0)
        speeds.append(v)
        remaining -= 1
    assert speeds[0] == 450.0 and max(speeds) == 850.0
    assert speeds[-1] == pytest.approx(450.0, abs=25)              # back at the start speed on the last step
    up = speeds[:speeds.index(850.0) + 1]
    assert all(b >= a for a, b in zip(up, up[1:]))
    # acceleration per distance never exceeds accel: v² grows by ≤ 2a per step
    assert all(b * b - a * a <= 2 * 3000 + 1e-6 for a, b in zip(speeds, speeds[1:]))
    assert all(a * a - b * b <= 2 * 3000 + 1e-6 for a, b in zip(speeds, speeds[1:]))
    t_sim = sum(1.0 / s for s in speeds)
    assert m.travel_time(2000) == pytest.approx(t_sim, rel=0.03)
    assert m.stop_distance(850) == math.ceil((850 ** 2 - 450 ** 2) / 6000) and m.stop_distance(400) == 0
    # a short move never reaches top speed but still ends slow
    v, speeds = 0.0, []
    for r in range(60, 0, -1):
        v = m.next_speed(v, r, 850.0)
        speeds.append(v)
    assert max(speeds) < 850 and speeds[-1] < 520
    assert m.flaps_within(m.flap_time(6) + 1e-6) == 6 and m.flaps_within(0) == 0
    assert m.capacity == pytest.approx(850 / 204.8)


def test_a_delayed_coil_write_reanchors_the_next_step(rig):
    """The coil word left 3 ms late (bus lock / scheduler): the step happened at the write, so the next one
    is a (slower) period after the write — never right behind it (pairs of such steps made rotors slip)."""
    m, clock, logs, t = homed(rig)
    m.set_desired(7, 9)
    t = run_until(m, lambda: m.modules[7].v >= 850, t0=t)
    mod = m.modules[7]
    t = mod.next_t
    write = m.bus.write

    def slow_write(word):
        write(word)
        clock.t += 0.003                                            # the write blocked for 3 ms

    m.bus = type("Bus", (), {"write": staticmethod(slow_write)})()
    clock.t = t
    m.tick(t)
    done = t + 0.003
    assert mod.v == pytest.approx(850 - 8 * 3000 * 0.003)           # it slowed down (rotor waited at the field)
    assert mod.next_t == pytest.approx(done + 1 / mod.v)            # a full period after the write
    m.bus = type("Bus", (), {"write": staticmethod(write)})()
    run_until(m, m.settled, t0=clock.t)
    assert digits_on(rig)[7] == 9


def test_real_time_loop_cruises_at_max_pps_despite_overhead(rig):
    """Run the real stepping thread (real clock): each coil write costs 0.25 ms (SPI + jitter). The
    wake-up grid is absolute, so overshoot and run time do not add up per step — the module cruises at
    ≈ max_pps (it used to settle near 1 / (tick + overhead) ≈ 80 %), and nothing is lost."""
    import threading
    import time as _time

    m, clock, logs, t = homed(rig)
    write = rig.write

    def slow_write(word):
        write(word)
        end = _time.perf_counter() + 0.00025
        while _time.perf_counter() < end:
            pass

    m.bus = type("Bus", (), {"write": staticmethod(slow_write)})()
    m.clock = _time.monotonic
    for mod in m.modules:
        mod.next_t, mod.v, mod.idle_since = None, 0.0, _time.monotonic()
    with m.lock:
        m.modules[0].goal = 60                                      # six turns: a long cruise
    stop = threading.Event()
    th = threading.Thread(target=m.run, args=(stop,), daemon=True)
    th.start()
    try:
        _time.sleep(0.3)                                            # ramp done (≈ 0.1 s)
        rates = []
        for _ in range(5):                                          # 5 windows: a host hiccup re-ramps one
            s0, t0 = m.modules[0].steps, _time.monotonic()
            _time.sleep(0.3)
            rates.append((m.modules[0].steps - s0) / (_time.monotonic() - t0))
    finally:
        stop.set()
        th.join(2)
    assert max(rates) > 0.93 * 850, rates
    assert max(rates) < 1.03 * 850, rates                           # never faster than the top speed


# -- moves ------------------------------------------------------------------------------------------------
def test_homing_from_anywhere_shows_zero(rig):
    m, *_ = homed(rig)
    assert digits_on(rig) == [0] * 8 and m.shown_digits() == [0] * 8
    assert rig.backwards == 0


def test_homing_with_offsets_and_timeout():
    rig = FakeRig(start=(10, 50, 0, 3000, 0, 0, 0, 0), offsets=(0, 37, 200, 5, 0, 0, 0, 4000))
    homed(rig)
    assert digits_on(rig) == [0] * 8
    # a module whose sensor never fires gives up after home_timeout_s
    dead = FakeRig(start=(0,) * 8, window=0)
    m2, clock, logs, _ = homed(dead, home_timeout_s=1.0)
    assert m2.homed() and all(s.home == "failed" for s in m2.modules)


def test_all_modules_move_together_one_coil_word_per_tick(rig):
    m, clock, logs, t = homed(rig)
    target = [0, 0, 1, 2, 3, 4, 5, 6]
    for i, d in enumerate(target):
        m.set_desired(i, d)
    w0 = rig.writes
    t1 = run_until(m, m.settled, t0=t)
    assert digits_on(rig) == target and m.shown_digits() == target
    assert t1 - t == pytest.approx(m.flap_time(6), abs=0.02)        # 6 flaps, ramped: ≈ 1.5 s
    longest, total = 6 * 204.8, 21 * 204.8                           # 6 flaps; 1+2+…+6 flaps over all modules
    assert longest <= rig.writes - w0 < total / 2                    # one coil word steps every due module
    t2 = run_until(m, lambda: rig.word == 0, t0=t1)                  # hold time passes → coils off
    assert t2 - t1 <= 0.09                                          # hold_ms = 60 (+ 20 ms polling)
    for i, d in enumerate([0, 0, 1, 2, 3, 4, 5, 5]):                # 6 → 5 goes forward: 9 flaps
        m.set_desired(i, d)
    run_until(m, m.settled, t0=t2)
    assert digits_on(rig)[-1] == 5 and rig.backwards == 0


def test_late_steps_restart_the_ramp(rig):
    m, clock, logs, t = homed(rig)
    m.set_desired(7, 9)
    t = run_until(m, lambda: m.modules[7].v >= 850, t0=t)
    t += 0.05                                                        # the loop stalls for 50 ms
    m.tick(t)
    assert m.modules[7].v == 450.0                                   # … the module starts slowly again
    run_until(m, m.settled, t0=t)
    assert digits_on(rig)[7] == 9


def test_real_gear_ratio_is_resynced_by_the_hall_edge():
    """Firmware assumes 4096 half-steps/turn, the motor needs 4076: the Hall edge every turn fixes it."""
    rig = FakeRig(true_spr=4076, start=(4000,) * 8)
    m, clock, logs, t = homed(rig)
    shown = []
    for rnd in range(25):                                            # 25 × 7 flaps ≈ 9 turns
        d = (7 * (rnd + 1)) % 10
        for i in range(8):
            m.set_desired(i, d)
        t = run_until(m, m.settled, t0=t)
        shown.append(rig.flap_shown(0))
        assert m.shown_digits()[0] == d
    # true position error stays below a quarter flap (no accumulation), silently (within tolerance)
    errs = [abs(f - round(f)) for f in shown]
    assert max(errs) < 0.25 and sum(s.resyncs for s in m.modules) >= 8 * 8
    assert not [line for line in logs if "RESYNC" in line or "VERIFY" in line]


def test_spin_keeps_turning_and_lands_when_the_count_ends(rig):
    m, clock, logs, t = homed(rig)
    t_end = t + 6.0                                                  # count ends in 6 s, final digit 3
    p0 = rig.pos[7]
    k = [0]

    def frame(now):
        m.set_desired(7, k[0] % 10, spin=True, final=3, time_left=max(0.0, t_end - now))
        k[0] += 1

    t = run_until(m, lambda: m.settled() and clock.t > t_end, t0=t, every=0.02, on_every=frame)
    assert digits_on(rig)[7] == 3
    assert rig.pos[7] - p0 > 20 * 204.8                             # 3 + 20 flaps: extra turns at full speed
    assert abs(t - t_end) < 0.5                                      # … and landed with the count


# -- closed loop -----------------------------------------------------------------------------------------
def _move(m, rig, t, digits):
    for i, d in enumerate(digits):
        m.set_desired(i, d)
    return run_until(m, m.settled, t0=t)


def test_lost_steps_are_found_at_the_hall_edge_and_corrected(rig):
    m, clock, logs, t = homed(rig)
    t = _move(m, rig, t, [0, 0, 0, 0, 0, 0, 0, 6])
    rig.knock(7, 40)                                                 # disturbance: 40 half-steps lost
    t = _move(m, rig, t, [0, 0, 0, 0, 0, 0, 0, 5])                   # 9 flaps → flap 15
    assert abs(rig.flap_shown(7) - 15) > 0.15                        # not checked yet: off by 40
    t = _move(m, rig, t, [0, 0, 0, 0, 0, 0, 0, 4])                   # passes the magnet → checked
    assert "RESYNC m7 err=+40" in logs
    assert digits_on(rig)[7] == 4 and abs(rig.flap_shown(7) - 4) < 0.02
    assert m.modules[7].lost == 40 and m.modules[7].cap == 850       # a single loss: no derate
    assert not [line for line in logs if line.startswith(("VERIFY", "DERATE"))]


def test_large_error_rehomes_the_module_then_continues(rig):
    m, clock, logs, t = homed(rig)
    t = _move(m, rig, t, [0, 0, 0, 0, 0, 0, 0, 6])
    rig.knock(7, 700)                                                # more than 1/10 turn
    t = _move(m, rig, t, [0, 0, 0, 0, 0, 0, 0, 5])
    t = _move(m, rig, t, [0, 0, 0, 0, 0, 0, 0, 4])
    # 700 lost > 1/10 turn: the count passes the magnet's place without seeing it → re-home
    assert any(line.startswith("RESYNC m7 missed Hall edge") and line.endswith("-> re-home") for line in logs)
    assert "HOME module 7: re-homed" in logs
    assert any(line.startswith("VERIFY m7") for line in logs)        # re-homed → checked once more
    assert digits_on(rig)[7] == 4 and m.modules[7].verified


def test_missed_hall_edge_rehomes_derates_and_verifies(rig):
    m, clock, logs, t = homed(rig)
    rig.frozen.add(3)                                                # rotor 3 stalls completely
    for i in range(8):
        m.set_desired(i, 9)
    t = run_until(m, lambda: any("missed Hall edge" in line for line in logs), t0=t,
                  every=0.02, on_every=lambda now: m.set_desired(3, 9, spin=True, final=9, time_left=8.0))
    rig.frozen.discard(3)                                            # it frees itself again
    t = run_until(m, m.settled, t0=t)
    assert any(line.startswith("RESYNC m3 missed Hall edge") for line in logs)
    assert any(line.startswith("DERATE m3") for line in logs) and m.modules[3].cap < 850
    assert "HOME module 3: re-homed" in logs and any(line.startswith("VERIFY m3") for line in logs)
    assert digits_on(rig) == [9] * 8 and m.settled()


def test_dead_sensor_never_spins_forever():
    rig = FakeRig(start=(4000,) * 8)
    m, clock, logs, t = homed(rig)
    rig.window = 0                                                   # the magnet / sensor stops working
    for i in range(8):
        m.set_desired(i, 9)
    for d in (9, 8, 7):                                              # 27 flaps: the magnet should have come
        for i in range(8):
            m.set_desired(i, d)
        t = run_until(m, m.settled, t0=t)
    assert all(m.modules[i].home == "failed" for i in range(8))
    assert sum("FAILED" in line for line in logs) == 8 and m.settled()
    assert m.shown_digits() == [7] * 8 and digits_on(rig) == [7] * 8


# -- controller -------------------------------------------------------------------------------------------
def run_sim(ctrl: Controller, motion, clock: SimClock, seconds: float, until=None):
    end = clock.t + seconds
    try:
        run_until(motion, lambda: (until is not None and until()) or clock.t >= end, t0=clock.t,
                  every=0.02, on_every=lambda now: ctrl.update())
    except AssertionError:
        return False
    return until is not None and until()


def test_controller_counts_up_and_logs_show():
    rig = FakeRig(start=(4000, 3900, 100, 0, 2048, 4095, 3000, 1234))
    clock, logs = SimClock(), []
    m = make_motion(rig, clock=clock)
    ctrl = Controller(Config(), m, clock=clock, log=logs.append)
    m.start_homing()
    assert run_sim(ctrl, m, clock, 15, until=lambda: "SHOW 000000,00" in logs)
    assert ctrl.handle('{"amount": 1234.56}')["ok"]
    seen = set()
    t0 = clock.t

    def watch():
        seen.add(tuple(digits_on_soft(rig)))
        return "SHOW 001234,56" in logs

    assert run_sim(ctrl, m, clock, 20, until=watch)
    assert digits_on(rig) == [0, 0, 1, 2, 3, 4, 5, 6]
    assert clock.t - t0 < 2.2                                        # was 5.2 s at a constant 600 half-steps/s
    assert len(seen) > 12                                            # visibly counted
    # quick last-digit change
    t0 = clock.t
    ctrl.handle("1234.57")
    assert run_sim(ctrl, m, clock, 5, until=lambda: "SHOW 001234,57" in logs)
    assert clock.t - t0 < 0.6
    # overflow clamps, invalid is ignored, a decrease turns forward
    t0 = clock.t
    assert ctrl.handle("2000000")["clamped"]
    assert any(line.startswith("CLAMP") for line in logs)
    assert run_sim(ctrl, m, clock, 30, until=lambda: "SHOW 999999,99" in logs)
    assert clock.t - t0 < 3.0
    assert not ctrl.handle("{bad")["ok"] and any(line.startswith("IGNORED") for line in logs)
    ctrl.handle("12.5")
    assert run_sim(ctrl, m, clock, 10, until=lambda: "SHOW 000012,50" in logs)
    assert rig.backwards == 0 and digits_on(rig) == [0, 0, 0, 0, 1, 2, 5, 0]


def test_rapid_updates_end_exact():
    rig = FakeRig(start=(4000, 3900, 100, 0, 2048, 4095, 3000, 1234))
    clock, logs = SimClock(), []
    m = make_motion(rig, clock=clock)
    ctrl = Controller(Config(), m, clock=clock, log=logs.append)
    m.start_homing()
    assert run_sim(ctrl, m, clock, 15, until=lambda: "SHOW 000000,00" in logs)
    amounts = [12.34, 15.0, 15.01, 99.99, 98.0, 250.5, 250.51, 1000.0, 999.0, 1234.56]
    for a in amounts:                                                # one every 0.2 s
        ctrl.handle(str(a))
        run_sim(ctrl, m, clock, 0.2)
    assert run_sim(ctrl, m, clock, 15, until=lambda: "SHOW 001234,56" in logs)
    assert digits_on(rig) == [0, 0, 1, 2, 3, 4, 5, 6] and rig.backwards == 0
    shows = [line for line in logs if line.startswith("SHOW")]
    assert shows[-1] == "SHOW 001234,56"


def digits_on_soft(rig):
    return [int(rig.flap_shown(i)) % 10 for i in range(rig.n)]
