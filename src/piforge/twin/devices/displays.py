"""Displays: SSD1306 OLED (I2C/SPI) and HD44780 16×2 LCD behind a PCF8574 I2C backpack."""

from __future__ import annotations

import io
from typing import Any

from piforge.twin.bus import I2CDevice, SPIDevice
from piforge.twin.devices.base import Device, PropSpec, register


_FONTS: dict[int, Any] = {}


def _font(size: int) -> Any:
    """Pillow's bundled font at ``size`` px (cached; bitmap fallback on old Pillow)."""
    if size not in _FONTS:
        from PIL import ImageFont

        try:
            _FONTS[size] = ImageFont.load_default(size=size)
        except TypeError:  # pragma: no cover - Pillow < 10.1
            _FONTS[size] = ImageFont.load_default()
    return _FONTS[size]


def _png(img: Any) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


# --- SSD1306 -------------------------------------------------------------------------------------
# Argument byte count per command.  # src: Solomon Systech SSD1306 datasheet rev 1.1, §9 command table
_ARGS = {0x81: 1, 0x20: 1, 0x21: 2, 0x22: 2, 0xA3: 2, 0xA8: 1, 0xD3: 1, 0xDA: 1, 0xD5: 1, 0xD9: 1,
         0xDB: 1, 0x8D: 1, 0xAD: 1, 0x26: 6, 0x27: 6, 0x29: 5, 0x2A: 5, 0x23: 1, 0xD6: 1}


class _OledFront(I2CDevice, SPIDevice):
    """I2C control-byte framing (Co / D/C#) or 4-wire SPI with the D/C GPIO."""

    def __init__(self, dev: "SSD1306") -> None:
        self.dev = dev

    def on_write(self, data: bytes) -> None:
        i = 0
        while i < len(data):
            ctrl = data[i]
            i += 1
            is_data = bool(ctrl & 0x40)
            if ctrl & 0x80:                                  # Co = 1: one byte, then a new control byte
                if i < len(data):
                    self.dev.feed(data[i:i + 1], is_data)
                    i += 1
            else:                                            # Co = 0: the rest is one stream
                self.dev.feed(data[i:], is_data)
                break

    def on_read(self, n: int) -> bytes:
        return bytes([0x00 if self.dev.on else 0x40]) * n  # status: bit 6 = display off

    def transfer(self, data: bytes) -> bytes:
        dc = self.dev.pins.get("dc")
        is_data = dc is not None and self.dev.pi.effective(dc) >= 0.5
        self.dev.feed(bytes(data), is_data)
        return bytes(len(data))


