# Corrections: scan-database rows and which of them are confirmed stale (hidden on the map).
"""Scan-database rows and confirmation of stale ones.

gaming.tools scan rows are keyed `type:trunc(x):trunc(y)` (same as the companion map)
and never expire client-side. A row is confirmed stale only on positive evidence:
the player was close enough that the actor would be loaded, and across several
consecutive live snapshots there was either no matching actor or one reporting
spawned == False. A row seen alive again is un-staled immediately.

Nodes respawn (ore 30-45 min), so a correction also expires after STALE_TTL_S: the row comes
back and is simply confirmed again if you pass by and it is still gone. Without this, maps
that keep their layout (Hagga Basin) collected corrections forever and hid respawned nodes.
"""

from __future__ import annotations

import itertools
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .areas import Node
from .catalog import NODE_MARKERS, SPICE_SIZE, marker_of, resource_of
from .fsutil import read_json, try_write_json
from .model import Actor, Pose
from .ships import SHIP_MARKER

log = logging.getLogger(__name__)

MATCH_CM = {True: 8000.0, False: 3000.0}  # spice fields are large; ore nodes are points
DEFAULT_CONFIRM_RADIUS_CM = 15000.0  # until the detection range is learned
MINED_MATCH_CM = 1000.0  # a mined node's own scan row sits within a few metres of it
MIN_ABSENT_SNAPSHOTS = 3
MIN_ABSENT_S = 6.0
INDEX_CELL_CM = 20000.0  # 200 m spatial buckets for scan rows and live actors
MAX_CONFIRM_RADIUS_CM = 300000.0
# Safety brake: absence of most nearby rows at once means our range assumption is wrong,
# not that the world emptied. Nothing is marked in that pass.
BRAKE_MIN_ROWS = 25
BRAKE_SHARE = 0.5
# how long a confirmed-stale row stays hidden: nodes respawn in 30-45 min (Titanium/Stravidium
# 45); a spent spice site only turns active again at a random later blow
STALE_TTL_S = {"node": 45 * 60, "spice": 3 * 60 * 60}
# a reported downed ship missing at its place was taken by a worm: it never comes back there
SHIP_STALE_TTL_S = 24 * 60 * 60


def stale_ttl_s(entry: dict, ttl_for: Callable[[str], float] | None = None) -> float:
    """How long a correction hides its row: its own ttl (mined nodes), 3 h for spice sites, and
    for nodes the respawn time `ttl_for(marker)` supplies (learned or per resource)."""
    if isinstance(entry.get("ttl"), (int, float)):
        return entry["ttl"]
    sprite = str(entry.get("sprite") or entry.get("type") or "").lower()
    if "spicefield" in sprite:
        return STALE_TTL_S["spice"]
    if sprite == SHIP_MARKER:
        return SHIP_STALE_TTL_S
    # only text values name a marker (a hand-edited file may hold anything)
    marker = marker_of(*(v for v in (entry.get("sprite"), entry.get("type")) if isinstance(v, str)))
    if ttl_for and marker in NODE_MARKERS:
        return ttl_for(marker)  # nodes you mine: the detector's respawn rule
    return STALE_TTL_S["node"]


def _bucket(x: float, y: float) -> tuple[int, int]:
    return (math.floor(x / INDEX_CELL_CM), math.floor(y / INDEX_CELL_CM))


def _near(index: dict, x: float, y: float, radius: float):
    r = math.ceil(radius / INDEX_CELL_CM)
    bx, by = _bucket(x, y)
    for i in range(bx - r, bx + r + 1):
        for j in range(by - r, by + r + 1):
            yield from index.get((i, j), ())


def row_key(row: dict) -> str:
    return f"{row.get('type')}:{math.trunc(row['x'])}:{math.trunc(row['y'])}"


@dataclass(slots=True)
class _Absence:
    first_t: float
    snapshots: int = 1


