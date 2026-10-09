from __future__ import annotations

import math

import pytest

from moneycounter.amount import InvalidAmount, format_cents, max_cents_for, parse_message
from moneycounter.animator import CountUp
from moneycounter.config import ConfigError, load


@pytest.mark.parametrize("msg, cents", [
    ('{"amount": 1234.56}', 123456), ('{"amount": "1234.56"}', 123456), ("1234.56", 123456),
    ("1234,56", 123456), (b"42", 4200), ('"0.1"', 10), ("0", 0), ('{"amount": 0.005}', 1),
    ('{"amount": 1e3}', 100000), (" 7 ", 700),
])
def test_parse_valid(msg, cents):
    assert parse_message(msg).cents == cents


@pytest.mark.parametrize("msg", [
    "", "abc", "{", '{"total": 5}', '{"amount": null}', '{"amount": true}', '{"amount": [1]}', "-5",
    '{"amount": -0.01}', "NaN", '{"amount": NaN}', "Infinity", '{"amount": "1,2,3"}', b"\xff\xfe", "[1, 2]",
])
def test_parse_invalid(msg):
    with pytest.raises(InvalidAmount):
        parse_message(msg)


def test_nested_path_and_clamp():
    assert parse_message('{"data": {"total": 12}}', "data.total").cents == 1200
    a = parse_message("2000000")
    assert a.clamped and a.cents == 99999999 and format_cents(a.cents) == "999999,99"
    assert parse_message("999999.99").clamped is False
    assert parse_message("500", max_cents=max_cents_for(100.0)).cents == 10000


def test_format():
    assert format_cents(123456) == "001234,56"
    assert format_cents(0) == "000000,00"


def test_config_file_and_errors(tmp_path):
    cfg = load()                                         # the shipped config.toml
    assert cfg.network.mode == "client" and len(cfg.motion.hall_pins) == 8
    bad = tmp_path / "c.toml"
    bad.write_text('[network]\nmode = "push"\n')
    with pytest.raises(ConfigError):
        load(bad)
    bad.write_text("[motion]\noffsets = [0, 0]\n")
    with pytest.raises(ConfigError):
        load(bad)
    bad.write_text("[motion]\nspeed = 3\n")
    with pytest.raises(ConfigError):
        load(bad)
    bad.write_text("[motion]\nmax_pps = 400\nstart_pps = 450\n")       # start above top speed
    with pytest.raises(ConfigError):
        load(bad)
    bad.write_text("[display]\ncount_time_min = 4.0\ncount_time_max = 3.0\n")
    with pytest.raises(ConfigError):
        load(bad)
    m = cfg.motion
    assert m.start_pps < 500 < m.max_pps < 950 and cfg.display.count_time_max <= 3.0


def _run(anim: CountUp, t_end: float, dt: float = 0.02):
    frames, t = [], 0.0
    while t <= t_end + 1e-9:
        frames.append((t, anim.frame(t)))
        t += dt
    return frames


def test_count_up_is_monotonic_and_lands_exactly():
    anim = CountUp(capacity=4.15)
    mode, dur = anim.set_target(123456, 0.0)
    assert mode == "count" and dur == pytest.approx(6 / 4.15 + 0.05)   # farthest digit: 0 → 6
    frames = _run(anim, dur + 0.1)
    values = [f.value for _, f in frames]
    assert values == sorted(values) and values[-1] == 123456 and frames[-1][1].done
    assert len(set(values)) > 50                          # it visibly counts
    # the low digits spin at first, the high ones follow; at the end nothing spins
    first = frames[1][1]
    assert first.spin[-1] and first.spin[-2] and not first.spin[0] and not first.spin[1]
    assert not any(frames[-1][1].spin)
    # a digit that never spins never changes faster than the module can turn
    for j in range(8):
        if any(f.spin[j] for _, f in frames):
            continue
        steps = [(t, f.digits[j]) for t, f in frames]
        for (t0, d0), (t1, d1) in zip(steps, steps[1:]):
            assert (d1 - d0) % 10 <= max(1, math.ceil((t1 - t0) * 4.15 * 1.5))


def test_duration_follows_the_farthest_digit_and_is_capped():
    flap_time = lambda n: 0.1 + n * 0.24                                  # noqa: E731
    anim = CountUp(flap_time=flap_time, count_time_min=0.25, count_time_max=3.0)
    assert anim.duration_for(0, 123456) == pytest.approx(flap_time(6) + 0.05)
    assert anim.duration_for(100, 99999999) == pytest.approx(flap_time(9) + 0.05)
    assert anim.duration_for(123456, 123457) == pytest.approx(0.39)    # one flap: a cent in ≈ 0.4 s
    slow = CountUp(flap_time=lambda n: n * 1.0, count_time_max=3.0)
    assert slow.duration_for(0, 99999999) == 3.0                        # count_time_max
    assert anim.duration_for(500, 400) == 0.0


def test_followers_are_commanded_one_flap_ahead():
    anim = CountUp(capacity=4.15)
    _, dur = anim.set_target(123457, 0.0)
    anim.start = 123456.0                                               # (as if counting from 1234,56)
    anim.set_target(123457, 0.0)
    # the last digit is commanded long before the odometer itself reaches the new value
    t_cmd = next(t for t, f in _run(anim, 1.0, 0.01) if f.digits[-1] == 7)
    t_val = next(t for t, f in _run(anim, 1.0, 0.01) if f.value == 123457)
    assert t_cmd < 0.1 and t_val > t_cmd


def test_small_change_is_quick_and_decrease_jumps():
    anim = CountUp(capacity=4.15, initial=123456)
    mode, dur = anim.set_target(123457, 0.0)
    assert mode == "count" and dur == pytest.approx(1 / 4.15 + 0.05)
    assert anim.frame(1.0).digits == [0, 0, 1, 2, 3, 4, 5, 7]
    mode, dur = anim.set_target(500, 2.0)
    assert mode == "jump" and anim.frame(2.0).done and anim.frame(2.0).value == 500
    assert anim.set_target(500, 3.0)[0] == "same"


def test_new_target_mid_count_continues_from_the_current_value():
    anim = CountUp(capacity=4.15)
    _, dur = anim.set_target(99999999, 0.0)
    assert dur == pytest.approx(9 / 4.15 + 0.05, abs=0.01)
    v = anim.value(1.0)
    mode, _ = anim.set_target(99999999 - 1, 1.0)       # still above the value shown: keeps counting
    assert mode == "count" and anim.value(1.0) == pytest.approx(v)
    assert anim.frame(20.0).value == 99999998
