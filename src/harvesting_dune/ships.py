# Downed ships: crashed ships the game shows, remembered after they unload, until a worm takes them.
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .areas import Node
from .model import Actor, Pose

SHIP_MARKER = "salvagesteel"  # the hull: BP_SmallShipwreck_01_C, marker SalvageSteel
CONTAINER_TYPE = "BP_SmallWreck_LootContainer"  # its three containers, cut open with the cutteray
SAME_SHIP_CM = 10_000.0  # reloaded under a new actor id at the same place: still the same ship
CONTAINER_CM = 10_000.0  # the containers lie within ~50 m of the hull
# The hull loads from 500-900 m away (journals 2026-09-26). Not shown inside 400 m: a worm took it.
GONE_CM = 40_000.0
REMEMBER_S = 30 * 60.0  # estimate: a worm swallows the wreck some time after the crash
MAX_WRECKS = 500
CONTAINERS_PER_SHIP = 3  # wiki and journals: the hull carries three
CELL_CM = 200.0  # containers sit exactly in place: a reloaded one (new actor id) is the same container


def parse_wrecks(payload: object, world_id: str | None) -> list[tuple[str, float, float, float]] | None:
    """{map, wrecks: [[name, x, y, z], ...]} from the companion page; None unless it is that shape
    and the map file is this world's (the map window can show another map than the one you are on)."""
    if not isinstance(payload, dict) or not world_id:
        return None
    map_id, rows = payload.get("map"), payload.get("wrecks")
    if not isinstance(map_id, str) or not (map_id == world_id or map_id.startswith(world_id + "_")):
        return None
    if not isinstance(rows, list):
        return None
    out = []
    for r in rows[:MAX_WRECKS]:
        if not (isinstance(r, list) and len(r) == 4 and isinstance(r[0], str)):
            continue
        x, y, z = r[1:]
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (x, y, z)):
            out.append((r[0][:80], float(x), float(y), float(z)))
    return out


@dataclass(slots=True)
class Ship:
    key: str
    x: float
    y: float
    z: float
    first_seen: float
    last_seen: float
    loaded: bool = True
    gone: bool = False
    containers: dict[str, tuple[int, int]] = field(default_factory=dict)  # actor id -> place
    emptied: bool = False

    @property
    def active(self) -> bool:
        return not self.gone and not self.emptied


def _emptied(ship: Ship, looted: dict[str, float]) -> bool:
    """All three containers seen and opened by you, counted by place: a container that loaded again
    has a new id, and one container opened early is not the whole ship."""
    places = set(ship.containers.values())
    opened = {p for k, p in ship.containers.items() if k in looted}
    return len(places) >= CONTAINERS_PER_SHIP and places <= opened


class DownedShips:
    def __init__(self) -> None:
        self.ships: dict[str, Ship] = {}

    def reset(self) -> None:
        self.ships.clear()

    def _match(self, x: float, y: float) -> Ship | None:
        return next((s for s in self.ships.values() if math.hypot(s.x - x, s.y - y) <= SAME_SHIP_CM), None)

    def observe(self, actors: list[Actor], pose: Pose | None, looted: dict[str, float], now: float) -> list[Ship]:
        """Update from one full actor snapshot. Returns the ships seen for the first time."""
        new, loaded = self._see_hulls(actors, now)
        containers = [a for a in actors if (a.actor_type or "").startswith(CONTAINER_TYPE)]
        for ship in self.ships.values():
            ship.loaded = ship.key in loaded
            ship.gone = ship.gone or (not ship.loaded and pose is not None and pose.dist2d(ship.x, ship.y) < GONE_CM)
            ship.containers.update((c.key, (round(c.x / CELL_CM), round(c.y / CELL_CM))) for c in containers
                                   if math.hypot(c.x - ship.x, c.y - ship.y) <= CONTAINER_CM)
            ship.emptied = _emptied(ship, looted)
        for key in [k for k, s in self.ships.items() if now - s.last_seen > REMEMBER_S]:
            del self.ships[key]
        return new

    def _see_hulls(self, actors: list[Actor], now: float) -> tuple[list[Ship], set[str]]:
        new, loaded = [], set()
        for a in (a for a in actors if a.marker == SHIP_MARKER):
            ship = self._match(a.x, a.y)
            if ship is None:
                ship = self.ships[a.key] = Ship(a.key, a.x, a.y, a.z, now, now)
                new.append(ship)
            ship.x, ship.y, ship.z, ship.last_seen, ship.gone = a.x, a.y, a.z, now, False
            loaded.add(ship.key)
        return new, loaded

    def active(self, now: float) -> list[Ship]:
        return [s for s in self.ships.values() if s.active and now - s.last_seen <= REMEMBER_S]

    def nodes(self, now: float, marker: str = SHIP_MARKER, weight: float | None = None) -> list[Node]:
        return [Node(marker, s.x, s.y, s.z, "live" if s.loaded else "memory", None, None, weight) for s in self.active(now)]

    def overlay(self, now: float) -> list[dict]:
        """Ships out of load range, kept on the companion's map like remembered spice fields."""
        return [{"id": f"dd-ship-{s.key}", "actorType": "BP_SmallShipwreck_01_C", "markerType": "SalvageSteel",
                 "spriteKey": SHIP_MARKER, "x": s.x, "y": s.y, "z": s.z, "spawned": True, "reportable": False}
                for s in self.active(now) if not s.loaded]

    def status(self, now: float, pose: Pose | None, zone_of) -> list[dict]:
        out = [{"distanceM": None if pose is None else round(pose.dist2d(s.x, s.y) / 100),
                "ageMin": round((now - s.first_seen) / 60), "loaded": s.loaded, "gone": s.gone,
                "emptied": s.emptied, "containers": len(set(s.containers.values())), "zone": zone_of(s.y)}
               for s in self.ships.values()]
        return sorted(out, key=lambda d: (not d["loaded"] and d["gone"], d["distanceM"] or 0))
