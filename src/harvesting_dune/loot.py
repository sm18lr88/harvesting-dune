# Loot: rare-cage loot tables from gaming.tools item data, cached in data\loot-rare.json.
"""Loot tables for the rare and ultra-rare cages around you.

gaming.tools' item database lists, per item, every container *location* that can drop it
(map, x/y/z, chance, quantity). `loot_crawl.js` inverts that inside a companion page into a
per-location loot table; the game's live cage actors sit exactly on those locations, so
each cage near you gets its own table.

Contents are rolled when you open a cage (per-player loot): this is what it CAN drop, with
chances, which is what the decision "is it worth the trip" needs.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from .fsutil import parse_json, try_write_json

log = logging.getLogger(__name__)

CRAWL_SCRIPT = resources.files(__package__).joinpath("loot_crawl.js").read_text("utf8")
MAX_AGE_S = 7 * 24 * 3600
AHEAD_S = 300.0  # a build stamped further ahead came from a fast PC clock: it would never age
CACHE_VERSION = 3  # bump when the crawl output changes shape; a stale cache is rebuilt (3: schematics, Small Ultra Rare)
MATCH_CM = 200.0  # live cages sit exactly on their database location (measured: 0.0 m)
CAGE_KINDS = {"UltraRareContainer": "Ultra Rare", "RareContainer": "Rare"}
RARITY_RANK = {"Common": 0, "Uncommon": 1, "Rare": 2, "Epic": 3, "Legendary": 4, "Unique": 5}


def cage_kind(actor_type: str | None) -> str | None:
    """'Ultra Rare' / 'Rare' for rare cage actors (incl. SmallUltraRare, Polar, Ecolab variants)."""
    if not actor_type:
        return None
    for needle, kind in CAGE_KINDS.items():  # UltraRare checked first
        if needle in actor_type:
            return kind
    return None


def is_special(d: dict) -> bool:
    """Worth a detour on its own: a schematic, or unique gear. Materials carry 'Unique' rarity in
    the database too (e.g. Spice-infused Plastanium Dust), so resources never count."""
    if d.get("schematic"):
        return True
    material = any(c.startswith("items/misc") or "resource" in c for c in d.get("categories") or [])
    return (d.get("rarity") == "Unique" or bool(d.get("unique"))) and not material


def _numeric(*values: object) -> bool:
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values)


def _combine(drops: list[dict]) -> list[dict]:
    """Merge repeated rolls of one item: chance of getting at least one, total quantity range."""
    by: dict[str, dict] = {}
    for d in drops:
        if not _numeric(d.get("chance") or 0.0, d.get("min") or 1, d.get("max") or 1):
            continue  # text amounts would be joined, not added
        lo, hi = sorted((d.get("min") or 1, d.get("max") or 1))  # some rows have them swapped
        cur = by.get(d["id"])
        if cur is None:
            by[d["id"]] = {**d, "min": lo, "max": hi, "miss": 1.0 - (d.get("chance") or 0.0)}
            continue
        cur["miss"] *= 1.0 - (d.get("chance") or 0.0)
        cur["min"] = min(cur["min"], lo)
        cur["max"] += hi
    out = []
    for d in by.values():
        d["chance"] = round(1.0 - d.pop("miss"), 4)
        d["special"] = is_special(d)
        d.pop("categories", None)
        out.append(d)
    out.sort(key=lambda d: (not d["special"], -RARITY_RANK.get(d.get("rarity"), 0), -d["chance"]))
    return out


@dataclass(slots=True)
class Cage:
    map: str
    x: float
    y: float
    z: float
    name: str
    drops: list[dict]


class LootIndex:
    def __init__(self, data_dir: Path | None) -> None:
        self.path = (data_dir / "loot-rare.json") if data_dir else None
        self.built: float | None = None
        self.cages: list[Cage] = []
        self._grid: dict[tuple[str, int, int], list[Cage]] = {}
        self._maps: set[str] = set()
        self.version: int | None = None
        self.load()

    @property
    def stale(self) -> bool:
        return self.built is None or self.version != CACHE_VERSION or not -AHEAD_S <= time.time() - self.built <= MAX_AGE_S

    def load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            data = parse_json(self.path.read_text("utf8"))
            self._set(data["built"] / 1000, [Cage(**c) for c in data["cages"]])
            self.version = data.get("version")
        except (OSError, ValueError, KeyError, TypeError):
            log.warning("ignoring unreadable %s", self.path)

    def ingest_crawl(self, raw: str) -> int:
        """Keep rare/ultra-rare cage locations from a crawl result; cache them."""
        data = parse_json(raw)
        cages = [
            Cage(c["map"], c["x"], c["y"], c["z"], c["name"], _combine(c["drops"]))
            for c in data.get("containers", [])
            if "rare" in (c.get("name") or "").lower() and c.get("map")
            # a location the CDN gave without a coordinate is no place to match a cage against
            and all(isinstance(c.get(k), (int, float)) and math.isfinite(c[k]) for k in ("x", "y", "z"))
        ]
        self._set(data.get("built", time.time() * 1000) / 1000, cages)
        self.version = CACHE_VERSION
        try_write_json(self.path, {"version": CACHE_VERSION, "built": self.built * 1000,
                                   "cages": [dataclasses.asdict(c) for c in cages]}, "loot index")
        return len(cages)

    def _set(self, built: float, cages: list[Cage]) -> None:
        self.built, self.cages, self._grid = built, cages, {}
        for c in cages:
            self._grid.setdefault(self._key(c.map, c.x, c.y), []).append(c)
        self._maps = {c.map for c in cages}

    @staticmethod
    def _key(map_id: str, x: float, y: float) -> tuple[str, int, int]:
        return (map_id, math.floor(x / 5000), math.floor(y / 5000))

    def lookup(self, world_id: str | None, x: float, y: float) -> Cage | None:
        """The database cage at this spot. The Deep Desert has one index per layout
        (deepdesert_1_00..11); positions are exact, so every variant of the map is tried."""
        if not world_id:
            return None
        maps = [m for m in self._maps if m == world_id or m.startswith(world_id + "_")]
        best, best_d = None, MATCH_CM
        for m in maps:
            bx, by = math.floor(x / 5000), math.floor(y / 5000)
            for i in (-1, 0, 1):
                for j in (-1, 0, 1):
                    for c in self._grid.get((m, bx + i, by + j), ()):
                        d = math.hypot(c.x - x, c.y - y)
                        if d <= best_d:
                            best, best_d = c, d
        return best
