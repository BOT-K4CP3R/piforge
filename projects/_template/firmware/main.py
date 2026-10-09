"""Template firmware: each press of the button on GPIO17 toggles the LED on GPIO27.

Runs unmodified on a Raspberry Pi (gpiozero) and in the PiForge digital twin
(``piforge twin run``, ``piforge twin test`` and the Twin tab of ``piforge serve``).
"""

from signal import pause

from gpiozero import LED, Button

led = LED(27)  # GPIO27 → R1 (330 Ω) → D1 → GND
button = Button(17)  # GPIO17 → SW1 → GND; gpiozero enables the internal pull-up


def toggle() -> None:
    """Flip the LED and log the new state."""
    led.toggle()
    print(f"button pressed -> LED {'on' if led.is_lit else 'off'}", flush=True)


button.when_pressed = toggle
print("template firmware ready: press the button to toggle the LED", flush=True)
pause()
