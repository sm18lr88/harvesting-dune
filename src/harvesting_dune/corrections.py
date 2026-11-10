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
    def _path(self, kind: str = "corrections") -> Path | None:
        if self._dir is None or not self.world or self.world[0] is None:
            return None
        world, seed = self.world
        return self._dir / f"{kind}-{world}-{seed}.json"

    def set_world(self, world: tuple) -> None:
        """Every loading screen reports no world: the rows stay until a different real world arrives."""
        if world == self.world or not world or world[0] is None:
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
        for v in self.stale.values():  # the overlay lower()s these; a hand-edited file may hold anything
            for c in ("sprite", "type"):
                if not isinstance(v.get(c), str):
                    v[c] = None

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
            names = (r.get("map_marker_id"), r.get("type"))
            if not all(n is None or isinstance(n, str) for n in names) or marker_of(*names) is None:
                continue
            clean.append(r)
        self.rows, self.status, self.fetched_at = clean, status, now
        self._index, self._nodes_cache = {}, None
        self._markers = {marker_of(r.get("map_marker_id"), r.get("type")) for r in clean}
        for r in clean:
            marker = marker_of(r.get("map_marker_id"), r.get("type"))
            self._index.setdefault(marker, {}).setdefault(_bucket(r["x"], r["y"]), []).append((r, row_key(r)))
        if clean:  # local copy for offline replay/diagnostics; overwritten on every refresh
            try_write_json(self._path("scan"), clean, "scan rows cache")

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
            meta = r.get("metadata")
            meta = meta if isinstance(meta, dict) else {}  # third-party rows: anything else means nothing
            if meta.get("spawned") is False:
                continue
            fill = meta.get("fillRate")
            fill = (fill / 100 if fill > 1 else fill) if isinstance(fill, (int, float)) else None
            out.append(Node(marker, r["x"], r["y"], r["z"], "scan", fill, key))
        self._nodes_cache = out
        return out

    # -- confirmation ------------------------------------------------------------------------
    def _own_actors(self, live: list[Actor]) -> dict[str, tuple[dict, str, list[Actor]]]:
        """Live actors by the scan row they belong to: the nearest row of their type within the
        match distance. Nodes in a cluster sit a few metres apart, so a neighbour must never
        count as this row seen alive."""
        own: dict[str, tuple[dict, str, list[Actor]]] = {}
        for a in live:
            index = self._index.get(a.marker) if a.marker else None
            if not index:
                continue
            best, best_d = None, MATCH_CM[a.marker in SPICE_SIZE]
            for r, key in _near(index, a.x, a.y, best_d):
                d = math.hypot(r["x"] - a.x, r["y"] - a.y)
                if d <= best_d:
                    best, best_d = (r, key), d
            if best is not None:
                own.setdefault(best[1], (best[0], a.marker, []))[2].append(a)
        return own

    def _judge(self, r: dict, key: str, marker: str, mine: list[Actor], now: float) -> tuple[bool, tuple | None]:
        """One row against its own live actors: (un-staled, stale candidate or None)."""
        if any(a.spawned is not False for a in mine):
            self._absent.pop(key, None)
            if self.stale.pop(key, None) is None:
                return False, None
            if key in self.refreshed:
                self._restored = True  # the page's scan layer still hides it until it reloads
            return True, None
        if key in self.stale:
            return False, None
        ab = self._absent.get(key)
        if ab is None:
            ab = self._absent[key] = _Absence(now, 0)
        ab.snapshots += 1
        # a downed ship: one snapshot is enough. Snapshots come ~20 s apart, so flying past you
        # are in range for one; a hull that loads late is seen and un-staled right away.
        if marker == SHIP_MARKER or (ab.snapshots >= MIN_ABSENT_SNAPSHOTS and now - ab.first_t >= MIN_ABSENT_S):
            return False, (r, key, marker, "not spawned" if mine else "absent")
        return False, None

    def _brake(self, candidates: list[tuple], evaluated: int) -> list[tuple]:
        """Candidates left after the safety brake. Only rows judged absent by range count: a row
        the game itself reports not spawned is evidence, not a range assumption."""
        absent = [c for c in candidates if c[3] == "absent"]
        if len(absent) < BRAKE_MIN_ROWS or len(absent) <= BRAKE_SHARE * evaluated:
            return candidates
        self.braked += 1
        log.warning(
            "safety brake: %d of %d nearby scan rows would go stale at once; not marking them", len(absent), evaluated
        )
        for _, key, _, _ in absent:
            self._absent.pop(key, None)
        return [c for c in candidates if c[3] != "absent"]

    def observe(self, pose: Pose | None, live: list[Actor], now: float, radius_for) -> bool:
        """Update stale/confirmed state from one live snapshot. Returns True if anything changed.

        `radius_for(marker)` is the distance inside which an actor of that marker would
        certainly be loaded (learned per marker), or None if not learned yet: rows of an
        unlearned type are only judged on direct evidence.
        """
        if pose is None or not self._index:
            return False
        own = self._own_actors(live)
        # Direct evidence: the game reports the row's own site and it is not spawned. That needs
        # no detection range at all, so it also holds beyond a learned one.
        dormant = {k: (r, m) for k, (r, m, acts) in own.items() if not any(a.spawned is not False for a in acts)}
        radii = {m: min(r, MAX_CONFIRM_RADIUS_CM) for m in self._markers if (r := radius_for(m))}
        if not radii and not dormant:
            return False
        changed = False
        evaluated, candidates, judged = 0, [], set()
        in_range = (
            (r, key, marker)
            for marker, radius in radii.items()
            for r, key in _near(self._index.get(marker, {}), pose.x, pose.y, radius)
            if math.hypot(r["x"] - pose.x, r["y"] - pose.y) <= radius
        )
        for r, key, marker in in_range:
            evaluated += 1
            judged.add(key)
            unstaled, cand = self._judge(r, key, marker, own.get(key, (None, None, []))[2], now)
            changed |= unstaled
            candidates += [cand] if cand else []
        for key, (r, marker) in dormant.items():
            if key not in judged:
                judged.add(key)
                _, cand = self._judge(r, key, marker, own[key][2], now)
                candidates += [cand] if cand else []
        # absence must be consecutive: a row left out of range starts counting again on the next visit
        self._absent = {k: v for k, v in self._absent.items() if k in judged}
        candidates = self._brake(candidates, evaluated)
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
