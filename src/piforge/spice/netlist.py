"""SPICE netlist builder for ngspice.

Build a circuit in Python with validated element values; device models from
:mod:`piforge.spice.models` are pulled in automatically when an element references them::

    c = SpiceCircuit("RC step")
    c.V("1", "in", "0", Pulse(0, 3.3))
    c.R("1", "in", "out", 1e3)          # SI units: Ω, F, H, V, A, s, Hz
    c.C("1", "out", "0", 1e-6)
    c.tran(1e-6, 3e-3)
    c.measure("vtau", "tran FIND v(out) AT=1m")
    print(c.to_netlist())

Conventions:

- Node ``"0"`` is ground; ``"gnd"`` is accepted as an alias. Node names are lower-cased so result
  vectors are always ``v(<node>)``.
- Element names get their SPICE type letter prepended unless they already start with it:
  ``R("1", …)`` → ``R1``, ``R("R5", …)`` → ``R5``, ``V("in", …)`` → ``Vin`` (current ``i(vin)``).
- Values may be numbers or engineering strings (``"4.7k"``, ``"100nF"``); ``"{expr}"`` passes a
  ``.param`` expression through unchecked. Invalid values raise :class:`SpiceValueError` at once.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from numbers import Real
from typing import Any

from piforge.core.errors import suggest
from piforge.spice.errors import SpiceValueError

__all__ = ["PWL", "Pulse", "Sine", "SpiceCircuit", "SpiceValueError", "fmt_value", "parse_value"]

_SUFFIX_BY_EXP = {12: "t", 9: "g", 6: "meg", 3: "k", 0: "", -3: "m", -6: "u", -9: "n",
                  -12: "p", -15: "f"}
_SCALE = {"t": 1e12, "g": 1e9, "meg": 1e6, "k": 1e3, "m": 1e-3, "u": 1e-6, "µ": 1e-6, "μ": 1e-6,
          "n": 1e-9, "p": 1e-12, "f": 1e-15}
_UNITS = {"", "ohm", "ohms", "ω", "f", "h", "v", "a", "s", "hz", "w", "deg"}
_VALUE_RE = re.compile(r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?)(meg|[tgkmunpfµμ])?([a-zω]*)$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
_NODE_RE = re.compile(r"^[a-z0-9_][a-z0-9_+\-]*$")
_MODEL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")
_MEAS_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_GROUND = {"0", "gnd"}
_MODEL_TYPES = {"D": {"D"}, "Q": {"NPN", "PNP"}, "M": {"NMOS", "PMOS", "VDMOS"}, "S": {"SW"},
                "X": {"SUBCKT"}}
_WRAP = 110  # ngspice accepts long lines, but '+' continuations keep decks readable


def _is_number(x: Any) -> bool:
    return isinstance(x, Real) and not isinstance(x, bool)


def fmt_value(x: float) -> str:
    """Format a number in SPICE engineering notation: 4700 → ``4.7k``, 1e-6 → ``1u``, 2e6 → ``2meg``.

    Nine significant digits are kept; magnitudes outside 1e-15…1e15 use exponent notation.
    """
    if not _is_number(x):
        raise SpiceValueError(f"expected a number, got {x!r}")
    v = float(x)
    if not math.isfinite(v):
        raise SpiceValueError(f"value must be finite, got {v!r}")
    if v == 0.0:
        return "0"
    sign = "-" if v < 0 else ""
    a = float(f"{abs(v):.9g}")  # round first so 999.9999999999 becomes 1k, not 1000
    if not 1e-15 <= a < 1e15:
        return sign + f"{a:.9g}"
    exp3 = int(math.floor(math.log10(a) / 3.0)) * 3
    mant = a / 10.0 ** exp3
    if mant >= 999.9999995:  # log10 rounding at an exact power of 1000
        exp3, mant = exp3 + 3, mant / 1000.0
    return f"{sign}{mant:.9g}{_SUFFIX_BY_EXP[exp3]}"


def parse_value(text: str | float) -> float:
    """Parse ``"4.7k"``, ``"100nF"``, ``"1meg"``, ``"3.3V"`` or a number into a float (SI units).

    SPICE rules apply: suffixes are case-insensitive, so ``m``/``M`` mean milli and ``meg`` mega;
    a bare ``f`` is femto. A trailing unit (Ω/ohm, F, H, V, A, s, Hz, W) is accepted and ignored.
    """
    if _is_number(text):
        v = float(text)
    elif isinstance(text, str):
        m = _VALUE_RE.match(text.strip().lower())
        if not m or m.group(3) not in _UNITS:
            raise SpiceValueError(f"cannot parse {text!r} as a value (examples: 4.7k, 100n, 1meg, 3.3)")
        v = float(m.group(1)) * (_SCALE[m.group(2)] if m.group(2) else 1.0)
    else:
        raise SpiceValueError(f"expected a number or an engineering string, got {text!r}")
    if not math.isfinite(v):
        raise SpiceValueError(f"value must be finite, got {text!r}")
    return v


def _num(what: str, x: Any) -> float:
    try:
        v = parse_value(x)
    except SpiceValueError as exc:
        raise SpiceValueError(f"{what}: {exc}") from None
    return v


@dataclass(frozen=True)
class Pulse:
    """``PULSE(v1 v2 td tr tf pw per)`` waveform: levels in V (or A), times in s."""

    v1: float
    v2: float
    td: float = 0.0
    tr: float = 1e-9
    tf: float = 1e-9
    pw: float = 1e-3
    per: float = 2e-3

    def spice(self) -> str:
        """Render as SPICE source text; raises :class:`SpiceValueError` for invalid timing."""
        v = {k: _num(f"Pulse.{k}", getattr(self, k)) for k in ("v1", "v2", "td", "tr", "tf", "pw", "per")}
        if min(v["td"], v["tr"], v["tf"], v["pw"]) < 0:
            raise SpiceValueError(f"Pulse times must be >= 0: {self}")
        if v["per"] <= 0 or v["per"] < v["tr"] + v["pw"] + v["tf"]:
            raise SpiceValueError(f"Pulse period must be > 0 and >= tr + pw + tf: {self}")
        return "PULSE(" + " ".join(fmt_value(v[k]) for k in ("v1", "v2", "td", "tr", "tf", "pw", "per")) + ")"


@dataclass(frozen=True)
class PWL:
    """Piece-wise linear waveform: ``((t0, v0), (t1, v1), …)`` with strictly increasing times."""

    points: tuple[tuple[float, float], ...]

    def spice(self) -> str:
        """Render as ``PWL(t0 v0 t1 v1 …)``; raises :class:`SpiceValueError` for invalid points."""
        if not self.points:
            raise SpiceValueError("PWL needs at least one (time, value) point")
        parts: list[str] = []
        last = -math.inf
        for i, pt in enumerate(self.points):
            if len(pt) != 2:
                raise SpiceValueError(f"PWL point {i} must be (time, value), got {pt!r}")
            t, val = _num(f"PWL time #{i}", pt[0]), _num(f"PWL value #{i}", pt[1])
            if t < 0 or t <= last:
                raise SpiceValueError(f"PWL times must be >= 0 and strictly increasing (point {i}: t={t})")
            last = t
            parts += [fmt_value(t), fmt_value(val)]
        return "PWL(" + " ".join(parts) + ")"


@dataclass(frozen=True)
class Sine:
    """``SIN(vo va freq td)`` waveform: offset and amplitude in V (or A), frequency in Hz."""

    vo: float
    va: float
    freq: float
    td: float = 0.0

    def spice(self) -> str:
        """Render as SPICE source text; the frequency must be > 0."""
        vo, va, f, td = (_num(f"Sine.{k}", getattr(self, k)) for k in ("vo", "va", "freq", "td"))
        if f <= 0 or td < 0:
            raise SpiceValueError(f"Sine needs freq > 0 and td >= 0: {self}")
        return f"SIN({fmt_value(vo)} {fmt_value(va)} {fmt_value(f)} {fmt_value(td)})"


Waveform = float | str | Pulse | PWL | Sine


@dataclass
class _Element:
    prefix: str
    name: str
    nodes: tuple[str, ...]
    tail: str = ""  # value / source text (R, C, L, V, I, B)
    model: str = ""  # D, Q, M, S, X
    body: str | None = None  # MOSFET bulk node (None → source)
    params: str = ""  # X instance parameters


def _wrap(line: str) -> list[str]:
    if len(line) <= _WRAP:
        return [line]
    out, cur = [], ""
    for tok in line.split(" "):
        if cur and len(cur) + 1 + len(tok) > _WRAP:
            out.append(cur)
            cur = "+ " + tok
        else:
            cur = f"{cur} {tok}" if cur else tok
    out.append(cur)
    return out


def _parse_definitions(text: str) -> dict[str, tuple[str, int]]:
    """Top-level ``.model``/``.subckt`` names in ``text`` → (type, number of subckt pins)."""
    found: dict[str, tuple[str, int]] = {}
    depth = 0
    for raw in text.splitlines():
        toks = raw.split()
        if not toks:
            continue
        head = toks[0].lower()
        if head == ".subckt" and len(toks) >= 2:
            if depth == 0:
                pins = 0
                for t in toks[2:]:
                    if t.lower() == "params:" or "=" in t:
                        break
                    pins += 1
                found[toks[1].lower()] = ("SUBCKT", pins)
            depth += 1
        elif head == ".ends":
            depth = max(0, depth - 1)
        elif head == ".model" and len(toks) >= 3 and depth == 0:
            mtype = re.match(r"[A-Za-z]+", toks[2])
            found[toks[1].lower()] = ((mtype.group(0) if mtype else toks[2]).upper(), 0)
    return found


class SpiceCircuit:
    """A SPICE circuit: elements, models, analyses, measurements; rendered by :meth:`to_netlist`."""

    def __init__(self, title: str) -> None:
        t = " ".join(str(title).split()) or "untitled circuit"
        self.title = t
        self._elements: list[_Element] = []
        self._names: set[str] = set()
        self._models: dict[str, tuple[str, int]] = {}  # model/subckt name → (type, subckt pins)
        self._model_texts: list[str] = []
        self._analyses: list[str] = []
        self._measures: dict[str, str] = {}
        self._saves: list[str] = []
        self._options: dict[str, str] = {}
        self._ics: dict[str, str] = {}
        self._params: dict[str, str] = {}
        self._raw: list[str] = []

    # -- helpers ----------------------------------------------------------------------------
    def _element_name(self, prefix: str, name: Any) -> str:
        s = str(name).strip()
        if not s or not _NAME_RE.match(s):
            raise SpiceValueError(f"invalid element name {name!r}: use letters, digits and '_' only")
        full = prefix + (s[1:] if s[0].upper() == prefix and len(s) > 1 else s)
        if full.lower() in self._names:
            raise SpiceValueError(f"duplicate element name {full!r} (names are case-insensitive)")
        return full

    @staticmethod
    def _node(n: Any) -> str:
        s = str(n).strip().lower()
        if s in _GROUND:
            return "0"
        if not _NODE_RE.match(s):
            raise SpiceValueError(f"invalid node name {n!r}: use letters, digits, '_', '+', '-' (no spaces)")
        return s

    def _add(self, el: _Element) -> None:
        self._names.add(el.name.lower())
        self._elements.append(el)

    @staticmethod
    def _positive(what: str, value: Any) -> str:
        if isinstance(value, str) and value.strip().startswith("{") and value.strip().endswith("}"):
            return value.strip()
        v = _num(what, value)
        if v <= 0:
            raise SpiceValueError(f"{what} must be > 0, got {value!r}")
        return fmt_value(v)

    @staticmethod
    def _source_text(what: str, value: Any, ac: float | None) -> str:
        if isinstance(value, (Pulse, PWL, Sine)):
            text = value.spice()
        elif isinstance(value, str) and value.strip().startswith("{"):
            text = value.strip()
        else:
            text = "DC " + fmt_value(_num(what, value))
        if ac is not None:
            text += " AC " + fmt_value(_num(f"{what} AC magnitude", ac))
        return text

    def _model_ref(self, element: str, model: Any) -> str:
        m = str(model).strip()
        if not _MODEL_RE.match(m):
            raise SpiceValueError(f"invalid model name {model!r}")
        key = m.lower()
        if key not in self._models:
            from piforge.spice import models as _lib  # local import: models never imports netlist

            if key in _lib.MODELS:
                self.use_model(key)
        if key in self._models:
            mtype = self._models[key][0]
            if mtype not in _MODEL_TYPES[element]:
                want = {"D": "a diode", "Q": "a BJT", "M": "a MOSFET", "S": "a switch",
                        "X": "a subcircuit"}[element]
                raise SpiceValueError(f"model {m!r} is {mtype}, but this element needs {want} model")
        return key

    # -- elements ---------------------------------------------------------------------------
    def R(self, name: Any, n1: Any, n2: Any, value: float | str) -> None:
        """Resistor (Ω, must be > 0)."""
        full = self._element_name("R", name)
        self._add(_Element("R", full, (self._node(n1), self._node(n2)),
                           tail=self._positive(f"{full} resistance", value)))

    def C(self, name: Any, n1: Any, n2: Any, value: float | str, *, ic: float | None = None) -> None:
        """Capacitor (F, must be > 0); ``ic`` = initial voltage used with ``tran(..., uic=True)``."""
        full = self._element_name("C", name)
        tail = self._positive(f"{full} capacitance", value)
        if ic is not None:
            tail += " IC=" + fmt_value(_num(f"{full} IC", ic))
        self._add(_Element("C", full, (self._node(n1), self._node(n2)), tail=tail))

    def L(self, name: Any, n1: Any, n2: Any, value: float | str, *, ic: float | None = None) -> None:
        """Inductor (H, must be > 0); ``ic`` = initial current (A) used with ``uic``."""
        full = self._element_name("L", name)
        tail = self._positive(f"{full} inductance", value)
        if ic is not None:
            tail += " IC=" + fmt_value(_num(f"{full} IC", ic))
        self._add(_Element("L", full, (self._node(n1), self._node(n2)), tail=tail))

    def V(self, name: Any, npos: Any, nneg: Any, value: Waveform, *, ac: float | None = None) -> None:
        """Voltage source (V): DC number, :class:`Pulse`, :class:`PWL` or :class:`Sine`; ``ac`` magnitude."""
        full = self._element_name("V", name)
        self._add(_Element("V", full, (self._node(npos), self._node(nneg)),
                           tail=self._source_text(full, value, ac)))

    def I(self, name: Any, npos: Any, nneg: Any, value: Waveform, *,  # noqa: E743 - SPICE letter
          ac: float | None = None) -> None:
        """Current source (A); positive current flows from ``npos`` through the source to ``nneg``."""
        full = self._element_name("I", name)
        self._add(_Element("I", full, (self._node(npos), self._node(nneg)),
                           tail=self._source_text(full, value, ac)))

    def D(self, name: Any, anode: Any, cathode: Any, model: str) -> None:
        """Diode / LED with a model key from :data:`piforge.spice.models.MODELS` (or own text)."""
        full = self._element_name("D", name)
        self._add(_Element("D", full, (self._node(anode), self._node(cathode)),
                           model=self._model_ref("D", model)))

    def Q(self, name: Any, c: Any, b: Any, e: Any, model: str) -> None:
        """Bipolar transistor (collector, base, emitter)."""
        full = self._element_name("Q", name)
        self._add(_Element("Q", full, (self._node(c), self._node(b), self._node(e)),
                           model=self._model_ref("Q", model)))

    def M(self, name: Any, d: Any, g: Any, s: Any, model: str, *, b: Any = None) -> None:
        """MOSFET (drain, gate, source[, bulk]). VDMOS power models are 3-terminal (body = source)."""
        full = self._element_name("M", name)
        key = self._model_ref("M", model)
        nodes = (self._node(d), self._node(g), self._node(s))
        body = self._node(b) if b is not None else None
        if body is not None and body != nodes[2] and self._models.get(key, ("", 0))[0] == "VDMOS":
            raise SpiceValueError(f"{full}: {key} is a VDMOS (3-terminal) model; its body is tied to "
                                  "the source — omit b=")
        self._add(_Element("M", full, nodes, model=key, body=body))

    def S(self, name: Any, n1: Any, n2: Any, cp: Any, cn: Any, model: str) -> None:
        """Voltage-controlled switch between n1/n2, controlled by V(cp, cn) (``sw_ideal``: on > 0.6 V)."""
        full = self._element_name("S", name)
        self._add(_Element("S", full, tuple(self._node(n) for n in (n1, n2, cp, cn)),
                           model=self._model_ref("S", model)))

    def B(self, name: Any, n1: Any, n2: Any, expr: str, *, kind: str = "V") -> None:
        """Behavioural source: ``kind="V"`` (voltage) or ``"I"`` (current) given by an ngspice expression."""
        full = self._element_name("B", name)
        k = str(kind).upper()
        if k not in ("V", "I"):
            raise SpiceValueError(f"{full}: kind must be 'V' or 'I', got {kind!r}")
        e = str(expr).strip()
        if not e or "\n" in e:
            raise SpiceValueError(f"{full}: expression must be a non-empty single line")
        self._add(_Element("B", full, (self._node(n1), self._node(n2)), tail=f"{k}={e}"))

    def X(self, name: Any, nodes: list[Any], subckt: str, *, params: dict[str, Any] | None = None) -> None:
        """Subcircuit instance; ``params`` override the subckt's ``params:`` defaults."""
        full = self._element_name("X", name)
        key = self._model_ref("X", subckt)
        ns = tuple(self._node(n) for n in nodes)
        if key in self._models and self._models[key][1] != len(ns):
            raise SpiceValueError(f"{full}: subckt {key} has {self._models[key][1]} pins, "
                                  f"got {len(ns)} nodes")
        ptxt = ""
        for k, v in (params or {}).items():
            if not _MEAS_NAME_RE.match(str(k)):
                raise SpiceValueError(f"{full}: invalid parameter name {k!r}")
            if isinstance(v, str) and v.strip().startswith("{"):
                ptxt += f" {k}={v.strip()}"
            else:
                ptxt += f" {k}={fmt_value(_num(f'{full}.{k}', v))}"
        self._add(_Element("X", full, ns, model=key, params=ptxt))

    # -- models -----------------------------------------------------------------------------
    def use_model(self, key: str) -> None:
        """Add the ``.model``/``.subckt`` text of library model ``key`` (once)."""
        from piforge.spice import models as _lib

        k = str(key).lower()
        if k not in self._models:
            self.add_model_text(_lib.model_text(k))

    def add_model_text(self, text: str) -> None:
        """Add raw ``.model``/``.subckt`` text (own models); names become usable by elements."""
        defs = _parse_definitions(text)
        if not defs:
            raise SpiceValueError("model text defines no .model or .subckt")
        if text in self._model_texts:
            return
        clash = sorted(set(defs) & set(self._models))
        if clash:
            raise SpiceValueError(f"model(s) {clash} already defined with different text")
        self._model_texts.append(text)
        self._models.update(defs)

    # -- analyses & output ------------------------------------------------------------------
    def op(self) -> None:
        """DC operating point."""
        self._analyses.append("op")

    def dc(self, src: str, start: float, stop: float, step: float) -> None:
        """DC sweep of a V/I source (by element name, e.g. ``"V1"`` or ``"1"``)."""
        wanted = str(src).lower()
        names = {e.name.lower(): e.name for e in self._elements if e.prefix in ("V", "I")}
        full = names.get(wanted) or names.get("v" + wanted) or names.get("i" + wanted)
        if full is None:
            raise SpiceValueError(f"dc sweep source {src!r} not found; sources: {sorted(names.values())}")
        a, b, s = _num("dc start", start), _num("dc stop", stop), _num("dc step", step)
        if s == 0 or (b - a) / s < 0 or abs((b - a) / s) > 1e6:
            raise SpiceValueError(f"dc sweep {a}→{b} with step {s} is invalid (sign or > 1e6 points)")
        self._analyses.append(f"dc {full} {fmt_value(a)} {fmt_value(b)} {fmt_value(s)}")

    def tran(self, step: float, stop: float, *, start: float = 0.0, uic: bool = False,
             tmax: float | None = None) -> None:
        """Transient analysis (s): print ``step``, end ``stop``; ``tmax`` caps the internal step."""
        st, sp, t0 = _num("tran step", step), _num("tran stop", stop), _num("tran start", start)
        if st <= 0 or sp <= 0 or t0 < 0 or t0 >= sp:
            raise SpiceValueError(f"tran needs step > 0 and 0 <= start < stop (step={step}, stop={stop})")
        cmd = f"tran {fmt_value(st)} {fmt_value(sp)}"
        if t0 or tmax is not None:
            cmd += f" {fmt_value(t0)}"
        if tmax is not None:
            tm = _num("tran tmax", tmax)
            if tm <= 0:
                raise SpiceValueError("tran tmax must be > 0")
            cmd += f" {fmt_value(tm)}"
        if uic:
            cmd += " uic"
        self._analyses.append(cmd)

    def ac(self, points: int, fstart: float, fstop: float, *, sweep: str = "dec") -> None:
        """Small-signal AC sweep (Hz); ``sweep`` = ``dec``/``oct`` (points per decade/octave) or ``lin``."""
        sw = str(sweep).lower()
        if sw not in ("dec", "oct", "lin"):
            raise SpiceValueError(f"ac sweep must be dec, oct or lin, got {sweep!r}")
        if isinstance(points, bool) or not isinstance(points, int) or points < 1:
            raise SpiceValueError(f"ac points must be a positive int, got {points!r}")
        f0, f1 = _num("ac fstart", fstart), _num("ac fstop", fstop)
        if not 0 < f0 < f1:
            raise SpiceValueError(f"ac needs 0 < fstart < fstop, got {f0}, {f1}")
        self._analyses.append(f"ac {sw} {points} {fmt_value(f0)} {fmt_value(f1)}")

    def measure(self, name: str, spec: str) -> None:
        """Add ``.meas <analysis> <name> <rest>``, e.g. ``measure("vtau", "tran FIND v(out) AT=1m")``."""
        if not _MEAS_NAME_RE.match(str(name)):
            raise SpiceValueError(f"invalid measure name {name!r}: letters, digits, '_' only")
        key = str(name).lower()
        if key in self._measures:
            raise SpiceValueError(f"duplicate measure name {name!r}")
        toks = str(spec).split(None, 1)
        if len(toks) != 2 or toks[0].lower() not in ("tran", "ac", "dc", "sp") or "\n" in spec:
            raise SpiceValueError(f"measure spec must start with tran/ac/dc/sp, got {spec!r}")
        self._measures[key] = f".meas {toks[0].lower()} {key} {toks[1].strip()}"

    def save(self, *vectors: str) -> None:
        """Restrict saved vectors (``.save``); include ``"all"`` to keep every node as well."""
        for v in vectors:
            s = str(v).strip()
            if not s or any(ch.isspace() for ch in s):
                raise SpiceValueError(f"invalid save vector {v!r}")
            self._saves.append(s)

    def option(self, **opts: Any) -> None:
        """``.options`` entries, e.g. ``option(reltol=1e-4, method="gear")``."""
        for k, v in opts.items():
            self._options[k] = fmt_value(v) if _is_number(v) else str(v)

    def ic(self, node: Any, voltage: float) -> None:
        """Initial node voltage for ``tran(..., uic=True)``."""
        self._ics[self._node(node)] = fmt_value(_num(f"ic {node}", voltage))

    def param(self, name: str, value: float | str) -> None:
        """``.param name=value`` (use as ``"{name}"`` in element values)."""
        if not _MEAS_NAME_RE.match(str(name)):
            raise SpiceValueError(f"invalid parameter name {name!r}")
        self._params[str(name)] = fmt_value(value) if _is_number(value) else str(value)

    def raw(self, line: str) -> None:
        """Append one raw netlist line (escape hatch; not validated)."""
        if "\n" in str(line):
            raise SpiceValueError("raw() takes a single line")
        self._raw.append(str(line))

    # -- introspection ----------------------------------------------------------------------
    @property
    def analyses(self) -> list[str]:
        """Analysis commands in order, without the leading dot (e.g. ``"tran 1u 3m"``)."""
        return list(self._analyses)

    @property
    def measure_names(self) -> list[str]:
        """Lower-case names of the ``.meas`` statements."""
        return list(self._measures)

    @property
    def nodes(self) -> set[str]:
        """All node names used by elements."""
        return {n for e in self._elements for n in e.nodes}

    # -- rendering --------------------------------------------------------------------------
    def _render(self, e: _Element) -> str:
        if e.prefix in ("R", "C", "L", "V", "I", "B"):
            return f"{e.name} {' '.join(e.nodes)} {e.tail}"
        mtype = self._models[e.model][0]
        nodes = list(e.nodes)
        if e.prefix == "M" and mtype != "VDMOS":
            nodes.append(e.body or e.nodes[2])
        elif e.prefix == "M" and e.body not in (None, e.nodes[2]):
            raise SpiceValueError(f"{e.name}: {e.model} is a VDMOS (3-terminal) model; omit b=")
        elif e.prefix == "X" and self._models[e.model][1] != len(nodes):
            raise SpiceValueError(f"{e.name}: subckt {e.model} has {self._models[e.model][1]} pins, "
                                  f"got {len(nodes)} nodes")
        return f"{e.name} {' '.join(nodes)} {e.model}{e.params}"

    def _validate(self) -> None:
        if not self._elements:
            raise SpiceValueError(f"circuit {self.title!r} has no elements")
        if not self._analyses:
            raise SpiceValueError(f"circuit {self.title!r} has no analysis: call op(), dc(), tran() or ac()")
        if "0" not in self.nodes:
            raise SpiceValueError(f"circuit {self.title!r} has no ground node '0' (or 'gnd')")
        from piforge.spice import models as _lib

        for e in self._elements:
            if e.model and e.model not in self._models:
                hint = suggest(e.model, list(_lib.MODELS) + list(self._models))
                raise SpiceValueError(f"{e.name}: unknown model {e.model!r}"
                                      + (f"; did you mean {', '.join(hint)}?" if hint else
                                         "; add it with add_model_text()"))
            if e.model and self._models[e.model][0] not in _MODEL_TYPES[e.prefix]:
                raise SpiceValueError(f"{e.name}: model {e.model!r} has the wrong type "
                                      f"({self._models[e.model][0]}) for a {e.prefix} element")

    def to_netlist(self) -> str:
        """Validate and render the complete netlist (title line … ``.end``)."""
        self._validate()
        lines = [self.title, f"* generated by piforge.spice: {len(self._elements)} elements"]
        if self._options:
            lines.append(".options " + " ".join(f"{k}={v}" for k, v in self._options.items()))
        lines += [f".param {k}={v}" for k, v in self._params.items()]
        for text in self._model_texts:
            lines += [ln.rstrip() for ln in text.strip().splitlines()]
        for e in self._elements:
            lines += _wrap(self._render(e))
        if self._ics:
            lines.append(".ic " + " ".join(f"v({n})={v}" for n, v in self._ics.items()))
        lines += self._raw
        if self._saves:
            lines.append(".save " + " ".join(self._saves))
        lines += list(self._measures.values())
        lines += ["." + a for a in self._analyses]
        lines.append(".end")
        return "\n".join(lines) + "\n"

    def __repr__(self) -> str:
        return (f"SpiceCircuit({self.title!r}: {len(self._elements)} elements, "
                f"analyses={self._analyses})")
