"""Virtual ``/sys/bus/w1/devices`` for DS18B20 firmware that reads sysfs files directly.

Classic code (``glob.glob('/sys/bus/w1/devices/28*')`` + ``open(.../w1_slave)``) works unmodified:
inside the runner, :func:`install` redirects paths under ``/sys/bus/w1/`` to a temporary mirror
and serves ``w1_slave`` / ``temperature`` with live values from the twin's ``ds18b20`` devices.
Only that path prefix is touched; everything else goes to the real functions. Nothing happens
at import time.
"""

from __future__ import annotations

import builtins
import io
import os
import tempfile
from pathlib import Path
from typing import Any

PREFIX = "/sys/bus/w1"


def install(twin: Any) -> Path | None:
    """Patch ``open``/``os.listdir``/``os.scandir``/``os.stat``… for ``/sys/bus/w1``; returns the mirror root."""
    devices = [d for d in twin.devices.values() if d.type == "ds18b20"]
    if not devices:
        return None
    root = Path(tempfile.mkdtemp(prefix="piforge-w1-"))
    base = root / "sys" / "bus" / "w1" / "devices"
    master = base / "w1_bus_master1"
    master.mkdir(parents=True)
    by_rom = {}
    for dev in devices:
        (base / dev.rom).mkdir()
        for name in ("w1_slave", "temperature", "name"):
            (base / dev.rom / name).write_text("", encoding="ascii")
        by_rom[dev.rom] = dev
    (master / "w1_master_slaves").write_text("\n".join(by_rom) + "\n", encoding="ascii")

    def rewrite(p: Any) -> Any:
        try:
            s = os.fsdecode(os.fspath(p))
        except TypeError:
            return p
        if s == PREFIX or s.startswith(PREFIX + "/"):
            return str(root) + s
        return p

    def live(p: Any) -> str | None:
        try:
            s = os.fsdecode(os.fspath(p))
        except TypeError:
            return None
        parts = s.rstrip("/").split("/")
        if len(parts) >= 2 and s.startswith(PREFIX + "/devices/") and parts[-2] in by_rom:
            dev = by_rom[parts[-2]]
            if parts[-1] == "w1_slave":
                return dev.w1_slave()
            if parts[-1] == "temperature":
                return f"{dev.millicelsius()}\n"
            if parts[-1] == "name":
                return dev.rom + "\n"
        return None

    real_open = builtins.open

    def patched_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if isinstance(file, (str, bytes, os.PathLike)):
            text = live(file)
            if text is not None and not any(c in mode for c in "wax+"):
                return io.BytesIO(text.encode("ascii")) if "b" in mode else io.StringIO(text)
            file = rewrite(file)
        return real_open(file, mode, *args, **kwargs)

    builtins.open = patched_open
    io.open = patched_open                          # pathlib.Path.open / read_text use io.open

    for name in ("listdir", "scandir", "stat", "lstat"):
        real = getattr(os, name)

        def wrapper(path: Any = ".", *args: Any, _real: Any = real, **kwargs: Any) -> Any:
            return _real(rewrite(path), *args, **kwargs)
        setattr(os, name, wrapper)
    for name in ("exists", "isdir", "isfile", "lexists"):
        real = getattr(os.path, name)

        def pwrapper(path: Any, _real: Any = real) -> Any:
            return _real(rewrite(path))
        setattr(os.path, name, pwrapper)
    return root
