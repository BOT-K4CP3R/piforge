"""PiForge — design, simulate, validate and export Raspberry Pi devices.

Subpackages are imported lazily by the user so that light-weight parts (electronics, SPICE,
digital twin) never pull in the OpenCascade CAD kernel:

- ``piforge.core``      findings/reports, errors
- ``piforge.fab``       printers, materials, printability, estimates, slicer bridge
- ``piforge.render``    headless PNG renders of meshes
- ``piforge.mech``      build123d CAD library: boards, fasteners, modules, enclosures, gears
- ``piforge.elec``      circuits, part library, ERC, power, pins, BOM, wiring, KiCad netlist
- ``piforge.spice``     ngspice simulation benches
- ``piforge.twin``      digital twin: virtual Raspberry Pi running real firmware
- ``piforge.analysis``  thermal, port access, (FEA)
- ``piforge.project``   project definition + build pipeline; ``piforge.cli`` the CLI
"""

__version__ = "0.1.0"
