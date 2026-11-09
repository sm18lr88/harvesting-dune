# Areas: clusters candidate nodes into areas and ranks them for a vehicle and a mining session.
"""Best harvesting areas for a chosen resource and vehicle.

1. Collect candidate nodes of the resource: live actors (certain) and scan-database rows
   (likely; spice rows least likely because fields despawn when harvested).
2. Cluster them into areas (nodes within AREA_RADIUS of a density peak).
3. Gate every area by the vehicle's access, from learned terrain:
     * spice ("drive_over"): the vehicle must reach the field and drive enough of it,
       and must be able to harvest spice at all;
     * everything else ("on_foot"): the vehicle must reach a parking cell within
       PARK_RADIUS; the rest is walked, with climbing penalised;
     * ornithopters fly straight and ignore terrain.
   Unreachable areas are excluded, never merely down-weighted.
4. Rank. Spice: value per minute of the whole trip (out, harvest, back), limited by what the
   harvester carries. Nodes you mine by hand: how much you can collect in a SESSION_S session,
   counting the trip there and back. A patch is the area plus every area within PATCH_CM (you walk
   or hop between them), so a dense island that keeps you busy the whole session beats a
   moderate spot that runs out after a few minutes, even if the island is farther away.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable
from dataclasses import dataclass, field

from .catalog import SPICE_SIZE, Resource
from .model import Pose
from .terrain import TerrainGrid, VehicleProfile, cell_center

AREA_RADIUS_CM = 25000.0  # 250 m
PARK_RADIUS_CM = 15000.0  # walk at most 150 m from the vehicle
WALK_CMS = 450.0
CLIMB_PENALTY_S_PER_M = 1.5
NODE_HARVEST_S = 25.0
# every stop costs time regardless of size (land/park, get out, find the rocks, leave again);
# without it a 3-node cluster next to you out-ranks a 20-node cluster a short flight away
STOP_OVERHEAD_S = 90.0
NODE_VALUE = {"lootcontainerultrarare": 3.0, "lootcontainerrare": 1.0}  # an ultra-rare cage is worth a detour
SPICE_UNITS = {"small": 2500.0, "medium": 8000.0, "large": 20000.0}
SPICE_RADIUS_CM = {"small": 3000.0, "medium": 6000.0, "large": 10000.0}
MAX_REPORTED = 30
ROUTE_BATCH = 6
PLAN_RANGE_CM = 800000.0  # 8 km: matches the route search window
LIKELIHOOD = {"live": 1.0, "memory": 0.8, "scan": 0.6, "scan_spice": 0.3}
SESSION_S = 30 * 60.0  # a mining session: how much can you collect in this time, trip included?
PATCH_CM = 80000.0  # neighbouring areas within 800 m belong to the same patch
PATCH_NEIGHBOUR_WEIGHT = 0.8  # a little less certain than the spot you land on
TRAVEL_PENALTY = 0.25  # travel time also costs a little on its own: equal hauls, the nearer patch wins


@dataclass(slots=True)
class Node:
    marker: str
    x: float
    y: float
    z: float
    source: str  # live | memory (seen earlier, now out of load range) | scan
    fill: float | None = None  # 0..1 for spice
    row_key: str | None = None  # scan-database identity, for corrections
    weight: float | None = None  # expected amount from this node (looted materials: chance x avg qty)

    @property
    def likelihood(self) -> float:
        if self.source in ("live", "memory"):
            return LIKELIHOOD[self.source]
        return LIKELIHOOD["scan_spice"] if self.marker in SPICE_SIZE else LIKELIHOOD["scan"]

    def value(self) -> float:
        if self.weight is not None:
            return self.weight
        size = SPICE_SIZE.get(self.marker)
        if size:
            return SPICE_UNITS[size] * (self.fill if self.fill is not None else 0.6)
        return NODE_VALUE.get(self.marker, 1.0)


@dataclass(slots=True)
class Area:
    x: float
    y: float
    z: float
    nodes: list[Node]
    feasible: bool = False
    utility: float = 0.0
    expected: float = 0.0  # likelihood-weighted value collected per trip
    trip_s: float | None = None
    distance_m: float | None = None
    live_nodes: int = 0
    yield_mult: float = 1.0  # e.g. 2.5 in the Deep Desert PvP zone
    patch_value: float = 0.0  # likelihood-weighted value of this area and its neighbours
    patch_nodes: int = 0
    reasons: list[str] = field(default_factory=list)

    def find_chance(self) -> float:
        """Chance to find at least one here (chance finds): 1 - product of (1 - chance) over its sources."""
        return 1 - math.prod(1 - min(max(n.weight or 0.0, 0.0), 1.0) for n in self.nodes)


def _clusters(nodes: list[Node]) -> list[Area]:
    """Greedy density-peak clustering on buckets: O(n), fine for tens of thousands of rows.

    Nodes are summed into buckets of 2/3 AREA_RADIUS; each area is the 3x3 block (about
    2 x AREA_RADIUS across) around the densest remaining bucket.
    """
    if not nodes:
        return []
    size = AREA_RADIUS_CM * 2 / 3
    buckets: dict[tuple[int, int], list[Node]] = {}
    weight: dict[tuple[int, int], float] = {}
    for n in nodes:
        k = (math.floor(n.x / size), math.floor(n.y / size))
        buckets.setdefault(k, []).append(n)
        weight[k] = weight.get(k, 0.0) + n.value() * n.likelihood

    def block(k: tuple[int, int]):
        return ((k[0] + i, k[1] + j) for i in (-1, 0, 1) for j in (-1, 0, 1))

    density = {k: sum(weight.get(b, 0.0) for b in block(k)) for k in buckets}
    taken: set[tuple[int, int]] = set()
    areas: list[Area] = []
    for k in sorted(buckets, key=lambda k: -density[k]):
        if k in taken:
            continue
        members: list[Node] = []
        for b in block(k):
            if b in buckets and b not in taken:
                members.extend(buckets[b])
                taken.add(b)
        ws = [m.value() * m.likelihood for m in members]
        w = sum(ws) or 1.0
        cx = sum(m.x * wi for m, wi in zip(members, ws, strict=True)) / w
        cy = sum(m.y * wi for m, wi in zip(members, ws, strict=True)) / w
        areas.append(Area(cx, cy, min(m.z for m in members), members))
    return areas


def recommend(
    resource: Resource,
    nodes: list[Node],
    pose: Pose,
    grid: TerrainGrid,
    v: VehicleProfile,
    base: tuple[float, float] | None = None,
    top: int = 3,
    travel: VehicleProfile | None = None,
    allow_far: bool = False,
    yield_at: Callable[[float, float], float] | None = None,
    need: int | None = None,
    node_s: float = NODE_HARVEST_S,
) -> list[Area]:
    """`v` harvests (spice capability, field footprint); `travel` gets you there, e.g. the scout
    ornithopter you are flying (straight line, no planning range). Defaults to `v`.
    `yield_at(x, y)` multiplies what an area yields (PvP bonus); `need` is how many feasible
    areas to route exactly (the target plus its alternative lines)."""
    t = travel or v
    wanted = {m.lower() for m in resource.marker_types}
    areas = _clusters([n for n in nodes if n.marker in wanted])
    if not areas:
        return []
    drive_over = resource.harvest == "drive_over"
    for a in areas:
        a.distance_m = round(math.hypot(a.x - pose.x, a.y - pose.y) / 100)
        a.live_nodes = sum(1 for n in a.nodes if n.source == "live")
        if yield_at is not None:
            a.yield_mult = yield_at(a.x, a.y)
    if drive_over and not v.harvests_spice:
        for a in areas:
            a.reasons.append(f"{v.name} cannot harvest spice")
        return sorted(areas, key=lambda a: a.distance_m)[:MAX_REPORTED]
    if not drive_over:
        _patches(areas)

    # Straight lines are a lower bound on any route (every cell costs >= 1), so this is an
    # upper bound on the true utility: areas are routed best-first in batches, and routing
    # stops once no unrouted area could still beat the current top results.
    candidates, far = [], []
    for a in areas:
        if not t.flies and a.distance_m * 100 > PLAN_RANGE_CM:
            if allow_far:
                far.append(a)  # still a target: estimated in a straight line below
            else:
                a.reasons.append(f"beyond {PLAN_RANGE_CM / 100_000:.0f} km planning range")
            continue
        candidates.append((_upper_bound(a, pose, v, base, drive_over, t, node_s), a))
    if far:
        straight = dataclasses.replace(t, flies=True)
        _evaluate(far, pose, grid, v, base, drive_over, straight, node_s)
        for a in far:
            if a.feasible:
                a.reasons.insert(0, "far: straight-line estimate")
    candidates.sort(key=lambda t: -t[0])
    need = need or (max(top, 3) if top else 3)
    i = 0
    while i < len(candidates):
        batch = [a for _, a in candidates[i : i + ROUTE_BATCH]]
        i += ROUTE_BATCH
        _evaluate(batch, pose, grid, v, base, drive_over, t, node_s)
        best = sorted((a.utility for _, a in candidates[:i] if a.feasible), reverse=True)
        if len(best) >= need and i < len(candidates) and best[need - 1] >= candidates[i][0]:
            for _, a in candidates[i:]:
                a.reasons.append("cannot beat the areas above")
            break

    areas.sort(key=lambda a: (not a.feasible, -a.utility, a.distance_m or 0))
    areas = areas[:MAX_REPORTED]
    return areas[:top] if top else areas


def _patches(areas: list[Area]) -> None:
    """Each area's patch: itself plus the areas whose centres lie within PATCH_CM."""
    own = [sum(n.value() * n.likelihood for n in a.nodes) for a in areas]
    grid: dict[tuple[int, int], list[int]] = {}
    for i, a in enumerate(areas):
        grid.setdefault((math.floor(a.x / PATCH_CM), math.floor(a.y / PATCH_CM)), []).append(i)
    for i, a in enumerate(areas):
        gx, gy = math.floor(a.x / PATCH_CM), math.floor(a.y / PATCH_CM)
        value, count = own[i], len(a.nodes)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((gx + dx, gy + dy), ()):
                    if j != i and math.hypot(areas[j].x - a.x, areas[j].y - a.y) <= PATCH_CM:
                        value += PATCH_NEIGHBOUR_WEIGHT * own[j]
                        count += len(areas[j].nodes)
        a.patch_value, a.patch_nodes = value, count


