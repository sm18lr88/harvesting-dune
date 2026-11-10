# Item sources: where to loot materials (Diamondine Dust...); writes data\item-sources.json.
"""Looted materials as search targets.

Items such as Diamondine Dust are not map nodes: they drop from containers and corpses. gaming.tools'
item data lists every drop location per map with chance and quantity. `loot_crawl.js` collects them
for material items; here each becomes a resource (key `item:<id>`), and its sources on the current map
become candidate nodes, weighted by expected quantity (chance x average amount).

The Deep Desert has one set of locations per weekly layout (deepdesert_1_00..11). The current layout
is the one whose container positions match the containers the game shows you (positions are exact).
"""

from __future__ import annotations

import logging
import math
import time
from pathlib import Path

from .catalog import CHANCE_GROUP, ITEM_GROUPS, MAP_ITEM_IDS, Resource
from .fsutil import read_json, try_write_json

log = logging.getLogger(__name__)

PREFIX = "item:"
CACHE_VERSION = 3  # 2: unique gear and schematics, map names; 3: their schematics' drop places
# readable names for map ids gaming.tools does not name (prefix -> name; the rest of the id follows).
# Public part only; the server's `regions` dataset adds the instance names (gate.on_dataset).
REGION_NAMES = {"survival_1": "Hagga Basin"}
MAX_REGIONS = 200
# "Fallen Shipwreck", "... (PvE Area)", "... (PvP Area)": the ships that crash at random places. Their rows
# sit at (0, 0, 0); the detector puts them where it saw a downed ship (ships.py).
SHIP_SOURCE = "Fallen Shipwreck"
MATCH_CM = 200.0  # temporary containers sit exactly on their database location
WRECK_CM = 20_000.0  # a shipwreck's containers lie within ~130 m of its map marker (map data, survival_1)


def _place(row: tuple, wrecks) -> tuple[str, str]:
    """("wreck", "<wreck name> (<container>)") for a container inside a named shipwreck, else ("fixed", kind)."""
    x, y, name = row[0], row[1], row[3]
    wreck = next((w for w in wrecks if math.hypot(w[1] - x, w[2] - y) <= WRECK_CM), None)
    return ("wreck", f"{wreck[0]} ({name})") if wreck else ("fixed", name)


def _add_source(kinds: dict, row: tuple, where: str, name: str, at, pose) -> dict:
    """Merge one source row into its card entry: best chance, amount range, expected, nearest."""
    _, _, _, _, chance, lo, hi = row
    k = kinds.setdefault(name, {"name": name, "where": where, "count": 0, "bestChance": 0.0, "min": lo, "max": hi,
                                "nearestM": None, "expected": 0.0})
    k["bestChance"] = max(k["bestChance"], round(chance, 4))
    k["min"], k["max"] = min(k["min"], lo), max(k["max"], hi)
    k["expected"] = round(max(k["expected"], chance * (lo + hi) / 2), 2)
    dists = [round(math.hypot(px - pose.x, py - pose.y) / 100) for px, py in at] if pose is not None else []
    if dists:
        k["nearestM"] = min(dists + ([k["nearestM"]] if k["nearestM"] is not None else []))
    return k


def _finite(*values: object) -> bool:
    return all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)


def _usable(row: object) -> bool:
    """A stored (x, y, z, name, chance, min, max) row the ranking can do arithmetic on; the crawl
    writes a missing coordinate as null (Math.round(undefined))."""
    return isinstance(row, (list, tuple)) and len(row) == 7 and _finite(*row[:3], *row[4:])


def _kept_item(meta: object, allowed: set[str]) -> dict | None:
    """The item if the readers can use it (a current class, a name), with only [name, price, stock] vendor
    rows; a hand-edited cache may hold anything."""
    if not isinstance(meta, dict) or meta.get("group") not in allowed or not isinstance(meta.get("name"), str):
        return None
    sold = meta.get("sold")
    return {**meta, "sold": [r for r in sold if _vendor_row(r)] if isinstance(sold, list) else []}


def _vendor_row(row: object) -> bool:
    """[name, price, stock]: the page shows the price and the stock as numbers; a vendor may list no stock."""
    if not (isinstance(row, list) and len(row) == 3):
        return False
    price, stock = row[1], row[2]
    return (_finite(price) and not isinstance(price, bool)
            and (stock is None or (isinstance(stock, int) and not isinstance(stock, bool))))


