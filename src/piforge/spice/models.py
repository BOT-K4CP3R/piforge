"""Device model library: SPICE ``.model``/``.subckt`` text fitted to datasheet key values.

Every model or subcircuit is named exactly like its key, so ``SpiceCircuit.D("1", "a", "k",
"led_red")`` works after the builder pulls in ``MODELS["led_red"]``. :data:`MODEL_INFO` records the
datasheet value each model reproduces, its source and the ratings the benches check against
(``tests/spice/test_models.py`` verifies every listed value by simulation).

Parameters are our own fits (diode: IS/N/RS solved for VF at the datasheet current; MOSFETs:
ngspice VDMOS with Vto/Kp/Rd/Rs/theta/ksubthres least-squares fitted to VGS(th) and RDS(on)),
except the two BJTs, which use the widely published Gummel-Poon parameters (National
Semiconductor data in the MicroSim/OrCAD evaluation library).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from piforge.core.errors import NotFoundError, ValidationError

__all__ = ["MODELS", "MODEL_INFO", "ModelInfo", "gpio_resistances", "list_models", "model_info",
           "model_text"]


@dataclass(frozen=True)
class ModelInfo:
    """Metadata for one library model.

    ``kind``: led | diode | zener | npn | nmos | switch | subckt. ``element``: SPICE letter used to
    instantiate it (D, Q, M, S, X). ``pins``: subcircuit pin order. ``ratings``: SI numbers the
    benches use (e.g. ``vds_max`` in V, ``id_max`` in A, ``pd_max`` in W, ``if_max`` in A).
    """

    key: str
    kind: str
    element: str
    description: str
    reproduces: str
    source: str
    pins: tuple[str, ...] = ()
    ratings: dict = field(default_factory=dict)


MODELS: dict[str, str] = {}
MODEL_INFO: dict[str, ModelInfo] = {}


def _add(info: ModelInfo, text: str) -> None:
    MODEL_INFO[info.key] = info
    MODELS[info.key] = text.strip() + "\n"


# ---------------------------------------------------------------------------- LEDs ----------
# IS solved from VF(20 mA) = N·Vt·ln(I/IS) + I·RS with Vt = 25.865 mV (TNOM 27 °C); N and RS chosen
# per technology (AlGaInP/GaP ≈ 2 / few Ω, InGaN ≈ 3.5 / 10 Ω, GaAs IR ≈ 1.6 / 1 Ω).
_LEDS = (
    # key, colour, IS, N, RS, VF typ @ 20 mA, IF max, source
    ("led_red", "red (AlGaInP, 624 nm)", 1.033e-18, 2.0, 3.0, 2.0, 0.025,
     "Everlight 333-2SURD/S530-A3: VF 2.0 V typ / 2.4 V max @ 20 mA, IF 25 mA"),
    ("led_green", "green (GaP, 568 nm)", 1.877e-18, 2.2, 5.0, 2.2, 0.025,
     "Kingbright WP7113GD: VF 2.2 V typ @ 20 mA, IF 25 mA"),
    ("led_blue", "blue (InGaN, 465 nm)", 2.686e-17, 3.5, 10.0, 3.3, 0.030,
     "Kingbright WP7113QBC/D: VF 3.3 V typ / 4.0 V max @ 20 mA, IF 30 mA"),
    ("led_white", "white (InGaN + phosphor)", 8.107e-17, 3.5, 10.0, 3.2, 0.030,
     "Everlight 334-15/T1C1-4WYA: VF 2.8–3.6 V @ 20 mA (model: 3.2 V mid-range), IF 30 mA"),
    ("led_ir", "infrared (GaAs, 940 nm)", 8.275e-15, 1.6, 1.0, 1.2, 0.100,
     "Everlight IR333-A: VF 1.2 V typ / 1.5 V max @ 20 mA, IF 100 mA"),
)
for _key, _colour, _is, _n, _rs, _vf, _ifmax, _src in _LEDS:
    _add(ModelInfo(_key, "led", "D", f"5 mm {_colour} indicator LED", f"VF = {_vf} V @ IF = 20 mA",
                   _src, ratings={"vf_typ": _vf, "if_test": 0.020, "if_max": _ifmax, "vr_max": 5.0}),
         # BV/IBV: datasheet VR = 5 V is a continuous reverse RATING (IR ≤ 10 µA there), not the breakdown
         # voltage; BV = 20 V assumption keeps reverse-biased LEDs from clamping at 5 V; CJO/TT: generic small-LED assumption.
         f".model {_key} D(IS={_is:.4g} N={_n:g} RS={_rs:g} BV=20 IBV=10u CJO=20p VJ=1.8 M=0.4 TT=20n)")

# ---------------------------------------------------------------------------- diodes --------
_add(ModelInfo("d1n4148", "diode", "D", "1N4148 small-signal switching diode",
               "VF = 0.72 V @ 10 mA (typical curve; datasheet max 1.0 V)",
               "onsemi/Vishay 1N4148: VF ≤ 1.0 V @ 10 mA, VRRM 100 V, CD ≤ 4 pF, trr ≤ 4 ns",
               ratings={"vf_max": 1.0, "if_test": 0.010, "if_max": 0.2, "vrrm": 100.0}),
     # TT 5 ns ≈ trr / ln(2) for IF = IR = 10 mA (trr ≤ 4 ns spec).
     ".model d1n4148 D(IS=1.41n N=1.75 RS=0.6 BV=100 IBV=100n CJO=4p VJ=0.7 M=0.4 TT=5n)")
_add(ModelInfo("d1n4007", "diode", "D", "1N4007 1 A / 1000 V standard-recovery rectifier",
               "VF = 0.93 V @ 1 A (typical curve; datasheet max 1.1 V)",
               "onsemi 1N4001-1N4007: VF ≤ 1.1 V @ 1 A, IF(AV) 1 A, VRRM 1000 V; CJ ≈ 15 pF @ 4 V",
               ratings={"vf_max": 1.1, "if_test": 1.0, "if_max": 1.0, "vrrm": 1000.0}),
     # TT 5 µs: assumption for a standard-recovery rectifier (trr is not specified, µs range).
     ".model d1n4007 D(IS=4.987n N=1.8 RS=0.04 BV=1000 IBV=5u CJO=28p VJ=0.7 M=0.33 TT=5u)")
_add(ModelInfo("d1n5819", "diode", "D", "1N5819 1 A / 40 V Schottky rectifier",
               "VF = 0.50 V @ 1 A (datasheet max 0.60 V; 0.90 V max @ 3 A)",
               "onsemi 1N5817-1N5819: VF ≤ 0.60 V @ 1 A, ≤ 0.90 V @ 3 A, VRRM 40 V",
               ratings={"vf_max": 0.60, "if_test": 1.0, "if_max": 1.0, "vrrm": 40.0}),
     # CJO 100 pF: assumption (typical 1 A Schottky); no stored charge (TT = 0).
     ".model d1n5819 D(IS=192.2n N=1.1 RS=0.06 BV=40 IBV=1m CJO=100p VJ=0.4 M=0.5 TT=0)")
_add(ModelInfo("zener_3v3", "zener", "D", "3.3 V 500 mW Zener diode (BZX79-C3V3 class)",
               "VZ = 3.3 V @ IZ = 5 mA (datasheet 3.1–3.5 V)",
               "Nexperia BZX79-C3V3: VZ 3.1–3.5 V @ IZT = 5 mA, Ptot 500 mW",
               ratings={"vz": 3.3, "iz_test": 0.005, "pd_max": 0.5}),
     # NBV = 10 gives the soft knee (≈ 50 Ω dynamic resistance at 5 mA) typical of low-voltage zeners.
     ".model zener_3v3 D(IS=1e-14 N=1 RS=1 BV=3.3 IBV=5m NBV=10 CJO=150p VJ=0.75 M=0.4)")

# ---------------------------------------------------------------------------- BJTs ----------
# src: National Semiconductor Gummel-Poon models as published in the MicroSim/OrCAD EVAL.LIB
# (Q2N2222 pid=19 TO18; Q2N3904 pid=23 TO92), widely reproduced in SPICE libraries.
_add(ModelInfo("q2n2222", "npn", "Q", "2N2222A general-purpose NPN (600 mA, 40 V)",
               "hFE 100–300 @ IC = 150 mA, VCE = 10 V; VCE(sat) ≤ 0.3 V @ 150/15 mA",
               "onsemi P2N2222A datasheet (limits); National Q2N2222 SPICE parameters",
               ratings={"vceo": 40.0, "ic_max": 0.6, "pd_max": 0.625, "hfe_min": 100.0,
                        "vce_sat_max": 0.3}),
     ".model q2n2222 NPN(Is=14.34f Xti=3 Eg=1.11 Vaf=74.03 Bf=255.9 Ne=1.307 Ise=14.34f Ikf=.2847\n"
     "+ Xtb=1.5 Br=6.092 Nc=2 Isc=0 Ikr=0 Rc=1 Cjc=7.306p Mjc=.3416 Vjc=.75 Fc=.5 Cje=22.01p\n"
     "+ Mje=.377 Vje=.75 Tr=46.91n Tf=411.1p Itf=.6 Vtf=1.7 Xtf=3 Rb=10)")
_add(ModelInfo("q2n3904", "npn", "Q", "2N3904 small-signal NPN (200 mA, 40 V)",
               "hFE 100–300 @ IC = 10 mA, VCE = 1 V; VCE(sat) ≤ 0.2 V @ 10/1 mA",
               "onsemi 2N3904 datasheet (limits); National Q2N3904 SPICE parameters",
               ratings={"vceo": 40.0, "ic_max": 0.2, "pd_max": 0.625, "hfe_min": 100.0,
                        "vce_sat_max": 0.2}),
     ".model q2n3904 NPN(Is=6.734f Xti=3 Eg=1.11 Vaf=74.03 Bf=416.4 Ne=1.259 Ise=6.734f Ikf=66.78m\n"
     "+ Xtb=1.5 Br=.7371 Nc=2 Isc=0 Ikr=0 Rc=1 Cjc=3.638p Mjc=.3085 Vjc=.75 Fc=.5 Cje=4.493p\n"
     "+ Mje=.2593 Vje=.75 Tr=239.5n Tf=301.2p Itf=.4 Vtf=4 Xtf=2 Rb=10)")

# ---------------------------------------------------------------------------- MOSFETs -------
# ngspice VDMOS (3-terminal, body diode built in). DC parameters least-squares fitted to the
# datasheet VGS(th) and typical RDS(on) points (≈ 70–80 % of the max limits). Body diode IS/RB
# from VSD; BV = 1.1 × V(BR)DSS (assumption: typical avalanche margin) so unclamped inductive
# turn-off clamps realistically. Capacitances from Ciss/Coss/Crss; RG and TT are assumptions.
_MOSFETS = (
    ("nmos_2n7000", "2N7000 60 V / 200 mA small-signal N-MOSFET (TO-92)",
     "VGS(th) 2.1 V @ 1 mA; RDS(on) 1.2 Ω @ 10 V/0.5 A, 1.8 Ω @ 4.5 V/75 mA (typ)",
     "onsemi 2N7000: VGS(th) 0.8–3.0 V; RDS(on) ≤ 5 Ω @ 10 V, ≤ 5.3 Ω @ 4.5 V; Ciss 20 pF, "
     "Crss 4 pF; VSD 0.88 V @ 400 mA",
     {"vds_max": 60.0, "id_max": 0.2, "pd_max": 0.4, "vgs_th": (0.8, 2.1, 3.0), "rds_on_max": 5.0},
     "Vto=2.155 Kp=0.511 Rd=0.3166 Rs=0.3166 theta=0.1542 ksubthres=0.1258 Rg=10 lambda=0.01\n"
     "+ Cgs=16p Cgdmax=15p Cgdmin=4p Cjo=40p Is=70f N=1 Rb=0.3 BV=66 IBV=10u TT=10n"),
    ("nmos_bss138", "BSS138 50 V / 220 mA logic-level N-MOSFET (SOT-23)",
     "VGS(th) 1.3 V @ 1 mA; RDS(on) 0.7 Ω @ 10 V, 1.0 Ω @ 4.5 V (0.22 A, typ)",
     "onsemi BSS138: VGS(th) 0.8–1.5 V; RDS(on) ≤ 3.5 Ω @ 10 V, ≤ 6.0 Ω @ 4.5 V; Ciss 27 pF, "
     "Coss 13 pF, Crss 6 pF; VSD 0.8 V typ @ 0.44 A",
     {"vds_max": 50.0, "id_max": 0.22, "pd_max": 0.36, "vgs_th": (0.8, 1.3, 1.5), "rds_on_max": 3.5},
     "Vto=1.380 Kp=0.7246 Rd=0.1863 Rs=0.1863 theta=0.1197 ksubthres=0.1244 Rg=10 lambda=0.01\n"
     "+ Cgs=21p Cgdmax=20p Cgdmin=6p Cjo=45p Is=490f N=1 Rb=0.2 BV=55 IBV=250u TT=10n"),
    ("nmos_ao3400", "AO3400 30 V / 5.7 A logic-level N-MOSFET (SOT-23)",
     "VGS(th) 1.05 V @ 250 µA; RDS(on) 19.6/23.1/36.4 mΩ @ 10/4.5/2.5 V (typ)",
     "Alpha & Omega AO3400: VGS(th) 0.65–1.45 V; RDS(on) < 28/33/52 mΩ @ VGS 10/4.5/2.5 V",
     {"vds_max": 30.0, "id_max": 5.7, "pd_max": 1.4, "vgs_th": (0.65, 1.05, 1.45),
      "rds_on_max": 0.028},
     "Vto=1.629 Kp=67.37 Rd=8.909m Rs=8.909m theta=0 ksubthres=0.1458 Rg=1.5 lambda=0.01\n"
     "+ Cgs=580p Cgdmax=250p Cgdmin=50p Cjo=300p Is=3.8p N=1 Rb=20m BV=33 IBV=250u TT=10n"),
    ("nmos_irlz44n", "IRLZ44N 55 V / 47 A logic-level N-MOSFET (TO-220)",
     "VGS(th) 1.5 V @ 250 µA; RDS(on) 16.9/20.3/23.8 mΩ @ 10/5/4 V (typ)",
     "Infineon IRLZ44N: VGS(th) 1.0–2.0 V; RDS(on) ≤ 22/25/35 mΩ @ VGS 10/5/4 V; Ciss 1700 pF, "
     "Coss 400 pF, Crss 150 pF",
     # pd_max: TO-220 without heatsink, RθJA 62 °C/W, TJ 175 °C → (175 − 25)/62 ≈ 2.4 W.
     {"vds_max": 55.0, "id_max": 47.0, "pd_max": 2.4, "vgs_th": (1.0, 1.5, 2.0), "rds_on_max": 0.022},
     "Vto=2.300 Kp=83.57 Rd=5.967m Rs=5.967m theta=0.2784 ksubthres=0.1852 Rg=2 lambda=0.01\n"
     "+ Cgs=1.55n Cgdmax=1n Cgdmin=150p Cjo=1.5n Is=6.4p N=1 Rb=10m BV=60.5 IBV=250u TT=50n"),
    ("nmos_irf540n", "IRF540N 100 V / 33 A standard-level N-MOSFET (TO-220)",
     "VGS(th) 3.2 V @ 250 µA; RDS(on) 33 mΩ @ 10 V/16 A (typ); ≈ off at VGS = 3.3 V",
     "Infineon IRF540N: VGS(th) 2.0–4.0 V; RDS(on) ≤ 44 mΩ @ 10 V/16 A; Ciss 1960 pF, "
     "Coss 250 pF, Crss 40 pF",
     {"vds_max": 100.0, "id_max": 33.0, "pd_max": 2.4, "vgs_th": (2.0, 3.2, 4.0), "rds_on_max": 0.044},
     "Vto=3.610 Kp=9.915 Rd=8.144m Rs=8.144m theta=0.002414 ksubthres=0.139 Rg=2 lambda=0.01\n"
     "+ Cgs=1.92n Cgdmax=600p Cgdmin=40p Cjo=1.2n Is=2.8p N=1 Rb=12m BV=110 IBV=250u TT=75n"),
)
for _key, _desc, _repro, _src, _ratings, _params in _MOSFETS:
    _add(ModelInfo(_key, "nmos", "M", _desc, _repro, _src, ratings=_ratings),
         f".model {_key} VDMOS({_params})")

# ---------------------------------------------------------------------------- switch --------
_add(ModelInfo("sw_ideal", "switch", "S", "Voltage-controlled switch (buttons, open-drain drivers)",
               "Ron 10 mΩ, Roff 1 GΩ; on above 0.6 V, off below 0.4 V (control V(cp, cn))",
               "assumption: near-ideal contact; Roff/Ron = 1e11 stays below ngspice's 1e12 limit",
               ratings={"ron": 0.01, "roff": 1e9}),
     ".model sw_ideal SW(RON=10m ROFF=1G VT=0.5 VH=0.1)")


# ---------------------------------------------------------------------------- Pi GPIO -------
def gpio_resistances(drive_ma: float = 8.0) -> tuple[float, float]:
    """Pi GPIO pad output resistance (high side, low side) in Ω for a drive strength in mA.

    src: raspberrypi/documentation ``gpio-pad-controls.adoc`` — drawing up to the drive-strength
    current the pad still guarantees VOH ≥ 3.0 V and VOL ≤ 0.14 V at VDD IO = 3.3 V; drivers are
    paralleled, so R ∝ 1/drive. At the reset default 8 mA (DRIVE = 3): 37.5 Ω / 17.5 Ω.
    """
    if drive_ma not in (2, 4, 6, 8, 10, 12, 14, 16):
        raise ValidationError(f"drive strength must be 2, 4, …, 16 mA, got {drive_ma!r}")
    i = drive_ma * 1e-3
    return (3.3 - 3.0) / i, 0.14 / i


_RH, _RL = gpio_resistances(8.0)
_add(ModelInfo("gpio_out", "subckt", "X", "Raspberry Pi GPIO output pad (push-pull, 3.3 V)",
               "VOH = 3.0 V sourcing 8 mA, VOL = 0.14 V sinking 8 mA (default 8 mA drive)",
               "raspberrypi/documentation gpio-pad-controls.adoc (VOH/VOL vs drive strength) and "
               "gpio-on-raspberry-pi.adoc (C_IN 5 pF typ); pins: pin vdd gnd ctrl",
               pins=("pin", "vdd", "gnd", "ctrl"),
               ratings={"rh": _RH, "rl": _RL, "i_safe": 0.016, "drive": 0.008}),
     f"""
