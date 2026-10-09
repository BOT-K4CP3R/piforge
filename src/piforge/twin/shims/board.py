"""``board`` shim: Raspberry Pi 40-pin header names (Blinka-compatible) + ``I2C()``/``SPI()``."""

from __future__ import annotations

from typing import Any

from microcontroller import Pin

_BOARD_IDS = {"rpi4b": "RASPBERRY_PI_4B", "rpi5": "RASPBERRY_PI_5", "rpi3bp": "RASPBERRY_PI_3B_PLUS",
              "rpizero2w": "RASPBERRY_PI_ZERO_2_W"}


def _board_id() -> str:
    try:
        from piforge.twin.runtime import get_twin

        return _BOARD_IDS.get(get_twin().config.board, "RASPBERRY_PI_4B")
    except Exception:
        return "RASPBERRY_PI_4B"


board_id = _board_id()

D0, D1, D2, D3, D4, D5, D6, D7, D8, D9 = (Pin(i) for i in range(10))
D10, D11, D12, D13, D14, D15, D16, D17, D18, D19 = (Pin(i) for i in range(10, 20))
D20, D21, D22, D23, D24, D25, D26, D27 = (Pin(i) for i in range(20, 28))

# Alternate names.  # src: Adafruit Blinka board/raspberrypi/raspi_40pin.py
SDA = SDA1 = D2
SCL = SCL1 = D3
ID_SD, ID_SC = D0, D1
SCLK = SCK = D11
MOSI = D10
MISO = D9
CE0 = D8
CE1 = D7
CE2 = D16          # SPI1 extra chip select (Blinka: CE2 is not standard on SPI0)
SCK_1 = SCLK_1 = D21
MOSI_1 = D20
MISO_1 = D19
CE0_1, CE1_1, CE2_1 = D18, D17, D16
TXD = TX = D14
RXD = RX = D15
PWM0 = D12
PWM1 = D13

_i2c: Any = None
_spi: Any = None


def I2C() -> Any:  # noqa: N802 - Blinka API name
    """The default I2C bus (SCL=GPIO3, SDA=GPIO2), shared singleton."""
    global _i2c
    if _i2c is None:
        import busio

        _i2c = busio.I2C(SCL, SDA)
    return _i2c


STEMMA_I2C = I2C


def SPI() -> Any:  # noqa: N802 - Blinka API name
    """The default SPI bus (SPI0: SCLK=11, MOSI=10, MISO=9), shared singleton."""
    global _spi
    if _spi is None:
        import busio

        _spi = busio.SPI(SCLK, MOSI, MISO)
    return _spi