@register
class SSD1306(Device):
    """SSD1306 128×64 / 128×32 OLED controller: full command parser, horizontal/vertical/page
    addressing, segment/COM remap, start line, offset, invert, entire-on, display on/off.
    Output: the visible framebuffer as a 1-bit PNG (lit pixel = white)."""

    type = "ssd1306"
    label = "SSD1306 OLED"
    bus_kinds = ("i2c", "spi")
    default_address = 0x3C
    optional_pins = ("dc", "reset")
    pin_aliases = {"d/c": "dc", "dc": "dc", "a0": "dc", "rst": "reset", "res": "reset"}
    defaults = {"width": 128, "height": 64}
    is_display = True
    outputs = {"on": PropSpec("bool", default=False, label="Display on"),
               "inverted": PropSpec("bool", default=False, label="Inverted"),
               "contrast": PropSpec("int", 0, 255, default=127, label="Contrast"),
               "lit_pixels": PropSpec("int", 0, 8192, default=0, label="Lit pixels"),
               "frame": PropSpec("int", 0, None, default=0, label="Frame counter")}
    example = {"bus": {"kind": "i2c", "bus": 1, "address": 0x3C}}

    def uses_pins(self, pins: dict[str, int]) -> bool:
        return False

    def needs_bus(self) -> bool:
        return True

    def setup(self) -> None:
        self.w = int(self.params.get("width", 128))
        self.h = int(self.params.get("height", 64))
        self.ram = bytearray(128 * 8)
        self._front = _OledFront(self)
        assert self.bus is not None
        if self.bus["kind"] == "i2c":
            self.pi.i2c_bus(self.bus["bus"]).attach(self.bus["address"], self._front)
        else:
            self.pi.spi[self.bus["bus"]].attach(self.bus["cs"], self._front, cs_gpio=self.bus.get("cs_pin"))
        if "reset" in self.pins:
            self.listen(self.pins["reset"], lambda b, lv, t: self._reset() if lv == 0 else None)
        self._reset()
        self._cache: tuple[int, Any] | None = None

    def _reset(self) -> None:
        """Power-on/RES# register defaults (GDDRAM content is kept).  # src: SSD1306 DS §8.9"""
        self.on = False
        self.inverted = False
        self.entire_on = False
        self.contrast = 0x7F
        self.mode = 2                         # page addressing after reset
        self.col_start, self.col_end, self.page_start, self.page_end = 0, 127, 0, 7
        self.col = self.page = 0
        self.seg_remap = False
        self.com_remap = False
        self.start_line = 0
        self.offset = 0
        self.mux = 63
        self._cmd: list[int] = []
        self.display_version += 1

    # -- protocol ---------------------------------------------------------------------------------
    def feed(self, data: bytes, is_data: bool) -> None:
        """Bytes from the bus: GDDRAM data or a command stream."""
        with self.pi.lock:
            if is_data:
                for b in data:
                    self._write_ram(b)
                self.display_version += 1
            else:
                for b in data:
                    self._command_byte(b)

    def _write_ram(self, b: int) -> None:
        self.ram[self.page * 128 + self.col] = b
        if self.mode == 0:                                   # horizontal
            self.col += 1
            if self.col > self.col_end:
                self.col = self.col_start
                self.page = self.page + 1 if self.page < self.page_end else self.page_start
        elif self.mode == 1:                                 # vertical
            self.page += 1
            if self.page > self.page_end:
                self.page = self.page_start
                self.col = self.col + 1 if self.col < self.col_end else self.col_start
        else:                                                # page mode
            self.col = self.col + 1 if self.col < 127 else 0

    def _command_byte(self, b: int) -> None:
        self._cmd.append(b)
        need = _ARGS.get(self._cmd[0], 0)
        if len(self._cmd) <= need:
            return
        cmd, args = self._cmd[0], self._cmd[1:]
        self._cmd = []
        self._execute(cmd, args)

    def _execute(self, c: int, a: list[int]) -> None:
        if c == 0x81:
            self.contrast = a[0]
        elif c in (0xA4, 0xA5):
            self.entire_on = c == 0xA5
        elif c in (0xA6, 0xA7):
            self.inverted = c == 0xA7
        elif c in (0xAE, 0xAF):
            self.on = c == 0xAF
        elif c == 0x20:
            self.mode = a[0] & 0x03 if (a[0] & 0x03) != 3 else 2
        elif c == 0x21:
            self.col_start, self.col_end = a[0] & 0x7F, a[1] & 0x7F
            self.col = self.col_start
        elif c == 0x22:
            self.page_start, self.page_end = a[0] & 0x07, a[1] & 0x07
            self.page = self.page_start
        elif 0x00 <= c <= 0x0F:
            self.col = (self.col & 0xF0) | c
        elif 0x10 <= c <= 0x1F:
            self.col = ((c & 0x0F) << 4) | (self.col & 0x0F)
        elif 0xB0 <= c <= 0xB7:
            self.page = c & 0x07
        elif 0x40 <= c <= 0x7F:
            self.start_line = c & 0x3F
        elif c in (0xA0, 0xA1):
            self.seg_remap = c == 0xA1
        elif c in (0xC0, 0xC8):
            self.com_remap = c == 0xC8
        elif c == 0xA8:
            self.mux = max(15, a[0] & 0x3F)
        elif c == 0xD3:
            self.offset = a[0] & 0x3F
        # 0x8D charge pump, 0xD5 clock, 0xD9 precharge, 0xDA COM pins, 0xDB VCOMH, scrolling, NOP:
        # accepted; no visible effect in the twin.
        self.display_version += 1

    # -- rendering ----------------------------------------------------------------------------------
    def pixels(self) -> Any:
        """Visible image as a ``(h, w)`` uint8 numpy array of 0/1 (cached per frame)."""
        import numpy as np

        with self.pi.lock:
            if self._cache is not None and self._cache[0] == self.display_version:
                return self._cache[1]
            w, h = self.w, self.h
            if not self.on:
                img = np.zeros((h, w), dtype=np.uint8)
            elif self.entire_on:
                img = np.ones((h, w), dtype=np.uint8)
            else:
                pages = np.frombuffer(bytes(self.ram), dtype=np.uint8).reshape(8, 128)
                bits = np.unpackbits(pages[:, :, None], axis=2, bitorder="little")   # (8, 128, 8)
                ram = bits.transpose(0, 2, 1).reshape(64, 128)                        # row, col
                off = (128 - w) // 2 if w < 128 else 0
                xs = np.arange(w)
                cols = xs + off if self.seg_remap else 127 - off - xs
                ys = np.arange(h)
                com = ys if self.com_remap else (h - 1 - ys)
                rows = (com + self.start_line + self.offset) % 64
                img = ram[np.ix_(rows, cols)]
                if self.inverted:
                    img = 1 - img
            self._cache = (self.display_version, img)
            return img

    def render_png(self) -> tuple[int, int, bytes]:
        from PIL import Image

        img = self.pixels()
        pil = Image.fromarray((img * 255).astype("uint8")).convert("1")   # 2-D uint8 → mode "L"
        return self.w, self.h, _png(pil)

    def outputs_state(self) -> dict:
        return {"on": self.on, "inverted": self.inverted, "contrast": self.contrast,
                "lit_pixels": int(self.pixels().sum()), "frame": self.display_version}


