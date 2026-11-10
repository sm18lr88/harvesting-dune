# Store: tracks actors across snapshots and learns how far away each type loads.
"""Observation store: turns full-state actor snapshots into tracked resources.

The companion publishes `dune.map.actors` as full state on every change (both
`snapshot` and `delta` replace the whole payload in its own client), so each update is
diffed against the previous one here.

An actor missing from an update is NOT assumed depleted. It only becomes
`missing_in_range` when the player is well inside the distance at which fields are
normally first observed; otherwise it is `out_of_range` (merely unloaded).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from enum import Enum

from .model import Actor, Pose

REMEMBER_SPICE_S = 45 * 60  # an active spice field stays on the map this long after it leaves range
# spice sites sit at fixed places; the game can report one again under a new actor id
SAME_SITE_CM = 1000.0


class Status(str, Enum):
    LIVE = "live"
    OFFLINE = "offline"  # companion reports spawned == False
    MISSING_IN_RANGE = "missing_in_range"  # should be visible but is not: likely harvested/despawned
    OUT_OF_RANGE = "out_of_range"  # not loaded by the client; last known state is stale


@dataclass(slots=True)
class Tracked:
    actor: Actor
    first_seen: float
    last_seen: float
    first_seen_distance: float | None
    status: Status = Status.LIVE
    fill_history: list[tuple[float, float]] = field(default_factory=list)
    last_live: float | None = None  # last time it was seen loaded AND spawned
    # seen not-spawned since it was last seen spawned; survives the companion's list flicker
    dormant_since_live: bool = False

    def fill_trend_per_min(self) -> float | None:
        """Change in fill rate per minute across recorded history (negative = being depleted)."""
        if len(self.fill_history) < 2:
            return None
        (t0, f0), (t1, f1) = self.fill_history[0], self.fill_history[-1]
        return None if t1 <= t0 else (f1 - f0) / ((t1 - t0) / 60.0)


@dataclass(slots=True)
class Change:
    kind: str  # appeared | fill_changed | status_changed
    tracked: Tracked
    before: Status | None = None

    @property
    def is_spice_blow(self) -> bool:
        """A spice site last seen empty is now active: a spice blow happened there."""
        return self.kind == "spice_blow"


class ObservationStore:
    def __init__(self, absence_radius_cm: float | None = None, min_range_samples: int = 5) -> None:
        self.items: dict[str, Tracked] = {}
        self.pose: Pose | None = None
        self.world: tuple[str | None, int | None] | None = None
        self._fixed_absence_radius = absence_radius_cm
        self._min_range_samples = min_range_samples
        # Detection range differs per map AND per actor class (spice fields load from far
        # away, plants only up close), so samples are kept per (world, marker).
        self._first_seen_distances: dict[tuple, list[float]] = {}
        self._primed = False  # first snapshot of a world only reflects the load-in position
        # every loading screen reports no world, so items survive a trip back to the same one
        self._last_real_world: tuple[str | None, int | None] | None = None
        self.saved_ranges: dict[str | None, dict[str, float]] = {}  # world id -> marker -> cm (earlier sessions)
        self.renamed: list[tuple[str, str]] = []  # (old id, new id) of actors the last snapshot reloaded
        self._unlisted: dict[tuple, str] | None = None  # place -> id of items the current snapshot omits

    # -- detection range -------------------------------------------------------------
    def detection_range_cm(self, marker: str | None = "spicefieldsmall") -> float | None:
        """Median distance at which actors of `marker` were first observed (learned)."""
        samples = self._first_seen_distances.get((self.world, marker), [])
        if len(samples) < self._min_range_samples:
            return None
        return statistics.median(samples)

    def absence_radius_cm(self, marker: str | None = "spicefieldsmall") -> float | None:
        """Inside this radius an actor of `marker` would certainly be loaded.

        Rare types (a map may show only one large spice field) borrow the smallest range learned
        for their family (spice field sizes share one), or a range saved from earlier sessions.
        """
        if self._fixed_absence_radius is not None:
            return self._fixed_absence_radius
        learned = self.detection_range_cm(marker)
        if learned is None and marker:
            family = marker[:10] if marker.startswith("spicefield") else None
            if family:
                siblings = [v for m, v in self.learned_markers().items() if m.startswith(family)]
                learned = min(siblings) if siblings else None
        if learned is None and marker:
            learned = self.saved_ranges.get(self.world[0] if self.world else None, {}).get(marker)
        return None if learned is None else learned * 0.6

    def learned_markers(self) -> dict[str, float]:
        out = {}
        for (world, marker), samples in self._first_seen_distances.items():
            if world == self.world and len(samples) >= self._min_range_samples:
                out[marker] = statistics.median(samples)
        return out

    # -- inputs ----------------------------------------------------------------------
    def set_world(self, world_id: str | None, seed: int | None) -> bool:
        """Returns True when the world changed; items are cleared only for a different real world."""
        key = (world_id, seed)
        changed = self.world is not None and self.world != key
        if changed:
            if world_id is not None and key != self._last_real_world:
                self.items.clear()
            self.pose = None  # a pose from the previous map is meaningless here
            self._primed = False
        self.world = key
        if world_id is not None:
            self._last_real_world = key
        return changed

    def set_pose(self, pose: Pose) -> None:
        self.pose = pose

    def _same_site(self, actor: Actor, listed: set[str]) -> Tracked | None:
        """A tracked spice site at this actor's place under another id that this snapshot no longer lists."""
        return next((t for k, t in self.items.items()
                     if k not in listed and t.actor.marker == actor.marker
                     and abs(t.actor.x - actor.x) <= SAME_SITE_CM and abs(t.actor.y - actor.y) <= SAME_SITE_CM), None)

    def _reloaded(self, actor: Actor, listed: set[str]) -> Tracked | None:
        """The tracked actor this one is, when the game loaded it again under a new id.

        Ids look like `A:B:n` and a reload changes only B; one spawner lists several nodes at the
        same place (`A:B:10`, `A:B:11`), so the place alone does not identify an actor.
        """
        place = _place(actor)
        if place is None:
            return None
        if self._unlisted is None:  # built once per snapshot, at the first unknown id
            self._unlisted = {p: k for k, t in self.items.items() if k not in listed and (p := _place(t.actor)) is not None}
        old_key = self._unlisted.pop(place, None)
        if old_key is None:
            return None
        item = self.items.pop(old_key)
        self.items[actor.key] = item
        self.renamed.append((old_key, actor.key))
        return item

    def _appeared(self, actor: Actor, now: float, listed: set[str], primed: bool) -> list[Change]:
        dist = self.pose.dist2d(actor.x, actor.y) if self.pose else None
        item = Tracked(actor, now, now, dist, Status.OFFLINE if actor.spawned is False else Status.LIVE)
        if item.status is Status.LIVE:
            item.last_live = now
        item.dormant_since_live = actor.spawned is False
        if actor.fill_rate is not None:
            item.fill_history.append((now, actor.fill_rate))
        changes = []
        old = self._same_site(actor, listed) if actor.is_spice_field else None
        if old is not None:
            # the same site under a new id: what it shows now replaces what we remembered
            del self.items[old.actor.key]
            if old.dormant_since_live and actor.spawned is True:
                changes.append(Change("spice_blow", item))
        self.items[actor.key] = item
        if primed and dist is not None and actor.marker is not None:
            samples = self._first_seen_distances.setdefault((self.world, actor.marker), [])
            samples.append(dist)
            if len(samples) > 400:
                del samples[:200]
        changes.append(Change("appeared", item))
        return changes

    def apply_snapshot(self, actors: list[Actor], now: float) -> list[Change]:
        changes: list[Change] = []
        seen: set[str] = set()
        listed = {a.key for a in actors}
        primed, self._primed = self._primed, True
        self.renamed, self._unlisted = [], None
        for actor in actors:
            seen.add(actor.key)
            new_status = Status.OFFLINE if actor.spawned is False else Status.LIVE
            item = self.items.get(actor.key) or self._reloaded(actor, listed)
            if item is None:
                changes.extend(self._appeared(actor, now, listed, primed))
                continue
            prev_fill = item.actor.fill_rate
            item.actor = actor
            item.last_seen = now
            if new_status is Status.LIVE:
                item.last_live = now
            if actor.spawned is False:
                item.dormant_since_live = True
            elif actor.spawned is True and item.dormant_since_live:
                item.dormant_since_live = False
                if actor.is_spice_field:
                    # the companion's list flickers (sites drop out and return), so the change
                    # is judged on what the site reported, not on consecutive snapshots
                    changes.append(Change("spice_blow", item))
            if actor.fill_rate is not None and actor.fill_rate != prev_fill:
                item.fill_history.append((now, actor.fill_rate))
                if len(item.fill_history) > 64:
                    del item.fill_history[1:33]  # keep the first sample for the long-run trend
                changes.append(Change("fill_changed", item))
            if item.status != new_status:
                before, item.status = item.status, new_status
                changes.append(Change("status_changed", item, before))

        radii: dict[str | None, float | None] = {}
        for key, item in self.items.items():
            if key in seen:
                continue
            marker = item.actor.marker
            if marker not in radii:
                radii[marker] = self.absence_radius_cm(marker)
            radius = radii[marker]
            inside = (
                radius is not None
                and self.pose is not None
                and self.pose.dist2d(item.actor.x, item.actor.y) < radius
            )
            status = Status.MISSING_IN_RANGE if inside else Status.OUT_OF_RANGE
            if item.status == Status.MISSING_IN_RANGE and not inside:
                continue  # keep the stronger evidence once collected
            if item.status != status:
                before, item.status = item.status, status
                changes.append(Change("status_changed", item, before))
        return changes

    def prune(self, now: float, forget_after_s: float, remember_spice_s: float) -> int:
        """Forget actors not loaded for a long time. Active spice fields are remembered longer."""
        drop = []
        for key, t in self.items.items():
            # Actors in the latest list are still loaded: the companion can stay quiet for over
            # 20 min while the player stands still. The longer bound still clears a dead feed.
            loaded = t.status in (Status.LIVE, Status.OFFLINE)
            active_spice = t.actor.is_spice_field and t.last_live is not None
            keep_for = remember_spice_s if loaded or active_spice else forget_after_s
            if now - t.last_seen > keep_for:
                drop.append(key)
        for key in drop:
            del self.items[key]
        return len(drop)

    def spice_fields(self) -> list[Tracked]:
        return [t for t in self.items.values() if t.actor.is_spice_field]


def _place(actor: Actor) -> tuple | None:
    """What stays the same when the game reloads a non-spice actor (spice sites: `_same_site`)."""
    parts = actor.key.split(":")
    if len(parts) != 3 or actor.is_spice_field:
        return None
    return actor.marker, actor.actor_type, actor.x, actor.y, parts[0], parts[2]
