# Routes: farm routes from the server's routes dataset (checked on load), the run you are on, and its done-times file.
"""Farm route mode: a route is ordered sites, a site is ordered stops (chests, corpses, consoles).

The routes are research, so they do not ship with the package: the guild server holds them as the
`routes` dataset (scripts/build_routes.py builds it; every position is copied from the item data or
the companion's map markers, never made up). Without it there are no routes. Parsing checks each
row: a bad one is skipped with a warning, never a crash.

A run never reshuffles: the site order is fixed when the route starts, and a site's stop order (the
sourced order, else nearest-next from where you enter the site) is fixed once, when you get there.
Stops you emptied are kept per world in data\\route-done-<map>-<seed>.json (this module is its only
writer), so a restart does not send you back to a chest you just looted.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .catalog import LOOTED_HIDE_S
from .fsutil import read_json, try_write_json

log = logging.getLogger(__name__)

SITE_CM = 15_000.0  # within 150 m of a stop you are at the site: its order is fixed from there
ARRIVE_CM = 600.0  # within 6 m you came to a stop
SAME_CM = 200.0  # a container sits exactly on its database position
AWAY_END_S = 600.0  # the run ends after 10 minutes outside the route's worlds
NOTICE_S = 120.0  # "Sheol done: next ..." stays on the card this long
UNKNOWN_RESET_S = LOOTED_HIDE_S  # no source gives a reset: the detector's own one-hour assumption
KEEP_DONE_S = 7 * 24 * 3600.0


@dataclass(frozen=True, slots=True)
class Stop:
    x: float
    y: float
    z: float
    kind: str  # chest | corpse | console | place
    name: str
    order: int | None  # sourced position in the site, None: nearest-next

    @property
    def key(self) -> str:
        return f"{round(self.x)}:{round(self.y)}"


@dataclass(frozen=True, slots=True)
class Site:
    name: str
    world: str  # map id: survival_1, deepdesert_1, an instance id
    center: tuple[float, float, float] | None
    entrance: tuple[float, float, float] | None
    stops: tuple[Stop, ...]
    from_items: bool  # Deep Desert weekly layout: stops come from this week's item data
    mission: bool = False  # an instance entered only with its Landsraad mission


@dataclass(frozen=True, slots=True)
class Route:
    id: str
    name: str
    items: tuple[str, ...]
    sites: tuple[Site, ...]
    reset_s: float | None
    confidence: str
    note: str  # "data only" / "order: nearest" / ""
    sources: tuple[str, ...]

    @property
    def worlds(self) -> set[str]:
        """Maps the run may be on. Instances (testing stations, overland wrecks) are entered from Hagga Basin."""
        own = {s.world for s in self.sites}
        return own | ({"survival_1"} if any(not w.startswith(("survival", "deepdesert")) for w in own) else set())


def _num(v: object) -> float | None:
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    return float(v) if ok else None


def _point(d: object) -> tuple[float, float, float] | None:
    if not isinstance(d, dict):
        return None
    x, y = _num(d.get("x")), _num(d.get("y"))
    return None if x is None or y is None else (x, y, _num(d.get("z")) or 0.0)


def _stop(d: object) -> Stop | None:
    p = _point(d)
    if p is None or not isinstance(d.get("kind"), str):
        return None
    order = d.get("order")
    return Stop(*p, d["kind"], str(d.get("name") or ""), order if type(order) is int else None)


def _site(d: object) -> Site | None:
    if not isinstance(d, dict) or not all(isinstance(d.get(k), str) for k in ("name", "world")):
        return None
    rows = d.get("stops") if isinstance(d.get("stops"), list) else []
    stops = tuple(s for s in map(_stop, rows) if s)
    if len(stops) < len(rows):
        log.warning("routes dataset: %s bad stop rows skipped in %s", len(rows) - len(stops), d["name"])
    from_items = d.get("fromItems") is True
    if not (stops or from_items):
        return None
    return Site(d["name"], d["world"], _point(d.get("center")), _point(d.get("entrance")), stops, from_items,
                d.get("mission") is True)


def parse_route(d: object) -> Route | None:
    if not isinstance(d, dict) or not isinstance(d.get("id"), str) or not isinstance(d.get("sites"), list):
        return None
    sites = tuple(s for s in map(_site, d["sites"]) if s)
    items = d.get("items") if isinstance(d.get("items"), list) else []
    if not sites:
        return None
    return Route(d["id"], str(d.get("name") or d["id"]), tuple(i for i in items if isinstance(i, str)), sites,
                 _num(d.get("resetS")), str(d.get("confidence") or "unknown"), str(d.get("note") or ""),
                 tuple(s for s in d.get("sources") or [] if isinstance(s, str)))


def parse_routes(data: object) -> list[Route]:
    """The routes of a dataset `{"routes": [...]}`; [] for any other shape."""
    rows = data.get("routes") if isinstance(data, dict) else None
    out = []
    for row in rows if isinstance(rows, list) else []:
        route = parse_route(row)
        if route is None:
            log.warning("routes dataset: skipped a bad route row %.80s", row)
        else:
            out.append(route)
    return out


class DoneTimes:
    """When you emptied each stop, per world (map + Coriolis seed: a Deep Desert layout is its own world)."""

    def __init__(self, data_dir: Path | None) -> None:
        self._dir = data_dir
        self.world: tuple | None = None
        self.items: dict[str, float] = {}

    def _path(self) -> Path | None:
        if self._dir is None or not self.world or self.world[0] is None:
            return None
        return self._dir / f"route-done-{self.world[0]}-{self.world[1]}.json"

    def set_world(self, world: tuple | None) -> None:
        if world != self.world:
            self.world = world
            loaded = read_json(self._path(), dict, {})
            self.items = {k: float(t) for k, t in loaded.items() if _num(t) is not None}

    def mark(self, key: str, now: float) -> None:
        self.items = {k: t for k, t in self.items.items() if now - t < KEEP_DONE_S} | {key: now}
        try_write_json(self._path(), self.items, "route done times")

    def done_since(self, key: str, since: float) -> bool:
        return self.items.get(key, -math.inf) >= since


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def nearest_next(points: list[tuple[float, float]], start: tuple[float, float]) -> list[int]:
    left, out, at = list(range(len(points))), [], start
    while left:
        i = min(left, key=lambda j: _dist(points[j], at))
        left.remove(i)
        out.append(i)
        at = points[i]
    return out


@dataclass(slots=True)
class FarmRun:
    """The route you are on. `resolve(site)` gives a site's stops (fixed, or this week's layout)."""

    route: Route
    resolve: Callable[[Site], tuple[Stop, ...]]
    done_times: DoneTimes
    site_order: list[int]
    pos: int = 0
    stops: tuple[Stop, ...] = ()
    order: list[int] | None = None  # frozen stop order of the current site
    done: set[int] = field(default_factory=set)
    visited: set[int] = field(default_factory=set)
    emptied: set[int] = field(default_factory=set)
    repeat: bool = False
    site_done_at: dict[int, float] = field(default_factory=dict)
    wait_until: float | None = None
    notice: tuple[float, str] | None = None
    world_id: str | None = None
    away_since: float | None = None

    @classmethod
    def start(cls, route: Route, resolve, done_times: DoneTimes, world_id: str | None, pose) -> FarmRun:
        here = [i for i, s in enumerate(route.sites) if s.world == world_id and s.center]
        if pose is not None and here:
            here = [here[i] for i in nearest_next([route.sites[i].center[:2] for i in here], (pose.x, pose.y))]
        order = here + [i for i in range(len(route.sites)) if i not in here]
        return cls(route, resolve, done_times, order, world_id=world_id)

    @property
    def site(self) -> Site:
        return self.route.sites[self.site_order[self.pos]]

    @property
    def reset_s(self) -> float:
        return self.route.reset_s or UNKNOWN_RESET_S

    @property
    def phase(self) -> str:
        if self.wait_until is not None:
            return "wait"
        if self.world_id != self.site.world:
            return "travel"
        return "site" if self.order is not None else "approach"

    @property
    def next_index(self) -> int | None:
        return next((i for i in self.order or () if i not in self.done), None)

    def signature(self) -> tuple:
        return self.phase, self.pos, self.next_index, len(self.done), self.notice

    def update(self, world_id: str | None, pose, now: float) -> bool:
        before = self.signature()
        self.world_id = world_id
        self.away_since = None if world_id in self.route.worlds else self.away_since or now
        if self.wait_until is not None:
            if now >= self.wait_until:
                self.wait_until = None
                self._enter_site(keep_order=self.repeat)
        elif world_id == self.site.world and pose is not None:
            self._walk((pose.x, pose.y), now)
        return self.signature() != before

    def _walk(self, at: tuple[float, float], now: float) -> None:
        if not self.stops:
            self.stops = self.resolve(self.site)
        dists = [_dist((s.x, s.y), at) for s in self.stops]
        if self.order is None and dists and min(dists) <= SITE_CM:
            self._freeze(at, now)
        elif self.order is not None:
            self.visited |= {i for i, d in enumerate(dists) if d <= ARRIVE_CM}
            self._settle(now)

    def _freeze(self, at: tuple[float, float], now: float) -> None:
        sourced = sorted((s.order, i) for i, s in enumerate(self.stops) if s.order is not None)
        rest = [i for i in range(len(self.stops)) if self.stops[i].order is None]
        start = (self.stops[sourced[-1][1]].x, self.stops[sourced[-1][1]].y) if sourced else at
        self.order = [i for _, i in sourced] + [rest[j] for j in nearest_next([(self.stops[i].x, self.stops[i].y) for i in rest], start)]
        since = now - self.reset_s
        self.done = {i for i, s in enumerate(self.stops) if self.done_times.done_since(s.key, since)}
        self._settle(now)

    def looted(self, x: float, y: float, now: float) -> bool:
        """The companion shows the container at (x, y) emptied: its stop counts once you came within 6 m."""
        hit = {i for i, s in enumerate(self.stops) if _dist((s.x, s.y), (x, y)) <= SAME_CM} - self.emptied
        self.emptied |= hit
        before = len(self.done)
        if hit and self.order is not None:
            self._settle(now)
        return len(self.done) != before

    def skip(self, now: float) -> bool:
        nxt = self.next_index
        if nxt is None:
            return False
        self.done.add(nxt)
        self._settle(now)
        return True

    def toggle_repeat(self) -> None:
        self.repeat = not self.repeat

    def _settle(self, now: float) -> None:
        for i in (self.visited & self.emptied) - self.done:
            self.done_times.mark(self.stops[i].key, now)
            self.done.add(i)
        if self.order is not None and self.next_index is None:
            self._finish_site(now)

    def _finish_site(self, now: float) -> None:
        done_site = self.site
        self.site_done_at[self.site_order[self.pos]] = now
        if self.repeat:
            self.wait_until = now + self.reset_s
            self.notice = (now, f"{done_site.name} done: repeat here in {round(self.reset_s / 60)} min")
            return
        ready = [p for p in range(len(self.site_order)) if now - self.site_done_at.get(self.site_order[p], -math.inf) >= self.reset_s]
        later = [p for p in ready if p > self.pos]
        self.pos = (later or ready or [min(range(len(self.site_order)), key=lambda p: self.site_done_at[self.site_order[p]])])[0]
        if not ready:  # every site was done within its reset: wait for the first one
            self.wait_until = self.site_done_at[self.site_order[self.pos]] + self.reset_s
        nxt = self.site
        far = f" ({_dist(done_site.center[:2], nxt.center[:2]) / 100_000:.1f} km)" if done_site.center and nxt.center and nxt.world == done_site.world else ""
        reset = f"repeat here in {round(self.reset_s / 60)} min" if self.route.reset_s else "reset time unknown"
        self.notice = (now, f"{done_site.name} done: next {nxt.name}{far} / {reset}")
        if self.wait_until is None:
            self._enter_site(keep_order=False)

    def _enter_site(self, keep_order: bool) -> None:
        if not keep_order:
            self.order, self.stops = None, ()
        self.done, self.visited, self.emptied = set(), set(), set()
