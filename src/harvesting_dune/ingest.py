# Ingest: turns companion bus messages into observations, terrain, vehicle and mined-node events.
"""Feed intake for the detector (a mixin of app.Detector; it shares the detector's state).

dune.session -> world and vehicle state; dune.player -> position, terrain and vehicle motion;
dune.rawStats -> vehicle names; dune.trackedSession -> who you play (name, id, battlegroup);
dune.trackedDestination -> where a guild pin lands (memory only);
dune.status -> in a game or not; the character topics (profile.TOPICS) -> characters.py;
dune.map.actors -> tracked actors, spice blows, looted cages,
nodes you mined, and stale scan rows.
"""

from __future__ import annotations

import logging
import math

from .catalog import CAGE_MARKERS, NEVER_TARGET, NODE_MARKERS
from .model import BusMessage, parse_pose, parse_stat_names
from .profile import TOPICS as PROFILE_TOPICS
from .ships import GONE_CM, SHIP_MARKER
from .store import Status
from .terrain import VEHICLES
from .worldfiles import blows_path, save_blows

log = logging.getLogger(__name__)

# A node that vanishes this close to you was taken: by you, your group, or another player. Nodes
# load from hundreds of metres away, so this is never the game unloading them. For the ranking a
# taken node is gone either way, and the pace is how fast your group empties the patch.
MINE_RADIUS_CM = 6000.0
MINED_MAX_PER_SNAPSHOT = 4  # more vanishing at once is the list unloading, not mining
PACE_GAP_MAX_S, PACE_RANGE_CM = 180.0, 15000.0  # consecutive mines this close in time and space
LOOT_RADIUS_CM = 2500.0
IDENTITY_TEXT_MAX = 64  # character names and battlegroup ids are short; this value goes on the wire


