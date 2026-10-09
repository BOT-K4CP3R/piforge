"""``micropython`` shim: ``const`` and no-op code-emitter decorators (as in Blinka)."""

from __future__ import annotations

from typing import Any, TypeVar

T = TypeVar("T")


def const(x: T) -> T:
    """Compile-time constant marker; returns ``x``."""
    return x


def native(f: Any) -> Any:
    """No-op decorator."""
    return f


def viper(f: Any) -> Any:
    """No-op decorator."""
    return f


def asm_thumb(f: Any) -> Any:
    """No-op decorator."""
    return f