# --- HD44780 + PCF8574 -----------------------------------------------------------------------------
def _a00_char(code: int) -> str:
    """HD44780U ROM A00 (Japanese) glyph for a DDRAM code.  # src: Hitachi HD44780U DS table 4"""
    if code < 0x10:
        return chr(0x2400 + (code & 0x07))                  # CGRAM glyphs → ␀…␇ placeholders
    if code == 0x5C:
        return "¥"
    if code == 0x7E:
        return "→"
    if code == 0x7F:
        return "←"
    if 0x20 <= code <= 0x7D:
        return chr(code)
    if 0xA1 <= code <= 0xDF:
        return chr(0xFF61 + code - 0xA1)                     # half-width katakana block
    high = "αäβεμσρg√⁻jˣ¢£ñöpqθ∞ΩüΣπxy千万円÷ █"
    if 0xE0 <= code <= 0xFF:
        return high[code - 0xE0]
    return " "


class HD44780:
    """HD44780 controller state machine (8-bit power-on mode, 4-bit after function set)."""

    def __init__(self) -> None:
        self.ddram = bytearray(b" " * 128)
        self.cgram = bytearray(64)
        self.ac = 0
        self.cg_mode = False
        self.eight_bit = True
        self.pending: int | None = None
        self.two_line = False
        self.display_on = False
        self.cursor = False
        self.blink = False
        self.increment = True
        self.shift_display = False
        self.shift = 0

    def strobe(self, rs: int, nibble: int) -> None:
        """One E falling edge with D7..D4 = ``nibble`` (D3..D0 unconnected → 0)."""
        if self.eight_bit:
            self._exec(rs, (nibble & 0x0F) << 4)
        elif self.pending is None:
            self.pending = nibble & 0x0F
        else:
            value = (self.pending << 4) | (nibble & 0x0F)
            self.pending = None
            self._exec(rs, value)

    def _advance(self, step: int) -> None:
        if self.cg_mode:
            self.ac = (self.ac + step) & 0x3F
            return
        if self.two_line:                                    # 0x00–0x27 and 0x40–0x67
            line, pos = (1, self.ac - 0x40) if self.ac >= 0x40 else (0, self.ac)
            pos += step
            if pos > 0x27:
                line, pos = 1 - line, 0
            elif pos < 0:
                line, pos = 1 - line, 0x27
            self.ac = (0x40 if line else 0) + pos
        else:
            self.ac = (self.ac + step) % 0x50

    def _exec(self, rs: int, v: int) -> None:
        step = 1 if self.increment else -1
        if rs:
            if self.cg_mode:
                self.cgram[self.ac & 0x3F] = v
            else:
                self.ddram[self.ac & 0x7F] = v
                if self.shift_display:
                    self.shift += step
            self._advance(step)
            return
        if v & 0x80:
            self.ac, self.cg_mode = v & 0x7F, False
        elif v & 0x40:
            self.ac, self.cg_mode = v & 0x3F, True
        elif v & 0x20:
            self.eight_bit = bool(v & 0x10)
            self.pending = None
            self.two_line = bool(v & 0x08)
        elif v & 0x10:
            right = 1 if v & 0x04 else -1
            if v & 0x08:
                self.shift -= right                          # display shift moves the window
            else:
                self._advance(right)
        elif v & 0x08:
            self.display_on, self.cursor, self.blink = bool(v & 4), bool(v & 2), bool(v & 1)
        elif v & 0x04:
            self.increment, self.shift_display = bool(v & 2), bool(v & 1)
        elif v & 0x02:
            self.ac, self.cg_mode, self.shift = 0, False, 0
        elif v & 0x01:
            self.ddram[:] = b" " * 128
            self.ac, self.cg_mode, self.shift, self.increment = 0, False, 0, True

    def row_codes(self, cols: int, rows: int) -> list[list[int]]:
        """DDRAM codes visible on each display row."""
        starts = [0x00, 0x40, cols, 0x40 + cols]
        out = []
        for r in range(rows):
            line: list[int] = []
            for i in range(cols):
                if self.two_line:
                    base = 0x40 if starts[r] >= 0x40 else 0x00
                    addr = base + (starts[r] - base + self.shift + i) % 40
                else:
                    addr = (starts[r] + self.shift + i) % 80
                line.append(self.ddram[addr])
            out.append(line)
        return out