def _harvest(a: Area, v: VehicleProfile, drive_over: bool, walk_s: float = 0.0,
             travel_s: float = 0.0, node_s: float = NODE_HARVEST_S) -> tuple[float, float]:
    """(value collected, harvest time). Spice: one harvester load. Nodes: what fits in a session."""
    if drive_over:
        # a yield bonus fills the harvester faster (same load, less time)
        expected = min(sum(n.value() * n.likelihood for n in a.nodes) * a.yield_mult, v.capacity)
        return expected, expected / max(v.harvest_rate * a.yield_mult, 1e-6)
    pool, count = (a.patch_value, a.patch_nodes) if a.patch_nodes else (
        sum(n.value() * n.likelihood for n in a.nodes), len(a.nodes))
    minable = max(0.0, (SESSION_S - travel_s - STOP_OVERHEAD_S - 2 * walk_s) / node_s)
    taken = min(count, minable)
    per_node = pool / count if count else 0.0
    return taken * per_node * a.yield_mult, STOP_OVERHEAD_S + node_s * taken + walk_s * 2


def _utility(expected: float, trip_s: float, drive_over: bool, travel_s: float = 0.0) -> float:
    # spice: per minute of the trip; nodes: per session (the session already holds the trip, which
    # limits the haul), with a small extra cost per second of travel
    return expected / max(trip_s, 1.0) if drive_over else expected / (SESSION_S + TRAVEL_PENALTY * travel_s)