.subckt gpio_out pin vdd gnd ctrl params: rh={_RH:g} rl={_RL:g} cpin=5p
* Pi GPIO pad: output high while V(ctrl,gnd) > V(vdd,gnd)/2. Smooth tanh switching of two
* conductances (rh to vdd, rl to gnd) keeps Newton iterations happy.
Bsel sel gnd V=0.5*(1+tanh(20*(V(ctrl,gnd)-0.5*V(vdd,gnd))))
Bh vdd pin I=V(sel,gnd)*V(vdd,pin)/{{rh}}
Bl pin gnd I=(1-V(sel,gnd))*V(pin,gnd)/{{rl}}
Cpin pin gnd {{cpin}}
* parasitic clamp structures to the rails (generic small-signal diode, assumption)
Dhi pin vdd dclamp
Dlo gnd pin dclamp
Rctl ctrl gnd 1G
.model dclamp D(IS=1e-15 N=1.2 RS=10)
.ends gpio_out
""")

# ---------------------------------------------------------------------------- loads ---------
_add(ModelInfo("relay_coil_5v", "subckt", "X", "5 V relay coil (Songle SRD-05VDC-SL-C class)",
               "70 Ω coil → 71.4 mA at 5 V; pick-up ≤ 3.75 V (75 %), drop-out ≥ 0.5 V (10 %)",
               "Songle SRD-05VDC-SL-C datasheet (coil 70 Ω ±10 %, 0.36 W). Inductance 150 mH, "
               "winding capacitance 30 pF and 10 kΩ core-loss resistor are assumptions "
               "(not specified; L/R ≈ 2 ms typical of small PCB relays)",
               pins=("p", "n"),
               ratings={"r_coil": 70.0, "i_nominal": 5.0 / 70.0, "v_pickup": 3.75, "v_dropout": 0.5,
                        "l_coil": 0.15}),
     """
