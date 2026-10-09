"""config.txt snippet generation from the circuit (interfaces inferred from wiring + configure())."""

from __future__ import annotations

from piforge.elec.bootconfig import boot_config
from piforge.elec.model import Circuit


def _sensors(board="rpi4b"):
    c = Circuit("boot")
    pi = c.add(board)
    bme = c.add("bme280_breakout")
    c.connect(pi["3V3"], bme["VIN"])
    c.connect(pi["GND"], bme["GND"])
    c.connect(pi["SDA1"], bme["SDA"])
    c.connect(pi["SCL1"], bme["SCL"])
    adc = c.add("mcp3008")
    c.connect(pi["3V3"], adc["VDD"], adc["VREF"])
    c.connect(pi["GND"], adc["DGND"], adc["AGND"])
    c.connect(pi["GPIO11"], adc["CLK"])
    c.connect(pi["GPIO10"], adc["DIN"])
    c.connect(pi["GPIO9"], adc["DOUT"])
    c.connect(pi["GPIO8"], adc["CS"])
    t = c.add("ds18b20")
    r = c.add("resistor", value=4700)
    c.connect(pi["3V3"], t["VDD"], r["1"])
    c.connect(pi["GND"], t["GND"])
    c.connect(pi["GPIO4"], t["DQ"], r["2"])
    return c, pi


def test_inferred_interfaces():
    c, pi = _sensors()
    text = boot_config(c)
    lines = [ln.strip() for ln in text.splitlines()]
    assert "dtparam=i2c_arm=on" in lines
    assert "dtparam=spi=on" in lines
    assert "dtoverlay=w1-gpio,gpiopin=4" in lines
    assert "enable_uart=1" not in lines
    assert text.startswith("#")  # header comment naming the board
    assert "rpi4b" in text.splitlines()[0] or "Raspberry Pi 4" in text.splitlines()[0]


def test_spi_single_cs_frees_ce1_when_gpio7_used():
    c, pi = _sensors()
    btn = c.add("pushbutton")
    c.connect(pi["GPIO7"], btn["A"])
    c.connect(btn["B"], pi["GND"])
    assert "dtoverlay=spi0-1cs" in boot_config(c).splitlines()


def test_configured_uart_pwm_and_baudrate():
    c, pi = _sensors()
    c.configure(pi, interfaces={"uart": True, "pwm": [12, 13], "i2c": {"baudrate": 400_000}})
    lines = boot_config(c).splitlines()
    assert "enable_uart=1" in lines
    assert "dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4" in lines
    assert "dtparam=i2c_arm_baudrate=400000" in lines


def test_pi5_uart_overlay_and_camera():
    c, pi = _sensors("rpi5")
    c.configure(pi, interfaces={"uart": True})
    c.add("pi_camera_v3")
    lines = boot_config(c).splitlines()
    assert "dtoverlay=uart0-pi5" in lines and "enable_uart=1" not in lines
    assert "camera_auto_detect=1" in lines


def test_empty_circuit_is_just_comments():
    c = Circuit("empty")
    c.add("rpi4b")
    text = boot_config(c)
    assert all(not ln.strip() or ln.startswith("#") for ln in text.splitlines())


def test_software_i2c_overlay_for_non_i2c_pins():
    c = Circuit("soft")
    pi = c.add("rpi4b")
    s = c.add("bme280_breakout")
    c.connect(pi["3V3"], s["VIN"])
    c.connect(pi["GND"], s["GND"])
    c.connect(pi["GPIO23"], s["SDA"])
    c.connect(pi["GPIO24"], s["SCL"])
    lines = boot_config(c).splitlines()
    assert "dtoverlay=i2c-gpio,bus=3,i2c_gpio_sda=23,i2c_gpio_scl=24" in lines
    assert "dtparam=i2c_arm=on" not in lines


def test_pwm_on_non_pwm_pin_is_a_comment():
    c = Circuit("pwm")
    pi = c.add("rpi4b")
    c.configure(pi, interfaces={"pwm": [17]})
    text = boot_config(c)
    assert "dtoverlay=pwm," not in text and "GPIO17 has no hardware PWM" in text
