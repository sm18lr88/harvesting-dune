# World files: learned terrain, spice blows and detection ranges saved per map and layout.
"""Per-map state on disk. Terrain and blows are per map and Coriolis layout (seed); detection
ranges are per map. This module is the only writer of terrain-*, blows-* and ranges.json."""

from __future__ import annotations

import logging
from pathlib import Path

from .fsutil import read_json, try_write_json
from .store import ObservationStore
from .terrain import TerrainGrid

log = logging.getLogger(__name__)

TERRAIN_SAVE_S = 60.0  # learned terrain is written at most this often (and on a clean exit)


class TerrainFiles:
    """Terrain grids per world, loaded on first use and saved when they have new evidence."""

    def __init__(self, data_dir: Path | None) -> None:
        self._dir = data_dir
        self.grids: dict[tuple | None, TerrainGrid] = {}
        self._saved: dict[tuple | None, int] = {}  # world -> grid revision on disk
        self._saved_at = 0.0

    def _path(self, world: tuple | None) -> Path | None:
        if self._dir is None or not world or world[0] is None:
            return None
        return self._dir / f"terrain-{world[0]}-{world[1]}.json"

    def grid(self, world: tuple | None) -> TerrainGrid:
        g = self.grids.get(world)
        if g is None:
            path = self._path(world)
            g = TerrainGrid.from_json(read_json(path, dict)) if path else TerrainGrid()
            if g.cells:
                log.info("terrain for %s: %s cells learned in earlier sessions", world, len(g.cells))
            self.grids[world] = g
            self._saved[world] = g.revision
        return g

    def save(self, now: float, force: bool = False) -> int:
        """Write every grid with new evidence (throttled). Returns how many were written."""
        if not force and now - self._saved_at < TERRAIN_SAVE_S:
            return 0
        self._saved_at = now
        written = 0
        for world, g in list(self.grids.items()):
            if g.revision == self._saved.get(world) or not g.cells:
                continue
            if try_write_json(self._path(world), g.to_json(), "learned terrain", separators=(",", ":")):
                self._saved[world] = g.revision
                written += 1
        return written


def blows_path(data_dir: Path | None, world: tuple | None) -> Path | None:
    if data_dir is None or not world:
        return None
    return data_dir / f"blows-{world[0]}-{world[1]}.json"


def load_blows(path: Path | None) -> dict:
    loaded = read_json(path, dict, {})
    return {k: b for k, b in loaded.items()
            if isinstance(b, dict) and all(isinstance(b.get(c), (int, float)) for c in ("x", "y", "z", "t"))}


def save_blows(path: Path | None, blows: dict) -> None:
    try_write_json(path, blows, "spice blows")


def load_ranges(path: Path | None) -> dict:
    loaded = read_json(path, dict, {})
    return {w: {m: float(v) for m, v in r.items() if isinstance(v, (int, float)) and v > 0}
            for w, r in loaded.items() if isinstance(r, dict)}


def save_ranges(path: Path | None, store: ObservationStore) -> None:
    """Remember learned detection ranges per map, so corrections work right after a restart."""
    world = store.world[0] if store.world else None
    learned = store.learned_markers()
    if not path or not world or not learned:
        return
    new = {m: round(v) for m, v in learned.items()}
    if store.saved_ranges.get(world, {}) | new == store.saved_ranges.get(world):
        return  # nothing new: no disk write every 15 s
    store.saved_ranges.setdefault(world, {}).update(new)
    try_write_json(path, store.saved_ranges, "learned ranges", indent=1)