def _cell(x: float, y: float) -> tuple[int, int]:
    return (round(x / MATCH_CM), round(y / MATCH_CM))


def _place_count(rows: list[tuple]) -> int:
    """Fixed places, not rows: the Deep Desert lists one container twice for an item."""
    return len({(r[0], r[1], r[3]) for r in rows if not ItemSources.from_ships(r)})


def _add_rows(by_map: dict, iid: str, drops: object) -> None:
    for row in drops if isinstance(drops, list) else []:
        if not isinstance(row, list) or len(row) < 8 or not row[0]:
            continue
        map_id, x, y, z, name, chance, lo, hi = row[:8]
        stored = (x, y, z, name, chance or 0.0, lo or 1, hi or 1)
        if not _usable(stored):
            continue
        lo, hi = sorted(stored[5:])
        by_map.setdefault(map_id, {}).setdefault(iid, []).append((*stored[:5], lo, hi))


def parse_regions(data: object) -> dict[str, str] | None:
    """`{"regionNames": {prefix: name}}` from the regions dataset; None for any other shape."""
    names = data.get("regionNames") if isinstance(data, dict) else None
    if not isinstance(names, dict):
        return None
    return {k[:64]: v[:60] for k, v in list(names.items())[:MAX_REGIONS] if isinstance(k, str) and k and isinstance(v, str)}


def item_id(key: str | None) -> str | None:
    return key[len(PREFIX):] if key and key.startswith(PREFIX) else None


def group_of(categories: list[str]) -> str | None:
    for c in categories or []:
        for prefix, name in ITEM_GROUPS.items():
            if c.startswith(prefix):
                return name
    return None