.subckt relay_coil_5v p n params: rcoil=70 lcoil=150m cpar=30p rpar=10k
Rcoil p a {rcoil}
Lcoil a n {lcoil}
Cpar p n {cpar}
Rpar p n {rpar}
.ends relay_coil_5v
""")

# Small 5 V brushless fan (30 × 30 × 10 mm class) seen from its two supply wires. Its driver IC
# commutates the windings internally, so the terminals look like: a DC resistance setting the run
# current, a modest series inductance (windings + leads) and the driver's input capacitance.
# src: typical 3010 5 V fan datasheets (e.g. Sunon MF30100V3 series) — 0.08–0.2 A at 5 V; 50 Ω →
#      100 mA matches piforge.elec fan_5v (load_ohms 50). L = 2 mH, C = 100 nF with 10 Ω in series
#      (driver input / reverse-polarity protection; limits the switch-on inrush to ≈ 0.5 A) and the
#      10 kΩ leakage resistor are assumptions (not specified; typical driver-IC bypass capacitor).
_add(ModelInfo("fan_5v", "subckt", "X", "Small 5 V BLDC fan (30 mm class, ≈ 100 mA)",
               "50 Ω → 100 mA at 5 V",
               "generic 3010 5 V fan (0.08–0.2 A); L 2 mH, input capacitance 100 nF + 10 Ω and 10 kΩ "
               "leakage are assumptions",
               pins=("p", "n"),
               ratings={"r_dc": 50.0, "i_nominal": 0.1, "l_coil": 2e-3, "c_in": 100e-9, "r_in": 10.0}),
     """
