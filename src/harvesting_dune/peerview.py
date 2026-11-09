# Peerview: what the guild shared, merged in memory and shown as aged rings, a roster and lists.
"""Receive side of guild sync, a base of peers.PeerMixin (so of app.Detector).

Ages arrive relative to the server's clock and are turned into feed-clock times at merge
(`seen_at = clock - ageS`), so replays and the live feed age peer rows the same way. Peer rows
never become ranking nodes, never touch the observation store, the scan index or a file, and a
peer "stale" row only hides a scan row with two distinct observers, a distance, no actor we see
live there ourselves, and, for a node that respawns, only until its respawn time has passed.
"""

from __future__ import annotations

import logging
import math

from . import logsetup
from .catalog import NODE_MARKERS, resource_of
from .corrections import MATCH_CM, stale_ttl_s
from .ships import REMEMBER_S as SHIP_REMEMBER_S
from .store import REMEMBER_SPICE_S, Status
from .zones import DEFAULT_FIT, cell_name, is_deep_desert

log = logging.getLogger(__name__)

CLASSES = ("spice", "blow", "ship", "mined", "stale", "ore")
RINGS = {  # cls -> (ring cm, colour with the alpha left open)
    "spice": (4000, "rgba(120,255,140,{})"),
    "blow": (5000, "rgba(255,120,220,{})"),
    "ship": (6000, "rgba(255,170,60,{})"),
    "ore": (3000, "rgba(180,150,255,{})"),
}
MEMBER_RING = (6000, "rgba(255,230,120,{})")
MEMBER_TTL_S = 900.0  # the span a late member's ring fades over
# the longest the server lists a member after its last sync (3 x 600 s promised + 20 s grace + 15 min
# offline): the server's list replaces ours, this only clears what an outage of our own left behind
MEMBER_KEEP_S = 3 * 600 + 20 + 900.0
STATES = ("online", "in_menu", "late", "offline")  # the server's presence states, in roster order
RINGED = ("online", "late")  # states drawn on the minimap
MAX_RINGS, MAX_RINGS_LEAN, MAX_LISTED = 40, 20, 20
WIRE_CELL_CM = 2000  # the server's sighting key cell (docs/SYNC-PROTOCOL.md)


def _num(v: object) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _skip(kind: str, what: str) -> None:
    logsetup.limited(log, f"peer-{kind}", "guild sync: skipped a %s row (%s)", kind, what)


def _alpha(age: float, lifetime: float) -> float:
    return round(0.75 - 0.5 * min(max(age, 0.0) / lifetime, 1.0), 2)


def _text(row: dict, key: str) -> str | None:
    return row[key] if isinstance(row.get(key), str) else None


def _sighting(row: object) -> dict | None:
    """One wire sighting as stored (still with its ageS and battlegroupId), or None if unusable."""
    if not isinstance(row, dict):
        return None
    x, y, age, cls = _num(row.get("x")), _num(row.get("y")), _num(row.get("ageS")), row.get("cls")
    if x is None or y is None or age is None or cls not in CLASSES or _text(row, "type") is None:
        return None
    observers = row.get("observers")
    return {
        "cls": cls, "type": row["type"], "x": x, "y": y, "z": _num(row.get("z")) or 0.0, "ageS": age,
        "battlegroupId": _text(row, "battlegroupId"), "worldId": row.get("worldId"), "layout": row.get("layout"),
        "fill": _num(row.get("fill")), "size": _text(row, "size"), "rowKey": _text(row, "rowKey"),
        "distCm": _num(row.get("distCm")), "observer": _text(row, "observerName"),
        "observers": {o for o in observers if isinstance(o, str)} if isinstance(observers, list) else set(),
    }


def _state(row: dict) -> str:
    """An older server sends no state: every row it lists is online. A state this client does not
    know yet counts as offline, so it is never drawn."""
    state = row.get("state", "online")
    return state if state in STATES else "offline"


def _member(row: object) -> dict | None:
    """One presence row (position only when that member shares it), or None if unusable."""
    if not isinstance(row, dict):
        return None
    cid, age = row.get("characterId"), _num(row.get("ageS"))
    if not isinstance(cid, int) or isinstance(cid, bool) or age is None or _text(row, "characterName") is None:
        return None
    x, y = _num(row.get("x")), _num(row.get("y"))
    placed = x is not None and y is not None
    return {"characterId": cid, "characterName": row["characterName"], "battlegroupId": row.get("battlegroupId"),
            "worldId": row.get("worldId"), "layout": row.get("layout"), "ageS": age, "state": _state(row),
            "x": x if placed else None, "y": y if placed else None, "z": _num(row.get("z")) if placed else None}