class IngestMixin:
    def handle(self, msg: BusMessage, now: float) -> None:
        try:
            self._handle(msg, now)
            if self.farm is not None:
                self.farm_feed(msg, now)  # farm.py: a running farm route follows you
        except Exception:  # one odd payload must never take the detector down
            log.exception("ignored malformed %s message", msg.topic)

    # Topics handled before anything else, then done. trackedSession comes first so a new character
    # never inherits this delta's profile part; the destination is only read into memory.
    _FIRST = {"dune.trackedSession": "_on_tracked_character", "dune.trackedDestination": "_on_destination"}

    def _on_tracked_character(self, msg: BusMessage) -> None:
        self._on_tracked_session(msg)
        self.on_character_topic(msg)

    def _handle(self, msg: BusMessage, now: float) -> None:
        self.clock = max(self.clock, now)  # message time: ages are measured on the feed's clock
        # Any bus traffic proves the feed is live; the bridge usually attaches after the
        # page's own hello handshake, so helloAck alone is not a reliable signal.
        self.connected = True
        if msg.type == "helloAck" or msg.topic is None:
            return
        self.journal.write(now, msg, self.lean)
        first = self._FIRST.get(msg.topic)
        if first is not None:
            getattr(self, first)(msg)
            return
        if msg.topic in PROFILE_TOPICS:
            self.on_character_topic(msg)
        if msg.type == "topicReset":
            return
        payload = msg.payload
        if msg.topic == "dune.session" and isinstance(payload, dict):
            self._session_world = (payload.get("worldId"), payload.get("coriolisWorldSeed"))
            self.world_name = payload.get("worldName") or payload.get("worldId")
            self.activity = payload.get("activity")
            self.character_state = payload.get("characterState")
            seed = payload.get("mapScanSeed")
            self.map_scan_seed = seed if isinstance(seed, int) and not isinstance(seed, bool) else None
            self._enter_world(self._session_world)
            if self.vehicles.on_session(self.activity, self.character_state):
                self._vehicle_changed()
        elif msg.topic == "dune.player":
            self._on_pose(payload, now)
        elif msg.topic == "dune.rawStats":
            if self.vehicles.on_stats_names(parse_stat_names(payload)):
                self._vehicle_changed()
        elif msg.topic == "dune.status" and isinstance(payload, dict) and isinstance(payload.get("hasGameSession"), bool):
            self._on_status(payload["hasGameSession"])
        elif msg.topic == "dune.map.actors" and isinstance(payload, dict):
            self._on_actors(payload, now)

    @property
    def identity_ready(self) -> bool:
        """Gate for anything shared with the guild: the server partitions by battlegroup and shows members by name."""
        return bool(self.identity.get("characterName") and self.identity.get("battlegroupId"))

    def _on_tracked_session(self, msg: BusMessage) -> None:
        """Identity is one unit. Early deltas lack the name, the id or the battlegroup, so the fields
        of one character fill in over several deltas; a delta naming another character (another id,
        or another name without an id) starts a new identity from its own fields only, so two
        characters' fields never mix. A topicReset ends the session; an invalid value is ignored.
        A world change is no end: the companion sends this topic once per game start, and every
        loading screen reports no world."""
        if msg.type == "topicReset":
            self._end_identity()
            return
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        name, char_id, group = payload.get("characterName"), payload.get("characterId"), payload.get("battlegroupId")
        fresh = {}
        if isinstance(name, str) and 0 < len(name) <= IDENTITY_TEXT_MAX:
            fresh["characterName"] = name
        if isinstance(char_id, int) and not isinstance(char_id, bool):
            fresh["characterId"] = char_id
        if isinstance(group, str) and 0 < len(group) <= IDENTITY_TEXT_MAX:
            fresh["battlegroupId"] = group
        known = self.identity
        if "characterId" in fresh:
            other = known.get("characterId", fresh["characterId"]) != fresh["characterId"]
        else:
            other = "characterName" in fresh and known.get("characterName", fresh["characterName"]) != fresh["characterName"]
        if other:
            self._end_identity()
        self.identity = {**self.identity, **fresh}

    def _on_status(self, in_game: bool) -> None:
        self.in_game = in_game
        if not in_game:  # the game closed: the next character may be another one
            self._end_identity()

    def _on_destination(self, msg: BusMessage) -> None:
        """Where the player's own destination is, for a pin; a reset or no destination clears it."""
        p = msg.payload if msg.type != "topicReset" and isinstance(msg.payload, dict) else {}
        placed = p.get("hasDestination") is True and all(isinstance(p.get(k), (int, float)) and not isinstance(p.get(k), bool)
                                                        and math.isfinite(p[k]) for k in ("x", "y", "z"))
        self.native_dest = p if placed else None

    def _end_identity(self) -> None:
        """Forget who plays: guild sync says goodbye for that character and drops what it built for it."""
        if not self.identity:
            return
        self.identity_ended()
        self.character_ended()
        self.identity = {}

    def _on_actors(self, payload: dict, now: float) -> None:
        tagged = (payload.get("worldId"), payload.get("coriolisWorldSeed"))
        if tagged[0] is not None:
            if self._session_world is not None and self._session_world[0] is not None and tagged != self._session_world:
                return  # leftover list from the previous map (the companion's map ignores these too)
            self._enter_world(tagged)
        actors = self._actor_parser.parse(payload)
        changes = self.store.apply_snapshot(actors, now)
        # Every newly seen actor's elevation is terrain evidence, including ore on rock.
        fresh = [c.tracked.actor for c in changes if c.kind == "appeared"]
        if fresh:
            self.grid.add_height_samples((a.x, a.y, a.z) for a in fresh if a.z)
        pose_now = self.store.pose
        self._track_mining(changes, len(actors), now)
        for c in changes:
            a = c.tracked.actor
            if c.is_spice_blow:
                self._spice_blow(a, now)
            if ((a.marker in CAGE_MARKERS or "LootContainer" in (a.actor_type or ""))
                    and c.kind == "status_changed" and c.before is Status.LIVE
                    and pose_now is not None and pose_now.dist2d(a.x, a.y) < LOOT_RADIUS_CM):
                self.looted[a.key] = now  # vanished while you stood next to it: you emptied it
            if a.is_spice_field and c.kind in ("status_changed", "fill_changed"):
                # small and rare: always journaled (even lean) to learn what a bubbling site looks like
                self.journal.write(now, BusMessage("event", "detector.spiceState", {
                    "id": a.key, "x": a.x, "y": a.y, "z": a.z, "spawned": a.spawned, "fill": a.fill_rate,
                    "status": c.tracked.status.value, "before": c.before.value if c.before else None, "kind": c.kind,
                }))
        for ship in self.ships.observe(actors, pose_now, self.looted, now):
            self._downed_ship(ship, now)
        if changes:
            self._dirty = True
        # stale confirmation needs a few snapshots seconds apart; no need to run it on every one
        if now - self._observed_at >= 2.0:
            self._observed_at = now
            if self.scan.observe(self.store.pose, actors, now, self._absence_radius_cm):
                self._changed()

    def _absence_radius_cm(self, marker: str | None) -> float | None:
        """Inside this radius the game would show an actor of `marker`. Downed ships are too rare to
        learn one: the hull loads from 500-900 m away, so a reported ship not shown within 400 m is gone."""
        return GONE_CM if marker == SHIP_MARKER else self.store.absence_radius_cm(marker)

    def _track_mining(self, changes: list, actor_count: int, now: float) -> None:
        """Nodes you mined (they vanish right next to you) and mined nodes the game shows again."""
        pose = self.store.pose
        vanished = [c for c in changes if c.kind == "status_changed" and c.before is Status.LIVE
                    and c.tracked.status in (Status.MISSING_IN_RANGE, Status.OUT_OF_RANGE)]
        # one or two nodes vanishing right next to you, with the rest of the list in place: you took
        # them. Many at once, or the list shrinking, is the game unloading an area.
        if (pose is not None and 0 < len(vanished) <= MINED_MAX_PER_SNAPSHOT
                and self._last_actor_count - actor_count <= MINED_MAX_PER_SNAPSHOT):
            for c in vanished:
                a = c.tracked.actor
                if a.marker in NODE_MARKERS and pose.dist2d(a.x, a.y) <= MINE_RADIUS_CM:
                    self._record_mined(a, now)
        self._last_actor_count = actor_count
        if self.mined_nodes.items:
            live = [c.tracked.actor for c in changes
                    if c.tracked.status is Status.LIVE and c.kind in ("appeared", "status_changed")]
            for marker, took in self.mined_nodes.seen_again(live, now):
                self.learning.learn("respawn", marker, took)  # at most this long: it was back when seen

    def _on_pose(self, payload: object, now: float) -> None:
        pose = parse_pose(payload)
        if pose is None or (pose.x == 0 and pose.y == 0 and pose.z == 0):
            return  # loading screens / menus report the origin
        prev, prev_t = self.store.pose, self._last_pose_t
        self.store.set_pose(pose)
        self._last_pose_t = now
        ground = self.grid.ground_below(pose.x, pose.y) if self.vehicles.driving else None
        if self.vehicles.on_pose(now, pose, ground):
            self._vehicle_changed()
        mode = self.vehicles.current
        if mode == "ornithopter":
            return  # flying altitude says nothing about the ground
        if mode == "foot":
            self.grid.add_height_sample(pose.x, pose.y, pose.z)
            return
        if prev is None or prev_t is None or now <= prev_t or not self.vehicles.confident:
            return  # never attribute tracks to a vehicle we are only guessing
        horiz = prev.dist2d(pose.x, pose.y)
        speed = horiz / (now - prev_t)
        v = VEHICLES[mode]
        # Only count samples that look like actual driving on the ground.
        if 150 <= speed <= v.speed_cms * 1.6 and abs(pose.z - prev.z) <= max(horiz, 1.0) * 0.7:
            self.grid.add_driven(pose.x, pose.y, pose.z, mode)

    def _spice_blow(self, actor, now: float) -> None:
        self.blows[actor.key] = {"x": actor.x, "y": actor.y, "z": actor.z, "size": actor.spice_size, "t": now}
        save_blows(blows_path(self.data_dir, self.store.world), self.blows)
        if actor.marker in NEVER_TARGET:
            return  # small fields: remembered, but no announcement, no re-ranking and not shared
        self.peer_note("blow", actor.marker, actor.x, actor.y, actor.z, now, size=actor.spice_size)
        pose = self.store.pose
        dist = f"{pose.dist2d(actor.x, actor.y) / 100_000:.1f} km away" if pose else "nearby"
        text = f"Spice blow! {actor.spice_size} field {dist}"
        self.events.append((now, text))
        log.info("%s at (%.0f, %.0f)", text, actor.x, actor.y)
        self.journal.write(now, BusMessage("event", "detector.spiceBlow", {"x": actor.x, "y": actor.y, "z": actor.z, "size": actor.spice_size}))
        self._scored_at = 0.0  # re-rank now: a fresh field may be the new best
        self._changed()

    def _downed_ship(self, ship, now: float) -> None:
        pose = self.store.pose
        dist = f"{pose.dist2d(ship.x, ship.y) / 100_000:.1f} km away" if pose else "nearby"
        self.ship_alert = (now, f"Downed ship! {dist}")
        self.peer_note("ship", SHIP_MARKER, ship.x, ship.y, ship.z, now)
        log.info("downed ship %s at (%.0f, %.0f)", dist, ship.x, ship.y)
        self.journal.write(now, BusMessage("event", "detector.downedShip", {"x": ship.x, "y": ship.y, "z": ship.z}))
        self._scored_at = 0.0  # re-rank now: it may be the new target
        self._changed()

    def _record_mined(self, a, now: float) -> None:
        last = self._last_mine.get(a.marker)
        if last and 0 < now - last[0] <= PACE_GAP_MAX_S and math.hypot(a.x - last[1], a.y - last[2]) <= PACE_RANGE_CM:
            self.learning.learn("pace", a.marker, now - last[0])  # your own speed from node to node
        self._last_mine[a.marker] = (now, a.x, a.y)
        self.mined_nodes.record(a.key, a.marker, a.x, a.y, a.z, now)
        self.peer_note("mined", a.marker, a.x, a.y, a.z, now)
        hidden = self.scan.mark_mined(a.marker, a.x, a.y, now, self.respawn_for(a.marker))
        near = self.mined_nodes.near(a.x, a.y, {a.marker})
        log.info("taken near you: %s (%s in this area)%s", a.marker, near, "; its map marker is hidden until it respawns" if hidden else "")
        self._scored_at = 0.0  # re-rank now: this area just got poorer
        self._changed()

    def _vehicle_changed(self) -> None:
        log.info("vehicle: %s (planning for %s)", self.vehicles.current, self.plan_vehicle)
        self._changed()
