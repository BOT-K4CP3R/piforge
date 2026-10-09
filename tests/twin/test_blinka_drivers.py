"""Real Adafruit CircuitPython drivers and RPLCD running unmodified on the twin shims.

Installed with ``uv pip install adafruit-circuitpython-bme280 adafruit-circuitpython-ssd1306
adafruit-circuitpython-framebuf RPLCD``. The shims shadow Adafruit-Blinka's hardware modules.
"""

from __future__ import annotations

import io

import pytest

from piforge.twin.config import DeviceConfig


def _lit(png: bytes) -> set[tuple[int, int]]:
    from PIL import Image

    img = Image.open(io.BytesIO(png)).convert("L")
    w, h = img.size
    px = img.load()
    return {(x, y) for y in range(h) for x in range(w) if px[x, y] > 127}


def test_adafruit_bme280(shims, make_twin):
    pytest.importorskip("adafruit_bme280")
    twin = make_twin(DeviceConfig("ENV1", "bme280", bus={"kind": "i2c", "bus": 1, "address": 0x76}))
    twin.set_input("ENV1", "temperature", 23.5)
    twin.set_input("ENV1", "humidity", 40.0)
    twin.set_input("ENV1", "pressure", 1013.25)
    import board
    from adafruit_bme280 import basic as adafruit_bme280

    sensor = adafruit_bme280.Adafruit_BME280_I2C(board.I2C(), address=0x76)
    assert sensor.temperature == pytest.approx(23.5, abs=0.5)
    assert sensor.humidity == pytest.approx(40.0, abs=2.0)
    assert sensor.pressure == pytest.approx(1013.25, abs=1.0)
    twin.set_input("ENV1", "temperature", -5.0)
    assert sensor.temperature == pytest.approx(-5.0, abs=0.5)


def test_adafruit_bme280_spi(shims, make_twin):
    pytest.importorskip("adafruit_bme280")
    twin = make_twin(DeviceConfig("ENV2", "bme280", bus={"kind": "spi", "bus": 0, "cs": 0, "cs_pin": 5}))
    twin.set_input("ENV2", "temperature", 31.0)
    import board
    import digitalio
    from adafruit_bme280 import basic as adafruit_bme280

    cs = digitalio.DigitalInOut(board.D5)
    sensor = adafruit_bme280.Adafruit_BME280_SPI(board.SPI(), cs)
    assert sensor.temperature == pytest.approx(31.0, abs=0.5)


def test_ssd1306_framebuffer(shims, make_twin):
    pytest.importorskip("adafruit_ssd1306")
    twin = make_twin(DeviceConfig("OLED1", "ssd1306", bus={"kind": "i2c", "bus": 1, "address": 0x3C}))
    import adafruit_ssd1306
    import board

    disp = adafruit_ssd1306.SSD1306_I2C(128, 64, board.I2C(), addr=0x3C)
    disp.fill(0)
    disp.pixel(0, 0, 1)
    disp.pixel(10, 20, 1)
    disp.pixel(127, 63, 1)
    disp.show()
    dev = twin.devices["OLED1"]
    w, h, png = dev.render_png()
    assert (w, h) == (128, 64)
    assert _lit(png) == {(0, 0), (10, 20), (127, 63)}
    disp.fill(1)
    disp.show()
    assert len(_lit(dev.render_png()[2])) == 128 * 64
    disp.invert(True)
    assert len(_lit(dev.render_png()[2])) == 0
    disp.poweroff()
    assert twin.state()["devices"]["OLED1"]["on"] is False


def test_ssd1306_128x32(shims, make_twin):
    pytest.importorskip("adafruit_ssd1306")
    twin = make_twin(DeviceConfig("OLED2", "ssd1306", bus={"kind": "i2c", "bus": 1, "address": 0x3D},
                                  params={"width": 128, "height": 32}))
    import adafruit_ssd1306
    import board

    disp = adafruit_ssd1306.SSD1306_I2C(128, 32, board.I2C(), addr=0x3D)
    disp.fill(0)
    disp.pixel(5, 31, 1)
    disp.show()
    w, h, png = twin.devices["OLED2"].render_png()
    assert (w, h) == (128, 32)
    assert _lit(png) == {(5, 31)}


