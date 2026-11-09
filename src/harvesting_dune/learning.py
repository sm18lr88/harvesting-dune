# Learning: values learned from play (respawn times, mining pace, map grids) and their file.
"""Values learned from your own play, saved in data\\learned.json (the only writer of that file).

Each value is used once it has LEARN_MIN_SAMPLES samples; until then a default applies.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .areas import NODE_HARVEST_S
from .catalog import respawn_s
from .fsutil import read_json, try_write_json
from .zones import DEFAULT_FIT, GridFit, fit_agreement, fit_grid, is_deep_desert

log = logging.getLogger(__name__)

LEARN_MIN_SAMPLES = 5
KEEP_SAMPLES = 50
RECENT_PACE_SAMPLES = 8  # the pace follows your latest mines: solo, a group on a buggy, ... change fast


class Learning:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.data: dict = {"respawn": {}, "pace": {}, "grids": {}}
        self.fitted_this_run: set[str] = set()  # maps whose grid was fitted since start
        self._load()

    def _load(self) -> None:
        d = read_json(self.path, dict, {})
        for part in ("respawn", "pace"):
            got = d.get(part) if isinstance(d.get(part), dict) else {}
            self.data[part] = {m: [float(v) for v in vals if isinstance(v, (int, float))][-KEEP_SAMPLES:]
                               for m, vals in got.items() if isinstance(m, str) and isinstance(vals, list)}
        grids = d.get("grids") if isinstance(d.get("grids"), dict) else {}
        self.data["grids"] = {w: g for w, g in grids.items() if GridFit.from_json(g)}

    def _save(self) -> None:
        try_write_json(self.path, self.data, "learned values")

    def learn(self, part: str, marker: str, value: float) -> None:
        vals = self.data[part].setdefault(marker, [])
        vals.append(round(value, 1))
        del vals[:-KEEP_SAMPLES]
        self._save()

    def samples(self, part: str, markers: set[str]) -> int:
        return sum(len(self.data[part].get(m, [])) for m in markers)

    def respawn_for(self, marker: str) -> float:
        """Respawn time: the lower quartile of what you have seen (each sighting is when it was
        already back, so an upper bound), else the patch-notes value."""
        seen = sorted(self.data["respawn"].get(marker, []))
        if len(seen) >= LEARN_MIN_SAMPLES:
            return min(max(seen[len(seen) // 4], 10 * 60), 2 * 60 * 60)
        return respawn_s(marker)

    def node_seconds(self, markers: set[str]) -> float:
        """Seconds per node for these markers: the median of the most recent samples (so it adapts
        within a few mines when the pace changes, for example a second player joins), else the
        default. All KEEP_SAMPLES stay on file as history."""
        seen = sorted(v for m in markers for v in self.data["pace"].get(m, [])[-RECENT_PACE_SAMPLES:])
        if len(seen) >= LEARN_MIN_SAMPLES:
            return min(max(seen[len(seen) // 2], 5.0), 180.0)
        return NODE_HARVEST_S

    def grid_fit(self, world_id: str | None) -> GridFit | None:
        """The map grid of this world: fitted from the companion's markers, else the Deep Desert
        fallback; None where no grid is known."""
        g = self.data["grids"].get(world_id) if world_id else None
        fit = GridFit.from_json(g) if g else None
        return fit or (DEFAULT_FIT if is_deep_desert(world_id) else None)

    def grid_fitted(self, world_id: str | None) -> bool:
        return bool(world_id and self.data["grids"].get(world_id))

    def grid_fit_wanted(self, world_id: str | None) -> bool:
        """True if this map's grid should still be fitted in this run."""
        return bool(world_id) and world_id not in self.fitted_this_run

    def adopt_grid(self, world_id: str, samples: list, fit: GridFit | None = None) -> bool | None:
        """Adopt a grid fitted from the companion's markers. Returns None if no fit was possible,
        False if kept as it was, True if the stored grid changed. A fit that places fewer of these
        markers in their own cell than the current grid is ignored."""
        fit = fit or fit_grid(samples)
        if fit is None:
            return None
        self.fitted_this_run.add(world_id)
        current = self.grid_fit(world_id)
        if current is not None and fit_agreement(fit, samples) < fit_agreement(current, samples):
            return False  # keep the better grid
        old = self.data["grids"].get(world_id)
        self.data["grids"][world_id] = fit.to_json()
        self._save()
        if old == fit.to_json():
            return False
        log.info("map grid of %s fitted from %s companion markers (%.0f%% in their own cell)",
                 world_id, fit.samples, 100 * fit_agreement(fit, samples))
        return True
