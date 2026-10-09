"""demo_gauge firmware: BME280 → OLED + servo needle + fan control, 2 readings per second.

Runs unmodified on a Raspberry Pi 4B (Raspberry Pi OS, ``pip install gpiozero
adafruit-circuitpython-bme280 adafruit-circuitpython-ssd1306``) and in the PiForge digital twin.

Wiring (see build/elec/pinout.md): BME280 0x76 and SSD1306 0x3C on I2C1 (GPIO2/3), SG90 signal
GPIO18, mode button GPIO17 → GND (internal pull-up), status LED GPIO27 → 330 Ω → LED → GND, fan
MOSFET gate GPIO22 (100 Ω, 100 kΩ pull-down).

Every reading prints one machine-checkable line::

    T=22.0 H=45 P=1013.2 fan=0 mode=0

Text on the OLED is drawn with a built-in 5×7 font straight into the driver's frame buffer, so no
font file or PIL is needed.
"""

import time

import board
from adafruit_bme280 import basic as adafruit_bme280
import adafruit_ssd1306
from gpiozero import LED, AngularServo, Button, DigitalOutputDevice

T_MIN, T_MAX = 0.0, 40.0  # dial range (°C) — must match projects/demo_gauge/parts.py
SWEEP = 70.0  # needle ±70° around "up"; T_MIN → +70° (left), T_MAX → −70° (right)
FAN_ON_C, FAN_OFF_C = 28.0, 27.0  # 1 K hysteresis so the fan does not chatter
PERIOD = 0.5  # s — 2 Hz loop
MODES = ("temp", "humidity", "pressure", "all")

# -- hardware ----------------------------------------------------------------------------------
i2c = board.I2C()
sensor = adafruit_bme280.Adafruit_BME280_I2C(i2c, address=0x76)
oled = adafruit_ssd1306.SSD1306_I2C(128, 64, i2c, addr=0x3C)
# SG90: 0.5 ms … 2.4 ms ≈ 180° of travel, mapped to −90° … +90° (CCW positive seen from above)
servo = AngularServo(18, min_angle=-90, max_angle=90, min_pulse_width=0.5e-3, max_pulse_width=2.4e-3)
status = LED(27)
fan = DigitalOutputDevice(22)
button = Button(17, bounce_time=0.03)

mode = 0


def next_mode() -> None:
    global mode
    mode = (mode + 1) % len(MODES)


button.when_pressed = next_mode


# -- 5×7 font (columns, LSB = top row) ---------------------------------------------------------
FONT = {
    "0": (0x3E, 0x51, 0x49, 0x45, 0x3E), "1": (0x00, 0x42, 0x7F, 0x40, 0x00), "2": (0x42, 0x61, 0x51, 0x49, 0x46),
    "3": (0x21, 0x41, 0x45, 0x4B, 0x31), "4": (0x18, 0x14, 0x12, 0x7F, 0x10), "5": (0x27, 0x45, 0x45, 0x45, 0x39),
    "6": (0x3C, 0x4A, 0x49, 0x49, 0x30), "7": (0x01, 0x71, 0x09, 0x05, 0x03), "8": (0x36, 0x49, 0x49, 0x49, 0x36),
    "9": (0x06, 0x49, 0x49, 0x29, 0x1E), "A": (0x7E, 0x11, 0x11, 0x11, 0x7E), "B": (0x7F, 0x49, 0x49, 0x49, 0x36),
    "C": (0x3E, 0x41, 0x41, 0x41, 0x22), "D": (0x7F, 0x41, 0x41, 0x22, 0x1C), "E": (0x7F, 0x49, 0x49, 0x49, 0x41),
    "F": (0x7F, 0x09, 0x09, 0x09, 0x01), "G": (0x3E, 0x41, 0x49, 0x49, 0x7A), "H": (0x7F, 0x08, 0x08, 0x08, 0x7F),
    "I": (0x00, 0x41, 0x7F, 0x41, 0x00), "L": (0x7F, 0x40, 0x40, 0x40, 0x40), "M": (0x7F, 0x02, 0x0C, 0x02, 0x7F),
    "N": (0x7F, 0x04, 0x08, 0x10, 0x7F), "O": (0x3E, 0x41, 0x41, 0x41, 0x3E), "P": (0x7F, 0x09, 0x09, 0x09, 0x06),
    "R": (0x7F, 0x09, 0x19, 0x29, 0x46), "S": (0x46, 0x49, 0x49, 0x49, 0x31), "T": (0x01, 0x01, 0x7F, 0x01, 0x01),
    "U": (0x3F, 0x40, 0x40, 0x40, 0x3F), "Y": (0x07, 0x08, 0x70, 0x08, 0x07), "a": (0x20, 0x54, 0x54, 0x54, 0x78),
    "h": (0x7F, 0x08, 0x04, 0x04, 0x78), ".": (0x00, 0x60, 0x60, 0x00, 0x00), "-": (0x08, 0x08, 0x08, 0x08, 0x08),
    "%": (0x23, 0x13, 0x08, 0x64, 0x62), ":": (0x00, 0x36, 0x36, 0x00, 0x00), " ": (0x00, 0x00, 0x00, 0x00, 0x00),
    "°": (0x00, 0x06, 0x09, 0x09, 0x06),
}


