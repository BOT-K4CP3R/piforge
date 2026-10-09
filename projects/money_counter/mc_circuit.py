"""Circuit of the money counter (imported by ``project.py``).

Pi Zero 2 W ─SPI0─▶ 4 × 74HCT595 (daisy chain, 32 outputs) ─▶ 8 × ULN2003 driver boards ─▶ 8 × 28BYJ-48
                  ◀─ 8 × A3144 Hall switches (open collector, internal pull-ups) ─ GPIO 4,5,6,13,16,19,20,21

* One 5 V 5 A barrel PSU feeds the 5 V rail: the Pi (through its 5 V header pins), the 595s, the
  driver boards (COM/+ to 5 V, so the ULN2003's clamp diodes catch the coil flyback) and the Halls.
* 74HCT595 (not HC): at 5 V its inputs accept the Pi's 3.3 V levels (VIH 2.0 V); its 5 V outputs
  are fine for the ULN2003 inputs (VIH 2.4 V).
* Module ``i`` (0 = leftmost) = motor ``M{i+1}``, driver ``U{11+i}``, Hall ``H{i+1}``; its coils
  IN1…IN4 are chain outputs ``4i … 4i+3`` (chip ``i // 2``, QA–QD for even, QE–QH for odd ``i``).
"""

from __future__ import annotations

from piforge.project import Project

BOARD = "rpizero2w"
N = 8
SR_REFS = ("U2", "U3", "U4", "U5")          # U2 = chain head (SER on MOSI) = twin device id
DRIVER_REF = "U{}"                           # U11 … U18
QS = "ABCDEFGH"


def module_bits(i: int) -> list[int]:
    """Chain bit indices of module ``i``'s IN1…IN4 (bit b = chip b // 8, output Q(b % 8))."""
    return [4 * i + j for j in range(4)]


def build_circuit(p: Project, hall_pins: tuple[int, ...]) -> None:
    c = p.circuit
    pi = c.add(BOARD, "U1")
    ps = c.add("psu_dc_5v5a", "PS1")             # 5 V 5 A barrel adapter → panel jack → perfboard rail
    c.connect(ps["V+"], pi["5V"])
    c.connect(ps["GND"], pi["GND"])
    # bulk capacitor on the 5 V rail at the perfboard (motor current steps of ~0.2 A per module)
    bulk = c.add("capacitor", "C1", value="470u", voltage_rating=16.0, kind="electrolytic")
    c.connect(bulk["+"], pi["5V"])
    c.connect(bulk["-"], pi["GND"])

    # 4 × 74HCT595 on SPI0: MOSI → SER (chip 0), SCLK → SRCLK, CE0 → RCLK (latch); OE low, SRCLR high
    srs = [c.add("sn74hct595", ref) for ref in SR_REFS]
    c.connect(pi["GPIO10"], srs[0]["SER"])
    for k, sr in enumerate(srs):
        c.connect(pi["GPIO11"], sr["SRCLK"])
        c.connect(pi["GPIO8"], sr["RCLK"])
        c.connect(sr["VCC"], pi["5V"], sr["SRCLR"])
        c.connect(sr["GND"], pi["GND"], sr["OE"])
        if k:
            c.connect(srs[k - 1]["QH'"], sr["SER"])
        dec = c.add("capacitor", f"C{2 + k}", value="100n")          # 100 nF at each chip's VCC pin
        c.connect(dec["1"], sr["VCC"])
        c.connect(dec["2"], pi["GND"])

    # 8 × (ULN2003 board + 28BYJ-48 + A3144)
    for i in range(N):
        drv = c.add("uln2003_board", DRIVER_REF.format(11 + i))
        mot = c.add("stepper_28byj48", f"M{i + 1}")
        hall = c.add("hall_a3144", f"H{i + 1}")
        sr = srs[i // 2]
        c.connect(drv["+"], pi["5V"])                 # board '+' = ULN2003 COM = motor red wire
        c.connect(drv["-"], pi["GND"])
        c.connect(drv["M+"], mot["COM"])
        for j, coil in enumerate("ABCD"):
            c.connect(sr["Q" + QS[4 * (i % 2) + j]], drv[f"IN{j + 1}"])
            c.connect(drv[coil], mot[coil])
        c.connect(hall["VCC"], pi["5V"])
        c.connect(hall["GND"], pi["GND"])
        c.connect(hall["OUT"], pi[f"GPIO{hall_pins[i]}"])

    c.configure(pi, interfaces={"spi": True}, pulls={f"GPIO{b}": "up" for b in hall_pins})