.subckt fan_5v p n params: rfan=50 lfan=2m cin=100n rin=10 rleak=10k
Rfan p a {rfan}
Lfan a n {lfan}
Rin p c {rin}
Cin c n {cin}
Rleak p n {rleak}
.ends fan_5v
""")

# Mabuchi FA-130RA-2270 @ 1.5 V nominal: no-load 9100 r/min (952.9 rad/s) & 0.20 A; stall 2.20 A,
# 2.55 mN·m.  Ra = 1.5 V / 2.2 A = 0.682 Ω; Ke = Kt = (1.5 − 0.2·0.682)/952.9 = 1.431 mV·s/rad;
# viscous friction B = Kt·0.2 A / 952.9 = 3.004e-7 N·m·s. Rotor inertia 1e-7 kg·m² and armature
# inductance 150 µH are assumptions (not in the datasheet). Model stall torque = 3.15 mN·m (+24 %).
_add(ModelInfo("dc_motor_small", "subckt", "X", "Small brushed DC motor (Mabuchi FA-130RA-2270, 1.5–3 V)",
               "no-load 9100 r/min & 0.20 A, stall current 2.20 A at 1.5 V",
               "Mabuchi FA-130RA-2270 datasheet (1.5 V nominal); J and La are assumptions. "
               "Node x<inst>.w = shaft speed in rad/s",
               pins=("p", "n"),
               # v_max 3 V: upper end of the 1.5-3 V operating range in the Mabuchi FA-130RA datasheet
               ratings={"v_nominal": 1.5, "v_max": 3.0, "i_stall": 2.2, "i_noload": 0.2, "rpm_noload": 9100.0}),
     """
