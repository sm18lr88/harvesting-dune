# Mined nodes: the nodes you mined on the current map, until they respawn, and their file.
"""Nodes you mined or gathered, per map and layout (data\\mined-<map>-<seed>.json).

The only writer of those files. The detector decides what counts as mined; this module keeps
the records, answers "was this node mined?", and forgets them when they respawn.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from pathlib import Path

from .fsutil import read_json, try_write_json

log = logging.getLogger(__name__)

SAME_NODE_CM = 500.0
AREA_NEAR_CM = 40000.0  # "this area" when counting what you mined there
RESPAWN_SEEN_MIN_S = 60  # reappearing sooner is the list flickering, not a respawn
EXPIRED_KEEP_S = 7200.0  # the upper clamp of Learning.respawn_for: later is no respawn of this node


def _same_node(actor, key: str, m: dict) -> bool:
    """The mined node itself, also under a reloaded id (`A:B:n`: a reload changes only B). Its
    neighbours, even siblings of one spawner at the same place, are other nodes."""
    if actor.key == key:
        return True
    ours, theirs = key.split(":"), actor.key.split(":")
    return (actor.marker == m["marker"] and actor.x == m["x"] and actor.y == m["y"]
            and len(ours) == len(theirs) == 3 and (ours[0], ours[2]) == (theirs[0], theirs[2]))


class MinedNodes:
    def __init__(self, data_dir: Path | None) -> None:
        self._dir = data_dir
        self.world: tuple | None = None
        self._last_real: tuple | None = None  # every loading screen reports no world
        self.items: dict[str, dict] = {}  # actor key -> {marker, x, y, z, t}
        # Records the clock already freed, kept in memory so a node back later than the learned
        # respawn still teaches its real (longer) time; never persisted, never read as mined.
        self.expired: dict[str, dict] = {}

    def _path(self) -> Path | None:
        if self._dir is None or not self.world or self.world[0] is None:
            return None
        return self._dir / f"mined-{self.world[0]}-{self.world[1]}.json"

    def set_world(self, world: tuple | None) -> None:
        if world == self.world:
            return
        if world is not None and world[0] is not None:
            if world != self._last_real:
                self.expired = {}
            self._last_real = world
        self.world = world
        loaded = read_json(self._path(), dict, {})
        self.items = {
            k: m for k, m in loaded.items()
            if isinstance(m, dict) and isinstance(m.get("marker"), str)
            and all(isinstance(m.get(c), (int, float)) for c in ("x", "y", "t"))
        }

    def _save(self) -> None:
        try_write_json(self._path(), self.items, "mined nodes")

    def record(self, key: str, marker: str, x: float, y: float, z: float, now: float) -> None:
        self.items[key] = {"marker": marker, "x": x, "y": y, "z": z, "t": now}
        self._save()

    def is_mined(self, marker: str, x: float, y: float) -> bool:
        return any(m["marker"] == marker and abs(m["x"] - x) <= SAME_NODE_CM and abs(m["y"] - y) <= SAME_NODE_CM
                   for m in self.items.values())

    def near(self, x: float, y: float, markers: set[str], radius_cm: float = AREA_NEAR_CM) -> int:
        return sum(1 for m in self.items.values()
                   if m["marker"] in markers and math.hypot(m["x"] - x, m["y"] - y) <= radius_cm)

    def seen_again(self, live: list, now: float) -> list[tuple[str, float]]:
        """Forget mined nodes the game shows again; returns (marker, seconds it took) for each."""
        def returned(pool: dict[str, dict]) -> dict[str, dict]:
            return {k: m for k, m in pool.items()
                    if now - m["t"] > RESPAWN_SEEN_MIN_S and any(_same_node(a, k, m) for a in live)}

        back, late = returned(self.items), returned(self.expired)
        for k in back:
            del self.items[k]
        for k in late:
            del self.expired[k]
        if back:
            self._save()
        # a record from an earlier run may be back long ago: past the bound, no time to learn
        return [(m["marker"], now - m["t"]) for m in (*back.values(), *late.values()) if now - m["t"] <= EXPIRED_KEEP_S]

    def expire(self, now: float, respawn_for: Callable[[str], float]) -> int:
        """Forget nodes whose respawn time has passed: they are candidates again."""
        old = [k for k, m in self.items.items() if now - m["t"] > respawn_for(m["marker"])]
        for k in old:
            self.expired[k] = self.items.pop(k)
        self.expired = {k: m for k, m in self.expired.items() if now - m["t"] <= EXPIRED_KEEP_S}
        if old:
            self._save()
            log.info("%s mined nodes have respawned (by the clock)", len(old))
        return len(old)

    def next_back_s(self, now: float, respawn_for: Callable[[str], float]) -> float | None:
        if not self.items:
            return None
        return min(respawn_for(m["marker"]) - (now - m["t"]) for m in self.items.values())
