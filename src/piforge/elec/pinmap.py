"""Pin allocator: assigns BCM GPIOs to requirements (interfaces first, plain GPIOs last).

Fixed-function interfaces get their hardware pins (I2C1 2/3, SPI0 10/9/11 + CE 8/7, SPI1 20/19/21 +
CE 18/17/16, UART0 14/15, PCM 18-21); hardware PWM uses distinct channels from the board's
``pwm_channels`` table (BCM boards: PWM0_0 on 12|18, PWM0_1 on 13|19; Pi 5: four channels on
12/13/18/19). Plain GPIOs are taken from pins without important alternate functions first. The result
carries the ``config.txt`` lines from :func:`piforge.elec.bootconfig.config_lines`.
"""

from __future__ import annotations

from dataclasses import dataclass

from piforge.core.errors import PiForgeError, ValidationError
from piforge.elec.bootconfig import InterfaceUse, config_lines

KINDS = ("gpio_in", "gpio_out", "pwm", "i2c", "spi", "uart", "onewire", "pcm")
# hardware interfaces first (they need specific pins), then bit-banged buses, then plain GPIOs
_PRIORITY = {"pcm": 0, "i2c": 1, "spi": 2, "uart": 3, "pwm": 4, "onewire": 5, "soft_i2c": 6, "gpio_in": 7,
             "gpio_out": 7}
# plain GPIOs first (SDIO/JTAG-only alternates), then pins whose alternate functions are often needed
# (GPIO4 = default 1-Wire/GPCLK0/UART3, 12/13/18/19 = PWM, 14/15 = UART0, 7-11 = SPI0, 2/3 = I2C1)
GENERAL_ORDER = (17, 27, 22, 23, 24, 25, 26, 5, 6, 16, 4, 13, 12, 19, 20, 21, 18, 14, 15, 7, 8, 9, 10, 11, 2, 3, 0, 1)


class AllocationError(PiForgeError):
    """The requirements cannot be met on this board (not enough pins/channels/interfaces)."""


@dataclass
class Requirement:
    """``kind`` in gpio_in, gpio_out, pwm (hardware), i2c, spi (``count`` = chip selects), uart, onewire, pcm."""

    kind: str
    count: int = 1
    label: str = ""
    prefer: tuple[int, ...] = ()


@dataclass
class PinAllocation:
    """``assignments``: label -> BCM pins (SPI: MOSI, MISO, SCLK, CE…; I2C: SDA, SCL; UART: TX, RX)."""

    assignments: dict[str, tuple[int, ...]]
    interfaces: tuple[str, ...]
    config_lines: tuple[str, ...]
    free: tuple[int, ...]


