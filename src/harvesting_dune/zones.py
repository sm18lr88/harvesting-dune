# Zones: the map grid (fitted from the companion's markers) and Deep Desert PvE/PvP rows.
"""Map grid cells (A1..I9) and Deep Desert PvE / PvP rows.

The Deep Desert is a 9 x 9 grid, rows A (south, bottom of the map) to I (north). Since
patch 1.1.0.17 the southern half is PvE and the northern half PvP, and PvP resource yields
are 2.5x.

Where the grid lies is learned, not assumed: the companion's own map markers carry their grid
cell ("H5") next to their world position, so every map is fitted from those (fit_grid) whenever
the detector starts. DEFAULT_FIT (fitted the same way from deepdesert_1_07) is only the fallback
until the first fit. Control points and shipwrecks are PvP everywhere; they are not resource
areas, so ignored.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

ROWS = "ABCDEFGHI"
DEFAULT_LAST_PVE_ROW = "D"
DEFAULT_LINES_FROM_ROW = "A"  # lines show from the entrance (the southern edge) on
DEFAULT_PVP_YIELD = 2.5
MIN_FIT_SAMPLES = 20
MIN_FIT_AGREEMENT = 0.9  # share of markers the fit must place in their own labelled cell


@dataclass(frozen=True, slots=True)
class GridFit:
    row_a: float  # y of row A's centre
    row_step: float  # y change from one row to the next (negative: A is the south, +y edge)
    col_1: float  # x of column 1's centre
    col_step: float
    samples: int = 0  # 0: the built-in fallback

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: object) -> GridFit | None:
        try:
            fit = cls(**{k: d[k] for k in ("row_a", "row_step", "col_1", "col_step", "samples")})  # type: ignore[index]
        except (TypeError, KeyError):
            return None
        return fit if abs(fit.row_step) > 1000 and abs(fit.col_step) > 1000 else None


DEFAULT_FIT = GridFit(1_029_650.0, -270_300.0, -1_133_050.0, 270_300.0)
# ROW_HEIGHT_CM kept for callers that only need the cell size
ROW_HEIGHT_CM = abs(DEFAULT_FIT.row_step)


def is_deep_desert(world_id: str | None) -> bool:
    return bool(world_id) and world_id.lower().startswith("deepdesert")


def row_index(y: float, fit: GridFit = DEFAULT_FIT) -> int:
    return min(max(math.floor((y - fit.row_a) / fit.row_step + 0.5), 0), len(ROWS) - 1)


def row_of(y: float, fit: GridFit = DEFAULT_FIT) -> str:
    return ROWS[row_index(y, fit)]


def col_of(x: float, fit: GridFit = DEFAULT_FIT) -> int:
    return min(max(math.floor((x - fit.col_1) / fit.col_step + 0.5) + 1, 1), len(ROWS))


def cell_name(x: float, y: float, fit: GridFit = DEFAULT_FIT) -> str:
    """Grid cell as the companion's map labels it, e.g. "H5"."""
    return f"{row_of(y, fit)}{col_of(x, fit)}"


def is_pvp(y: float, last_pve_row: str = DEFAULT_LAST_PVE_ROW, fit: GridFit = DEFAULT_FIT) -> bool:
    return row_index(y, fit) > ROWS.index(last_pve_row)


def yield_multiplier(y: float, last_pve_row: str, pvp_yield: float, fit: GridFit = DEFAULT_FIT) -> float:
    """Resource yield multiplier at map position y: pvp_yield in PvP rows, else 1."""
    return pvp_yield if is_pvp(y, last_pve_row, fit) else 1.0


def _line(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Least squares v = a + b * k over (k, v)."""
    n = len(points)
    if n < 2:
        return None
    mk = sum(k for k, _ in points) / n
    mv = sum(v for _, v in points) / n
    var = sum((k - mk) ** 2 for k, _ in points)
    if var == 0:
        return None
    b = sum((k - mk) * (v - mv) for k, v in points) / var
    return mv - b * mk, b


def _axis_index(v: float, a: float, b: float) -> int:
    return math.floor((v - a) / b + 0.5)


def _fit_axis(points: list[tuple[int, float]]) -> tuple[float, float] | None:
    """Centre of cell 0 and step along one axis from (cell index, coordinate) pairs.

    Markers bunch up inside cells, so a line through the markers themselves is skewed (it put
    28% of real markers in the wrong cell). The borders are what matter: between neighbouring
    cells k and k+1 the border lies in the gap between their markers. A line through those gap
    midpoints gives the grid, then a small search keeps the version that puts the most markers
    in their own cell.
    """
    rough = _line(points)
    if rough is None or rough[1] == 0:
        return None
    sign = 1.0 if rough[1] > 0 else -1.0
    by = {}
    for k, v in points:
        by.setdefault(k, []).append(v)
    borders = []
    for k in sorted(by):
        if k + 1 in by:
            # sign > 0: cell k lies below cell k+1; the border sits between them
            lo = max(by[k]) if sign > 0 else max(by[k + 1])
            hi = min(by[k + 1]) if sign > 0 else min(by[k])
            borders.append((k + 0.5, (lo + hi) / 2))
    a, b = _line(borders) if len(borders) >= 2 else rough

    def agree(a_: float, b_: float) -> int:
        return sum(1 for k, v in points if _axis_index(v, a_, b_) == k)

    best = (agree(a, b), a, b)
    for i in range(-30, 31):  # step within +-6 %, offset within +-15 % of a cell
        bb = b * (1 + i * 0.002)
        for j in range(-15, 16):
            aa = a + j * 0.01 * abs(bb)
            score = agree(aa, bb)
            if score > best[0]:
                best = (score, aa, bb)
    return best[1], best[2]


def fit_agreement(fit: GridFit, samples: list) -> float:
    """Share of (cell, x, y) samples this grid puts in their own labelled cell."""
    ok = n = 0
    for sample in samples:
        try:
            cell, x, y = sample
            label = f"{str(cell)[0].upper()}{int(str(cell)[1:])}"
            got = cell_name(float(x), float(y), fit)
        except (TypeError, ValueError, IndexError):
            continue
        n += 1
        ok += got == label
    return ok / n if n else 0.0


def fit_grid(samples: list) -> GridFit | None:
    """Fit the grid from (cell, x, y) marker samples, e.g. [("H5", -103796.0, -906916.0), ...].

    Returns None unless there are enough markers across several rows and columns and the fit
    puts at least MIN_FIT_AGREEMENT of them back into their own labelled cell.
    """
    rows, cols, good = [], [], []
    for sample in samples:
        try:
            cell, x, y = sample
            letter, number = str(cell)[0].upper(), int(str(cell)[1:])
            x, y = float(x), float(y)
        except (TypeError, ValueError, IndexError):
            continue
        if letter not in ROWS or not 1 <= number <= len(ROWS) or not (math.isfinite(x) and math.isfinite(y)):
            continue
        rows.append((ROWS.index(letter), y))
        cols.append((number - 1, x))
        good.append((cell, x, y))
    if len(rows) < MIN_FIT_SAMPLES or len({r for r, _ in rows}) < 3 or len({c for c, _ in cols}) < 3:
        return None
    ry, cx = _fit_axis(rows), _fit_axis(cols)
    if ry is None or cx is None:
        return None
    fit = GridFit(ry[0], ry[1], cx[0], cx[1], len(rows))
    if abs(fit.row_step) < 1000 or abs(fit.col_step) < 1000:
        return None
    return fit if fit_agreement(fit, good) >= MIN_FIT_AGREEMENT else None
