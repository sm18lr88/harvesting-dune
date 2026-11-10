# Model: parses companion payloads into actors, poses and bus messages.
"""Payload shapes published by the gaming.tools companion bridge.

Field names mirror the companion's own renderer (app.gaming.tools map chunk), which
consumes the `dune.map.actors`, `dune.player` and `dune.session` topics:

    dune.map.actors -> {gated, worldId?, coriolisWorldSeed?, actors: [
        {id?, actorType, markerType?, resourceItemId?, spriteKey?, x, y, z,
         fillRate?, spawned?, reportable?, hasLoot?}]}
    dune.player     -> {x, y, z, ...}
    dune.session    -> {worldId?, coriolisWorldSeed?, ...}

Coordinates are Unreal world units (centimetres).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .catalog import SPICE_SIZE, marker_of
from .loot import cage_kind


@dataclass(frozen=True, slots=True)
class Actor:
    key: str
    actor_type: str | None
    marker_type: str | None
    resource_item_id: str | None
    sprite_key: str | None
    marker: str | None  # catalog marker (lowercase) or None for non-resources
    x: float
    y: float
    z: float
    fill_rate: float | None
    spawned: bool | None
    has_loot: bool | None = None  # containers: False once emptied (it stays listed)

    @property
    def spice_size(self) -> str | None:
        return SPICE_SIZE.get(self.marker or "")

    @property
    def is_spice_field(self) -> bool:
        return self.spice_size is not None


@dataclass(frozen=True, slots=True)
class BusMessage:
    """One companion bus message: {type, topic, payload}."""

    type: str
    topic: str | None
    payload: Any


def parse_stat_names(payload: object) -> list[str]:
    """Group and entry names from a dune.rawStats payload."""
    names: list[str] = []
    groups = payload.get("groups") if isinstance(payload, dict) else None
    for g in groups if isinstance(groups, list) else []:
        if not isinstance(g, dict):
            continue
        for key in ("name", "title", "id"):
            if isinstance(g.get(key), str):
                names.append(g[key])
        entries = g.get("entries")
        for e in entries if isinstance(entries, list) else []:
            if isinstance(e, dict) and isinstance(e.get("name"), str):
                names.append(e["name"])
    return names


@dataclass(frozen=True, slots=True)
class Pose:
    x: float
    y: float
    z: float

    def dist2d(self, x: float, y: float) -> float:
        return math.hypot(self.x - x, self.y - y)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def actor_key(raw: dict[str, Any]) -> str:
    # Same fallback identity the companion renderer uses when `id` is absent.
    if raw.get("id") is not None:
        return str(raw["id"])
    return f"{raw.get('actorType')}:{raw.get('resourceItemId')}:{raw.get('markerType')}:{raw.get('x')}:{raw.get('y')}:{raw.get('z')}"


def parse_actor(raw: Any) -> Actor | None:
    if not isinstance(raw, dict):
        return None
    x, y, z = _num(raw.get("x")), _num(raw.get("y")), _num(raw.get("z"))
    if x is None or y is None:
        return None
    spawned, has_loot = raw.get("spawned"), raw.get("hasLoot")
    actor_type, marker_type, item, sprite = (
        v if isinstance(v, str) else None for v in (raw.get(k) for k in ("actorType", "markerType", "resourceItemId", "spriteKey")))
    return Actor(
        key=actor_key(raw),
        actor_type=actor_type,
        marker_type=marker_type,
        resource_item_id=item,
        sprite_key=sprite,
        marker=marker_of(marker_type, sprite, actor_type) or _cage_marker(actor_type),
        x=x,
        y=y,
        z=z if z is not None else 0.0,
        fill_rate=_num(raw.get("fillRate")),
        spawned=spawned if isinstance(spawned, bool) else None,
        has_loot=has_loot if isinstance(has_loot, bool) else None,
    )


def _cage_marker(actor_type: Any) -> str | None:
    kind = cage_kind(actor_type) if isinstance(actor_type, str) else None
    return {"Ultra Rare": "lootcontainerultrarare", "Rare": "lootcontainerrare"}.get(kind) if kind else None


def parse_actors(payload: Any) -> list[Actor]:
    if not isinstance(payload, dict):
        return []
    actors = payload.get("actors")
    if not isinstance(actors, list):
        return []
    return [a for a in (parse_actor(r) for r in actors) if a is not None]


class ActorParser:
    """parse_actors with reuse: actors whose fields did not change keep their Actor object.

    The companion republishes the full list (thousands of actors) about twice a second,
    and almost none of them change between updates.
    """

    def __init__(self) -> None:
        self._cache: dict[Any, tuple[tuple, Actor]] = {}

    def parse(self, payload: Any) -> list[Actor]:
        raw_list = payload.get("actors") if isinstance(payload, dict) else None
        if not isinstance(raw_list, list):
            return []
        cache, fresh, out = self._cache, {}, []
        for raw in raw_list:
            if not isinstance(raw, dict):
                continue
            rid = raw.get("id")
            if isinstance(rid, bool) or not isinstance(rid, (str, int)):
                rid = None  # only plain ids can key the cache
            sig = (raw.get("x"), raw.get("y"), raw.get("z"), raw.get("fillRate"), raw.get("spawned"), raw.get("markerType"), raw.get("spriteKey"),
                   raw.get("hasLoot"))
            hit = cache.get(rid) if rid is not None else None
            actor = hit[1] if hit is not None and hit[0] == sig else parse_actor(raw)
            if actor is None:
                continue
            if rid is not None:
                fresh[rid] = (sig, actor)
            out.append(actor)
        self._cache = fresh
        return out


def parse_pose(payload: Any) -> Pose | None:
    if not isinstance(payload, dict):
        return None
    x, y, z = _num(payload.get("x")), _num(payload.get("y")), _num(payload.get("z"))
    if x is None or y is None:
        return None
    return Pose(x, y, z if z is not None else 0.0)
