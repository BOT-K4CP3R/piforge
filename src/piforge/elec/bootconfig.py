"""Boot ``config.txt`` lines for the interfaces a circuit uses, plus interface inference.

Interfaces are inferred from the wiring (module pins tagged ``I2C_SDA``, ``SPI_MOSI``, ``UART_TX``,
``ONEWIRE``… connected to board GPIOs) and from ``Circuit.configure(pi, interfaces=...)``.

Sources: Raspberry Pi overlays README (raspberrypi/firmware boot/overlays/README): base DTB params
``i2c_arm``, ``i2c_arm_baudrate`` (default 100000), ``spi``, ``i2s``; overlays ``spi0-1cs``,
``spi1-1cs/2cs/3cs`` (default CS 18/17/16), ``w1-gpio`` (gpiopin, default 4), ``pwm``/``pwm-2chan``
(PWM0: 12,4(Alt0) 18,2(Alt5); PWM1: 13,4(Alt0) 19,2(Alt5)), ``pwm-pio`` (Pi 5 only), ``i2c-gpio``,
``uart2..5`` (Pi 4) and ``uartN-pi5``; overlay_map.dts maps w1-gpio/uart0/disable-bt to their -pi5
variants. UART: documentation "Configure UARTs" (enable_uart=1 fixes the mini-UART core clock on
Pi 3/4/Zero 2 W; Pi 5 exposes UART0 on GPIO14/15 via uart0-pi5).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from piforge.core.report import Severity
from piforge.elec.model import Circuit, Part, PartDef, PinRef

ROLE_FUNCS = ("I2C_SDA", "I2C_SCL", "SPI_MOSI", "SPI_MISO", "SPI_SCLK", "SPI_CS", "UART_TX", "UART_RX",
              "ONEWIRE", "PWM_IN")


@dataclass
class InterfaceUse:
    """An enabled interface: ``name`` (i2c1, i2c_gpio, spi0, spi1, uart0, uartN, onewire, pwm, pcm, camera)."""

    name: str
    pins: tuple[int, ...] = ()
    params: dict = field(default_factory=dict)


def _bcm(ref: PinRef) -> int | None:
    for f in ref.pin.functions:
        if f.startswith("BCM"):
            return int(f[3:])
    return None


def _roles(c: Circuit, board: Part) -> list[tuple[int, str, PinRef, PinRef]]:
    """(bcm, role, module pin, board pin) for every role-tagged module pin wired to a board GPIO."""
    out = []
    for net in c.nets:
        gpios = [r for r in net.refs if r.part is board and _bcm(r) is not None]
        if not gpios:
            continue
        for r in net.refs:
            if r.part is board:
                continue
            for f in r.pin.functions:
                if f in ROLE_FUNCS:
                    out.append((_bcm(gpios[0]), f, r, gpios[0]))
    return out


def _used_pins(c: Circuit, board: Part) -> set[int]:
    used = set()
    for net in c.nets:
        if any(r.part is not board for r in net.refs):
            for r in net.refs:
                if r.part is board and _bcm(r) is not None:
                    used.add(_bcm(r))
    return used


def infer_interfaces(c: Circuit, board: Part) -> list[InterfaceUse]:
    """Interfaces needed by the circuit (wiring + ``configure(..., interfaces=...)``)."""
    roles = _roles(c, board)
    cfg = c.config(board)["interfaces"]
    uses: dict[str, InterfaceUse] = {}
    funcs = {b: set(PinRef(board, board.pin(f"GPIO{b}")).pin.functions) for b in range(28)}

    i2c = [(b, role) for b, role, _, _ in roles if role.startswith("I2C")]
    if any(b in (2, 3) for b, _ in i2c):
        uses["i2c1"] = InterfaceUse("i2c1", (2, 3))
    soft = {}
    for b, role, mod, _ in roles:
        if role in ("I2C_SDA", "I2C_SCL") and b not in (2, 3):
            soft.setdefault(mod.part.ref, {})[role] = b
    k = 0
    for ref, d in soft.items():
        if "I2C_SDA" in d and "I2C_SCL" in d:
            uses[f"i2c_gpio{k}"] = InterfaceUse("i2c_gpio", (d["I2C_SDA"], d["I2C_SCL"]), {"bus": 3 + k})
            k += 1
    for bus, data_pins in (("spi0", (9, 10, 11)), ("spi1", (19, 20, 21))):
        prefix = bus.upper() + "_CE"  # e.g. SPI0_CE0 / SPI0_CE1
        ce = sorted({b for b, role, _, _ in roles
                     if role == "SPI_CS" and any(f.startswith(prefix) for f in funcs[b])})
        data = [b for b, role, _, _ in roles if role in ("SPI_MOSI", "SPI_MISO", "SPI_SCLK") and b in data_pins]
        if data or ce:
            uses[bus] = InterfaceUse(bus, data_pins + tuple(ce), {"cs_pins": tuple(ce)})
    if any(b in (14, 15) and role in ("UART_TX", "UART_RX") for b, role, _, _ in roles):
        uses["uart0"] = InterfaceUse("uart0", (14, 15))
    ow = sorted({b for b, role, _, _ in roles if role == "ONEWIRE"})
    if ow:
        uses["onewire"] = InterfaceUse("onewire", tuple(ow))
    if any(p.key == "pi_camera_v3" or "csi" in p.features for p in c.parts):
        uses["camera"] = InterfaceUse("camera")

    # explicit configuration wins
    for name, value in cfg.items():
        if value is False or value is None:
            uses.pop(name, None)
            continue
        params = value if isinstance(value, dict) else {}
        if name == "i2c1":
            uses["i2c1"] = InterfaceUse("i2c1", (2, 3), {**uses.get("i2c1", InterfaceUse("i2c1")).params, **params})
        elif name == "spi0":
            cs = int(params.get("cs", 2))
            uses["spi0"] = InterfaceUse("spi0", (9, 10, 11) + (8, 7)[:max(cs, 1)], {"cs_pins": (8, 7)[:max(cs, 1)]})
        elif name == "spi1":
            cs = int(params.get("cs", 1))
            uses["spi1"] = InterfaceUse("spi1", (19, 20, 21) + (18, 17, 16)[:max(cs, 1)],
                                        {"cs_pins": (18, 17, 16)[:max(cs, 1)]})
        elif name == "uart0":
            uses["uart0"] = InterfaceUse("uart0", (14, 15))
        elif name == "onewire":
            pins = [4] if value is True else (list(value) if isinstance(value, (list, tuple)) else [int(value)])
            known = set(uses["onewire"].pins) if "onewire" in uses else set()
            uses["onewire"] = InterfaceUse("onewire", tuple(sorted(known | {int(x) for x in pins})))
        elif name == "pwm":
            pins = list(value) if isinstance(value, (list, tuple)) else [18]
            uses["pwm"] = InterfaceUse("pwm", tuple(int(p) for p in pins))
        elif name == "pcm":
            uses["pcm"] = InterfaceUse("pcm", (18, 19, 20, 21))
        elif name == "camera":
            uses["camera"] = InterfaceUse("camera")
    # Inferred SPI0: the default overlay claims CE0 and CE1 (spidev0.0 / spidev0.1). CE1 (GPIO7) is released
    # with spi0-1cs when something else is wired to it. A lone device on CE1 keeps both chip selects (it stays
    # /dev/spidev0.1) unless GPIO8 is needed elsewhere: then spi0-1cs,cs0_pin=7 moves the only CS to GPIO7
    # (the device becomes /dev/spidev0.0). spi0-1cs alone would keep CE0 and drop the device's CE1.
    if "spi0" in uses and "spi0" not in cfg:
        u = uses["spi0"]
        cs = tuple(u.params.get("cs_pins", ()))
        used = _used_pins(c, board)
        if cs == (7,) and 8 in used:
            u.params["cs0_pin"] = 7
        elif 8 not in u.pins:
            u.pins = u.pins + (8,)
        if 7 not in cs:
            u.params["one_cs"] = 7 in used
    return list(uses.values())


def config_lines(board: PartDef | Part, uses: list[InterfaceUse]) -> list[str]:
    """config.txt lines (``dtparam=``/``dtoverlay=``) enabling ``uses`` on ``board``."""
    d = board.definition if isinstance(board, Part) else board
    pi5 = d.params.get("overlay_suffix") == "-pi5"
    pwm_func = d.params.get("pwm_func", {12: 4, 13: 4, 18: 2, 19: 2})
    lines: list[str] = []
    for u in uses:
        if u.name == "i2c1":
            lines.append("dtparam=i2c_arm=on")
            if u.params.get("baudrate"):
                lines.append(f"dtparam=i2c_arm_baudrate={int(u.params['baudrate'])}")
        elif u.name == "i2c_gpio":
            lines.append(f"dtoverlay=i2c-gpio,bus={u.params.get('bus', 3)},i2c_gpio_sda={u.pins[0]},"
                         f"i2c_gpio_scl={u.pins[1]}")
        elif u.name == "spi0":
            lines.append("dtparam=spi=on")
            cs = tuple(u.params.get("cs_pins", (8, 7)))
            if u.params.get("cs0_pin") is not None:
                lines.append(f"dtoverlay=spi0-1cs,cs0_pin={int(u.params['cs0_pin'])}")
                lines.append(f"# the SPI device on GPIO{int(u.params['cs0_pin'])} is /dev/spidev0.0")
            elif u.params.get("one_cs") or cs == (8,):
                lines.append("dtoverlay=spi0-1cs")
        elif u.name == "spi1":
            cs = list(u.params.get("cs_pins") or (18,))
            extra = "".join(f",cs{i}_pin={p}" for i, p in enumerate(cs) if p != (18, 17, 16)[i])
            lines.append(f"dtoverlay=spi1-{len(cs)}cs{extra}")
        elif u.name == "uart0":
            if pi5:
                lines.append("dtoverlay=uart0-pi5")
            else:
                lines.append("enable_uart=1")
                lines.append("# optional: dtoverlay=disable-bt  (full PL011 UART on GPIO14/15 instead of the mini UART)")
        elif u.name.startswith("uart"):
            lines.append(f"dtoverlay={u.name}{'-pi5' if pi5 else ''}")
        elif u.name == "onewire":
            lines.extend(f"dtoverlay=w1-gpio,gpiopin={p}" for p in u.pins)
        elif u.name == "pwm":
            chans = d.params.get("pwm_channels", {})
            for p in u.pins:
                if not any(p in v for v in chans.values()):
                    lines.append(f"# GPIO{p} has no hardware PWM on {d.key}: use software PWM"
                                 + (f" or dtoverlay=pwm-pio,gpio={p}" if pi5 else ""))
            order = sorted((p for p in u.pins if any(p in v for v in chans.values())),
                           key=lambda p: next(k for k, v in chans.items() if p in v))
            if len(order) == 1:
                lines.append(f"dtoverlay=pwm,pin={order[0]},func={pwm_func.get(order[0], 4)}")
            elif order:
                a, b = order[0], order[1]
                lines.append(f"dtoverlay=pwm-2chan,pin={a},func={pwm_func.get(a, 4)},pin2={b},func2={pwm_func.get(b, 4)}")
                for p in order[2:]:
                    lines.append(f"dtoverlay=pwm-pio,gpio={p}" if pi5 else f"# no third hardware PWM on GPIO{p}")
        elif u.name == "pcm":
            lines.append("dtparam=i2s=on")
        elif u.name == "camera":
            lines.append("camera_auto_detect=1")
    return lines


def boot_config(circuit: Circuit) -> str:
    """``config.txt`` snippet for the circuit's board (empty-but-commented when nothing is needed)."""
    board = circuit.board
    if board is None:
        return "# config.txt: no Raspberry Pi in this circuit\n"
    lines = config_lines(board, infer_interfaces(circuit, board))
    head = [f"# config.txt snippet generated by PiForge for {circuit.name!r} on {board.key} ({board.name})",
            "# Add under [all] in /boot/firmware/config.txt and reboot."]
    if not lines:
        head.append("# (no interfaces to enable)")
    return "\n".join(head + lines) + "\n"


