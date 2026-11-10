# Ranking: candidate nodes, the ranking job (routing off the event loop), the hotspot and zones.
"""Recommendation for the detector (a mixin of app.Detector; it shares the detector's state).

Candidates are copied from live state on the event loop, routed in a worker thread
(areas.recommend), then applied on the event loop again.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time

from .areas import AREA_RADIUS_CM, MAX_REPORTED, PATCH_CM, Area, Node, _clusters, recommend
from .catalog import LOOTED_HIDE_S, NEVER_TARGET, SPICE_SIZE
from .corrections import MATCH_CM
from .mined import MinedNodes
from .ships import SAME_SHIP_CM, SHIP_MARKER
from .store import REMEMBER_SPICE_S, Status
from .terrain import CELL_CM, VEHICLES
from .zones import (
    DEFAULT_FIT,
    ROWS,
    cell_name,
    is_deep_desert,
    is_pvp,
    row_index,
    row_of,
    yield_multiplier,
)

log = logging.getLogger(__name__)

SWITCH_MARGIN = 1.15  # a new best area must beat the current one by 15% to replace it
ALT_LIMIT = 4  # thin alternative lines
ROUTED_AREAS = 1 + 2 * ALT_LIMIT  # areas ranked exactly: the target and enough to pick alternatives from


# reasons that say only "not routed this time", never "not there" or "not worth it"
UNROUTED = ("no practical", "cannot beat", "not routed")


ITEM_MATCH_CM = 200.0  # the crawl rounds positions, and some containers stand up to 2 m off their database place
ITEM_GONE_CM = 6000.0  # within 60 m a container would be loaded: none there means none


def _place_index(points: list[tuple[float, float]]) -> dict[tuple[int, int], list[tuple[float, float]]]:
    index: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for x, y in points:
        index.setdefault((math.floor(x / ITEM_MATCH_CM), math.floor(y / ITEM_MATCH_CM)), []).append((x, y))
    return index


def _has_near(index: dict, x: float, y: float) -> bool:
    """By distance, not by equal cells: a container a centimetre from its place can sit across a cell edge."""
    bx, by = math.floor(x / ITEM_MATCH_CM), math.floor(y / ITEM_MATCH_CM)
    return any(math.hypot(px - x, py - y) <= ITEM_MATCH_CM
               for i in (-1, 0, 1) for j in (-1, 0, 1) for px, py in index.get((bx + i, by + j), ()))


def _you_mined(mined: MinedNodes, marker: str, x: float, y: float, key: str | None = None) -> bool:
    """By its actor key, or by place for a node without one (a scan row) or drawn under a new key."""
    return bool(mined.items) and (key in mined.items or mined.is_mined(marker, x, y))

def tour_order(areas: list[Area], x: float, y: float) -> list[Area]:
    """Nearest first, then always the nearest one left from there: one efficient trip."""
    left, out = list(areas), []
    while left:
        nxt = min(left, key=lambda a: math.hypot(a.x - x, a.y - y))
        left.remove(nxt)
        out.append(nxt)
        x, y = nxt.x, nxt.y
    return out


def _near_stop(a, b) -> bool:
    return math.hypot(a.x - b.x, a.y - b.y) < AREA_RADIUS_CM


def _rescued_stop(areas: list, prev):
    """The stop we were heading to is still there, only not routed this time: the ranking routes the most
    valuable few, a tour wants the nearest (live: a reported ship 2.8 km away left the routed set and the
    line jumped to one 5.7 km away). Kept with its last estimate; a stop gone or emptied is not rescued."""
    lost = next((a for a in areas if not a.feasible and a.reasons and _near_stop(a, prev)
                 and all(r.startswith(UNROUTED) for r in a.reasons)), None)
    if lost is not None:
        lost.feasible, lost.utility, lost.expected, lost.trip_s = True, prev.utility, prev.expected, prev.trip_s
        lost.reasons = ["kept as your next stop"]
    return lost


def _keep_target(feasible: list[Area], areas: list[Area], prev: Area) -> None:
    """Put the area at the previous target first again unless a new one is clearly better (SWITCH_MARGIN)."""
    def near(a: Area) -> bool:
        return math.hypot(a.x - prev.x, a.y - prev.y) < AREA_RADIUS_CM

    again = next((a for a in feasible if near(a)), None)
    lost = None if again else next((a for a in areas if not a.feasible and near(a) and a.reasons
                                     and all(r.startswith(UNROUTED) for r in a.reasons)), None)
    if lost is not None:
        # only the route search from where you are now failed (in flight, behind a ridge), or it was not
        # routed this time (pruned among near-equal areas): the place you head to was reachable a moment
        # ago, so it stays the target (live: flour sand jumped 4 km)
        no_route = all(r.startswith("no practical") for r in lost.reasons)
        lost.feasible, lost.utility, lost.expected, lost.trip_s = True, prev.utility, prev.expected, prev.trip_s
        lost.reasons = ["no route found from here yet: kept as your target" if no_route else "kept as your target"]
        feasible.append(lost)
        again = lost
    if again is not None and again is not feasible[0] and feasible[0].utility < again.utility * SWITCH_MARGIN:
        feasible.remove(again)
        feasible.insert(0, again)  # keep the current target unless clearly beaten


class RankingMixin:
    # -- zones -------------------------------------------------------------------------------
    def _fit(self):
        return self.grid_fit() or DEFAULT_FIT

    def yield_at(self, x: float, y: float) -> float:
        """Resource yield multiplier at a map position (Deep Desert PvP bonus; 1 on other maps)."""
        world = self.store.world
        if not world or not is_deep_desert(world[0]):
            return 1.0
        return yield_multiplier(y, self.last_pve_row, self.pvp_yield, self._fit())

    def _pvp_only_here(self) -> bool:
        world = self.store.world
        return self.pvp_only and self.resource.pvp_only_applies and bool(world) and is_deep_desert(world[0])

    def lines_here(self) -> bool:
        """Deep Desert: lines start once you reach the chosen row (row A: from the entrance on)."""
        world, pose = self.store.world, self.store.pose
        if not world or not is_deep_desert(world[0]) or pose is None:
            return True
        return row_index(pose.y, self._fit()) >= ROWS.index(self.lines_from_row)

    def zone_of(self, y: float) -> str | None:
        world = self.store.world
        if not world or not is_deep_desert(world[0]):
            return None
        fit = self._fit()
        return f"{row_of(y, fit)} {'PvP' if is_pvp(y, self.last_pve_row, fit) else 'PvE'}"

    # -- candidates ----------------------------------------------------------------------------
    def nodes(self, markers: set[str] | None = None, now: float | None = None, counts=None) -> list[Node]:
        """Candidate nodes (live first, then scan rows not already seen live), optionally
        restricted to `markers` so only the selected resource is de-duplicated, and to the actor
        types `counts(actor_type)` accepts (a search variant such as buggy and ornithopter wrecks)."""
        live: list[Node] = []
        now = self.clock if now is None else now
        mined = self.mined_nodes
        for t in self.store.items.values():
            a = t.actor
            if a.marker is None or t.status in (Status.OFFLINE, Status.MISSING_IN_RANGE):
                continue
            if markers is not None and a.marker not in markers:
                continue
            if counts is not None and not counts(a.actor_type):
                continue
            if a.is_spice_field and (t.last_live is None or now - t.last_live > REMEMBER_SPICE_S):
                continue  # an empty spawn point, or a field not seen active for too long
            if self._emptied(a, now):
                continue
            if t.status is not Status.LIVE and _you_mined(mined, a.marker, a.x, a.y, a.key):
                continue  # you mined it; it comes back when it respawns
            fill = a.fill_rate
            if fill is not None and fill > 1:
                fill /= 100
            # a remembered-but-unloaded actor is as uncertain as a scan row
            live.append(Node(a.marker, a.x, a.y, a.z, "live" if t.status is Status.LIVE else "memory", fill))
        gone: set[str] = set()
        if markers is None or not markers.isdisjoint(SPICE_SIZE):
            live, gone = self._newest_spice(live, markers, now)
        guild = [n for n in live if n.source == "guild"]
        cell = MATCH_CM[True]  # bucket size >= every match radius
        # Every site the game has told us about, whatever its state: its live state supersedes a
        # scan row there (active -> already a candidate above; dormant/never active -> row is wrong).
        index: dict[tuple, list] = {}
        for t in self.store.items.values():
            a = t.actor
            if a.marker is not None and (markers is None or a.marker in markers):
                index.setdefault((a.marker, math.floor(a.x / cell), math.floor(a.y / cell)), []).append(a)
        out = list(live)
        for n in self.scan.nodes():
            if markers is not None and n.marker not in markers:
                continue
            if counts is not None and not counts((n.row_key or "").split(":", 1)[0]):
                continue
            if n.row_key in gone:
                continue  # a guildmate saw this site empty, later than anyone saw it active
            match = MATCH_CM[n.marker.startswith("spicefield")]
            bx, by = math.floor(n.x / cell), math.floor(n.y / cell)
            dup = any(math.hypot(m.x - n.x, m.y - n.y) <= match
                      for i in (-1, 0, 1) for j in (-1, 0, 1) for m in index.get((n.marker, bx + i, by + j), ()))
            dup = dup or any(g.marker == n.marker and math.hypot(g.x - n.x, g.y - n.y) <= match for g in guild)
            if not dup and not _you_mined(mined, n.marker, n.x, n.y):
                out.append(n)  # not already represented by a live observation, and not mined by you
        return out

    def _newest_spice(self, nodes: list[Node], markers: set[str] | None, now: float) -> tuple[list[Node], set[str]]:
        """At each spice site the newest report wins, whoever made it: our own companion, a guildmate
        (via guild sync) or the crowd scan database (no time: always the oldest). A site our companion
        loads now is ours alone. Returns the nodes with guild-active fields added and our older
        memories of guild-emptied sites removed, plus the scan row keys a guildmate reported empty."""
        match = MATCH_CM[True]
        newest = self._newest_reports(now)
        own = [t for t in self.store.items.values() if t.actor.marker in SPICE_SIZE]
        out, gone = list(nodes), set()
        for r in newest:
            if not r["active"]:
                gone.add(r["row_key"])
            here = [t for t in own if t.actor.marker == r["marker"] and math.hypot(t.actor.x - r["x"], t.actor.y - r["y"]) <= match]
            if any(t.status is not Status.OUT_OF_RANGE or t.last_seen >= r["t"] for t in here):
                continue  # our own companion saw this site later (or shows it now): our view stands
            out = [n for n in out if not (n.source == "memory" and n.marker == r["marker"]
                                          and math.hypot(n.x - r["x"], n.y - r["y"]) <= match)]
            if r["active"] and r["marker"] not in NEVER_TARGET and (markers is None or r["marker"] in markers):
                out.append(Node(r["marker"], r["x"], r["y"], r["z"], "guild", r["fill"]))
        return out, gone

    def _newest_reports(self, now: float) -> list[dict]:
        """The newest guild report of each spice site. A site whose newest report is an active one too
        old to count is left out: an older empty report there must not decide it."""
        match = MATCH_CM[True]
        newest: list[dict] = []
        for r in sorted(self.peer_spice_reports(), key=lambda r: -r["t"]):
            if not any(k["marker"] == r["marker"] and math.hypot(k["x"] - r["x"], k["y"] - r["y"]) <= match for k in newest):
                newest.append(r)
        return [r for r in newest if not (r["active"] and now - r["t"] > REMEMBER_SPICE_S)]

    def _emptied(self, a, now: float) -> bool:
        """You emptied it within the hour, or the companion lists it emptied, whoever opened it."""
        return a.has_loot is False or (a.key in self.looted and now - self.looted[a.key] < LOOTED_HIDE_S)

    def _remember_looted(self, now: float) -> list[tuple[float, float]]:
        """Where the containers you emptied this hour stand. The governor forgets an unloaded actor
        after 20 minutes, so the place is kept while the actor is still known."""
        kept = self.__dict__.setdefault("_looted_places", {})  # actor key -> (x, y); memory only
        for k, t in self.looted.items():
            tracked = self.store.items.get(k)
            if tracked is not None and now - t < LOOTED_HIDE_S:
                kept[k] = (tracked.actor.x, tracked.actor.y)
        for k in [k for k in kept if k not in self.looted or now - self.looted[k] >= LOOTED_HIDE_S]:
            del kept[k]  # the hour is over, or the game shows it full again
        return list(kept.values())

    def _item_places(self, key: str, map_id: str | None) -> dict[tuple, list]:
        """(x, y, source name) -> [z, expected amount of all its rows]: the Deep Desert lists one
        container twice for an item, and it is still one place to go to."""
        places: dict[tuple, list] = {}
        for row in self.items.sources(key, map_id):
            if not self.items.from_ships(row):  # no fixed place: it drops wherever a ship crashed
                place = places.setdefault((row[0], row[1], row[3]), [row[2], 0.0])
                place[1] += self.items.weight(row)
        return places

    def item_nodes(self, key: str, now: float) -> list[Node]:
        """Where a looted material drops on this map: containers and corpses at known places.

        A source with a container of the game on it is live; one emptied (by you within the hour, or
        listed emptied) is skipped; one close to you with nothing there is gone for now.
        """
        world, pose = self.store.world, self.store.pose
        live, emptied = [], self._remember_looted(now)
        for t in self.store.items.values():
            a = t.actor
            if "LootContainer" not in (a.actor_type or ""):
                continue
            if self._emptied(a, now):
                emptied.append((a.x, a.y))
            elif t.status is Status.LIVE:
                live.append((a.x, a.y))
        map_id = self.items.layout_for(world, live, self.map_files.get(world))
        live_at, emptied_at = _place_index(live), _place_index(emptied)
        out = []
        for (x, y, _), (z, weight) in self._item_places(key, map_id).items():
            if _has_near(emptied_at, x, y):
                continue
            here = _has_near(live_at, x, y)
            if not here and pose is not None and math.hypot(x - pose.x, y - pose.y) < ITEM_GONE_CM:
                continue  # you are right there and the game shows no container: taken or not spawned
            out.append(Node(key, x, y, z, "live" if here else "scan", None, None, weight))
        return out + self._ship_item_nodes(key, map_id)

    def _ship_item_nodes(self, key: str, map_id: str | None) -> list[Node]:
        """The downed ships you saw, each worth what one ship drops of this item."""
        world = self.store.world
        out = []
        for ship in self.ships.active(self.clock):  # ships age on the feed's clock
            weight = self.items.ship_weight(key, map_id, self._pvp_at(ship.y), world[0] if world else None)
            if weight > 0:
                out.append(Node(key, ship.x, ship.y, ship.z, "live" if ship.loaded else "memory", None, None, weight))
        return out

    def _pvp_at(self, y: float) -> bool | None:
        """True / False in Deep Desert PvP / PvE rows; None on other maps."""
        world = self.store.world
        if not world or not is_deep_desert(world[0]):
            return None
        return is_pvp(y, self.last_pve_row, self._fit())

    def reported_ship_nodes(self) -> list[Node]:
        """Downed ships other players reported (the gaming.tools scan database). The game shows a hull
        only within ~0.5-0.9 km of you, so these are the only way to know of one farther away. A ship
        you saw yourself (also one a worm took) is yours instead; a reported one you reach and do not
        find is confirmed stale like any scan row. No row key: the live-only rule keeps them."""
        known = list(self.ships.ships.values())
        return [Node(n.marker, n.x, n.y, n.z, "scan") for n in self.scan.nodes()
                if n.marker == SHIP_MARKER and not any(math.hypot(s.x - n.x, s.y - n.y) <= SAME_SHIP_CM for s in known)]

    def shipwreck_nodes(self) -> list[Node]:
        """The named shipwrecks of this map (fixed places from the companion's map data)."""
        return [Node("shipwreck", x, y, z, "memory") for _, x, y, z in self.wrecks_here()]

    def wrecks_here(self) -> list[tuple[str, float, float, float]]:
        return self.shipwrecks.get(self.store.world) or []

    # -- the ranking job -------------------------------------------------------------------------
    def _drop_ranking(self) -> None:
        """Everything one ranking showed: none of it may outlive the choice or world it was made for."""
        self.areas = []
        self.hotspot = None
        self._known_nodes = 0

    def _restart_ranking(self, clear: bool = True) -> None:
        """Rank again at once; `clear` first drops the old ranking (a new choice, a closed gate).
        Zeroing _scored_at also voids a ranking job in flight: it is that job's generation token."""
        if clear:
            self._drop_ranking()
        self._scored_at = 0.0
        self._changed()

    def _rescore_due(self, now: float) -> tuple | None:
        pose = self.store.pose
        if pose is None or 0 <= now - self._scored_at < self.min_rescore_s:  # a clock set back is due, never "too soon"
            return None
        # Re-rank after ~100 m of movement, a change, or a batch of new terrain evidence.
        step = 2 * CELL_CM
        key = (self.store.world, math.floor(pose.x / step), math.floor(pose.y / step), self.grid.revision // 50)
        if not self._dirty and key == self._scored_key:
            return None
        return key

    def _ranking_job(self, now: float):
        """Everything ranking reads from live state, copied on the event loop. The routing itself
        then runs in a worker thread without touching dicts the feed keeps changing."""
        world, pose = self.store.world, self.store.pose
        if pose is None:
            return None
        resource = self.resource
        wanted = {m.lower() for m in resource.marker_types} - NEVER_TARGET
        nodes = self._candidates(resource, wanted, now)
        if resource.live_only:
            nodes = [n for n in nodes if n.row_key is None]  # only what the game shows now
        if resource.confirmed_only and any(n.source == "live" or n.row_key is None for n in nodes):
            # confirmed-active fields exist: never send you to an unverified scan row instead
            nodes = [n for n in nodes if n.row_key is None]
        cur = self.vehicles.current
        travel = VEHICLES[cur] if cur in VEHICLES and VEHICLES[cur].flies and not self.vehicles.override else None
        dd = bool(world) and is_deep_desert(world[0])
        last_pve, pvp, fit = self.last_pve_row, self.pvp_yield, self._fit()
        yield_at = (lambda _x, y: yield_multiplier(y, last_pve, pvp, fit)) if dd else None
        pvp_only = dd and self.pvp_only and resource.pvp_only_applies
        if pvp_only:
            nodes = [n for n in nodes if is_pvp(n.y, last_pve, fit)]  # PvE rows: never a target or a line
        everywhere = nodes  # for the hotspot: the best place on the map, however far
        if self.max_km > 0:
            limit = self.max_km * 100_000
            nodes = [n for n in nodes if math.hypot(n.x - pose.x, n.y - pose.y) <= limit]
        args = (resource, nodes, pose, self.grid, VEHICLES[self.plan_vehicle], self.base)
        # targeting modes never lose a far big field; PvP-only never loses the PvP rows, which can
        # start beyond the ground planning range when you are in the south
        kw = {"top": 0, "travel": travel, "allow_far": resource.confirmed_only or pvp_only,
              "yield_at": yield_at, "need": ROUTED_AREAS, "node_s": self.node_seconds(wanted),
              "order": self._ranking_order(resource, pose), "cap": False}
        # every settings change resets _scored_at: with the resource it tells a result chosen for the old choice
        return world, nodes, args, kw, everywhere, self.resource_key, self._scored_at

    @staticmethod
    def _ranking_order(resource, pose):
        """A tour and a chance find rank by their own key: routing by value would leave out the nearest
        stop or the likeliest place whenever enough richer areas exist."""
        if resource.tour:
            px, py = pose.x, pose.y
            return lambda a: math.hypot(a.x - px, a.y - py)
        if resource.chance:
            return lambda a: -a.find_chance()
        return None

    def _candidates(self, resource, wanted: set[str], now: float) -> list[Node]:
        self._remember_looted(now)  # whatever is searched now: an item search later still knows the place
        if self.items.is_item(resource.key):
            return self.item_nodes(resource.key, now)
        if resource.key == "downed_ships":
            return self.ships.nodes(self.clock) + self.reported_ship_nodes()
        if resource.key == "shipwrecks":
            return self.shipwreck_nodes()
        return self.nodes(wanted, now, resource.counts if resource.actor_types else None)

    def compute(self, now: float) -> None:
        job = self._ranking_job(now)
        if job is None:
            return
        t0 = time.perf_counter()
        self._apply_ranking(job, self._rank(job), t0)

    def _rank(self, job) -> list[Area] | None:
        _, _, args, kw, _, _, _ = job
        try:
            return recommend(*args, **kw)
        except Exception:
            log.exception("recommendation failed; will retry on next change")
            self._dirty = True
            return None

    def _apply_ranking(self, job, areas: list[Area] | None, t0: float) -> None:
        world, nodes, _, _, everywhere, resource_key, scored_at = job
        if areas is None or world != self.store.world:
            return  # failed, or the map changed while ranking: drop results for the old map
        if (resource_key, scored_at) != (self.resource_key, self._scored_at) or not self.gate_open():
            return  # the choice changed or the gate closed while ranking: never show the old result
        self.hotspot = self._hotspot(world, everywhere)
        feasible = [a for a in areas if a.feasible]
        prev = self._previous_target()
        if self.resource.tour and self.store.pose is not None:
            feasible = self._tour_kept(feasible, areas, prev)
        elif self.resource.chance:
            feasible.sort(key=lambda a: -a.find_chance())  # the most likely place first
        elif prev:
            _keep_target(feasible, areas, prev)
        self._notice_move_on(prev, feasible)  # judged on the target actually shown, after the hysteresis
        # cut only now: the current target may be among the areas a cut would drop
        self.areas = (feasible + [a for a in areas if not a.feasible])[:MAX_REPORTED]
        self._areas_vehicle = self.plan_vehicle
        self._known_nodes = len(nodes)
        self.score_ms = round((time.perf_counter() - t0) * 1000, 1)
        state = json.dumps(self.page_state(), sort_keys=True)
        if state != self._last_published:
            self._last_published = state
            self.publish_rev += 1

    def _previous_target(self) -> Area | None:
        """The target shown so far, unless the plan vehicle changed since: a place the old vehicle
        reached says nothing about the new one (a manual vehicle choice clears the areas outright)."""
        if getattr(self, "_areas_vehicle", self.plan_vehicle) != self.plan_vehicle:
            return None
        return next((a for a in self.areas if a.feasible), None)

    def _tour_kept(self, feasible: list[Area], areas: list[Area], prev: Area | None) -> list[Area]:
        """Tour stops nearest-first, but the stop you are heading to stays first unless another is clearly
        nearer (live: reported ships about the same distance away swapped after 70 m of flight)."""
        p = self.store.pose
        feasible = tour_order(feasible, p.x, p.y)
        if prev is None:
            return feasible
        again = next((a for a in feasible if _near_stop(a, prev)), None)
        if again is None:
            again = _rescued_stop(areas, prev)
            if again is not None:
                feasible.append(again)
        if again is not None and again is not feasible[0] and (
                math.hypot(again.x - p.x, again.y - p.y) < SWITCH_MARGIN * math.hypot(feasible[0].x - p.x, feasible[0].y - p.y)):
            feasible.remove(again)
            feasible = [again, *tour_order(feasible, again.x, again.y)]
        return feasible

    def _notice_move_on(self, prev: Area | None, feasible: list[Area]) -> None:
        """'Area mined out -> next 1.2 km' when the target leaves the patch you mined in.

        Judged by patch (PATCH_CM), not by 500 m area: inside a big island the target moves from
        one part to the next as you mine, and that is not "moving on" (live: "Move on (98 left
        here)" 194 m away inside the H5 island).
        """
        wanted = {m.lower() for m in self.resource.marker_types}
        if (prev is None or self.resource.harvest != "on_foot" or self.mined_nodes.near(prev.x, prev.y, wanted) == 0
                or any(math.hypot(a.x - prev.x, a.y - prev.y) <= PATCH_CM for a in feasible[:1])):
            return
        nxt = feasible[0] if feasible else None
        left = sum(len(a.nodes) for a in feasible if math.hypot(a.x - prev.x, a.y - prev.y) <= PATCH_CM)
        head = f"Move on ({left} left here)" if left else "Area mined out"
        if nxt is None:
            text = f"{head} · nothing else known nearby"
        else:
            d = nxt.distance_m or 0
            text = f"{head} → next {d / 1000:.1f} km" if d >= 1000 else f"{head} → next {d} m"
        self.notice = (self.clock, text)
        log.info("%s", text)

    def maybe_rescore(self, now: float) -> bool:
        key = self._rescore_due(now) if self.gate_open() else None
        if key is None:
            return False
        self._scored_at, self._scored_key, self._dirty = now, key, False
        self.compute(now)
        return True

    async def maybe_rescore_async(self, now: float) -> bool:
        """Same as maybe_rescore, but keeps the feed loop responsive while ranking."""
        key = self._rescore_due(now) if self.gate_open() else None
        if key is None:
            return False
        self._scored_at, self._scored_key, self._dirty = now, key, False
        job = self._ranking_job(now)
        if job is None:
            return True
        t0 = time.perf_counter()
        areas = await asyncio.to_thread(self._rank, job)
        self._apply_ranking(job, areas, t0)
        return True

    def _hotspot(self, world, nodes: list[Node]) -> dict | None:
        """Densest concentration of the searched resource on this map, as a grid cell.

        Same clusters as the recommendation (about 500 m across), weighted by likelihood and the
        PvP yield; `nodes` already excludes PvE rows when "PvP areas only" is on.
        """
        fit = self.grid_fit(world[0] if world else None)
        if not world or fit is None or not nodes:
            return None  # no grid known for this map
        best, best_w = None, 0.0
        for a in _clusters(nodes):
            w = sum(n.value() * n.likelihood for n in a.nodes) * self.yield_at(a.x, a.y)
            if w > best_w:
                best, best_w = a, w
        if best is None:
            return None
        zone = ("PvP" if is_pvp(best.y, self.last_pve_row, fit) else "PvE") if is_deep_desert(world[0]) else None
        return {"cell": cell_name(best.x, best.y, fit), "nodes": len(best.nodes), "x": best.x, "y": best.y, "zone": zone}
