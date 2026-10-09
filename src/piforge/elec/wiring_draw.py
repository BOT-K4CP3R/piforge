"""Matplotlib renderer for :func:`piforge.elec.wiring.wiring_diagram` (matplotlib imported in :func:`draw`).

Layout (inches, y grows downward until the final flip): the Pi 2x20 header on the left with pin
names outside the two columns; parts as boxes in columns ordered by graph distance from the Pi
(parts wired to the Pi in column 1, parts wired only to those in column 2…). Wires run in vertical
lanes between columns; a wire leaving the header slips between header rows (odd pins above their
row, even pins below) so it never covers a label. Lanes are ordered to minimise crossings. Each box
row also names where its wire comes from ("pin 11", "R1.2"), so the picture reads even where wires cross.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

from piforge.elec.bom import part_value
from piforge.elec.model import Circuit, Part, PinType
from piforge.elec.wiring import WireRow

# every signal colour is >= 20 CIE76 dE (or 2:1 contrast) from every other; GND black, 5V red, 3V3 orange
HEX = {"black": "#1b1b1b", "red": "#d62728", "orange": "#f28500", "blue": "#1f5fd6", "yellow": "#d9b400",
       "green": "#2a9d3a", "white": "#b5b5b5", "purple": "#8e44ad", "grey": "#6f6f6f", "pink": "#e377c2",
       "cyan": "#12a5b8", "brown": "#8c564b", "olive": "#7a7a00", "lime": "#9ccc3c"}


def _luminance(hexcol: str) -> float:
    """WCAG relative luminance of ``#rrggbb``."""
    ch = [int(hexcol[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in ch]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def label_color(fill: str) -> str:
    """Text colour (``"white"`` or near-black) with the higher WCAG contrast on ``fill``."""
    lum = _luminance(fill)
    dark_text = _luminance("#111111")
    return "white" if (1.05 / (lum + 0.05)) >= ((lum + 0.05) / (dark_text + 0.05)) else "#111111"


P = 0.30            # header row pitch
PIN_R = 0.078
X_ODD = 1.05
X_EVEN = X_ODD + 0.30
Y0 = 1.05           # first header row
LANE = 0.085
BOX_W = 1.75
ROW_H = 0.2
TITLE_H = 0.36
BOX_GAP = 0.24
CH_PAD = 0.18       # gap between a column edge and its first/last lane
FONT = "DejaVu Sans"


@dataclass
class End:
    col: int
    x_l: float          # wire attach point when arriving from the left
    x_r: float          # wire attach point when leaving to the right
    y: float
    stub: list = field(default_factory=list)  # header: [(x_pin, y_pin), (x_exit, y_exit)]
    label: str = ""


@dataclass
class Group:
    src: End
    targets: list[End]
    color: str
    u_turn: bool = False

    def span(self) -> tuple[float, float]:
        ys = [self.src.y] + [t.y for t in self.targets]
        return min(ys), max(ys)


def _layers(circuit: Circuit, rows: list[WireRow]) -> dict[str, int]:
    adj: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        if r.a_ref != r.b_ref:
            adj[r.a_ref].add(r.b_ref)
            adj[r.b_ref].add(r.a_ref)
    layer: dict[str, int] = {}
    starts = ([(circuit.board.ref, 0)] if circuit.board else []) + [(p.ref, 1) for p in circuit.parts]
    for ref, base in starts:
        if ref in layer or (base == 1 and not adj[ref]):
            continue
        layer[ref] = base
        q = deque([ref])
        while q:
            x = q.popleft()
            for y in sorted(adj[x]):
                if y not in layer:
                    layer[y] = layer[x] + 1
                    q.append(y)
    return layer


def _cost(g: Group, h: Group) -> int:
    """Crossings when lane of ``g`` is left of lane of ``h``."""
    lo_g, hi_g = g.span()
    lo_h, hi_h = h.span()
    c = 0
    if not g.u_turn:
        c += sum(1 for t in g.targets if lo_h < t.y < hi_h)
    c += 1 if lo_g < h.src.y < hi_g else 0
    if h.u_turn:
        c += sum(1 for t in h.targets if lo_g < t.y < hi_g)
    return c


def _order(groups: list[Group]) -> list[int]:
    n = len(groups)
    if n < 2:
        return list(range(n))
    cost = [[_cost(groups[i], groups[j]) if i != j else 0 for j in range(n)] for i in range(n)]

    def total(order: list[int]) -> int:
        return sum(cost[order[i]][order[j]] for i in range(n) for j in range(i + 1, n))

    mid = lambda g: sum(g.span()) / 2  # noqa: E731
    seeds = [sorted(range(n), key=lambda i: groups[i].src.y),
             sorted(range(n), key=lambda i: -groups[i].src.y),
             sorted(range(n), key=lambda i: (groups[i].u_turn is False, groups[i].span()[1] - groups[i].span()[0])),
             sorted(range(n), key=lambda i: mid(groups[i]))]
    best, best_cost = None, None
    for order in seeds:
        order = list(order)
        for _ in range(60):  # adjacent-swap hill climbing on the pairwise cost matrix
            improved = False
            for k in range(n - 1):
                a, b = order[k], order[k + 1]
                if cost[b][a] < cost[a][b]:
                    order[k], order[k + 1] = b, a
                    improved = True
            if not improved:
                break
        c = total(order)
        if best_cost is None or c < best_cost:
            best, best_cost = order, c
    return best


def _short(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def draw(circuit: Circuit, rows: list[WireRow]):
    """Build and return the matplotlib Figure (Agg canvas attached)."""
    import matplotlib.patheffects as pe
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.patches import Circle, FancyBboxPatch, Rectangle

    board = circuit.board
    layer = _layers(circuit, rows)
    parts = [p for p in circuit.parts if p is not board]
    loose = [p for p in parts if p.ref not in layer]
    ncols = max([v for v in layer.values()] + [0])
    if loose:
        ncols += 1
        for p in loose:
            layer[p.ref] = ncols
    header_y = {}
    if board is not None:
        for p in board.pins():
            n = int(p.number)
            header_y[p.number] = Y0 + ((n - 1) // 2) * P
    # incoming labels per (part, pin)
    incoming: dict[tuple[str, str], str] = {}
    for r in rows:
        src = f"pin {r.a_phys}" if board is not None and r.a_ref == board.ref else f"{r.a_ref}.{r.a_pin}"
        incoming.setdefault((r.b_ref, r.b_phys), src)
        if board is None or r.a_ref != board.ref:
            incoming.setdefault((r.a_ref, r.a_phys), f"{r.b_ref}.{r.b_pin}")

    # channel widths need lane counts, which need groups: build groups with provisional x, then place
    cols: dict[int, list[Part]] = defaultdict(list)
    for p in parts:
        cols[layer[p.ref]].append(p)
    box_top: dict[str, float] = {}
    pin_y: dict[tuple[str, str], float] = {}

    def box_h(p: Part) -> float:
        return TITLE_H + max(1, len(p.pins())) * ROW_H + 0.1

    # order columns by barycentre of their connections to the previous column
    for col in range(1, ncols + 1):
        def bary(p: Part) -> float:
            ys = []
            for r in rows:
                for me, other_ref, other_phys in ((r.b_ref, r.a_ref, r.a_phys), (r.a_ref, r.b_ref, r.b_phys)):
                    if me != p.ref:
                        continue
                    if board is not None and other_ref == board.ref:
                        ys.append(header_y.get(other_phys, 0.0))
                    elif (other_ref, other_phys) in pin_y and layer.get(other_ref, 99) < col:
                        ys.append(pin_y[(other_ref, other_phys)])
            return sum(ys) / len(ys) if ys else 1e6
        cols[col].sort(key=lambda p: (bary(p), parts.index(p)))
        y = Y0 - 0.2
        for p in cols[col]:
            box_top[p.ref] = y
            for i, pin in enumerate(p.pins()):
                pin_y[(p.ref, pin.number)] = y + TITLE_H + (i + 0.5) * ROW_H
            y += box_h(p) + BOX_GAP

    def end_of(ref: str, phys: str) -> End:
        if board is not None and ref == board.ref:
            yy = header_y[phys]
            odd = int(phys) % 2 == 1
            x_pin = X_ODD if odd else X_EVEN
            y_exit = yy - 0.33 * P if odd else yy + 0.33 * P
            x_exit = X_ODD + 0.15 if odd else X_EVEN + 0.15
            dx, dy = x_exit - x_pin, y_exit - yy
            k = PIN_R / (dx * dx + dy * dy) ** 0.5
            return End(0, x_exit, x_exit, y_exit, stub=[(x_pin + dx * k, yy + dy * k), (x_exit, y_exit)])
        return End(layer[ref], 0.0, 0.0, pin_y[(ref, phys)])

    # groups per channel
    channels: dict[int, dict[tuple, Group]] = defaultdict(dict)
    for r in rows:
        a, b = end_of(r.a_ref, r.a_phys), end_of(r.b_ref, r.b_phys)
        a.label, b.label = r.a_ref, r.b_ref
        if b.col < a.col:
            a, b = b, a
        ch = a.col
        key = (a.label, round(a.y, 4), a.col, a.col == b.col)
        g = channels[ch].get(key)
        if g is None:
            g = channels[ch][key] = Group(a, [], r.color, u_turn=(a.col == b.col))
        g.targets.append(b)
    lanes = {ch: _order(list(gs.values())) for ch, gs in channels.items()}

    # x positions: header block, then for each column: channel lanes + boxes
    x = X_EVEN + 0.8 if board is not None else 0.4
    col_x: dict[int, float] = {}
    lane_x: dict[int, list[float]] = {}
    for col in range(0, ncols + 1):
        if col > 0:
            col_x[col] = x
            x += BOX_W
        n = len(channels.get(col, {}))
        lane_x[col] = [x + CH_PAD + k * LANE for k in range(n)]
        x += 2 * CH_PAD + max(0, n - 1) * LANE if n else 0.35
    width = x + 0.25
    bottom = max([Y0 + 19 * P + 0.6 if board is not None else 1.0] +
                 [box_top[p.ref] + box_h(p) for p in parts]) + 0.8
    height = bottom + 0.1

    def fy(y: float) -> float:  # flip to matplotlib's upward y
        return height - y

    fig = Figure(figsize=(width, height))
    FigureCanvasAgg(fig)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(0, width)
    ax.set_ylim(0, height)
    ax.set_aspect("equal")
    ax.axis("off")
    fig.patch.set_facecolor("white")
    title = f"Wiring — {circuit.name}" + (f"  ({board.ref}: {board.name})" if board is not None else "")
    title_art = ax.text(0.35, fy(0.32), title, fontsize=12, fontweight="bold", va="center", family=FONT)
    renderer = fig.canvas.get_renderer()
    while len(title) > 12 and title_art.get_window_extent(renderer).width / fig.dpi > width - 0.5:
        title = title[:-2].rstrip(" ,(—·…") + "…"  # long project name: ellipsize to stay inside the image
        title_art.set_text(title)

    # boxes
    for p in parts:
        bx, by, h = col_x[layer[p.ref]], box_top[p.ref], box_h(p)
        ax.add_patch(FancyBboxPatch((bx, fy(by + h)), BOX_W, h, boxstyle="round,pad=0,rounding_size=0.05",
                                    facecolor="#f6f6f1", edgecolor="#4a4a4a", linewidth=0.9, zorder=2))
        ax.add_patch(Rectangle((bx, fy(by + TITLE_H)), BOX_W, TITLE_H, facecolor="#e2e6ee", edgecolor="none", zorder=2))
        val = part_value(p)
        ax.text(bx + 0.07, fy(by + 0.12), f"{p.ref}", fontsize=7.5, fontweight="bold", va="center", zorder=3, family=FONT)
        ax.text(bx + 0.07, fy(by + 0.27), _short(p.name + (f" · {val}" if val else ""), 34), fontsize=5.6,
                va="center", zorder=3, family=FONT, color="#333333")
        note = ("plugs into the Pi's power input" if "psu" in p.features and not any(
            (p.ref, pin.number) in incoming for pin in p.pins()) else
                "camera ribbon (CSI), no header pins" if not p.pins() else "")
        if note:
            ax.text(bx + BOX_W - 0.07, fy(by + 0.12), note, fontsize=5.2, va="center", ha="right",
                    color="#666666", zorder=3, family=FONT, style="italic")
        for pin in p.pins():
            yy = pin_y[(p.ref, pin.number)]
            src = incoming.get((p.ref, pin.number))
            ax.text(bx + 0.07, fy(yy), f"{pin.number:>2}  {pin.name}", fontsize=6, va="center", zorder=3,
                    family=FONT, color="#111111" if src else "#9a9a9a", fontweight="bold" if src else "normal")
            if src:
                ax.text(bx + BOX_W - 0.07, fy(yy), src, fontsize=5.4, va="center", ha="right", zorder=3,
                        family=FONT, color="#555555")
    # header
    if board is not None:
        top, bot = Y0 - 0.17, Y0 + 19 * P + 0.17
        ax.add_patch(FancyBboxPatch((X_ODD - 0.115, fy(bot)), X_EVEN - X_ODD + 0.23, bot - top,
                                    boxstyle="round,pad=0,rounding_size=0.05", facecolor="#20462c",
                                    edgecolor="#102a18", zorder=2))
        ax.text(X_ODD + 0.15, fy(Y0 - 0.38), f"{board.ref} 40-pin header", fontsize=7, ha="center",
                fontweight="bold", family=FONT)
        used = {r.a_phys for r in rows if r.a_ref == board.ref} | {r.b_phys for r in rows if r.b_ref == board.ref}
        pin_color = {r.a_phys: r.color for r in rows if r.a_ref == board.ref}
        for pin in board.pins():
            yy, odd = header_y[pin.number], int(pin.number) % 2 == 1
            xx = X_ODD if odd else X_EVEN
            if pin.type == PinType.GND:
                fc = HEX["black"]
            elif pin.name == "5V":
                fc = HEX["red"]
            elif pin.name == "3V3":
                fc = HEX["orange"]
            else:
                fc = HEX.get(pin_color.get(pin.number, ""), "#dcdcdc") if pin.number in used else "#dcdcdc"
            ax.add_patch(Circle((xx, fy(yy)), PIN_R, facecolor=fc, edgecolor="#f0f0f0", linewidth=0.6, zorder=4))
            ax.text(xx, fy(yy), pin.number, fontsize=4.9, ha="center", va="center", zorder=5, family=FONT,
                    color=label_color(fc))
            label = pin.name + (f" ({pin.aliases[0]})" if pin.name in ("GPIO0", "GPIO1") else "")
            ax.text(xx - 0.17 if odd else xx + 0.17, fy(yy), label, fontsize=6.3, ha="right" if odd else "left",
                    va="center", family=FONT, zorder=5, color="#111111" if pin.number in used else "#8c8c8c",
                    fontweight="bold" if pin.number in used else "normal")
    # wires
    halo = [pe.Stroke(linewidth=3.0, foreground="#202020", alpha=0.35), pe.Normal()]
    for ch, gs in channels.items():
        glist = list(gs.values())
        for k, gi in enumerate(lanes[ch]):
            g = glist[gi]
            lx = lane_x[ch][k]
            color = HEX.get(g.color, "#444444")
            src_x = g.src.x_r if g.src.col == 0 else col_x[g.src.col] + BOX_W
            pts = list(g.src.stub) + [(src_x, g.src.y), (lx, g.src.y)]
            lo, hi = g.span()
            for t in g.targets:
                tx = (t.x_l if t.col == 0 else col_x[t.col] + BOX_W) if g.u_turn else \
                    (t.x_l if t.col == 0 else col_x[t.col])
                path = [(lx, t.y), (tx, t.y)] + list(reversed(t.stub))
                ax.plot([p[0] for p in path], [fy(p[1]) for p in path], color=color, linewidth=1.5,
                        solid_capstyle="round", zorder=6, path_effects=halo)
                if len(g.targets) > 1:
                    ax.add_patch(Circle((lx, fy(t.y)), 0.025, facecolor=color, edgecolor="none", zorder=7))
            ax.plot([p[0] for p in pts], [fy(p[1]) for p in pts], color=color, linewidth=1.5,
                    solid_capstyle="round", zorder=6, path_effects=halo)
            if lo < hi:
                ax.plot([lx, lx], [fy(lo), fy(hi)], color=color, linewidth=1.5, zorder=6, path_effects=halo)
    # legend (two rows so it fits narrow figures)
    items = [[("5V", "red"), ("3V3", "orange"), ("GND", "black"), ("other supply", "brown")],
             [("I2C SDA", "blue"), ("I2C SCL", "yellow")]]
    for row, ly in zip(items, (bottom - 0.5, bottom - 0.25)):
        lx = 0.35
        for name, col in row:
            ax.plot([lx, lx + 0.3], [fy(ly), fy(ly)], color=HEX[col], linewidth=2.2, path_effects=halo)
            ax.text(lx + 0.36, fy(ly), name, fontsize=6.5, va="center", family=FONT)
            lx += 0.36 + 0.065 * len(name) + 0.3
    ax.text(lx, fy(bottom - 0.25), "other signals: one colour per net", fontsize=6.5, va="center",
            family=FONT, color="#444444")
    return fig
