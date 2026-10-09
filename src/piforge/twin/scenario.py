"""Scripted twin scenarios: timed inputs + expectations → pass/fail :class:`~piforge.core.report.Report`.

::

    Scenario("button lights LED", duration=2.0, steps=[
        Step(at=0.3, action="input", device="SW1", prop="pressed", value=True),
        Step(at=0.8, action="expect", device="D1", prop="brightness", value=1.0, tol=0.01),
        Step(at=0.9, action="expect_log", pattern=r"pressed"),
    ])

Time 0 is the runner's ``hello`` (firmware about to start); leave ~0.2 s for firmware set-up.
An ``expect`` passes if the condition holds in some state between ``at`` and ``at + settle``
(default 0.5 s, per scenario; a step may set its own ``settle``), so firmware reaction time does not
make results flaky. An ``expect_log`` passes if any log line up to ``at + settle`` matches
``pattern`` (regex). Steps run in order: an expectation waits (up to its settle) before later steps.

Codes: ``TWIN.EXPECT_FAILED`` (ERROR), ``TWIN.FIRMWARE_ERROR`` (ERROR), ``TWIN.SCENARIO_INVALID``
(ERROR, nothing run), ``TWIN.CONFIG_INVALID`` (ERROR), ``TWIN.INPUT_NOT_DELIVERED`` (WARNING),
``TWIN.STEPS_SKIPPED`` (INFO), twin diagnostics such as ``TWIN.CONTENTION`` (WARNING) and
``TWIN.SCENARIO_OK`` (INFO, only when there is no error).
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from piforge.core.errors import PiForgeError, ValidationError
from piforge.core.report import Report
from piforge.twin.config import TwinConfig

ACTIONS = ("input", "expect", "expect_log")
OPS = ("==", "!=", "<", "<=", ">", ">=", "~=", "contains", "matches")


@dataclass
class Step:
    """One timed action. ``op``/``tol`` apply to ``expect``; ``pattern`` to ``expect_log``; ``settle``
    (seconds, optional) overrides the scenario's settle window for this expectation."""

    at: float
    action: str
    device: str = ""
    prop: str = ""
    value: object = None
    op: str = "=="
    tol: float = 0.0
    pattern: str = ""
    settle: float | None = None

    def __post_init__(self) -> None:
        try:
            self.at = float(self.at)
        except (TypeError, ValueError):
            raise ValidationError(f"Step.at must be a number of seconds, got {self.at!r}") from None
        if not math.isfinite(self.at) or self.at < 0:
            raise ValidationError(f"Step.at must be ≥ 0 s, got {self.at!r}")
        if self.action not in ACTIONS:
            raise ValidationError(f"Step.action must be one of {ACTIONS}, got {self.action!r}")
        if self.op not in OPS:
            raise ValidationError(f"Step.op must be one of {OPS}, got {self.op!r}")
        if not self.tol >= 0:
            raise ValidationError(f"Step.tol must be ≥ 0, got {self.tol!r}")
        if self.settle is not None and not (isinstance(self.settle, (int, float)) and 0 <= self.settle < 3600):
            raise ValidationError(f"Step.settle must be a number of seconds ≥ 0, got {self.settle!r}")
        if self.action in ("input", "expect") and not (self.device and self.prop):
            raise ValidationError(f"{self.action} step at {self.at} s needs device and prop")
        if self.action == "expect_log":
            if not self.pattern:
                raise ValidationError(f"expect_log step at {self.at} s needs a pattern")
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValidationError(f"invalid regex {self.pattern!r}: {exc}") from None

    def to_dict(self) -> dict:
        """Plain-JSON form (``settle`` only when set)."""
        d = asdict(self)
        if d["settle"] is None:
            del d["settle"]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Step":
        """Inverse of :meth:`to_dict`."""
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Scenario:
    """A named list of steps run for ``duration`` seconds."""

    name: str
    steps: list[Step] = field(default_factory=list)
    duration: float = 5.0
    settle: float = 0.5

    def __post_init__(self) -> None:
        self.steps = [s if isinstance(s, Step) else Step.from_dict(s) for s in self.steps]
        if not self.duration > 0:
            raise ValidationError(f"Scenario.duration must be > 0 s, got {self.duration!r}")

    def to_dict(self) -> dict:
        """Plain-JSON form."""
        return {"name": self.name, "duration": self.duration, "settle": self.settle,
                "steps": [s.to_dict() for s in self.steps]}

    @classmethod
    def from_dict(cls, d: dict) -> "Scenario":
        """Inverse of :meth:`to_dict`."""
        return cls(name=d["name"], steps=[Step.from_dict(s) for s in d.get("steps", [])],
                   duration=d.get("duration", 5.0), settle=d.get("settle", 0.5))