def _upper_bound(a: Area, pose: Pose, v: VehicleProfile, base, drive_over: bool, t: VehicleProfile,
                 node_s: float = NODE_HARVEST_S) -> float:
    out = math.hypot(a.x - pose.x, a.y - pose.y)
    back = math.hypot(a.x - base[0], a.y - base[1]) if base else out
    if not drive_over and not t.flies:  # may park up to PARK_RADIUS away
        out, back = max(0.0, out - PARK_RADIUS_CM), max(0.0, back - PARK_RADIUS_CM)
    travel = out / t.speed_cms + back / t.loaded_speed_cms
    expected, harvest_s = _harvest(a, v, drive_over, travel_s=travel, node_s=node_s)
    return _utility(expected, travel + harvest_s, drive_over, travel)


def _evaluate(batch: list[Area], pose: Pose, grid: TerrainGrid, v: VehicleProfile, base, drive_over: bool,
              t: VehicleProfile | None = None, node_s: float = NODE_HARVEST_S) -> None:
    t = t or v
    for a in batch:
        if t.flies:
            straight = math.hypot(a.x - pose.x, a.y - pose.y)
            out_s = straight / t.speed_cms
            back_s = (math.hypot(a.x - base[0], a.y - base[1]) if base else straight) / t.loaded_speed_cms
            walk_s, note = 0.0, None
            if drive_over and not v.flies:
                # flying there, but the harvester still has to work the field
                size = max((SPICE_SIZE[n.marker] for n in a.nodes), key=lambda s: SPICE_UNITS[s])
                fp = grid.footprint_access(a.x, a.y, SPICE_RADIUS_CM[size], v)
                if fp < v.min_footprint:
                    a.reasons.append(f"only {fp:.0%} of the field is drivable by {v.name}")
                    continue
        else:
            access = _access(a, pose, grid, v, base, drive_over)
            if access is None:
                a.reasons.append(f"no practical {v.name} route" + ("" if drive_over else " to within 150 m"))
                continue
            out_s, back_s, walk_s, note = access
            if drive_over:
                size = max((SPICE_SIZE[n.marker] for n in a.nodes), key=lambda s: SPICE_UNITS[s])
                fp = grid.footprint_access(a.x, a.y, SPICE_RADIUS_CM[size], v)
                if fp < v.min_footprint:
                    a.reasons.append(f"only {fp:.0%} of the field is drivable by {v.name}")
                    continue
        if note:
            a.reasons.append(note)
        expected, harvest_s = _harvest(a, v, drive_over, walk_s, out_s + back_s, node_s)
        if expected <= 0:
            a.reasons.append(f"too far to mine anything in a {SESSION_S / 60:.0f} min session")
            continue
        a.feasible = True
        a.expected = expected
        a.trip_s = out_s + harvest_s + back_s
        a.utility = _utility(expected, a.trip_s, drive_over, out_s + back_s)
        if a.live_nodes == 0:
            seen = any(n.source == "memory" for n in a.nodes)
            a.reasons.append("seen active earlier" if seen else "not yet confirmed live")