.subckt dc_motor_small p n params: ra=0.682 la=150u ke=1.431m kt=1.431m jm=1e-7 bm=3.004e-7 tload=0
Ra p a {ra}
La a b {la}
Vsense b c 0
* back-EMF from shaft speed V(w) [rad/s]
Bemf c n V={ke}*V(w)
* mechanics: torque Kt*i - Tload charges inertia J (capacitor), viscous friction 1/B (resistor)
Bt 0 w I={kt}*I(Vsense)-{tload}
Cm w 0 {jm}
Rm w 0 {1/bm}
.ends dc_motor_small
""")

# Official Raspberry Pi 15 W USB-C PSU: 5.1 V, 3 A, 1.5 m 18 AWG cable (product brief RP-008244-DS).
# 18 AWG copper = 20.9 mΩ/m → 2 × 1.5 m = 62.7 mΩ round trip. Loop inductance ≈ 0.8 µH/m for a
# two-wire cable (µ0/π·ln(d/r) with d ≈ 2 mm, r ≈ 0.5 mm) → 1.2 µH. Source resistance 20 mΩ is an
# assumption (≈ 1 % load regulation at 3 A).
_add(ModelInfo("psu_cable", "subckt", "X", "5.1 V USB power supply + cable (Pi official 15 W PSU)",
               "5.1 V source, 62.7 mΩ round-trip cable (1.5 m 18 AWG), 1.2 µH, 20 mΩ source",
               "Raspberry Pi 15W USB-C Power Supply product brief (5.1 V, 3 A, 1.5 m 18 AWG); "
               "AWG resistance table; two-wire inductance formula. pins: out gnd",
               pins=("out", "gnd"),
               ratings={"vset": 5.1, "i_max": 3.0, "rcable": 0.0627, "lcable": 1.2e-6, "rsrc": 0.02}),
     """