def _ringed(m: dict) -> bool:
    return m["x"] is not None and m["state"] in RINGED


def _rings(members: dict) -> dict:
    """What the minimap draws of these members, to tell whether a response changed it."""
    return {cid: (m["x"], m["y"], m["worldId"], m["layout"], m["state"]) for cid, m in members.items() if _ringed(m)}


class PeerViewMixin:
    # -- merging a server response -------------------------------------------------------------
    def ingest_peer(self, response: dict, now: float) -> None:
        """Merge one successful sync response; `now` is wall time. Bad rows are skipped with a
        rate-limited warning; nothing here raises on an odd shape. Only a response that moved a ring
        asks for a redraw: an unchanged one just refreshes the members' age stamps."""
        if not isinstance(response, dict):
            _skip("response", "not an object")
            return
        peers = self.peers
        peers.last_sync, peers.key_state = now, "valid"
        if isinstance(response.get("next"), int) and not isinstance(response["next"], bool):
            peers.since = response["next"]
        part, group = self.partition(), self.identity.get("battlegroupId")
        here = (group, part["worldId"], part["layout"]) if part and group else None
        drew = self._merge_sightings(response.get("sightings"), here)
        drew |= self._merge_members(response.get("presence"), group)
        if drew:
            self._changed()

    def _merge_sightings(self, rows: object, here: tuple | None) -> bool:
        drew = False
        for row in rows if isinstance(rows, list) else ():
            s = _sighting(row)
            if s is None:
                _skip("sighting", "missing or bad cls, type, x, y or ageS")
                continue
            if here is None or (s.pop("battlegroupId") or here[0], s["worldId"], s["layout"]) != here:
                continue  # another battlegroup, map or layout: its coordinates mean nothing here
            s["seen_at"] = self.clock - s.pop("ageS")
            self.peers.sightings[(s["cls"], s["type"], math.floor(s["x"] / WIRE_CELL_CM), math.floor(s["y"] / WIRE_CELL_CM))] = s
            drew |= s["cls"] in RINGS or s["cls"] == "stale"
        return drew

    def _merge_members(self, rows: object, group: str | None) -> bool:
        """The server lists every member that is not gone, so the list replaces ours: a member it no
        longer lists disappears, one it lists again comes back. True when a ring moved, came or went."""
        if not isinstance(rows, list):
            return False
        members = {}
        for row in rows:
            m = _member(row)
            if m is None:
                _skip("member", "missing or bad characterId, characterName or ageS")
                continue
            if group is None or m.pop("battlegroupId") != group:
                continue
            m["seen_at"] = self.clock - m.pop("ageS")
            members[m.pop("characterId")] = m
        drew = _rings(self.peers.members) != _rings(members)
        self.peers.members = members
        return drew

    # -- ageing --------------------------------------------------------------------------------
    def peer_lifetime(self, cls: str, marker: str) -> float:
        match cls:
            case "spice" | "blow":
                return REMEMBER_SPICE_S
            case "ship":
                return SHIP_REMEMBER_S
            case "mined" | "ore":
                return self.respawn_for(marker)
            case _:  # stale
                return stale_ttl_s({"type": marker}, self.respawn_for)

    def peer_expire(self, now: float) -> None:
        """Drop rows older than their lifetime; `now` is the feed clock."""
        sightings, members = self.peers.sightings, self.peers.members
        for k in [k for k, s in sightings.items() if now - s["seen_at"] > self.peer_lifetime(s["cls"], s["type"])]:
            del sightings[k]
        for k in [k for k, m in members.items() if now - m["seen_at"] > MEMBER_KEEP_S]:
            del members[k]

    # -- what is shown -------------------------------------------------------------------------
    def _peer_here(self) -> tuple[str, str] | None:
        part = self.partition()
        return (part["worldId"], part["layout"]) if part else None

    def _here_sightings(self) -> list[dict]:
        here = self._peer_here()
        return [s for s in self.peers.sightings.values() if (s["worldId"], s["layout"]) == here]

    def _nearest(self, items: list[dict], limit: int) -> list[dict]:
        pose = self.store.pose
        if pose is not None:
            items.sort(key=lambda d: pose.dist2d(d["x"], d["y"]))
        else:
            items.sort(key=lambda d: -d["seen_at"])
        return items[:limit]

    def peer_marks(self) -> list[dict]:
        """Rings for shared spice, blows, ships, ore and the guildmates you chose to show, faded by age."""
        if not self.enabled:
            return []
        out = []
        for s in self._here_sightings():
            if s["cls"] in RINGS:
                ring, colour = RINGS[s["cls"]]
                alpha = _alpha(self.clock - s["seen_at"], self.peer_lifetime(s["cls"], s["type"]))
                out.append({"x": s["x"], "y": s["y"], "size": None, "kind": "peer", "ring": ring,
                            "color": colour.format(alpha), "weight": 2, "seen_at": s["seen_at"]})
        here, me = self._peer_here(), self.identity.get("characterId")
        for cid, m in self.peers.members.items():
            if cid == me or cid in self.sync_hidden_members or not _ringed(m) or (m["worldId"], m["layout"]) != here:
                continue
            # online: full; late: faded further as its last position ages
            alpha = 0.75 if m["state"] == "online" else round(_alpha(self.clock - m["seen_at"], MEMBER_TTL_S) / 2, 2)
            out.append({"x": m["x"], "y": m["y"], "size": None, "kind": "member", "ring": MEMBER_RING[0],
                        "color": MEMBER_RING[1].format(alpha), "weight": 2, "seen_at": m["seen_at"]})
        marks = self._nearest(out, MAX_RINGS_LEAN if self.lean else MAX_RINGS)
        return [{k: v for k, v in d.items() if k != "seen_at"} for d in marks]

    def peer_stale_keys(self) -> set[str]:
        """Scan rows the guild confirmed gone: two distinct observers, a distance, nothing live here,
        and for a node that respawns, a report younger than its respawn time."""
        live = [t.actor for t in self.store.items.values() if t.status is Status.LIVE]
        out = set()
        for s in self._here_sightings():
            if s["cls"] != "stale" or s["rowKey"] is None or s["distCm"] is None or len(s["observers"]) < 2:
                continue
            if s["type"] in NODE_MARKERS and self.clock - s["seen_at"] >= self.respawn_for(s["type"]):
                continue  # it has had time to grow back
            radius = MATCH_CM["spicefield" in s["type"].lower()]
            if not any(math.hypot(a.x - s["x"], a.y - s["y"]) <= radius for a in live):
                out.add(s["rowKey"])
        return out

    def peer_members(self) -> list[dict]:
        """The roster: you first (never drawn), then the guild in presence order."""
        here, pose, me = self._peer_here(), self.store.pose, self.identity.get("characterId")
        out = []
        if me is not None and self.identity.get("characterName"):
            out.append({"characterId": me, "name": self.identity["characterName"], "worldId": here[0] if here else None,
                        "sector": None, "ageS": 0, "distanceM": None, "self": True, "shown": True, "state": "you"})
        order = sorted(((cid, m) for cid, m in self.peers.members.items() if cid != me),
                       key=lambda kv: (STATES.index(kv[1]["state"]), kv[1]["characterName"].lower()))
        for cid, m in order:
            placed = m["x"] is not None
            fit = self.learning.grid_fit(m["worldId"]) or DEFAULT_FIT
            near = placed and pose is not None and (m["worldId"], m["layout"]) == here
            out.append({
                "characterId": cid, "name": m["characterName"], "worldId": m["worldId"],
                "sector": cell_name(m["x"], m["y"], fit) if placed and is_deep_desert(m["worldId"]) else None,
                "ageS": round(self.clock - m["seen_at"]),
                "distanceM": round(pose.dist2d(m["x"], m["y"]) / 100) if near else None,
                "self": False, "shown": cid not in self.sync_hidden_members, "state": m["state"],
            })
        return out

    def peer_sightings(self) -> list[dict]:
        pose = self.store.pose
        return [{
            "cls": s["cls"], "type": s["type"], "ageS": round(self.clock - s["seen_at"]), "observer": s["observer"],
            "distanceM": round(pose.dist2d(s["x"], s["y"]) / 100) if pose is not None else None,
            "size": s["size"], "fill": s["fill"], "name": res.name if (res := resource_of(s["type"])) else None,
            "lifetimeS": round(self.peer_lifetime(s["cls"], s["type"])),
        } for s in self._nearest(self._here_sightings(), MAX_LISTED)]