def compare(actual: Any, op: str, expected: Any, tol: float = 0.0) -> bool:
    """Evaluate ``actual <op> expected`` (numbers compare within ``tol``); False on type mismatch."""
    def num(x: Any) -> bool:
        return isinstance(x, (int, float)) and not isinstance(x, bool)

    try:
        if op in ("==", "!="):
            eq = abs(actual - expected) <= tol if num(actual) and num(expected) else actual == expected
            return eq if op == "==" else not eq
        if op == "~=":
            return num(actual) and num(expected) and abs(actual - expected) <= tol
        if op == "<":
            return actual < expected
        if op == "<=":
            return actual <= expected + tol if num(actual) else actual <= expected
        if op == ">":
            return actual > expected
        if op == ">=":
            return actual >= expected - tol if num(actual) else actual >= expected
        if op == "contains":
            if isinstance(actual, (list, tuple)):
                return any(expected == a or (isinstance(a, str) and str(expected) in a) for a in actual)
            return str(expected) in str(actual)
        if op == "matches":
            return re.search(str(expected), "\n".join(actual) if isinstance(actual, list) else str(actual)) is not None
    except (TypeError, ValueError, re.error):
        return False
    return False


def _validate(config: TwinConfig, scenario: Scenario, rep: Report) -> bool:
    """Check devices/props/values against the config before spawning anything."""
    from piforge.twin.clock import ManualClock
    from piforge.twin.runtime import Twin

    try:
        twin = Twin(config, clock=ManualClock())
    except PiForgeError as exc:
        rep.add("TWIN.CONFIG_INVALID", "error", f"twin config cannot be built: {exc}")
        return False
    ok = True
    for st in scenario.steps:
        if st.action == "expect_log":
            continue
        dev = twin.devices.get(st.device)
        if dev is None:
            rep.add("TWIN.SCENARIO_INVALID", "error", f"step at {st.at} s: unknown device {st.device!r}",
                    subject=st.device, hint=f"devices: {', '.join(twin.devices) or 'none'}")
            ok = False
            continue
        if st.action == "input":
            spec = dev.inputs.get(st.prop)
            if spec is None:
                rep.add("TWIN.SCENARIO_INVALID", "error",
                        f"step at {st.at} s: {st.device} has no input {st.prop!r}",
                        subject=f"{st.device}.{st.prop}", hint=f"inputs: {', '.join(dev.inputs) or 'none'}")
                ok = False
                continue
            try:
                spec.coerce(st.value, f"{st.device}.{st.prop}")
            except ValidationError as exc:
                rep.add("TWIN.SCENARIO_INVALID", "error", f"step at {st.at} s: {exc}",
                        subject=f"{st.device}.{st.prop}")
                ok = False
        else:
            known = set(dev.inputs) | set(dev.outputs) | set(dev.state())
            if st.prop not in known:
                rep.add("TWIN.SCENARIO_INVALID", "error",
                        f"step at {st.at} s: {st.device} has no property {st.prop!r}",
                        subject=f"{st.device}.{st.prop}", hint=f"properties: {', '.join(sorted(known))}")
                ok = False
    return ok


def _sleep_until(deadline: float, session: Any) -> None:
    while session.running:
        left = deadline - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(left, 0.05))


def _last_line(text: str | None) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip() and not ln.startswith("PiForge twin:")]
    return lines[-1] if lines else ""