.subckt psu_cable out gnd params: vset=5.1 rsrc=20m rcable=62.7m lcable=1.2u
Vpsu src psun {vset}
Rsrc src a {max(rsrc,1u)}
Rp a b {max(rcable/2,1u)}
Lp b out {max(lcable/2,1p)}
Rn gnd c {max(rcable/2,1u)}
Ln c psun {max(lcable/2,1p)}
.ends psu_cable
""")


def model_text(key: str) -> str:
    """SPICE text (``.model``/``.subckt``) for library model ``key``; NotFoundError suggests matches."""
    k = str(key).lower()
    if k not in MODELS:
        raise NotFoundError("SPICE model", key, MODELS)
    return MODELS[k]


def model_info(key: str) -> ModelInfo:
    """:class:`ModelInfo` for ``key`` (raises NotFoundError with close matches)."""
    k = str(key).lower()
    if k not in MODEL_INFO:
        raise NotFoundError("SPICE model", key, MODEL_INFO)
    return MODEL_INFO[k]


def list_models(kind: str | None = None) -> list[str]:
    """Sorted model keys, optionally only one ``kind`` (led, diode, zener, npn, nmos, switch, subckt)."""
    return sorted(k for k, info in MODEL_INFO.items() if kind is None or info.kind == kind)
