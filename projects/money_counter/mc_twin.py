"""Digital-twin devices and scenarios of the money counter (imported by ``project.py``).

Derived from the circuit: ``U2`` (``shift_register_74hc595``, 4 chips on SPI0/CE0) and ``M1…M8``
(``stepper_28byj48`` with ``coil_source`` = their four chain bits). Added here:

* ``SF1…SF8`` — ``splitflap`` modules (position 0 = leftmost), each turned by ``M{i}``, pulling its
  Hall GPIO low at the home angle; ``offset_steps`` equal ``firmware/config.toml`` ``offsets``;
  ``start_angle`` is where each spool happens to stand at power-up (homing has to find the magnet);
* ``M1…M8`` get the 28BYJ-48 **stall model** (``MOTOR_LIMITS``): steps faster than the motor can follow
  are lost in the twin (``lost_steps``), exactly as on the real motor — so the firmware's ramp and its
  Hall-edge closed loop are tested, not assumed. The ``slip`` input knocks a rotor back (disturbance);
* ``FEED1`` — ``ws_feed``: a WebSocket server in the twin that pushes ``{"amount": x}``; its URL
  reaches the firmware as ``$MONEY_COUNTER_URL`` (client mode, the default in config.toml).

The GUI's Twin tab shows the split-flap row (digits of SF1…SF8, comma after the 6th) and an
"Amount" box that sets ``FEED1.amount``.
"""

from __future__ import annotations

from piforge.project import Project
from piforge.twin.config import DeviceConfig
from piforge.twin.scenario import Scenario, Step

N = 8
FEED = "FEED1"
# 5 V 28BYJ-48 with a light flap spool. # src: est — see stepper_28byj48 docstring (pull-out ≈ 900–1000
# half-steps/s, pull-in ≈ 500); the firmware runs 450 → 850 at 3000/s², i.e. ≈ 10 % below each limit.
MOTOR_LIMITS = {"stall_model": True, "max_pps": 950.0, "start_pps": 500.0, "max_accel": 5000.0}
# spool angle at power-up (deg): 20–70° before the magnet, so the twin's homing is short but real
START_ANGLES = (310.0, 335.0, 295.0, 320.0, 345.0, 300.0, 330.0, 290.0)


def sf(i: int) -> str:
    return f"SF{i + 1}"


def add_twin_devices(p: Project, hall_pins: tuple[int, ...], offsets: tuple[int, ...], steps_per_rev: int,
                     flaps: int) -> None:
    for i in range(N):
        p.twin_override(f"M{i + 1}", **MOTOR_LIMITS)
    for i in range(N):
        p.twin_device(DeviceConfig(sf(i), "splitflap", params={
            "stepper": f"M{i + 1}", "hall_pin": hall_pins[i], "position": i, "flaps": flaps,
            "steps_per_rev": steps_per_rev, "offset_steps": offsets[i], "home_angle": 0.0,
            "start_angle": START_ANGLES[i]}))
    p.twin_device(DeviceConfig(FEED, "ws_feed", params={"port": 0}))


def digits_expect(at: float, text: str, settle: float = 0.5) -> list[Step]:
    """Expect every module to show ``text`` (``001234,56``)."""
    digits = text.replace(",", "")
    return [Step(at=at, action="expect", device=sf(i), prop="digit", value=int(d), settle=settle)
            for i, d in enumerate(digits)]


def no_lost_steps(at: float) -> list[Step]:
    """No motor lost a step (the twin's stall model counts what a real 28BYJ-48 would lose)."""
    return [Step(at=at, action="expect", device=f"M{i + 1}", prop="lost_steps", value=0) for i in range(N)]


def amount(at: float, value: float) -> Step:
    return Step(at=at, action="input", device=FEED, prop="amount", value=value)


def show(at: float, text: str, within: float) -> Step:
    """``SHOW <text>`` (settled and checked) is logged at most ``within`` seconds after ``at`` (steps run in
    order: a step waits for its expectation before the next one runs, so put it after earlier checks)."""
    return Step(at=at, action="expect_log", pattern=f"SHOW {text}", settle=within)


# Power-up: homing (≤ 70° at 450 half-steps/s) + one checked turn at 850 (self-test) ≈ 7–9 s in the twin;
# amounts sent before that are kept and counted once it is done. Scenarios act from READY on.
READY = 15.0


def homed(at: float = 0.3) -> Step:
    return show(at, "000000,00", READY - at)