def _access(a: Area, pose: Pose, grid: TerrainGrid, v: VehicleProfile, base, drive_over: bool):
    """Drive there (onto the field, or to the nearest reachable parking cell) and back."""
    radius = 0.0 if drive_over else PARK_RADIUS_CM
    out = grid.route((pose.x, pose.y), (a.x, a.y), v, radius)
    if out is None:
        return None
    back = grid.route(base, (a.x, a.y), v, radius) if base else out
    if back is None:
        return None
    walk_s = 0.0
    if not drive_over:
        px, py = cell_center(out[3])
        ev = grid.cells.get(out[3])
        park_z = ev.z_mean if ev and ev.z_mean is not None else None
        climb_m = max(0.0, (a.z - park_z) / 100) if park_z is not None else 0.0
        walk_s = math.hypot(px - a.x, py - a.y) / WALK_CMS + climb_m * CLIMB_PENALTY_S_PER_M
    return out[0] / v.speed_cms, back[0] / v.loaded_speed_cms, walk_s, _confidence(out, back)


def _confidence(o: tuple, b: tuple) -> str | None:
    cells = o[1] + b[1]
    if not cells:
        return None
    share = (o[2] + b[2]) / cells
    return None if share >= 0.5 else f"terrain {share:.0%} mapped"