def allocate_pins(board: str, requirements: list[Requirement], *, reserved: tuple[int, ...] = (0, 1)) -> PinAllocation:
    """Assign BCM pins on ``board`` (library key, e.g. ``"rpi4b"``) to ``requirements``."""
    from piforge.elec.library import get_def

    d = get_def(board)
    if d.category != "board":
        raise ValidationError(f"{board!r} is not a board (use rpi5, rpi4b, rpi3bp or rpizero2w)")
    for r in requirements:
        if r.kind not in KINDS:
            raise ValidationError(f"Requirement kind {r.kind!r}: use one of {', '.join(KINDS)}")
        if not isinstance(r.count, int) or r.count < (1 if r.kind in ("spi", "pwm", "onewire") else 0):
            raise ValidationError(f"Requirement {r.kind!r}: invalid count {r.count!r}")
    labels: list[str] = []
    for r in requirements:
        base = r.label or r.kind
        lab, n = base, 2
        while lab in labels:
            lab, n = f"{base}_{n}", n + 1
        labels.append(lab)

    taken = set(int(p) for p in reserved)
    uses: list[InterfaceUse] = []
    result: dict[int, tuple[int, ...]] = {}
    counters = {"i2c": 0, "soft_i2c": 0, "spi": 0, "uart": 0}

    def free(p: int) -> bool:
        return 0 <= p <= 27 and p not in taken

    def need(pins: tuple[int, ...], what: str) -> tuple[int, ...]:
        busy = [p for p in pins if not free(p)]
        if busy:
            raise AllocationError(f"{what} needs GPIO {', '.join(map(str, busy))}, already used or reserved")
        taken.update(pins)
        return pins

    def plain(n: int, prefer: tuple[int, ...], what: str) -> tuple[int, ...]:
        got: list[int] = []
        for p in tuple(prefer) + GENERAL_ORDER:
            if len(got) == n:
                break
            if free(p) and p not in got:
                got.append(p)
        if len(got) < n:
            raise AllocationError(f"{what} needs {n} GPIOs but only {len(got)} are free on {d.key}")
        taken.update(got)
        return tuple(got)

    prio, n_i2c = [], 0
    for r in requirements:  # only the first I2C bus can be the hardware I2C1
        kind = r.kind
        if kind == "i2c":
            kind = "i2c" if n_i2c == 0 else "soft_i2c"
            n_i2c += 1
        prio.append(_PRIORITY[kind])
    order = sorted(range(len(requirements)), key=lambda i: (prio[i], i))
    for i in order:
        r, lab = requirements[i], labels[i]
        if r.kind == "pcm":
            result[i] = need((18, 19, 20, 21), "PCM/I2S")
            uses.append(InterfaceUse("pcm", result[i]))
        elif r.kind == "i2c":
            if counters["i2c"] == 0 and free(2) and free(3):
                result[i] = need((2, 3), "I2C1")
                uses.append(InterfaceUse("i2c1", result[i]))
            else:
                result[i] = plain(2, r.prefer, f"software I2C '{lab}'")
                # i2c-gpio: first software bus as /dev/i2c-3 (overlays README "bus" parameter)
                uses.append(InterfaceUse("i2c_gpio", result[i], {"bus": 3 + counters["soft_i2c"]}))
                counters["soft_i2c"] += 1
            counters["i2c"] += 1
        elif r.kind == "spi":
            if r.count > 3:
                raise AllocationError(f"SPI '{lab}': {r.count} chip selects; the header offers at most 3 (SPI1)")
            spi0_ok = r.count <= 2 and all(free(p) for p in (10, 9, 11) + (8, 7)[:r.count])
            if counters["spi"] == 0 and spi0_ok:
                ce = (8, 7)[:r.count]
                result[i] = need((10, 9, 11) + ce, "SPI0")
                uses.append(InterfaceUse("spi0", result[i], {"cs_pins": ce}))
            else:
                ce = (18, 17, 16)[:r.count]
                result[i] = need((20, 19, 21) + ce, "SPI1")
                uses.append(InterfaceUse("spi1", result[i], {"cs_pins": ce}))
            counters["spi"] += 1
        elif r.kind == "uart":
            uarts = d.params.get("uarts", {"uart0": (14, 15)})
            for name, pins in uarts.items():
                if all(free(p) for p in pins) and name not in {u.name for u in uses}:
                    result[i] = need(tuple(pins), name.upper())
                    uses.append(InterfaceUse(name, result[i]))
                    break
            else:
                raise AllocationError(f"UART '{lab}': no free UART on {d.key} (available: {', '.join(uarts)})")
        elif r.kind == "pwm":
            chans = d.params.get("pwm_channels", {})
            used_ch = {ch for u in uses if u.name == "pwm" for ch, cand in chans.items() if set(cand) & set(u.pins)}
            if r.count > len(chans) - len(used_ch):
                raise AllocationError(f"PWM '{lab}': {r.count} hardware PWM channels requested, {d.key} has "
                                      f"{len(chans)} on the header ({', '.join(chans)})")
            got: list[int] = []
            for ch, cand in chans.items():
                if len(got) == r.count or ch in used_ch:
                    continue
                pick = [p for p in r.prefer if p in cand and free(p)] or [p for p in cand if free(p)]
                if pick:
                    got.append(pick[0])
                    taken.add(pick[0])
            if len(got) < r.count:
                raise AllocationError(f"PWM '{lab}': only {len(got)} hardware PWM channel(s) have a free pin")
            result[i] = tuple(got)
            prev = next((u for u in uses if u.name == "pwm"), None)
            if prev:
                prev.pins = prev.pins + result[i]
            else:
                uses.append(InterfaceUse("pwm", result[i]))
        elif r.kind == "onewire":
            result[i] = plain(r.count, tuple(r.prefer) + (4,), f"1-Wire '{lab}'")
            prev = next((u for u in uses if u.name == "onewire"), None)
            if prev:
                prev.pins = prev.pins + result[i]
            else:
                uses.append(InterfaceUse("onewire", result[i]))
        else:
            result[i] = plain(r.count, r.prefer, f"{r.kind} '{lab}'")

    assignments = {labels[i]: result[i] for i in range(len(requirements))}
    return PinAllocation(
        assignments=assignments,
        interfaces=tuple(u.name for u in uses),
        config_lines=tuple(config_lines(d, uses)),
        free=tuple(p for p in range(28) if p not in taken),
    )