class ItemSources:
    def __init__(self, data_dir: Path | None) -> None:
        self.path = (data_dir / "item-sources.json") if data_dir else None
        self.items: dict[str, dict] = {}  # id -> {name, icon, rarity, tier, group, sold}
        # map id -> item id -> [(x, y, z, source name, chance, min, max)]
        self.by_map: dict[str, dict[str, list[tuple]]] = {}
        self._layout: dict[tuple, str] = {}  # (world, seed) -> matched layout map id
        self._cells: dict[str, set[tuple[int, int]]] = {}  # layout map id -> its source cells
        self.built: float | None = None
        self.map_names: dict[str, str] = {}  # map id -> gaming.tools' name for it
        self.region_names = dict(REGION_NAMES)  # map id prefix -> our name for it
        self.load()

    # -- data ---------------------------------------------------------------------------------
    def load(self) -> None:
        data = read_json(self.path, dict) if self.path else None
        if not data or data.get("version") != CACHE_VERSION:
            return
        try:
            self._set(data["built"], data["items"], data["byMap"], data.get("mapNames"))
        except (KeyError, TypeError, ValueError, AttributeError):  # AttributeError: a list where a map belongs
            log.warning("ignoring malformed %s", self.path)

    def ingest(self, crawl: dict) -> int:
        items, by_map = {}, {}
        for m in crawl.get("materials") or []:
            if not isinstance(m, dict) or not m.get("id") or m["id"] in MAP_ITEM_IDS:
                continue
            group = group_of(m.get("categories") or [])
            if group is None:
                continue
            items[m["id"]] = {"name": m.get("name") or m["id"], "icon": m.get("icon"), "rarity": m.get("rarity"),
                              "tier": m.get("tier"), "group": group, "sold": m.get("sold") or []}
            _add_rows(by_map, m["id"], m.get("drops"))
        for u in crawl.get("uniques") or []:
            if not isinstance(u, dict) or not isinstance(u.get("id"), str) or u["id"] in items:
                continue
            items[u["id"]] = {"name": u.get("name") or u["id"], "icon": u.get("icon"), "rarity": u.get("rarity"),
                              "tier": u.get("tier"), "group": CHANCE_GROUP, "chance": bool(u.get("drops")),
                              "schematic": bool(u.get("schematic")), "via": u.get("via") if u.get("via") in ("item", "schematic") else "item",
                              "reward": [r for r in u.get("reward") or [] if isinstance(r, str)][:4], "sold": u.get("sold") or []}
            _add_rows(by_map, u["id"], u.get("drops"))
        names = crawl.get("mapNames")
        self._set(crawl.get("built", time.time() * 1000) / 1000, items, by_map, names)
        if self.path:
            try_write_json(self.path, {"version": CACHE_VERSION, "built": self.built, "items": self.items,
                                       "byMap": self.by_map, "mapNames": self.map_names}, "item sources")
        return len(items)

    def _set(self, built: float, items: dict, by_map: dict, map_names: object = None) -> None:
        # only current material classes: a class dropped later (consumables) vanishes from an old cache
        allowed = set(ITEM_GROUPS.values()) | {CHANCE_GROUP}
        self.map_names = ({k: v[:60] for k, v in map_names.items() if isinstance(k, str) and isinstance(v, str)}
                          if isinstance(map_names, dict) else {})
        items = {i: kept for i, m in items.items() if (kept := _kept_item(m, allowed))}
        by_map = {mp: {i: rows for i, rows in per.items() if i in items} for mp, per in by_map.items()}
        self.built, self.items, self._layout, self._cells = built, items, {}, {}
        # an old cache may hold rows written before they were checked
        self.by_map = {mp: {i: [tuple(r) for r in rows if _usable(r)] for i, rows in per.items()} for mp, per in by_map.items()}

    # -- resources -----------------------------------------------------------------------------
    def is_item(self, key: str | None) -> bool:
        iid = item_id(key)
        return iid is not None and iid in self.items

    def resource(self, key: str) -> Resource | None:
        iid = item_id(key)
        meta = self.items.get(iid) if iid else None
        if meta is None:
            return None
        # each source is its own stop (min_patch 1); sources are known places, never "live only"
        return Resource(key, meta["name"], (key,), "on_foot", min_patch=1, chance=bool(meta.get("chance")))

    # -- where ---------------------------------------------------------------------------------
    def maps_for(self, world_id: str | None) -> list[str]:
        if not world_id:
            return []
        return [m for m in self.by_map if m == world_id or m.startswith(world_id + "_")]

    def layout_for(self, world: tuple | None, live_positions: list[tuple[float, float]],
                   map_file: str | None = None) -> str | None:
        """The map id of the current Deep Desert layout: the map file the companion loaded (`map_file`),
        else the layout whose source positions match the containers the game shows."""
        if not world or not world[0]:
            return None
        maps = self.maps_for(world[0])
        if map_file in maps:
            return map_file  # the companion itself drew this week's layout
        if len(maps) <= 1:
            return maps[0] if maps else None
        known = self._layout.get(world)
        if known:
            return known
        live = [_cell(x, y) for x, y in live_positions]
        (hits, best), (second, _) = sorted(((sum(c in self._layout_cells(m) for c in live), m) for m in maps), reverse=True)[:2]
        if hits == second:
            return None  # several layouts fit what you saw (or none does): wait for a container that decides
        if hits - second >= 2:
            self._layout[world] = best  # settled for this Coriolis cycle
        return best

    def _layout_cells(self, map_id: str) -> set[tuple[int, int]]:
        cells = self._cells.get(map_id)
        if cells is None:
            cells = self._cells[map_id] = {_cell(x, y) for rows in self.by_map[map_id].values() for x, y, *_ in rows}
        return cells

    def sources(self, key: str, map_id: str | None) -> list[tuple]:
        iid = item_id(key)
        if not iid or not map_id:
            return []
        return self.by_map.get(map_id, {}).get(iid, [])

    @staticmethod
    def weight(row: tuple) -> float:
        """Expected quantity from one source: chance x average amount."""
        _, _, _, _, chance, lo, hi = row
        return chance * (lo + hi) / 2

    @staticmethod
    def from_ships(row: tuple) -> bool:
        return str(row[3]).startswith(SHIP_SOURCE) or (row[0] == 0 and row[1] == 0)

    def ship_rows(self, key: str, map_id: str | None, world_id: str | None = None) -> list[tuple]:
        """What a downed ship drops here. It is the same in every Deep Desert layout, so it does not
        wait until the layout is matched from the containers you see."""
        rows = [r for r in self.sources(key, map_id) if self.from_ships(r)]
        for m in [] if rows else self.maps_for(world_id):
            rows = [r for r in self.sources(key, m) if self.from_ships(r)]
            if rows:
                break
        return rows

    def ship_weight(self, key: str, map_id: str | None, pvp: bool | None, world_id: str | None = None) -> float:
        """Expected quantity from one downed ship: its PvP or PvE loot in the Deep Desert."""
        rows = self.ship_rows(key, map_id, world_id)
        area = None if pvp is None else "(PvP Area)" if pvp else "(PvE Area)"
        picked = [r for r in rows if area is None or area in r[3] or "(" not in r[3]] or rows
        return sum(self.weight(r) for r in picked)

    def region(self, map_id: str) -> str:
        """A readable name for a map: gaming.tools' own, else a known kind of map plus the rest of its id."""
        if map_id.startswith("deepdesert"):
            return "Deep Desert"  # its weekly layouts are one region
        if map_id in self.map_names:
            return self.map_names[map_id]
        for prefix, name in sorted(self.region_names.items(), key=lambda kv: -len(kv[0])):  # the longest prefix wins
            if map_id.startswith(prefix):
                return f"{name} {map_id[len(prefix):].strip('_').replace('_', ' ')}".strip()
        return map_id.replace("_", " ")

    def elsewhere(self, key: str, world_id: str | None, limit: int = 8) -> list[dict]:
        """Where else it drops: the best chance in each other region, best first."""
        iid = item_id(key)
        best: dict[str, dict] = {}
        for mp, per in self.by_map.items():
            rows = per.get(iid) if iid else None
            if not rows or (world_id and (mp == world_id or mp.startswith(world_id + "_"))):
                continue
            top = max(rows, key=lambda r: r[4])
            region = self.region(mp)
            if region not in best or top[4] > best[region]["bestChance"]:
                best[region] = {"region": region, "source": top[3], "bestChance": round(top[4], 4), "places": _place_count(rows)}
        return sorted(best.values(), key=lambda b: -b["bestChance"])[:limit]

    # -- settings page -------------------------------------------------------------------------
    def picker(self, map_id: str | None, world_id: str | None = None) -> list[dict]:
        """Material classes with their items, and how many sources each has on this map."""
        here = self.by_map.get(map_id, {}) if map_id else {}
        groups: dict[str, list] = {}
        for iid, meta in self.items.items():
            fixed = _place_count(here.get(iid, []))
            ships = bool(self.ship_rows(PREFIX + iid, map_id, world_id))
            # [key, name, fixed places on this map, also drops from downed ships, found only by chance]
            groups.setdefault(meta["group"], []).append([PREFIX + iid, meta["name"], fixed, ships, bool(meta.get("chance"))])
        return [{"group": g, "items": sorted(v, key=lambda r: (r[2] == 0 and not r[3], r[1]))} for g, v in groups.items()]

    def info(self, key: str, map_id: str | None, pose=None, ships=(), wrecks=(), world_id: str | None = None) -> dict | None:
        """Sources of one item on this map, grouped by where they are, and its vendors.

        `ships`: (x, y) of the downed ships known now; their drops have no fixed place. `wrecks`:
        (name, x, y, z) of the named shipwrecks; a container inside one is listed under its name.
        """
        iid = item_id(key)
        meta = self.items.get(iid) if iid else None
        if meta is None:
            return None
        kinds: dict[str, dict] = {}
        seen: set[tuple] = set()
        for row in self.sources(key, map_id):
            if not self.from_ships(row):
                where, name = _place(row, wrecks)
                _add_source(kinds, row, where, name, [(row[0], row[1])], pose)["count"] += (row[0], row[1], row[3]) not in seen
                seen.add((row[0], row[1], row[3]))
        ship_rows = self.ship_rows(key, map_id, world_id)
        for row in ship_rows:  # one entry for the PvE and PvP variants
            _add_source(kinds, row, "ships", "Downed ships", ships, pose)
        if ship_rows:
            per_variant: dict[str, float] = {}
            for r in ship_rows:
                per_variant[r[3]] = per_variant.get(r[3], 0.0) + self.weight(r)
            kinds["Downed ships"].update(count=len(ships), expected=round(max(per_variant.values()), 2))  # one whole ship
        return {"key": key, "name": meta["name"], "group": meta["group"], "icon": meta.get("icon"),
                "rarity": meta.get("rarity"), "chance": bool(meta.get("chance")), "via": meta.get("via", "item"),
                "reward": meta.get("reward") or [],
                "elsewhere": self.elsewhere(key, world_id),
                "sources": sorted(kinds.values(), key=lambda k: (-k["expected"], -k["count"])),
                "vendors": [{"name": n, "price": p, "stock": s} for n, p, s in meta.get("sold") or [] if n]}