def test_lcd1602(shims, make_twin):
    pytest.importorskip("RPLCD")
    twin = make_twin(DeviceConfig("LCD1", "lcd1602_pcf8574", bus={"kind": "i2c", "bus": 1, "address": 0x27}))
    from RPLCD.i2c import CharLCD

    lcd = CharLCD("PCF8574", 0x27, port=1, cols=16, rows=2)
    lcd.write_string("Hello")
    st = twin.state()["devices"]["LCD1"]
    assert st["lines"][0] == "Hello"
    lcd.cursor_pos = (1, 0)
    lcd.write_string("World 42")
    assert twin.state()["devices"]["LCD1"]["lines"] == ["Hello", "World 42"]
    lcd.backlight_enabled = False
    assert twin.state()["devices"]["LCD1"]["backlight"] is False
    lcd.clear()
    assert twin.state()["devices"]["LCD1"]["lines"] == ["", ""]
    lcd.close()


def test_digitalio_and_pwmio(shims, make_twin, wait_until):
    twin = make_twin(DeviceConfig("D1", "led", {"pin": 17}), DeviceConfig("SW1", "button", {"pin": 27}),
                     DeviceConfig("SRV1", "servo", {"pin": 18}))
    import board
    import digitalio
    import pwmio

    led = digitalio.DigitalInOut(board.D17)
    led.direction = digitalio.Direction.OUTPUT
    led.value = True
    assert twin.devices["D1"].state()["on"] is True
    btn = digitalio.DigitalInOut(board.D27)
    btn.switch_to_input(pull=digitalio.Pull.UP)
    assert btn.value is True
    twin.set_input("SW1", "pressed", True)
    assert btn.value is False
    servo = pwmio.PWMOut(board.D18, frequency=50, duty_cycle=int(65535 * 0.075))
    assert wait_until(lambda: abs(twin.devices["SRV1"].state()["angle"]) < 2, timeout=3.0)
    servo.deinit()
    led.deinit()
    btn.deinit()


def test_neopixel_shim(shims, make_twin):
    twin = make_twin(DeviceConfig("NP1", "neopixel", {"pin": 18}, params={"count": 3}))
    import board
    import neopixel

    pixels = neopixel.NeoPixel(board.D18, 3, brightness=0.5, auto_write=False)
    pixels.fill((255, 0, 0))
    pixels[2] = (0, 0, 255)
    assert twin.state()["devices"]["NP1"]["pixels"] == ["#000000"] * 3   # not shown yet
    pixels.show()
    st = twin.state()["devices"]["NP1"]
    assert st["pixels"] == ["#800000", "#800000", "#000080"]
    assert len(pixels) == 3 and pixels[0] == (255, 0, 0)


def test_picamera2_shim(shims, make_twin, spaced_tmp):
    twin = make_twin(DeviceConfig("CAM1", "camera"))
    from picamera2 import Picamera2

    cam = Picamera2()
    cfg = cam.create_still_configuration(main={"size": (320, 240), "format": "RGB888"})
    cam.configure(cfg)
    cam.start()
    arr = cam.capture_array()
    assert arr.shape == (240, 320, 3)
    out = spaced_tmp / "shot 1.jpg"
    cam.capture_file(str(out))
    assert out.exists() and out.stat().st_size > 1000
    img = cam.capture_image()
    assert img.size == (320, 240)
    cam.stop()
    cam.close()
    assert twin.state()["devices"]["CAM1"]["captures"] == 3


def test_dht_and_ds18b20_shims(shims, make_twin):
    twin = make_twin(DeviceConfig("H1", "dht22", {"pin": 4}), DeviceConfig("T1", "ds18b20", {"pin": 17}))
    twin.set_input("H1", "temperature", 24.0)
    twin.set_input("H1", "humidity", 61.5)
    twin.set_input("T1", "temperature", 19.25)
    import adafruit_dht
    import Adafruit_DHT
    import board
    from w1thermsensor import W1ThermSensor

    dht = adafruit_dht.DHT22(board.D4)
    assert dht.temperature == pytest.approx(24.0, abs=0.1)
    assert dht.humidity == pytest.approx(61.5, abs=0.1)
    hum, temp = Adafruit_DHT.read_retry(Adafruit_DHT.DHT22, 4)
    assert (round(hum, 1), round(temp, 1)) == (61.5, 24.0)
    sensors = W1ThermSensor.get_available_sensors()
    assert len(sensors) == 1
    assert sensors[0].get_temperature() == pytest.approx(19.25, abs=0.07)
