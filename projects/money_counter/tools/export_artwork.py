"""Export the digit sticker artwork into ``projects/money_counter/artwork/`` (run once after changes).

    .venv/bin/python projects/money_counter/tools/export_artwork.py

Writes ``flap_KK_front|back.svg|png`` (one per flap side, drawn as seen on the display),
``sheet_front.svg`` / ``sheet_back.svg`` (duplex sheets, flip on the long edge) and ``index.json``.
See piforge.mech.splitflap_art for which half goes where.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from mc_mech import module  # noqa: E402
from project import MATERIAL, PRINTER  # noqa: E402

from piforge.mech import digit_artwork  # noqa: E402


def main() -> None:
    out = HERE / "artwork"
    digit_artwork(out, module(PRINTER, MATERIAL), font="Arial", bold=True, dpi=300)
    print(f"wrote the sticker artwork ({len(list(out.iterdir()))} files) to {out}")


if __name__ == "__main__":
    main()
