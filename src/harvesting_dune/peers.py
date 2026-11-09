# Peers: guild sync state (what we publish, what the guild shared) and its settings-page view.
"""A mixin of app.Detector. The key, the switches and the per-member show/hide choice are
settings (settings.py persists them); what the guild shared stays in `PeerStore`, in memory only,
so peer data never reaches the ranking, the observation store or a file under data\\.

The key is a secret: no log line, page state or exception may carry it; the snapshot only says
whether one is set.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import settings
from .catalog import NEVER_TARGET, ORE_MARKERS, marker_of
from .gate import REFUSED
from .store import Status
from .peerview import PeerViewMixin
from .zones import is_deep_desert

SERVER = "158.69.179.64:8443"
NOTE_CELL_CM = 2000  # one queued note per class, type and 20 m cell: the latest wins
MAX_QUEUED = 500
MAX_RECORDS = 200  # per request, as the server accepts them (docs/SYNC-PROTOCOL.md)
ORE_REFRESH_S = 1200.0  # a live ore node still there is confirmed again this often, so guild rings never fade on it
IDENTITY_FIELDS = ("characterName", "characterId", "battlegroupId")


def _ore_cell(marker: str, x: float, y: float) -> tuple[str, int, int]:
    return marker, int(x // NOTE_CELL_CM), int(y // NOTE_CELL_CM)


@dataclass
class PeerStore:
    key_state: str = "none"  # none | unknown (set, not used yet) | valid | invalid | expired (gate.REFUSED: a 401)
    last_sync: float | None = None  # wall clock of the last successful sync
    last_error: str | None = None  # unreachable | tls | rejected | rate-limited | server | None
    connected_as: str | None = None  # character name of the last accepted sync; None after any failure
    members: dict = field(default_factory=dict)
    sightings: dict = field(default_factory=dict)
    outbox: dict = field(default_factory=dict)  # (cls, type, cell x, cell y) -> record, until outbox() sends it
    outbox_world: tuple | None = None  # notes belong to the world they were seen in
    stale_sent: set = field(default_factory=set)  # scan row keys already published as stale
    ore_sent: dict = field(default_factory=dict)  # (marker, cell x, cell y) -> (x, y, sent at) of the live node published there
    since: int = 0
    last_partition: dict | None = None  # where this identity last published: the heartbeat's place in menus
    farewell: dict | None = None  # {key, identity, partition}: the goodbye the sync loop sends next


class PeerMixin(PeerViewMixin):
    def set_sync_key(self, key: str) -> bool:
        """Use a new guild key ("" forgets it) and save it. False, changing nothing, for a key of
        the wrong format."""
        if key and not settings.KEY_RE.fullmatch(key):
            return False
        if self.sync_key and key != self.sync_key:
            self.queue_farewell()  # the old key's characters leave the guild's view now
        if key != self.sync_key:
            self.key_checked_at, self.key_error = None, None  # gate.py: a new key needs its own check
        self.sync_key = key or None
        self.peers.key_state = "unknown" if key else "none"
        self.peers.connected_as = None
        self.peers.since = 0  # a new key may belong to another guild: fetch everything again
        self.forget_guild_profiles()
        settings.save(self)
        self.gate_poll()
        self.sync_wake()
        return True

    def queue_farewell(self) -> None:
        """Say goodbye (`leaving`) with the current key and identity on the loop's next pass, best
        effort; only when this identity has published somewhere and the key was not rejected."""
        part, ident = self.peers.last_partition, self.identity
        if self.sync_key and self.peers.key_state not in REFUSED and part and self.identity_ready and "characterId" in ident:
            self.peers.farewell = {"key": self.sync_key, "identity": {f: ident[f] for f in IDENTITY_FIELDS}, "partition": part}
            self.sync_wake()

    def identity_ended(self) -> None:
        """Called before the identity is dropped: it says goodbye, and nothing queued or staged for it
        is ever sent under the next one."""
        if self.sync_enabled:
            self.queue_farewell()
        p = self.peers
        p.outbox.clear()
        p.connected_as, p.since, p.last_partition = None, 0, None

    # -- publishing --------------------------------------------------------------------------
    def peer_note(self, cls: str, marker: str, x: float, y: float, z: float, now: float,
                  fill: float | None = None, size: str | None = None,
                  row_key: str | None = None, dist_cm: float | None = None) -> None:
        """Queue one strong sighting for the guild. Without a key, or with sync paused, nothing is kept."""
        if not (self.sync_key and self.sync_enabled):
            return
        queued = self.peers.outbox
        if self.peers.outbox_world != self.store.world:
            queued.clear()
            self.peers.outbox_world = self.store.world
        key = (cls, marker, int(x // NOTE_CELL_CM), int(y // NOTE_CELL_CM))
        queued.pop(key, None)
        queued[key] = {"cls": cls, "type": marker, "x": x, "y": y, "z": z, "observedAt": now,
                       "fill": fill, "size": size, "rowKey": row_key, "distCm": dist_cm}
        if len(queued) > MAX_QUEUED:
            del queued[min(queued, key=lambda k: queued[k]["observedAt"])]

    def partition(self) -> dict | None:
        """Where our records belong. Deep Desert layouts reuse one world id, so there the companion's
        map file names the layout; until it is known nothing is published."""
        world = self.store.world
        if not world or not world[0]:
            return None
        if not is_deep_desert(world[0]):
            return {"worldId": world[0], "layout": world[0]}
        layout = self.map_files.get(world)
        return {"worldId": world[0], "layout": layout} if layout else None

    def outbox(self, now: float) -> dict | None:
        """The next upload (the sync loop adds `since` and `sentAt`), or None when there is nothing
        we may publish. Sent parts leave the queue; this is the only place that drains it."""
        who, part = self._publisher(), self.partition()
        if who is None:
            return None
        if part is None:
            return self._heartbeat_only(who)
        self.peers.last_partition = part
        if self.peers.outbox_world != self.store.world:
            self.peers.outbox.clear()  # seen on another map: wrong coordinates for this partition
        pose = self.store.pose
        queued, stale, ore = list(self.peers.outbox.items()), self._new_stale(), self._new_ore(now)
        records = [r for _, r in queued] + [r for _, r in stale] + ore + self._live_spice(now)
        if pose is not None:
            records.sort(key=lambda r: pose.dist2d(r["x"], r["y"]))
        records = records[:MAX_RECORDS]
        sent = {id(r) for r in records}  # what the cap left out stays queued for the next upload
        for k, r in queued:
            if id(r) in sent:
                del self.peers.outbox[k]
        self.peers.stale_sent.update(k for k, r in stale if id(r) in sent)
        self._ore_published(records, now)
        return {
            "identity": who,
            "partition": part,
            "position": {"x": pose.x, "y": pose.y, "z": pose.z, "observedAt": now} if self.sync_positions and pose else None,
            "records": records,
        }

    def _publisher(self) -> dict | None:
        """Who we publish as: one whole identity, with a key and sync on; else None."""
        ident = self.identity
        if not (self.sync_key and self.sync_enabled and self.identity_ready) or "characterId" not in ident:
            return None
        return {f: ident[f] for f in IDENTITY_FIELDS}

    def _heartbeat_only(self, who: dict) -> dict | None:
        """A loading screen or a menu: only the heartbeat goes on, from where we last published."""
        last = self.peers.last_partition
        return None if last is None else {"identity": who, "partition": last, "position": None, "records": []}

    def _live_spice(self, now: float) -> list[dict]:
        return [
            {"cls": "spice", "type": a.marker, "x": a.x, "y": a.y, "z": a.z, "observedAt": now,
             "fill": a.fill_rate, "size": a.spice_size, "rowKey": None, "distCm": None}
            for t in self.store.items.values()
            if (a := t.actor).is_spice_field and t.status is Status.LIVE and a.marker not in NEVER_TARGET
        ]

    def _new_ore(self, now: float) -> list[dict]:
        """Live ore, crystal and stone nodes (one per 20 m cell) that changed since we published that
        cell (newly live, live again after it went missing, another node in the cell), or that were
        last published ORE_REFRESH_S ago and are still there."""
        live = {_ore_cell(a.marker, a.x, a.y): a
                for t in self.store.items.values() if t.status is Status.LIVE and (a := t.actor).marker in ORE_MARKERS}
        sent = self.peers.ore_sent
        self.peers.ore_sent = {k: s for k, s in sent.items()
                               if k in live and (live[k].x, live[k].y) == s[:2] and now - s[2] < ORE_REFRESH_S}
        return [{"cls": "ore", "type": a.marker, "x": a.x, "y": a.y, "z": a.z, "observedAt": now,
                 "fill": None, "size": None, "rowKey": None, "distCm": None}
                for k, a in live.items() if k not in self.peers.ore_sent]

    def _ore_published(self, sent: list[dict], now: float) -> None:
        self.peers.ore_sent.update((_ore_cell(r["type"], r["x"], r["y"]), (r["x"], r["y"], now)) for r in sent if r["cls"] == "ore")

    def _new_stale(self) -> list[tuple[str, dict]]:
        """Scan rows confirmed stale since they were last published. The distance is measured now,
        not at confirmation (the row does not keep it): at most one sync period later."""
        stale, pose = self.scan.stale, self.store.pose
        self.peers.stale_sent &= stale.keys()  # a row that comes back and goes stale again is news
        out = []
        for key, row in stale.items():
            marker = marker_of(*(v for v in (row.get("sprite"), row.get("type")) if isinstance(v, str)))
            if key in self.peers.stale_sent or marker is None:
                continue
            out.append((key, {"cls": "stale", "type": marker, "x": row["x"], "y": row["y"], "z": row["z"],
                              "observedAt": row["confirmedAt"], "fill": None, "size": None, "rowKey": key,
                              "distCm": pose.dist2d(row["x"], row["y"]) if pose else None}))
        return out

    def peers_snapshot(self) -> dict:
        peers = self.peers
        self.peer_expire(self.clock)
        ident = getattr(self, "identity", None) or {}  # from dune.trackedSession; absent until ingest sets it
        named = ident.get("characterName") and ident.get("battlegroupId")
        return {
            "enabled": self.sync_enabled,
            "keySet": self.sync_key is not None,
            "keyState": peers.key_state,
            "positions": self.sync_positions,
            "identity": {"characterName": ident["characterName"], "battlegroupId": ident["battlegroupId"]} if named else None,
            # in a game, but the companion does not track the session: nothing can be shared
            "needsTracking": bool(self.sync_key and self.in_game and self.partition() and not (named and "characterId" in ident)),
            "lastSyncAgeS": None if peers.last_sync is None else max(0.0, time.time() - peers.last_sync),
            "lastError": peers.last_error,
            # a 401 shows as keyState "invalid", not here
            "syncError": {"rate-limited": "rate_limited", "rejected": None}.get(peers.last_error, peers.last_error),
            "connectedAs": peers.connected_as,
            "members": self.peer_members(),
            "hiddenMembers": sorted(self.sync_hidden_members),  # guildmates you chose not to draw
            "sightings": self.peer_sightings(),
            "server": SERVER,
        }
