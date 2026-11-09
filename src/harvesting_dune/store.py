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
        self.saved_ranges: dict[str | None, dict[str, float]] = {}  # world id -> marker -> cm (earlier sessions)

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
        """Returns True when the world changed and tracked items were cleared."""
        key = (world_id, seed)
        if self.world is not None and self.world != key:
            self.items.clear()
            self.pose = None  # a pose from the previous map is meaningless here
            self._primed = False
            self.world = key
            return True
        self.world = key
        return False

    def set_pose(self, pose: Pose) -> None:
        self.pose = pose

    def apply_snapshot(self, actors: list[Actor], now: float) -> list[Change]:
        changes: list[Change] = []
        seen: set[str] = set()
        primed, self._primed = self._primed, True
        for actor in actors:
            seen.add(actor.key)
            new_status = Status.OFFLINE if actor.spawned is False else Status.LIVE
            item = self.items.get(actor.key)
            if item is None:
                dist = self.pose.dist2d(actor.x, actor.y) if self.pose else None
                item = Tracked(actor, now, now, dist, new_status)
                if new_status is Status.LIVE:
                    item.last_live = now
                item.dormant_since_live = actor.spawned is False
                if actor.fill_rate is not None:
                    item.fill_history.append((now, actor.fill_rate))
                self.items[actor.key] = item
                if primed and dist is not None and actor.marker is not None:
                    samples = self._first_seen_distances.setdefault((self.world, actor.marker), [])
                    samples.append(dist)
                    if len(samples) > 400:
                        del samples[:200]
                changes.append(Change("appeared", item))
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

        radii = {m: r * 0.6 for m, r in self.learned_markers().items()}
        if self._fixed_absence_radius is not None:
            radii = None
        for key, item in self.items.items():
            if key in seen:
                continue
            radius = self._fixed_absence_radius if radii is None else radii.get(item.actor.marker)
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
            if t.status in (Status.LIVE, Status.OFFLINE) and now - t.last_seen < 5:
                continue  # currently loaded
            keep_for = remember_spice_s if t.actor.is_spice_field and t.last_live is not None else forget_after_s
            if now - t.last_seen > keep_for:
                drop.append(key)
        for key in drop:
            del self.items[key]
        return len(drop)

    def spice_fields(self) -> list[Tracked]:
        return [t for t in self.items.values() if t.actor.is_spice_field]