def text(x: int, y: int, s: str, scale: int = 1) -> int:
    """Draw ``s`` at (x, y) (top-left) with ``scale``× pixels; returns the x after the text."""
    for ch in s:
        cols = FONT.get(ch, FONT[" "])
        for cx, bits in enumerate(cols):
            for cy in range(7):
                if bits >> cy & 1:
                    oled.fill_rect(x + cx * scale, y + cy * scale, scale, scale, 1)
        x += 6 * scale
    return x


def text_width(s: str, scale: int = 1) -> int:
    return len(s) * 6 * scale - scale


def centered(y: int, s: str, scale: int) -> None:
    text((128 - text_width(s, scale)) // 2, y, s, scale)


def draw(t: float, h: float, p: float, fan_on: bool) -> None:
    oled.fill(0)
    name = MODES[mode]
    if name == "temp":
        centered(0, "TEMPERATURE", 1)
        centered(16, f"{t:.1f}°C", 3)
        # bar: where the needle points on the 0–40 °C dial
        frac = min(max((t - T_MIN) / (T_MAX - T_MIN), 0.0), 1.0)
        oled.rect(4, 44, 120, 8, 1)
        oled.fill_rect(6, 46, int(116 * frac), 4, 1)
    elif name == "humidity":
        centered(0, "HUMIDITY", 1)
        centered(18, f"{h:.0f}%", 3)
    elif name == "pressure":
        centered(0, "PRESSURE", 1)
        centered(16, f"{p:.0f}", 3)
        centered(42, "hPa", 1)
    else:
        text(0, 4, f"T {t:5.1f} °C", 2)
        text(0, 22, f"H {h:5.0f} %", 2)
        text(0, 40, f"P {p:6.1f} hPa", 1)
    text(0, 56, "FAN ON" if fan_on else "FAN OFF", 1)
    for i in range(len(MODES)):  # mode dots, bottom right
        (oled.fill_rect if i == mode else oled.rect)(100 + i * 7, 57, 5, 5, 1)
    oled.show()


def needle_angle(t: float) -> float:
    t = min(max(t, T_MIN), T_MAX)
    return SWEEP - 2 * SWEEP * (t - T_MIN) / (T_MAX - T_MIN)


print("demo_gauge firmware ready", flush=True)
next_t = time.monotonic()
while True:
    t, h, p = sensor.temperature, sensor.relative_humidity, sensor.pressure
    if t >= FAN_ON_C:
        fan.on()
    elif t <= FAN_OFF_C:
        fan.off()
    servo.angle = needle_angle(t)
    status.blink(on_time=0.1, off_time=0.1, n=1)  # one short flash per reading
    draw(t, h, p, bool(fan.value))
    print(f"T={t:.1f} H={h:.0f} P={p:.1f} fan={int(fan.value)} mode={mode}", flush=True)
    next_t += PERIOD
    time.sleep(max(0.0, next_t - time.monotonic()))
