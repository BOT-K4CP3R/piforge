"""Findings and reports — the common output of every PiForge check.

Every validator (ERC, printability, assembly, SPICE, twin scenarios, thermal…) returns a
:class:`Report` made of :class:`Finding` objects. Codes are dotted upper-case identifiers whose
first segment names the subsystem, e.g. ``ERC.LEVEL_MISMATCH`` or ``PRINT.OVERHANG``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import PurePath
from typing import Any


class Severity(IntEnum):
    """How bad a finding is. ERROR means the design will not work or cannot be built."""

    INFO = 0
    WARNING = 1
    ERROR = 2

    @property
    def label(self) -> str:
        return self.name.lower()

    @classmethod
    def parse(cls, value: "Severity | int | str") -> "Severity":
        """Accept a Severity, its int value, or a name such as ``"error"``/``"warn"``."""
        if isinstance(value, Severity):
            return value
        if isinstance(value, int):
            return cls(value)
        key = str(value).strip().upper()
        aliases = {"WARN": "WARNING", "ERR": "ERROR", "E": "ERROR", "W": "WARNING", "I": "INFO"}
        key = aliases.get(key, key)
        try:
            return cls[key]
        except KeyError:
            raise ValueError(f"Unknown severity {value!r}; use info, warning or error") from None


def jsonable(value: Any) -> Any:
    """Convert numpy scalars/arrays, tuples, sets, paths and dataclass-free objects to JSON types."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, IntEnum):
        return int(value)
    if isinstance(value, PurePath):
        return str(value)
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in value]
    # numpy without importing it eagerly
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        return jsonable(tolist())
    item = getattr(value, "item", None)
    if callable(item):
        return jsonable(item())
    return str(value)


@dataclass
class Finding:
    """One observation about a design."""

    code: str
    severity: Severity
    message: str
    subject: str = ""
    data: dict = field(default_factory=dict)
    hint: str = ""
    source: str = ""  # title of the report that produced it (filled by Report.merge)

    def __post_init__(self) -> None:
        if not self.code or not isinstance(self.code, str):
            raise ValueError("Finding.code must be a non-empty string like 'ERC.LEVEL_MISMATCH'")
        self.severity = Severity.parse(self.severity)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity.label,
            "message": self.message,
            "subject": self.subject,
            "data": jsonable(self.data),
            "hint": self.hint,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Finding":
        return cls(
            code=d["code"],
            severity=Severity.parse(d["severity"]),
            message=d.get("message", ""),
            subject=d.get("subject", ""),
            data=dict(d.get("data") or {}),
            hint=d.get("hint", ""),
            source=d.get("source", ""),
        )

    def __str__(self) -> str:
        subj = f" [{self.subject}]" if self.subject else ""
        return f"{self.severity.name}: {self.code}{subj}: {self.message}"


_ICON = {Severity.ERROR: "❌", Severity.WARNING: "⚠️", Severity.INFO: "ℹ️"}


@dataclass
class Report:
    """An ordered collection of findings with helpers for summarising and serialising."""

    title: str = "report"
    findings: list[Finding] = field(default_factory=list)

    # -- building -------------------------------------------------------------------------
    def add(
        self,
        code: str,
        severity: Severity | int | str,
        message: str,
        subject: str = "",
        hint: str = "",
        **data: Any,
    ) -> Finding:
        """Create, append and return a finding. Extra keyword arguments go into ``data``."""
        f = Finding(code=code, severity=Severity.parse(severity), message=message,
                    subject=subject, data=data, hint=hint, source=self.title)
        self.findings.append(f)
        return f

    def extend(self, items: "Iterable[Finding] | Report") -> "Report":
        """Append findings from an iterable or another report; returns self for chaining."""
        src = items.findings if isinstance(items, Report) else items
        for f in src:
            if not isinstance(f, Finding):
                raise TypeError(f"Report.extend expects Finding objects, got {type(f).__name__}")
            if not f.source:
                f.source = items.title if isinstance(items, Report) else self.title
            self.findings.append(f)
        return self

    @classmethod
    def merge(cls, *reports: "Report", title: str = "report") -> "Report":
        """Combine several reports; each finding keeps its original report title in ``source``."""
        out = cls(title=title)
        for r in reports:
            if r is None:
                continue
            for f in r.findings:
                if not f.source:
                    f.source = r.title
                out.findings.append(f)
        return out

    # -- querying -------------------------------------------------------------------------
    def __iter__(self) -> Iterator[Finding]:
        return iter(self.findings)

    def _of(self, sev: Severity) -> list[Finding]:
        return [f for f in self.findings if f.severity == sev]

    @property
    def errors(self) -> list[Finding]:
        return self._of(Severity.ERROR)

    @property
    def warnings(self) -> list[Finding]:
        return self._of(Severity.WARNING)

    @property
    def infos(self) -> list[Finding]:
        return self._of(Severity.INFO)

    @property
    def ok(self) -> bool:
        """True when there is no ERROR finding."""
        return not self.errors

    @property
    def worst(self) -> Severity | None:
        return max((f.severity for f in self.findings), default=None)

    def counts(self) -> dict[str, int]:
        return {s.label: len(self._of(s)) for s in (Severity.ERROR, Severity.WARNING, Severity.INFO)}

    def by_code(self, code: str) -> list[Finding]:
        """Findings whose code equals ``code`` or starts with ``code + '.'``."""
        return [f for f in self.findings if f.code == code or f.code.startswith(code + ".")]

    def has(self, code: str, severity: Severity | int | str | None = None) -> bool:
        hits = self.by_code(code)
        if severity is not None:
            sev = Severity.parse(severity)
            hits = [f for f in hits if f.severity == sev]
        return bool(hits)

    # -- serialising ----------------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "ok": self.ok,
            "counts": self.counts(),
            "findings": [f.to_dict() for f in self.findings],
        }

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> "Report":
        return cls(title=d.get("title", "report"),
                   findings=[Finding.from_dict(x) for x in d.get("findings", [])])

    def to_markdown(self, *, include_info: bool = True) -> str:
        c = self.counts()
        status = "PASS" if self.ok else "FAIL"
        lines = [f"## {self.title} — {status}", "",
                 f"{c['error']} error(s), {c['warning']} warning(s), {c['info']} info", ""]
        ordered = sorted(self.findings, key=lambda f: (-int(f.severity), f.source, f.code))
        for f in ordered:
            if f.severity == Severity.INFO and not include_info:
                continue
            subj = f" `{f.subject}`" if f.subject else ""
            src = f" _({f.source})_" if f.source and f.source != self.title else ""
            lines.append(f"- {_ICON[f.severity]} **{f.code}**{subj}: {f.message}{src}")
            if f.hint:
                lines.append(f"  - fix: {f.hint}")
        return "\n".join(lines) + "\n"

    def __str__(self) -> str:
        c = self.counts()
        return f"Report({self.title!r}: {c['error']}E {c['warning']}W {c['info']}I)"
