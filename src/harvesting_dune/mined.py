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


class MinedNodes:
    def __init__(self, data_dir: Path | None) -> None:
        self._dir = data_dir
        self.world: tuple | None = None
        self.items: dict[str, dict] = {}  # actor key -> {marker, x, y, z, t}

    def _path(self) -> Path | None:
        if self._dir is None or not self.world or self.world[0] is None:
            return None
        return self._dir / f"mined-{self.world[0]}-{self.world[1]}.json"

    def set_world(self, world: tuple | None) -> None:
        if world == self.world:
            return
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
        back = {k for k, m in self.items.items() for a in live
                if a.marker == m["marker"] and abs(a.x - m["x"]) <= SAME_NODE_CM and abs(a.y - m["y"]) <= SAME_NODE_CM
                and now - m["t"] > RESPAWN_SEEN_MIN_S}
        out = [(self.items[k]["marker"], now - self.items[k]["t"]) for k in back]
        for k in back:
            del self.items[k]
        if back:
            self._save()
        return out

    def expire(self, now: float, respawn_for: Callable[[str], float]) -> int:
        """Forget nodes whose respawn time has passed: they are candidates again."""
        old = [k for k, m in self.items.items() if now - m["t"] > respawn_for(m["marker"])]
        for k in old:
            del self.items[k]
        if old:
            self._save()
            log.info("%s mined nodes have respawned (by the clock)", len(old))
        return len(old)

    def next_back_s(self, now: float, respawn_for: Callable[[str], float]) -> float | None:
        if not self.items:
            return None
        return min(respawn_for(m["marker"]) - (now - m["t"]) for m in self.items.values())