class _PCF8574(I2CDevice):
    """8-bit quasi-bidirectional port: P0=RS P1=RW P2=E P3=backlight P4..P7=D4..D7.
    # src: common "LCM1602 IIC" backpack wiring (RPLCD/LiquidCrystal_I2C pin map)"""

    def __init__(self, dev: "LCD1602PCF8574") -> None:
        self.dev = dev

    def on_write(self, data: bytes) -> None:
        for b in data:
            self.dev.port_write(b)

    def on_read(self, n: int) -> bytes:
        return bytes([self.dev.port_read()]) * n


@register
class LCD1602PCF8574(Device):
    """HD44780 character LCD (16×2 default; ``cols``/``rows`` params) on a PCF8574 I2C backpack."""

    type = "lcd1602_pcf8574"
    label = "LCD 16×2 (I2C)"
    bus_kinds = ("i2c",)
    default_address = 0x27
    defaults = {"cols": 16, "rows": 2, "color": "green"}
    is_display = True
    outputs = {"text": PropSpec("text", default="", label="Text"),
               "backlight": PropSpec("bool", default=False, label="Backlight"),
               "display_on": PropSpec("bool", default=False, label="Display on")}
    example = {"bus": {"kind": "i2c", "bus": 1, "address": 0x27}}

    def setup(self) -> None:
        self.cols = int(self.params.get("cols", 16))
        self.rows = int(self.params.get("rows", 2))
        self.ctrl = HD44780()
        self._port = 0xFF
        assert self.bus is not None
        self.pi.i2c_bus(self.bus["bus"]).attach(self.bus["address"], _PCF8574(self))

    def port_write(self, b: int) -> None:
        with self.pi.lock:
            prev, self._port = self._port, b & 0xFF
            if (prev & 0x08) != (b & 0x08):
                self.display_version += 1
            if prev & 0x04 and not b & 0x04 and not prev & 0x02:   # E falling edge, write cycle
                self.ctrl.strobe(prev & 0x01, (prev >> 4) & 0x0F)
                self.display_version += 1

    def port_read(self) -> int:
        with self.pi.lock:
            v = self._port | 0xF0
            if self._port & 0x02 and self._port & 0x04:            # reading BF: never busy
                v &= 0x7F
            return v

    def lines(self) -> list[str]:
        """Visible text per row, trailing spaces stripped."""
        with self.pi.lock:
            return ["".join(_a00_char(c) for c in row).rstrip() for row in self.ctrl.row_codes(self.cols, self.rows)]

    def outputs_state(self) -> dict:
        lines = self.lines()
        c = self.ctrl
        cursor = None
        if c.cursor and not c.cg_mode:
            line = 1 if c.ac >= 0x40 else 0
            cursor = [line, (c.ac & 0x3F) - c.shift]
        return {"text": "\n".join(lines), "lines": lines, "backlight": bool(self._port & 0x08),
                "display_on": c.display_on, "cursor": cursor}

    def render_png(self) -> tuple[int, int, bytes]:
        from PIL import Image, ImageDraw

        dot, pad = 3, 8
        cw, ch = 6 * dot, 9 * dot
        w, h = self.cols * cw + 2 * pad, self.rows * ch + 2 * pad
        with self.pi.lock:
            bl = bool(self._port & 0x08)
            on = self.ctrl.display_on
            codes = self.ctrl.row_codes(self.cols, self.rows)
            cgram = bytes(self.ctrl.cgram)
        blue = self.params.get("color") == "blue"
        bg = ((40, 90, 220) if blue else (120, 190, 40)) if bl else ((10, 20, 60) if blue else (40, 60, 20))
        fg = (235, 240, 255) if blue else (20, 30, 10)
        img = Image.new("RGB", (w, h), bg)
        if on:
            draw = ImageDraw.Draw(img)
            font = _font(7 * dot)
            for r, row in enumerate(codes):
                for i, code in enumerate(row):
                    x0, y0 = pad + i * cw, pad + r * ch
                    if code < 0x10:                          # custom CGRAM glyph: draw the dots
                        for gy in range(8):
                            bits = cgram[(code & 7) * 8 + gy]
                            for gx in range(5):
                                if bits & (0x10 >> gx):
                                    draw.rectangle([x0 + gx * dot, y0 + gy * dot,
                                                    x0 + gx * dot + dot - 2, y0 + gy * dot + dot - 2], fill=fg)
                    elif code != 0x20:
                        draw.text((x0 + cw / 2, y0 + ch / 2), _a00_char(code), fill=fg, font=font, anchor="mm")
        return w, h, _png(img)