class ScanIndex:
    def __init__(self, data_dir: Path | None, ttl_for: Callable[[str], float] | None = None) -> None:
        self._ttl_for = ttl_for  # respawn time per node marker (the detector's rule)
        self.rows: list[dict] = []
        self.world: tuple | None = None
        self.map_id: str | None = None
        self.status: int | None = None
        self.fetched_at: float | None = None
        self.stale: dict[str, dict] = {}
        self._absent: dict[str, _Absence] = {}
        self._dir = data_dir
        # marker -> bucket -> [(row, key)]: each type is only searched within its own radius
        self._index: dict[str, dict[tuple[int, int], list[tuple[dict, str]]]] = {}
        self._nodes_cache: list[Node] | None = None
        self._markers: set[str] = set()
        self.braked = 0
        self.refreshed: set[str] = set()  # stale keys already removed from the page's scan layer
        self._restored = False  # expired corrections: rows the page still hides until it reloads

    # -- persistence ----------------------------------------------------------------------
    def _path(self) -> Path | None:
        if self._dir is None or self.world is None:
            return None
        world, seed = self.world
        return self._dir / f"corrections-{world}-{seed}.json"

    def set_world(self, world: tuple) -> None:
        if world == self.world:
            return
        self.world, self.rows, self.stale, self._absent = world, [], {}, {}
        self.refreshed = set()
        self._index, self._nodes_cache = {}, None
        self.map_id, self.status, self.fetched_at = None, None, None
        loaded = read_json(self._path(), dict, {})
        self.stale = {
            k: v for k, v in loaded.items()
            if isinstance(k, str) and isinstance(v, dict)
            and all(isinstance(v.get(c), (int, float)) for c in ("x", "y", "z", "confirmedAt"))
        }

    def _save(self) -> None:
        try_write_json(self._path(), self.stale, "stale corrections", indent=0)

    def expire(self, now: float) -> int:
        """Drop corrections older than their respawn time. Returns how many came back."""
        old = [k for k, v in self.stale.items() if now - v["confirmedAt"] > stale_ttl_s(v, self._ttl_for)]
        for k in old:
            del self.stale[k]
        if old:
            self._nodes_cache = None
            self._restored = True  # the page's scan layer must be reloaded to show them again
            self._save()
            log.info("%s stale scan entries expired (respawn time passed); %s remain", len(old), len(self.stale))
        return len(old)

    # -- rows --------------------------------------------------------------------------------
    def load_rows(self, rows: list[dict], status: int, now: float) -> None:
        clean = []
        for r in rows if isinstance(rows, list) else []:
            if not isinstance(r, dict):
                continue
            x, y, z = r.get("x"), r.get("y"), r.get("z")
            if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (x, y, z)) or (x == 0 and y == 0):
                continue
            if marker_of(r.get("map_marker_id"), r.get("type")) is None:
                continue
            clean.append(r)
        self.rows, self.status, self.fetched_at = clean, status, now
        self._index, self._nodes_cache = {}, None
        self._markers = {marker_of(r.get("map_marker_id"), r.get("type")) for r in clean}
        for r in clean:
            marker = marker_of(r.get("map_marker_id"), r.get("type"))
            self._index.setdefault(marker, {}).setdefault(_bucket(r["x"], r["y"]), []).append((r, row_key(r)))
        if self._dir is not None and self.world is not None and clean:
            # local copy for offline replay/diagnostics; overwritten on every refresh
            path = self._dir / f"scan-{self.world[0]}-{self.world[1]}.json"
            try_write_json(path, clean, "scan rows cache")

    def nodes(self) -> list[Node]:
        """Scan rows as nodes (cached until rows or stale entries change)."""
        if self._nodes_cache is not None:
            return self._nodes_cache
        out = []
        for r in self.rows:
            key = row_key(r)
            if key in self.stale:
                continue
            marker = marker_of(r.get("map_marker_id"), r.get("type"))
            meta = r.get("metadata") or {}
            if meta.get("spawned") is False:
                continue
            fill = meta.get("fillRate")
            fill = (fill / 100 if fill > 1 else fill) if isinstance(fill, (int, float)) else None
            out.append(Node(marker, r["x"], r["y"], r["z"], "scan", fill, key))
        self._nodes_cache = out
        return out

    # -- confirmation ------------------------------------------------------------------------
    def observe(self, pose: Pose | None, live: list[Actor], now: float, radius_for) -> bool:
        """Update stale/confirmed state from one live snapshot. Returns True if anything changed.

        `radius_for(marker)` is the distance inside which an actor of that marker would
        certainly be loaded (learned per marker), or None if not learned yet: rows of an
        unlearned type are never judged.
        """
        if pose is None or not self._index:
            return False
        radii = {}
        # 1) Direct evidence: the game reports a site of the same type right at the row and it is
        #    not spawned. That needs no detection range at all.
        dormant_rows: set[str] = set()
        for a in live:
            if a.marker and a.spawned is False and a.marker in self._index:
                match_cm = MATCH_CM[a.marker in SPICE_SIZE]
                for r, key in _near(self._index[a.marker], a.x, a.y, match_cm):
                    if math.hypot(r["x"] - a.x, r["y"] - a.y) <= match_cm:
                        dormant_rows.add(key)
        # live actors by (marker, bucket) with buckets the size of the match distance, so
        # "is there a live one here?" touches a handful of actors, not every plant nearby
        live_index: dict[tuple, list[Actor]] = {}
        for a in live:
            if a.marker:
                cell = MATCH_CM[a.marker in SPICE_SIZE]
                live_index.setdefault((a.marker, math.floor(a.x / cell), math.floor(a.y / cell)), []).append(a)
        for marker in self._markers:
            r = radius_for(marker)
            if r:
                radii[marker] = min(r, MAX_CONFIRM_RADIUS_CM)
        if not radii and not dormant_rows:
            return False
        # rows with direct evidence are judged even when their type has no learned range
        direct = [
            (r, key, marker, 0.0)
            for marker, buckets in self._index.items()
            if marker not in radii
            for rows in buckets.values()
            for r, key in rows
            if key in dormant_rows
        ] if dormant_rows else []

        changed = False
        evaluated, candidates = 0, []
        nearby = itertools.chain(
            (
                (r, key, marker, radius)
                for marker, radius in radii.items()
                for r, key in _near(self._index.get(marker, {}), pose.x, pose.y, radius)
            ),
            direct,
        )
        for r, key, marker, radius in nearby:
            if key not in dormant_rows and math.hypot(r["x"] - pose.x, r["y"] - pose.y) > radius:
                continue
            evaluated += 1
            match_cm = MATCH_CM[marker in SPICE_SIZE]
            bx, by = math.floor(r["x"] / match_cm), math.floor(r["y"] / match_cm)
            near = [
                a
                for i in (-1, 0, 1)
                for j in (-1, 0, 1)
                for a in live_index.get((marker, bx + i, by + j), ())
                if math.hypot(a.x - r["x"], a.y - r["y"]) <= match_cm
            ]
            if any(a.spawned is not False for a in near):
                self._absent.pop(key, None)
                if self.stale.pop(key, None) is not None:
                    changed = True
                continue
            if key in self.stale:
                continue
            ab = self._absent.get(key)
            if ab is None:
                ab = self._absent[key] = _Absence(now, 0)
            ab.snapshots += 1
            # a downed ship: one snapshot is enough. Snapshots come ~20 s apart, so flying past you
            # are in range for one; a hull that loads late is seen and un-staled right away.
            if marker == SHIP_MARKER or (ab.snapshots >= MIN_ABSENT_SNAPSHOTS and now - ab.first_t >= MIN_ABSENT_S):
                candidates.append((r, key, marker, "not spawned" if near else "absent"))

        if candidates and len(candidates) >= BRAKE_MIN_ROWS and len(candidates) > BRAKE_SHARE * evaluated:
            self.braked += 1
            log.warning(
                "safety brake: %d of %d nearby scan rows would go stale at once; not marking any", len(candidates), evaluated
            )
            for _, key, _, _ in candidates:
                self._absent.pop(key, None)
            return changed
        for r, key, marker, reason in candidates:
            res = resource_of(marker)
            self.stale[key] = {
                "reason": reason,
                "confirmedAt": now,
                "resource": res.key if res else marker,
                "x": r["x"], "y": r["y"], "z": r["z"],
                "type": r.get("type"), "sprite": r.get("map_marker_id"),
            }
            self._absent.pop(key, None)
            log.debug("confirmed stale %s (%s)", key, reason)
            changed = True
        if changed:
            self._nodes_cache = None
            self._save()
            if candidates:
                log.info("confirmed %s more stale scan entries (%s total)", len(candidates), len(self.stale))
        return changed

    def mark_mined(self, marker: str, x: float, y: float, now: float, ttl: float) -> bool:
        """You mined the node at (x, y): hide its scan row now, until it respawns.

        Only the nearest row within MINED_MATCH_CM: nodes in a cluster sit a few metres apart.
        """
        best, best_d = None, MINED_MATCH_CM
        for r, key in _near(self._index.get(marker, {}), x, y, MINED_MATCH_CM):
            d = math.hypot(r["x"] - x, r["y"] - y)
            if d <= best_d:
                best, best_d = (r, key), d
        if best is None:
            return False
        r, key = best
        res = resource_of(marker)
        self.stale[key] = {
            "reason": "mined", "confirmedAt": now, "ttl": ttl,
            "resource": res.key if res else marker,
            "x": r["x"], "y": r["y"], "z": r["z"], "type": r.get("type"), "sprite": r.get("map_marker_id"),
        }
        self._absent.pop(key, None)
        self._nodes_cache = None
        self._save()
        return True

    def drop_caches(self) -> None:
        """Free rebuildable memory (lean mode)."""
        self._nodes_cache = None

    @property
    def refresh_pending(self) -> bool:
        return self._restored or bool(set(self.stale) - self.refreshed)

    def mark_refreshed(self) -> None:
        self.refreshed = set(self.stale)
        self._restored = False

    def overlay_actors(self, pose: Pose | None, max_cm: float = 300000.0, limit: int = 200) -> list[dict]:
        """'Not spawned' markers over stale scan rows the page has not reloaded away yet."""
        out = []
        for key, s in self.stale.items():
            if key in self.refreshed:
                continue  # already gone from the page's scan layer
            if (s.get("sprite") or "").lower().startswith("spicefield"):
                continue  # spice: the stale row is simply removed; a grey marker would mislead
            if pose is not None and math.hypot(s["x"] - pose.x, s["y"] - pose.y) > max_cm:
                continue
            marker = marker_of(s.get("sprite"), s.get("type"))
            res = resource_of(marker)
            proper = next((m for m in (res.marker_types if res else ()) if m.lower() == marker), None)
            out.append({
                "id": f"dd-stale-{key}", "actorType": s.get("type"), "markerType": proper,
                "spriteKey": s.get("sprite"), "x": s["x"], "y": s["y"], "z": s["z"],
                "spawned": False, "fillRate": None, "reportable": False,
            })
        if pose is not None and len(out) > limit:  # the nearest ones matter; keep the minimap light
            out.sort(key=lambda a: math.hypot(a["x"] - pose.x, a["y"] - pose.y))
            out = out[:limit]
        return out