def add_scenarios(p: Project) -> None:
    """Timing (850 half-steps/s, 3000/s² ramps): a flap takes ≈ 0.29 s from rest; a count lasts as long
    as the farthest-moving digit needs (≤ 9 flaps ≈ 2.3 s). Before (600 half-steps/s, log-time count):
    0 → 1234,56 took 5.2 s, → 999999,99 7.7 s. Windows below are those times with host-load margin."""
    # 1. power-up: every spool turns to its magnet, one checked turn, then digit 0
    p.scenario(Scenario("homing_to_zero", duration=READY + 3.0, steps=[
        Step(at=0.3, action="expect_log", pattern=r"SELFTEST m7: one turn at 850", settle=READY - 0.3),
        homed(),
        *digits_expect(READY, "000000,00"),
        Step(at=READY + 1.0, action="expect", device="M1", prop="energized", value=False),   # coils released
        Step(at=READY + 1.0, action="expect", device="SF8", prop="hall", value=True),        # at the magnet
        *no_lost_steps(READY + 1.5),
    ]))
    # 2. 0 → 1234,56 counts up (low digits scroll) and lands exactly — in < 3 s (was 5.2 s)
    p.scenario(Scenario("count_up_1234_56", duration=READY + 5.0, steps=[
        homed(),
        amount(READY, 1234.56),
        Step(at=READY + 0.05, action="expect_log", pattern=r"TARGET 001234,56 \(count-up 1\.\d s\)"),
        Step(at=READY + 0.4, action="expect", device="M8", prop="rpm", value=1.0, op=">"),   # low digits turning
        show(READY + 0.5, "001234,56", 2.5),                    # ≤ 3 s after the amount
        *digits_expect(READY + 3.2, "001234,56"),
        *no_lost_steps(READY + 4.0),
    ]))
    # 3. 1234,56 → 1234,57: only the last module turns one flap, in well under a second
    p.scenario(Scenario("last_digit_1234_57", duration=READY + 7.0, steps=[
        homed(),
        amount(READY, 1234.56),
        show(READY, "001234,56", 3.0),
        amount(READY + 4.0, 1234.57),
        show(READY + 4.0, "001234,57", 0.9),
        Step(at=READY + 5.0, action="expect", device="SF7", prop="digit", value=5),
        Step(at=READY + 5.0, action="expect", device="SF8", prop="digit", value=7),
    ]))
    # 4. big jump 1 → 999999,99: every digit spins at full speed, all land on 9 together (was 7.7 s)
    p.scenario(Scenario("big_jump_999999_99", duration=READY + 9.0, steps=[
        homed(),
        amount(READY, 1.0),
        show(READY, "000001,00", 2.5),
        amount(READY + 3.0, 999999.99),
        Step(at=READY + 3.05, action="expect_log", pattern=r"TARGET 999999,99"),
        Step(at=READY + 3.5, action="expect", device="M8", prop="rpm", value=1.0, op=">"),
        Step(at=READY + 3.5, action="expect", device="M5", prop="rpm", value=1.0, op=">"),
        show(READY + 3.6, "999999,99", 3.4),                    # ≤ 4 s after the amount
        *digits_expect(READY + 7.2, "999999,99"),
        *no_lost_steps(READY + 8.0),
    ]))
    # 5. overflow: 2 000 000 is clamped to the maximum and logged (sent during power-up: kept, then counted)
    p.scenario(Scenario("overflow_clamps", duration=READY + 5.0, steps=[
        amount(0.3, 2_000_000.0),
        Step(at=0.5, action="expect_log", pattern=r"CLAMP 2000000.* -> showing 999999,99"),
        show(0.5, "999999,99", READY + 3.0),
        *digits_expect(READY + 4.0, "999999,99"),
    ]))
    # 6. invalid amount (negative) is logged and ignored; the next valid one works
    p.scenario(Scenario("invalid_ignored", duration=READY + 7.0, steps=[
        homed(),
        amount(READY, 12.34),
        show(READY, "000012,34", 3.0),
        amount(READY + 3.5, -5.0),
        Step(at=READY + 3.6, action="expect_log", pattern=r"IGNORED invalid message .*negative"),
        *digits_expect(READY + 4.0, "000012,34"),
        amount(READY + 4.5, 12.35),
        show(READY + 4.5, "000012,35", 1.2),
    ]))
    # 7. the WebSocket server goes away, the amount changes meanwhile, it comes back: reconnect, catch up
    p.scenario(Scenario("feed_reconnect", duration=READY + 14.0, steps=[
        homed(),
        amount(READY, 5.0),
        show(READY, "000005,00", 2.5),
        Step(at=READY + 3.0, action="input", device=FEED, prop="online", value=False),
        Step(at=READY + 3.1, action="expect_log", pattern=r"offline .*reconnecting in"),
        amount(READY + 4.0, 77.77),
        Step(at=READY + 6.0, action="input", device=FEED, prop="online", value=True),
        Step(at=READY + 3.1, action="expect_log", pattern=r"ConnectionRefused", settle=3.0),   # retried offline
        show(READY + 6.0, "000077,77", 6.0),
        *digits_expect(READY + 12.5, "000077,77", settle=1.0),
    ]))
    # 8. ten amounts in two seconds (counting up, down, up …): it never loses its place, ends exact
    rapid = (12.34, 15.0, 15.01, 99.99, 98.0, 250.5, 250.51, 1000.0, 999.0, 1234.56)
    p.scenario(Scenario("rapid_updates", duration=READY + 8.0, steps=[
        homed(),
        *[amount(READY + 0.2 * k, v) for k, v in enumerate(rapid)],
        show(READY + 1.8, "001234,56", 4.0),
        *digits_expect(READY + 6.0, "001234,56"),
        *no_lost_steps(READY + 7.0),
    ]))
    # 9. a rotor is knocked back 300 half-steps (≈ 1.5 flaps) while it turns: the Hall check at its magnet
    #    finds the error (RESYNC m7 err=+300), corrects it and the module still lands on its digit
    p.scenario(Scenario("slip_resync", duration=READY + 11.0, steps=[
        homed(),
        amount(READY, 1234.56),                                   # SF8 → flap 6
        show(READY, "001234,56", 3.0),
        amount(READY + 3.5, 1235.55),                             # SF8 6 → 5: flap 15
        show(READY + 3.5, "001235,55", 3.0),
        amount(READY + 7.0, 1236.54),                             # SF8 5 → 4: flap 24, past its magnet
        Step(at=READY + 7.15, action="input", device="M8", prop="slip", value=300),
        Step(at=READY + 7.2, action="expect_log", pattern=r"RESYNC m7 err=\+300", settle=3.0),
        show(READY + 7.25, "001236,54", 3.3),
        *digits_expect(READY + 10.6, "001236,54"),
    ]))
