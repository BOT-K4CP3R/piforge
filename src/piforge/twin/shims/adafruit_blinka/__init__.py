"""Blocks Adafruit-Blinka's hardware backend inside the twin.

Blinka's ``board``/``busio``/``digitalio``/… are shadowed by PiForge shims; any *other* Blinka module
that reaches for the real hardware backend lands here and gets a clear error instead of probing
the host machine.
"""

raise ImportError(
    "adafruit_blinka (the Blinka hardware backend) is replaced by the PiForge twin. Supported: board, "
    "busio (I2C/SPI), digitalio, pwmio, neopixel, adafruit_dht, micropython, microcontroller; this "
    "Blinka module is not simulated.")
