"""Configuration: ``config.toml`` next to ``main.py`` (or ``$MONEY_COUNTER_CONFIG``), with defaults.

Every value is validated here so the rest of the firmware can trust it; a bad file stops the
firmware at start with a clear message (``ConfigError``).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

N_MODULES = 8          # 000000,00
DECIMALS = 2


class ConfigError(ValueError):
    """config.toml is missing a value or holds a wrong one."""


@dataclass(frozen=True)
class NetworkConfig:
    mode: str = "client"
    url: str = "ws://127.0.0.1:8765/"
    json_path: str = "amount"
    reconnect_min_s: float = 0.5
    reconnect_max_s: float = 10.0
    listen_host: str = "0.0.0.0"
    ws_port: int = 8765
    http_port: int = 8080


@dataclass(frozen=True)
class DisplayConfig:
    max_amount: float = 999999.99
    count_time_min: float = 0.25
    count_time_max: float = 3.0


@dataclass(frozen=True)
class MotionConfig:
    steps_per_rev: int = 4096
    flaps: int = 20
    max_pps: float = 850.0
    start_pps: float = 450.0
    accel: float = 3000.0
    hold_ms: float = 60.0
    spi_bus: int = 0
    spi_device: int = 0
    spi_speed_hz: int = 1_000_000
    home_timeout_s: float = 12.0
    resync_tolerance: int = 24
    selftest: bool = True
    hall_pins: tuple[int, ...] = (4, 5, 6, 13, 16, 19, 20, 21)
    offsets: tuple[int, ...] = (0,) * N_MODULES


@dataclass(frozen=True)
class Config:
    network: NetworkConfig = field(default_factory=NetworkConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    motion: MotionConfig = field(default_factory=MotionConfig)


def default_path() -> Path:
    env = os.environ.get("MONEY_COUNTER_CONFIG")
    return Path(env) if env else Path(__file__).resolve().parent.parent / "config.toml"


def _section(cls, raw: dict, name: str):
    known = {f for f in cls.__dataclass_fields__}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"[{name}]: unknown key(s) {sorted(unknown)}; known: {sorted(known)}")
    values = {}
    for key, value in raw.items():
        default = getattr(cls(), key)
        if isinstance(default, tuple):
            if not isinstance(value, list) or not all(isinstance(v, int) and not isinstance(v, bool) for v in value):
                raise ConfigError(f"[{name}].{key} must be a list of integers, got {value!r}")
            value = tuple(value)
        elif isinstance(default, bool) or isinstance(default, str):
            if not isinstance(value, type(default)):
                raise ConfigError(f"[{name}].{key} must be a {type(default).__name__}, got {value!r}")
        elif isinstance(default, int):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(f"[{name}].{key} must be an integer, got {value!r}")
        elif isinstance(default, float):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(f"[{name}].{key} must be a number, got {value!r}")
            value = float(value)
        values[key] = value
    return cls(**values)


def validate(cfg: Config) -> Config:
    n, d, m = cfg.network, cfg.display, cfg.motion
    if n.mode not in ("client", "server"):
        raise ConfigError(f"[network].mode must be 'client' or 'server', got {n.mode!r}")
    if not 0 < n.reconnect_min_s <= n.reconnect_max_s:
        raise ConfigError("[network]: need 0 < reconnect_min_s <= reconnect_max_s")
    for key in ("ws_port", "http_port"):
        if not 0 < getattr(n, key) < 65536:
            raise ConfigError(f"[network].{key} must be 1…65535")
    if not 0 < d.max_amount <= 999999.99:
        raise ConfigError("[display].max_amount must be in (0, 999999.99] (6 + 2 digits)")
    if not 0 < d.count_time_min <= d.count_time_max:
        raise ConfigError("[display]: need 0 < count_time_min <= count_time_max")
    if m.flaps % 10 or m.flaps < 10:
        raise ConfigError("[motion].flaps must be a multiple of 10")
    if m.steps_per_rev < m.flaps * 10:
        raise ConfigError("[motion].steps_per_rev is too small")
    if not 50 <= m.start_pps <= m.max_pps <= 3000:
        raise ConfigError("[motion]: need 50 <= start_pps <= max_pps <= 3000 half-steps/s "
                          "(5 V 28BYJ-48: pull-in ≈ 500, pull-out ≈ 950)")
    if not 100 <= m.accel <= 100_000:
        raise ConfigError("[motion].accel must be 100…100000 half-steps/s²")
    if not 0 <= m.resync_tolerance < m.steps_per_rev // 10:
        raise ConfigError("[motion].resync_tolerance must be 0 … steps_per_rev/10")
    if m.home_timeout_s * m.start_pps < m.steps_per_rev:
        raise ConfigError("[motion].home_timeout_s must cover one spool turn at start_pps")
    if len(m.hall_pins) != N_MODULES or len(set(m.hall_pins)) != N_MODULES or \
            not all(0 <= p <= 27 for p in m.hall_pins):
        raise ConfigError(f"[motion].hall_pins must be {N_MODULES} different BCM pins 0…27")
    if len(m.offsets) != N_MODULES or not all(0 <= o < m.steps_per_rev for o in m.offsets):
        raise ConfigError(f"[motion].offsets must be {N_MODULES} values in [0, steps_per_rev)")
    return cfg


def load(path: Path | str | None = None) -> Config:
    """Read and validate the config file; a missing file gives the defaults."""
    p = Path(path) if path is not None else default_path()
    raw: dict = {}
    if p.is_file():
        try:
            raw = tomllib.loads(p.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{p}: {exc}") from None
    unknown = set(raw) - {"network", "display", "motion"}
    if unknown:
        raise ConfigError(f"{p}: unknown section(s) {sorted(unknown)}")
    cfg = Config(network=_section(NetworkConfig, raw.get("network", {}), "network"),
                 display=_section(DisplayConfig, raw.get("display", {}), "display"),
                 motion=_section(MotionConfig, raw.get("motion", {}), "motion"))
    return validate(cfg)


def client_url(cfg: Config) -> str:
    """The URL the client connects to: ``$MONEY_COUNTER_URL`` wins over the config file."""
    return os.environ.get("MONEY_COUNTER_URL") or cfg.network.url