# ---------------------------------------------------------------------------------- ERC support
_PAIRS = {"I2C_SDA": ("_SDA", "_SCL"), "I2C_SCL": ("_SCL", "_SDA"), "SPI_MOSI": ("_MOSI", "_MISO"),
          "SPI_MISO": ("_MISO", "_MOSI"), "SPI_SCLK": ("_SCLK", "_MOSI"), "UART_TX": ("_RX", "_TX"),
          "UART_RX": ("_TX", "_RX")}


def interface_claims(c: Circuit, board: Part):
    """({bcm: (interface, allowed(pinref) -> bool)}, [(severity, board pin, message, hint)]) for ERC."""
    def by_func(*prefixes: str) -> Callable[[PinRef], bool]:
        return lambda r: (r.part.category == "passive" or "level_shifter" in r.part.features
                          or any(f.startswith(prefixes) for f in r.pin.functions))

    rules = {"i2c1": ("I2C1", by_func("I2C_")), "i2c_gpio": ("software I2C", by_func("I2C_")),
             "spi0": ("SPI0", by_func("SPI_")), "spi1": ("SPI1", by_func("SPI_")),
             "uart0": ("UART0", by_func("UART_")), "onewire": ("1-Wire", by_func("ONEWIRE")),
             "pcm": ("PCM/I2S", by_func("PCM_")),
             "pwm": ("hardware PWM", lambda r: r.part.category not in ("switch",) and r.pin.type.value != "output")}
    claims: dict[int, tuple[str, Callable]] = {}
    for u in infer_interfaces(c, board):
        if u.name in rules:
            for p in u.pins:
                claims.setdefault(p, rules[u.name])
    miswired = []
    pwm = c.config(board)["interfaces"].get("pwm")
    if isinstance(pwm, list):
        chans = board.params.get("pwm_channels", {})
        first: dict[str, int] = {}
        for p in pwm:
            ref = PinRef(board, board.pin(f"GPIO{p}"))
            ch = next((k for k, v in chans.items() if p in v), None)
            if ch is None:
                miswired.append((Severity.WARNING, ref, f"GPIO{p} is configured for PWM but has no hardware PWM "
                                 f"channel on {board.key}.", "Use GPIO12/13/18/19 for hardware PWM."))
            elif ch in first:
                miswired.append((Severity.ERROR, ref, f"GPIO{p} and GPIO{first[ch]} share hardware PWM channel "
                                 f"{ch}: they cannot carry different signals.",
                                 "Use one pin per channel (BCM: 12|18 = channel 0, 13|19 = channel 1)."))
            else:
                first[ch] = p
    for bcm, role, mod, gpio in _roles(c, board):
        if role in ("ONEWIRE",):
            continue
        funcs = gpio.pin.functions
        if role == "PWM_IN":
            if not any(f.startswith("PWM0") or f == "PWM1" for f in funcs):
                miswired.append((Severity.INFO, gpio, f"{mod.label} (PWM input) is on {gpio.pin.name}: software PWM "
                                 "only (timing jitter).", "Use GPIO12/13/18/19 for hardware PWM."))
            continue
        if role == "SPI_CS":
            if not any("_CE" in f and f.startswith("SPI") for f in funcs):
                miswired.append((Severity.WARNING, gpio, f"{mod.label} chip select is on {gpio.pin.name}, not a "
                                 "hardware CE pin.", "Use CE0 (GPIO8) / CE1 (GPIO7) or configure cs-gpios."))
            continue
        good, bad = _PAIRS[role]
        family = role.split("_")[0]
        hw = [f for f in funcs if f.startswith(family) and f.endswith(good)]
        wrong = [f for f in funcs if f.startswith(family) and f.endswith(bad)]
        if role.startswith("I2C"):
            hw = [f for f in hw if f.startswith("I2C1")]
            wrong = [f for f in wrong if f.startswith("I2C1")]
        if role.startswith("UART"):
            hw = [f for f in hw if f.startswith("UART0")]
            wrong = [f for f in wrong if f.startswith("UART0")]
        if hw:
            continue
        if wrong:
            miswired.append((Severity.ERROR, gpio, f"{mod.label} ({role}) is wired to {gpio.pin.name}, which is "
                             f"{wrong[0]}: the lines are swapped.",
                             "TX goes to RX; SDA to SDA (GPIO2), SCL to SCL (GPIO3); MOSI to MOSI (GPIO10)."))
        else:
            miswired.append((Severity.WARNING, gpio, f"{mod.label} ({role}) is on {gpio.pin.name}, which has no "
                             "hardware function for it: needs a software (bit-banged) driver/overlay.",
                             "Prefer the hardware pins: I2C GPIO2/3, SPI0 GPIO9/10/11/8/7, UART GPIO14/15."))
    return claims, miswired
