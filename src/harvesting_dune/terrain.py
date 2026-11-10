# Terrain: vehicle-aware passability learned from tracks and heights, and route search.
"""Vehicle-aware traversability learned from observed evidence.

Evidence sources, strongest first:
  * manual blocks (user marks a cell impassable for a vehicle)
  * driven tracks (a cell a vehicle actually drove through is passable for it and for
    any vehicle with a laxer terrain envelope)
  * elevation samples (player pose z, and z of every actor the companion reports:
    ore nodes on rock outcrops sit far above the sand, so they reveal mountains the
    player has never visited)

Cells with no evidence are "unknown": traversable at an extra cost and reported as
lower confidence, never silently treated as proven.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Iterable
from dataclasses import dataclass, field

CELL_CM = 5000.0  # 50 m cells


@dataclass(frozen=True, slots=True)
class VehicleProfile:
    name: str
    max_slope_deg: float  # steepest sustained grade between neighbouring cells
    max_roughness_deg: float  # steepest grade implied by elevation spread inside one cell
    speed_cms: float  # typical cruising speed over sand, cm/s
    loaded_speed_cms: float
    capacity: float  # spice sand units carried per trip
    harvest_rate: float  # units per second while harvesting
    unknown_cost: float  # travel multiplier through cells with no evidence
    min_footprint: float  # fraction of a field's area that must be drivable
    strictness: int  # higher = pickier terrain envelope
    flies: bool = False  # ignores terrain entirely (ornithopter)
    harvests_spice: bool = False  # can harvest spice sand by driving over a field

    def speed_for(self, loaded: bool) -> float:
        return self.loaded_speed_cms if loaded else self.speed_cms


# Starting estimates only; every number here is meant to be calibrated from journals.
VEHICLES: dict[str, VehicleProfile] = {
    "sandbike": VehicleProfile("sandbike", 35.0, 42.0, 2600.0, 2400.0, 300.0, 0.0, 1.2, 0.2, 0),
    "buggy": VehicleProfile("buggy", 32.0, 40.0, 2000.0, 1800.0, 1500.0, 12.0, 1.25, 0.25, 1, harvests_spice=True),
    "sandcrawler": VehicleProfile("sandcrawler", 18.0, 24.0, 900.0, 800.0, 10000.0, 60.0, 1.6, 0.5, 2, harvests_spice=True),
    "ornithopter": VehicleProfile("ornithopter", 90.0, 90.0, 4500.0, 4000.0, 1200.0, 0.0, 1.0, 0.0, -1, flies=True),
}
GROUND_VEHICLES = tuple(k for k, v in VEHICLES.items() if not v.flies)


def cell_of(x: float, y: float) -> tuple[int, int]:
    return (math.floor(x / CELL_CM), math.floor(y / CELL_CM))


def cell_center(c: tuple[int, int]) -> tuple[float, float]:
    return ((c[0] + 0.5) * CELL_CM, (c[1] + 0.5) * CELL_CM)


@dataclass(slots=True)
class CellEvidence:
    z_min: float = math.inf
    z_max: float = -math.inf
    z_sum: float = 0.0
    z_n: int = 0
    driven_by: set[str] = field(default_factory=set)
    blocked_for: set[str] = field(default_factory=set)

    def add_z(self, z: float) -> None:
        self.z_min = min(self.z_min, z)
        self.z_max = max(self.z_max, z)
        self.z_sum += z
        self.z_n += 1

    @property
    def z_mean(self) -> float | None:
        return self.z_sum / self.z_n if self.z_n else None

    def roughness_deg(self) -> float:
        if self.z_n < 2:
            return 0.0
        return math.degrees(math.atan((self.z_max - self.z_min) / CELL_CM))


_STEP = (1.0, math.sqrt(2.0))
_NEIGHBOURS = [(-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)]


class TerrainGrid:
    def __init__(self) -> None:
        self.cells: dict[tuple[int, int], CellEvidence] = {}
        self.revision = 0
        self._driven_vehicles: set[str] = set()

    def _cell(self, c: tuple[int, int]) -> CellEvidence:
        ev = self.cells.get(c)
        if ev is None:
            ev = self.cells[c] = CellEvidence()
        return ev

    # -- evidence ingestion -----------------------------------------------------------
    def add_height_sample(self, x: float, y: float, z: float) -> None:
        self._cell(cell_of(x, y)).add_z(z)
        self.revision += 1

    def add_height_samples(self, samples: Iterable[tuple[float, float, float]]) -> None:
        for x, y, z in samples:
            self._cell(cell_of(x, y)).add_z(z)
        self.revision += 1

    def add_driven(self, x: float, y: float, z: float, vehicle: str) -> None:
        ev = self._cell(cell_of(x, y))
        ev.add_z(z)
        self._driven_vehicles.add(vehicle)
        if vehicle not in ev.driven_by:
            ev.driven_by.add(vehicle)
            ev.blocked_for.discard(vehicle)
        self.revision += 1

    def block(self, x: float, y: float, vehicle: str) -> None:
        self._cell(cell_of(x, y)).blocked_for.add(vehicle)
        self.revision += 1

    # -- persistence --------------------------------------------------------------------
    def to_json(self) -> dict:
        """Compact form: [i, j, z_min, z_max, z_sum, z_n, driven_by, blocked_for] per cell."""
        cells = []
        for (i, j), ev in list(self.cells.items()):
            has_z = ev.z_n > 0
            cells.append([i, j, round(ev.z_min) if has_z else None, round(ev.z_max) if has_z else None,
                          round(ev.z_sum) if has_z else 0, ev.z_n, sorted(ev.driven_by), sorted(ev.blocked_for)])
        return {"version": 1, "cellCm": CELL_CM, "cells": cells}

    @classmethod
    def from_json(cls, data: object) -> TerrainGrid:
        """Rebuild a grid saved by to_json; malformed cells are skipped, never fatal."""
        grid = cls()
        if not isinstance(data, dict) or data.get("version") != 1 or data.get("cellCm") != CELL_CM:
            return grid
        for row in data.get("cells") or []:
            try:
                i, j, z_min, z_max, z_sum, z_n, driven, blocked = row
                ev = CellEvidence(driven_by={d for d in driven if d in VEHICLES},
                                  blocked_for={b for b in blocked if b in VEHICLES})
                if z_n and z_n > 0 and z_min is not None and z_max is not None:
                    ev.z_min, ev.z_max, ev.z_sum, ev.z_n = float(z_min), float(z_max), float(z_sum), int(z_n)
                grid.cells[(int(i), int(j))] = ev
                grid._driven_vehicles |= ev.driven_by
            except (TypeError, ValueError, OverflowError):
                continue
        return grid

    # -- queries ----------------------------------------------------------------------
    def proven_for(self, ev: CellEvidence, v: VehicleProfile) -> bool:
        # Driven by this vehicle, or by one with an equal-or-stricter envelope.
        # tuple(): ranking runs off the event loop while tracks keep arriving
        return any(VEHICLES.get(d, v).strictness >= v.strictness for d in tuple(ev.driven_by))

    def cell_cost(self, c: tuple[int, int], v: VehicleProfile) -> float | None:
        """Travel multiplier for entering cell `c`; None = impassable."""
        ev = self.cells.get(c)
        if ev is None:
            return v.unknown_cost
        if v.name in ev.blocked_for:
            return None
        if self.proven_for(ev, v):
            return 1.0
        rough = ev.roughness_deg()
        if rough > v.max_roughness_deg:
            return None
        # Rougher terrain costs more even when under the limit.
        return v.unknown_cost * (1.0 + rough / v.max_roughness_deg)

    def edge_ok(self, a: tuple[int, int], b: tuple[int, int], v: VehicleProfile) -> bool:
        if a == b:
            return True
        ea, eb = self.cells.get(a), self.cells.get(b)
        if ea is None or eb is None:
            return True
        if self.proven_for(ea, v) and self.proven_for(eb, v):
            return True
        za, zb = ea.z_mean, eb.z_mean
        if za is None or zb is None:
            return True
        run = CELL_CM * math.hypot(a[0] - b[0], a[1] - b[1])
        return math.degrees(math.atan(abs(za - zb) / run)) <= v.max_slope_deg

    def ground_below(self, x: float, y: float, radius_cells: int = 20) -> float | None:
        """Typical known ground within ~1 km (median of cell means): an aircraft rarely has
        evidence right under it, but spawn sites and nodes around it pin the ground level.
        Not the lowest point: in dunes that put a buggy on a crest 25 m "in the air"."""
        cx, cy = cell_of(x, y)
        heights = []
        for i in range(cx - radius_cells, cx + radius_cells + 1):
            for j in range(cy - radius_cells, cy + radius_cells + 1):
                ev = self.cells.get((i, j))
                if ev is not None and ev.z_n:
                    heights.append(ev.z_sum / ev.z_n)
        if not heights:
            return None
        heights.sort()
        return heights[len(heights) // 2]

    def has_proven(self, v: VehicleProfile) -> bool:
        return any(VEHICLES.get(d, v).strictness >= v.strictness for d in tuple(self._driven_vehicles))

    def known(self, c: tuple[int, int]) -> bool:
        ev = self.cells.get(c)
        return ev is not None and (ev.z_n > 0 or bool(ev.driven_by) or bool(ev.blocked_for))

    def _search(
        self,
        start: tuple[float, float],
        v: VehicleProfile,
        is_goal,
        heuristic,
        window_cells: int,
        stop_when_all: set[tuple[int, int]] | None = None,
        max_expansions: int = 120_000,
        f_limit: float = math.inf,
    ) -> tuple[dict[tuple[int, int], tuple[float, int, int]], set[tuple[int, int]], tuple[int, int] | None]:
        """Shared best-first search (Dijkstra when the heuristic is 0, A* otherwise).

        Every cell costs at least its straight distance (multipliers >= 1) and moves are
        8-connected, so the octile distance is an admissible, consistent heuristic.
        """
        s = cell_of(*start)
        best: dict[tuple[int, int], tuple[float, int, int]] = {s: (0.0, 0, 0)}
        done: set[tuple[int, int]] = set()
        # (f, -g, cell): on ties prefer the deeper node, so uniform ground is crossed in a line
        heap: list[tuple[float, float, tuple[int, int]]] = [(heuristic(s), -0.0, s)]
        cost_cache: dict[tuple[int, int], float | None] = {}
        info: dict[tuple[int, int], tuple[float | None, bool]] = {}  # cell -> (z_mean, proven)
        tan_max = math.tan(math.radians(v.max_slope_deg))
        remaining = set(stop_when_all) if stop_when_all else None

        def cost_of(k: tuple[int, int]) -> float | None:
            if k not in cost_cache:
                cost_cache[k] = self.cell_cost(k, v)
            return cost_cache[k]

        def info_of(k: tuple[int, int]) -> tuple[float | None, bool]:
            r = info.get(k)
            if r is None:
                ev = self.cells.get(k)
                r = info[k] = (None, False) if ev is None else (ev.z_mean, self.proven_for(ev, v))
            return r

        def step_ok(a: tuple[int, int], b: tuple[int, int]) -> bool:
            (za, pa), (zb, pb) = info_of(a), info_of(b)
            if za is None or zb is None or (pa and pb):
                return True
            run = CELL_CM * (_STEP[1] if a[0] != b[0] and a[1] != b[1] else 1.0)
            return abs(za - zb) <= run * tan_max

        while heap and len(done) < max_expansions:
            f, neg_d, c = heapq.heappop(heap)
            d = -neg_d
            if f > f_limit:
                break  # every remaining path is a detour we would never recommend
            if c in done:
                continue
            done.add(c)
            if is_goal is not None and is_goal(c):
                return best, done, c
            if remaining is not None:
                remaining.discard(c)
                if not remaining:
                    break
            _, n_cells, n_known = best[c]
            for dx, dy in _NEIGHBOURS:
                nb = (c[0] + dx, c[1] + dy)
                if nb in done or abs(nb[0] - s[0]) > window_cells or abs(nb[1] - s[1]) > window_cells:
                    continue
                cost = cost_of(nb)
                if cost is None or not step_ok(c, nb):
                    continue
                if dx and dy:
                    # No corner cutting: a diagonal step must also work as an L-shaped
                    # detour, or a wall aligned with the grid looks gentler diagonally.
                    k1, k2 = (c[0] + dx, c[1]), (c[0], c[1] + dy)
                    if not any(cost_of(k) is not None and step_ok(c, k) and step_ok(k, nb) for k in (k1, k2)):
                        continue
                nd = d + CELL_CM * _STEP[dx != 0 and dy != 0] * cost
                prev = best.get(nb)
                if prev is None or nd < prev[0]:
                    best[nb] = (nd, n_cells + 1, n_known + (nb in self.cells))
                    heapq.heappush(heap, (nd + heuristic(nb), -nd, nb))
        return best, done, None

    def travel_field(
        self,
        start: tuple[float, float],
        v: VehicleProfile,
        targets: set[tuple[int, int]] | None = None,
        margin_cells: int = 12,
        max_cells: int = 160,
    ) -> dict[tuple[int, int], tuple[float, int, int]]:
        """Single-source Dijkstra: cell -> (weighted_distance_cm, cells_on_path, known_cells_on_path)."""
        s = cell_of(*start)
        window = max_cells
        if targets:
            window = min(max_cells, max(max(abs(t[0] - s[0]), abs(t[1] - s[1])) for t in targets) + margin_cells)
        best, done, _ = self._search(start, v, None, lambda _c: 0.0, window, stop_when_all=targets)
        return {c: best[c] for c in done}

    def route(
        self,
        start: tuple[float, float],
        goal: tuple[float, float],
        v: VehicleProfile,
        goal_radius_cm: float = 0.0,
        margin_cells: int = 12,
        max_cells: int = 160,
        max_detour: float = 2.5,
        detour_slack_cm: float = 150_000.0,
    ) -> tuple[float, int, int, tuple[int, int]] | None:
        """A* to the nearest reachable cell within `goal_radius_cm` of `goal`.

        Returns (weighted_distance_cm, cells_on_path, known_cells_on_path, end_cell) or None.
        Explores a corridor toward the goal instead of a whole disc, widening only around
        obstacles, so each call is cheap over open sand.
        """
        s, g = cell_of(*start), cell_of(*goal)
        r = math.ceil(goal_radius_cm / CELL_CM)
        reach_cm = goal_radius_cm + CELL_CM / 2

        def is_goal(c: tuple[int, int]) -> bool:
            if r == 0:
                return c == g
            px, py = cell_center(c)
            return math.hypot(px - goal[0], py - goal[1]) <= reach_cm

        # Cheapest possible multiplier: 1.0 on cells proven drivable, else the unknown cost
        # (evidence without proof only ever costs more). Keeps the heuristic admissible.
        floor = 1.0 if self.has_proven(v) else v.unknown_cost

        def h(c: tuple[int, int]) -> float:
            dx, dy = abs(c[0] - g[0]), abs(c[1] - g[1])
            octile = max(dx, dy) + (_STEP[1] - 1.0) * min(dx, dy)
            return max(0.0, octile - r * _STEP[1]) * CELL_CM * floor

        span = max(abs(g[0] - s[0]), abs(g[1] - s[1]))
        if span - r > max_cells:
            return None  # no goal cell lies inside the window: searching it would only flood it
        window = min(max_cells, span + r + margin_cells)
        # Bounding f turns a failed search from "flood the whole window" into a small ellipse.
        straight = h(s)
        limit = straight * max_detour * v.unknown_cost + detour_slack_cm
        best, _, end = self._search(start, v, is_goal, h, window, f_limit=limit)
        if end is None:
            return None
        d, n, k = best[end]
        return d, n, k, end

    def footprint_access(self, x: float, y: float, radius_cm: float, v: VehicleProfile) -> float:
        """Fraction of cells covering a field that the vehicle can drive on."""
        cx, cy = cell_of(x, y)
        r = max(0, math.ceil(radius_cm / CELL_CM))
        total = ok = 0
        for i in range(cx - r, cx + r + 1):
            for j in range(cy - r, cy + r + 1):
                px, py = cell_center((i, j))
                if (i, j) != (cx, cy) and math.hypot(px - x, py - y) > radius_cm + CELL_CM / 2:
                    continue
                total += 1
                if self.cell_cost((i, j), v) is not None and self.edge_ok((cx, cy), (i, j), v):
                    ok += 1
        return ok / total if total else 0.0