def run_scenario(config: TwinConfig, firmware: Path | str, scenario: Scenario, *,
                 cwd: Path | str | None = None, start_timeout: float = 30.0) -> Report:
    """Run ``firmware`` on a fresh twin, apply the scenario, and report pass/fail."""
    from piforge.twin.session import TwinSession

    rep = Report(title=f"twin scenario: {scenario.name}")
    if not _validate(config, scenario, rep):
        return rep
    session = TwinSession(config, firmware, cwd=cwd, start_timeout=start_timeout)
    n_expect = n_pass = 0
    skipped = 0
    try:
        session.start()
    except PiForgeError as exc:
        rep.add("TWIN.FIRMWARE_ERROR", "error", f"cannot start the twin runner: {exc}", subject=str(firmware))
        return rep
    try:
        if session.wait_ready(start_timeout) is None:
            ex = session.exit_message or {}
            err = ex.get("error") or f"runner did not start within {start_timeout:g} s"
            rep.add("TWIN.FIRMWARE_ERROR", "error", f"twin runner failed before the firmware started: "
                    f"{_last_line(err) or err}", subject=str(firmware), traceback=err,
                    stderr_tail=session.stderr_tail()[-20:])
            return rep
        t0 = time.monotonic()
        steps = sorted(scenario.steps, key=lambda s: s.at)
        for i, st in enumerate(steps):
            _sleep_until(t0 + st.at, session)
            ex = session.exit_message
            if ex is not None and (ex.get("code") or ex.get("error")):
                skipped = len(steps) - i
                break
            if st.action == "input":
                try:
                    session.send_input(st.device, st.prop, st.value)
                except PiForgeError as exc:
                    rep.add("TWIN.INPUT_NOT_DELIVERED", "warning",
                            f"input at t={st.at:.2f} s ({st.device}.{st.prop}={st.value!r}) not delivered: {exc}",
                            subject=f"{st.device}.{st.prop}")
            elif st.action == "expect":
                n_expect += 1

                def ok(m: dict, st: Step = st) -> bool:
                    return m["op"] == "state" and compare(m["devices"][st.device][st.prop], st.op, st.value, st.tol)

                settle = scenario.settle if st.settle is None else st.settle
                if session.wait_for(ok, settle) is not None:
                    n_pass += 1
                else:
                    actual = session.latest_state().get("devices", {}).get(st.device, {}).get(st.prop)
                    tol = f" ± {st.tol:g}" if st.tol else ""
                    rep.add("TWIN.EXPECT_FAILED", "error",
                            f"t={st.at:.2f} s: expected {st.device}.{st.prop} {st.op} {st.value!r}{tol} "
                            f"(within {settle:g} s), got {actual!r}",
                            subject=f"{st.device}.{st.prop}", at=st.at, op=st.op, expected=st.value,
                            actual=actual, tol=st.tol)
            else:
                n_expect += 1
                rx = re.compile(st.pattern)
                settle = scenario.settle if st.settle is None else st.settle
                hit = any(rx.search(line) for line in session.logs()) or session.wait_for(
                    lambda m, rx=rx: m["op"] == "log" and rx.search(str(m["text"])) is not None,
                    settle) is not None
                if hit:
                    n_pass += 1
                else:
                    rep.add("TWIN.EXPECT_FAILED", "error",
                            f"t={st.at:.2f} s: no log line matched /{st.pattern}/", subject="log",
                            at=st.at, pattern=st.pattern, log_tail=session.logs()[-10:])
        if not skipped:
            _sleep_until(t0 + scenario.duration, session)
    finally:
        session.stop()

    ex = session.exit_message or {}
    code, reason, error = ex.get("code"), ex.get("reason"), ex.get("error")
    if (code not in (0, None)) or reason in ("crash", "runner-error", "setup", "killed") or (
            error and reason != "stopped"):
        rep.add("TWIN.FIRMWARE_ERROR", "error",
                f"firmware exited with code {code} ({reason}): {_last_line(error) or 'no traceback'}",
                subject=Path(str(firmware)).name, exit_code=code, reason=reason, traceback=error,
                hint="see data.traceback; firmware stdout/stderr are in the twin log")
    if skipped:
        rep.add("TWIN.STEPS_SKIPPED", "info", f"{skipped} step(s) not run because the firmware stopped early")
    seen: set[tuple[str, str]] = set()
    for ev in session.events():
        key = (str(ev.get("code")), str(ev.get("text")))
        if key in seen or ev.get("code") in ("TWIN.BAD_INPUT",):
            continue
        seen.add(key)
        sev = "error" if ev.get("level") == "error" else "warning"
        rep.add(str(ev.get("code")), sev, str(ev.get("text")), subject="twin", event=ev.get("data") or {})
    if rep.ok:
        rep.add("TWIN.SCENARIO_OK", "info",
                f"{n_pass}/{n_expect} expectation(s) met in {scenario.duration:g} s; firmware {reason or 'ran'}",
                passed=n_pass, expectations=n_expect)
    return rep
