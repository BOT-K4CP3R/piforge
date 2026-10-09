"""Exception hierarchy for PiForge.

Design problems (a blocked port, a 5 V signal on a 3.3 V pin, an overhang) are NOT exceptions —
they are reported as :class:`piforge.core.report.Finding`. Exceptions are for usage errors:
unknown names, invalid arguments, missing external tools.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable


class PiForgeError(Exception):
    """Base class for every usage/programming error raised by PiForge."""


class ValidationError(PiForgeError, ValueError):
    """An argument is outside its valid range or has the wrong form."""


class ToolNotFoundError(PiForgeError):
    """A required external program (ngspice, a slicer, …) is not installed."""


def suggest(name: str, candidates: Iterable[str], n: int = 3, cutoff: float = 0.5) -> list[str]:
    """Return up to ``n`` candidates that look like ``name`` (case-insensitive)."""
    cands = list(candidates)
    lowered = {c.lower(): c for c in cands}
    hits = difflib.get_close_matches(name.lower(), list(lowered), n=n, cutoff=cutoff)
    return [lowered[h] for h in hits]


class NotFoundError(PiForgeError, LookupError):
    """A named thing (printer, board, pin, part, model…) does not exist.

    The message lists close matches, or the available names when nothing is close.
    """

    def __init__(self, kind: str, name: object, candidates: Iterable[str] = ()):
        self.kind = kind
        self.name = name
        cands = sorted(str(c) for c in candidates)
        self.suggestions = suggest(str(name), cands)
        msg = f"Unknown {kind} {name!r}."
        if self.suggestions:
            msg += f" Did you mean: {', '.join(self.suggestions)}?"
        elif cands:
            shown = ", ".join(cands[:25])
            more = f" (+{len(cands) - 25} more)" if len(cands) > 25 else ""
            msg += f" Available: {shown}{more}."
        super().__init__(msg)

    def __str__(self) -> str:  # LookupError would otherwise repr() the message
        return self.args[0]
