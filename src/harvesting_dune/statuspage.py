# Status page: the state shown on the settings page (http://127.0.0.1:8765) and its live updates.
"""Settings-page output (a mixin of app.Detector; it shares the detector's state).

`snapshot()` is the contract with viewer.html (GET /state and each /events message); the
server adds "pageVersion". Keys are only ever added, so an older open tab keeps working until it
reloads itself.
"""

from __future__ import annotations

import json
import math
import platform
import re
import sys
from importlib import metadata
from pathlib import Path

from . import logsetup

from .catalog import GROUP_OF, GROUP_ORDER, RESOURCES
from .loot import cage_kind
from .store import Status

RARITY = ["Common", "Uncommon", "Rare", "Epic", "Legendary", "Unique"]
VERSION_FILE = Path(__file__).resolve().parents[2] / "version.txt"  # the release build writes it beside pyproject.toml
VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,63}")  # also sent as an HTTP header (X-Client-Version)


def _shipped_version() -> str | None:
    """The build's own version: a dev build is 'x.y.z-dev.<run>' while pyproject.toml still says x.y.z."""
    try:
        text = VERSION_FILE.read_text("ascii").strip()
    except (OSError, UnicodeError):
        return None
    return text if VERSION.fullmatch(text) else None


def about() -> dict:
    """What a problem report needs besides the errors."""
    version = _shipped_version()
    if version is None:
        try:
            version = metadata.version("harvesting-dune")
        except metadata.PackageNotFoundError:
            version = "dev"
    return {"version": version, "python": sys.version.split()[0], "platform": platform.platform()}


class StatusPageMixin:
    def snapshot(self) -> dict:
        pose = self.store.pose
        rng = self.store.detection_range_cm()
        markers = {m.lower() for m in self.resource.marker_types}
        world_id = self.store.world[0] if self.store.world else None
        next_back = self.mined_nodes.next_back_s(self.clock, self.respawn_for)
        return {
            "gate": self.gate_snapshot(),  # gate.py: closed -> the page shows only the key field and this reason
            "connected": self.connected,
            "viewers": len(self.listeners),  # open settings pages (the watcher opens one only if none is)
            "world": self.store.world,
            "worldName": self.world_name,
            "activity": self.activity,
            "characterState": self.character_state,
            "vehicle": self.vehicles.current,
            "planVehicle": self.plan_vehicle,
            "vehicleOverride": self.vehicles.override,
            "resource": self.resource_key,
            "enabled": self.enabled,
            "maxKm": self.max_km,
            "zones": {"lastPveRow": self.last_pve_row, "linesFromRow": self.lines_from_row, "pvpYield": self.pvp_yield, "pvpOnly": self.pvp_only,
                      "here": self.zone_of(pose.y) if pose else None},
            "base": self.base,
            "pose": None if pose is None else {"x": pose.x, "y": pose.y, "z": pose.z},
            "detectionRangeM": None if rng is None else round(rng / 100),
            "tracked": len(self.store.items),
            "scanRows": len(self.scan.rows),
            "scanStatus": self.scan.status,
            "stale": len(self.scan.stale),
            "scoreMs": self.score_ms,
            "status": self.status_text(),
            "hotspot": self.hotspot,
            "learned": {
                "gridFitted": self.learning.grid_fitted(world_id),
                "secondsPerNode": round(self.node_seconds(markers)),
                "paceSamples": self.learning.samples("pace", markers),
            },
            "mined": {"count": len(self.mined_nodes.items),
                      "nextBackMin": None if next_back is None else round(next_back / 60)},
            "resources": [[r.key, r.name, GROUP_OF.get(r.key, "")] for r in RESOURCES],
            "groupOrder": GROUP_ORDER,
            "itemGroups": self.items.picker(self._item_map(), world_id),
            "itemInfo": self.items.info(self.resource_key, self._item_map(), pose,
                                        [(s.x, s.y) for s in self.ships.active(self.clock)], self.wrecks_here(), world_id),
            "ships": self.ships.status(self.clock, pose, self.zone_of),
            "shipsReported": len(self.reported_ship_nodes()),  # by other players (gaming.tools scan)
            "problems": logsetup.PROBLEMS.snapshot(),
            "about": about(),
            "shipwrecks": [{"name": n, "distanceM": None if pose is None else round(pose.dist2d(x, y) / 100),
                            "zone": self.zone_of(y)} for n, x, y, _ in self.wrecks_here()],
            "itemIndex": {"items": len(self.items.items), "builtAt": self.items.built},
            "minimap": self.minimap_panel(),
            "peers": self.peers_snapshot(),
            "characters": self.characters_snapshot(),  # rows only: GET /character?id= has the details
            "loot": self.loot_nearby(),
            "farmRoutes": self.farm_rows(),  # farm.py: the searched item's routes
            "farm": self.farm_card(),  # the farm route you are on, or None
            "lootIndex": {"cages": len(self.loot.cages), "builtAt": self.loot.built},
            "blows": [
                {**b, "ageMin": round((self.clock - b["t"]) / 60),
                 "distanceM": round(pose.dist2d(b["x"], b["y"]) / 100) if pose else None}
                for b in sorted(self.blows.values(), key=lambda b: -b["t"])[:10]
            ],
            "areas": [
                {
                    "x": a.x, "y": a.y, "feasible": a.feasible, "distance_m": a.distance_m,
                    "nodes": len(a.nodes), "live": a.live_nodes, "expected": round(a.expected),
                    "trip_s": None if a.trip_s is None else round(a.trip_s), "reasons": a.reasons,
                    "detail": self.describe(a) if a.feasible else "",
                }
                for a in self.areas[:25]
            ],
        }

    def _item_map(self) -> str | None:
        """The item-source map for where you are (the matching Deep Desert layout)."""
        live = [(t.actor.x, t.actor.y) for t in self.store.items.values()
                if "LootContainer" in (t.actor.actor_type or "") and t.status is Status.LIVE]
        return self.items.layout_for(self.store.world, live, self.map_files.get(self.store.world))

    def loot_nearby(self, limit: int = 12, max_items: int = 14) -> list[dict]:
        """Rare / ultra-rare cages the game currently shows around you, with their loot tables."""
        pose, world = self.store.pose, self.store.world
        if pose is None or not world:
            return []
        out = []
        for t in self.store.items.values():
            a = t.actor
            kind = cage_kind(a.actor_type)
            if kind is None or t.status is not Status.LIVE or self._emptied(a, self.clock):
                continue  # an emptied cage can stay listed (hasLoot false); it has nothing left to show
            cage = self.loot.lookup(world[0], a.x, a.y)
            drops = cage.drops if cage else []
            special = [d for d in drops if d.get("special")]
            out.append({
                "kind": kind,
                "distanceM": round(pose.dist2d(a.x, a.y) / 100),
                "heightM": round((a.z - pose.z) / 100),
                "known": cage is not None,
                "drops": drops[:max_items],
                "moreDrops": max(0, len(drops) - max_items),
                "specialChance": round(1 - math.prod(1 - d["chance"] for d in special), 3) if special else 0.0,
                "bestRarity": max((d.get("rarity") for d in drops),
                                  key=lambda r: RARITY.index(r) if r in RARITY else -1, default=None),
            })
        out.sort(key=lambda c: (c["kind"] != "Ultra Rare", c["distanceM"]))
        return out[:limit]

    def publish_viewer(self) -> None:
        snap = json.dumps(self.snapshot())
        for q in list(self.listeners):
            if q.qsize() < 4:
                q.put_nowait(snap)
