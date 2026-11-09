# Outputs: what the companion shows (destination, route, alternative lines, badge, picker).
"""Companion-facing output (a mixin of app.Detector; it shares the detector's state).

Contract with bridge.js (`__dd.set(page_state())`):
    dest: {hasDestination, x, y, z, title, detail} | None   -> the companion's destination marker
    route: {mapId, stops: [{x, y, z, houseName}]} | None      -> the main (cyan) line
    alternatives: [{x, y, size, kind, color, weight}]        -> thin extra lines; kind "peer" / "member" draw a ring only (ring in cm, default 4000), never pulsed;
                                                              farm route: "route-stop" rings carry `num` (drawn inside), "route-done" rings are dim
    stale: [row key]                                          -> scan rows the page must drop
    extraActors: [actor]                                      -> overlay markers we add
    objects: [{id, kind, x, y, z, body, mine, ageS, ttlS, by}] -> guild objects for this map (objects.py); bridge.js draws known kinds only
    problem: {id, text} | None                                -> notice in the map window after an error
`badge()` is the one-line minimap text; `picker()` feeds the map window's picker.
With the gate closed (gate.py) every key is empty and the badge is None: the detector draws nothing.
"""

from __future__ import annotations

import math
import time

from . import logsetup
from .areas import AREA_RADIUS_CM, PATCH_CM, Area
from .catalog import NEVER_TARGET, RESOURCES
from .ranking import ALT_LIMIT, ROUTED_AREAS
from .store import REMEMBER_SPICE_S, Status
from .vehicle import VEHICLE_CHOICES

# secondary lines: thinner and fainter than the companion's main route line (cyan, weight 4)
FRESH_STYLE = {"color": "rgba(255,70,200,0.85)", "weight": 2.5}  # fresh blows: magenta
HARVESTED_ALPHA = 0.25  # fields someone is emptying: much fainter
ALT_STYLE = {
    "large": {"color": "rgba(89,221,255,0.45)", "weight": 2},
    "medium": {"color": "rgba(255,176,59,0.60)", "weight": 2},
    "small": {"color": "rgba(255,255,255,0.35)", "weight": 1.5},
}
FRESH_BLOW_S = 20 * 60  # a blow is "fresh" (distinct line colour) for 20 minutes
EVENT_SHOW_S = 90.0
PROBLEM_SHOW_S = 30 * 60.0  # the map window's notice about an error, for this long after it
# Chance finds (unique gear, schematics) are search areas, not a route: a pulsing gold ring around each
# likely place, its radius growing with the chance there (40 m, up to 200 m at 25 % or more), and one
# dashed bearing line to the most likely place (the minimap is zoomed far in: the line gives direction).
CHANCE_RING_CM = (4_000.0, 16_000.0)
CHANCE_STYLE = ({"color": "rgba(255,209,102,0.95)", "weight": 3}, {"color": "rgba(255,209,102,0.55)", "weight": 2})


def pct(p: float) -> str:
    return f"{p * 100:.1f}%" if p < 0.1 else f"{round(p * 100)}%"
NOTICE_S = 60.0
SHORT_NAMES = {"spice_large": "Spice L", "spice_medium": "Spice M", "spice": "Spice", "cages": "Cages R+UR",
               "cages_ultra": "Ultra-rare cages", "wrecks": "Vehicle wrecks", "downed_ships": "Downed ships",
               "shipwrecks": "Shipwrecks"}
PLACE_NAMES = {"downed_ships": "downed ship", "shipwrecks": "shipwreck", "wrecks": "wreck"}


class OutputsMixin:
    def top(self, n: int = 3) -> list[Area]:
        return [a for a in self.areas if a.feasible][:n]

    def route_stops(self) -> int:
        return ALT_LIMIT + 1 if self.resource.tour else 1

    def fresh_blow(self, key: str) -> dict | None:
        b = self.blows.get(key)
        return b if b and self.clock - b["t"] <= FRESH_BLOW_S else None

    def recent_event(self, now: float) -> str | None:
        while self.events and now - self.events[0][0] > EVENT_SHOW_S:
            self.events.popleft()
        return self.events[-1][1] if self.events else None

    def remembered_spice(self, now: float, limit: int = 100) -> list[dict]:
        """Active fields seen earlier that are now out of load range: kept on the map for a while."""
        out = []
        for t in self.store.items.values():
            a = t.actor
            if not a.is_spice_field or t.status is not Status.OUT_OF_RANGE or t.last_live is None:
                continue
            if now - t.last_live > REMEMBER_SPICE_S:
                continue
            out.append({
                "id": f"dd-mem-{a.key}", "actorType": a.actor_type, "markerType": a.marker_type,
                "spriteKey": a.sprite_key, "resourceItemId": a.resource_item_id,
                "x": a.x, "y": a.y, "z": a.z, "fillRate": a.fill_rate, "spawned": True, "reportable": False,
            })
        pose = self.store.pose
        if pose is not None and len(out) > limit:
            out.sort(key=lambda d: pose.dist2d(d["x"], d["y"]))
            out = out[:limit]
        return out

    def describe(self, a: Area) -> str:
        mins = max(1, round((a.trip_s or 0) / 60))
        if self.resource_key.startswith("spice"):
            fills = [n.fill for n in a.nodes if n.fill is not None]
            fill = f", {round(100 * max(fills))}% full" if fills else ""
            what = f"{len(a.nodes)} field{'s' if len(a.nodes) > 1 else ''}{fill} · ~{round(a.expected):,} sand"
        elif self.resource.chance:
            n = len(a.nodes)
            what = f"{n} place{'s' if n > 1 else ''} · {pct(a.find_chance())} chance to find one"
        elif self.items.is_item(self.resource_key):
            n = len(a.nodes)
            what = f"{n} source{'s' if n > 1 else ''} ({a.live_nodes} live) · ~{a.expected:.1f} expected"
        elif self.resource_key in PLACE_NAMES:
            n, place = len(a.nodes), PLACE_NAMES[self.resource_key]
            what = f"{n} {place}{'s' if n > 1 else ''}" + (f" ({a.live_nodes} in view)" if a.live_nodes else "")
        else:
            what = f"{len(a.nodes)} node{'s' if len(a.nodes) > 1 else ''} ({a.live_nodes} live)"
            if a.patch_nodes > len(a.nodes):
                what += f", {a.patch_nodes} in the patch"
        zone = self.zone_of(a.y)
        if zone:
            zone = f" · {zone}" + (f" x{a.yield_mult:g}" if a.yield_mult != 1 else "")
        extra = f" · {'; '.join(a.reasons)}" if a.reasons else ""
        return f"{what} · ~{mins} min round trip{zone or ''}{extra}"

    def page_state(self) -> dict:
        if not self.gate_open():
            return {"dest": None, "route": None, "alternatives": [], "stale": [], "extraActors": [], "problem": None,
                    "objects": []}
        self.peer_expire(self.clock)
        scan_stale = list(self.scan.stale)
        state = {
            "dest": None,
            "route": None,
            "stale": scan_stale + sorted(self.peer_stale_keys() - set(scan_stale)),
            "extraActors": self.scan.overlay_actors(self.store.pose, limit=100 if self.lean else 200)
            + self.remembered_spice(time.time(), limit=50 if self.lean else 100)
            + self.ships.overlay(self.clock),
            "problem": self.problem_notice(),
        }
        if self.farm is not None:  # a farm route is its own target; the ranking underneath is untouched
            self.farm_targets(state)
        else:
            self._targets(state)
        state["alternatives"] = state.get("alternatives", []) + self.peer_marks()  # guild rings, never ranked
        state["objects"] = self.object_marks()
        return state

    def _targets(self, state: dict) -> None:
        """Fill in the destination, the main line and the ranked alternatives."""
        top = self.top(self.route_stops()) if self.enabled else []
        lines = self.enabled and self.lines_active and self.lines_here()
        if not top:
            # no main target (e.g. no active large field known): still point at the good alternatives
            state["alternatives"] = self.alternatives(None) if lines else []
            return
        best = top[0]
        state["dest"] = {
            "hasDestination": True,
            "x": best.x, "y": best.y, "z": best.z,
            "title": f"{self.resource.name} · " + ("most likely place" if self.resource.chance else f"best for {self.plan_vehicle}"),
            "detail": self.describe(best),
        }
        # the main line from you to the best target: the minimap is zoomed far in and has no edge
        # arrow for an off-screen destination, so the line is the direction cue
        if lines:
            if self.resource.chance:  # a matter of luck: search areas, never a route that looks certain
                state["alternatives"] = self.chance_marks()
                return
            # scattered single places: one route through the nearest stops in visiting order
            stops = top if self.resource.tour else [best]
            state["route"] = {
                "mapId": self.scan.map_id or (self.store.world[0] if self.store.world else None),
                "stops": [{"x": a.x, "y": a.y, "z": a.z, "houseName": f"{self.resource.name} · {self.describe(a)}"} for a in stops],
            }
            state["alternatives"] = [] if self.resource.tour else self.alternatives(best)

    def chance_marks(self) -> list[dict]:
        """The most likely places as rings (bigger = better chance), plus a dashed line to the best one."""
        out = []
        for i, a in enumerate(self.top(ALT_LIMIT + 1)):
            p = a.find_chance()
            ring = CHANCE_RING_CM[0] + CHANCE_RING_CM[1] * min(p / 0.25, 1.0)
            out.append({"x": a.x, "y": a.y, "size": None, "kind": "chance-area", "ring": round(ring), "pulse": True,
                        "chance": round(p, 4), **CHANCE_STYLE[0 if i == 0 else 1]})
        if out:
            out.insert(0, {**out[0], "kind": "chance", "ring": None, "dash": True})
        return out

    def problem_notice(self) -> dict | None:
        last = logsetup.PROBLEMS.last()
        if last is None or time.time() - last["t"] > PROBLEM_SHOW_S:
            return None
        n = logsetup.PROBLEMS.count
        what = "a problem" if n == 1 else f"{n} problems"
        return {"id": last["id"], "text": f"harvesting-dune hit {what}. The companion works as usual. To get it fixed, "
                                          f"open {self.settings_url} and press \"Copy report\"."}

    def alternatives(self, best: Area | None, limit: int = ALT_LIMIT) -> list[dict]:
        """Thin secondary lines to the next best options.

        Spice modes list confirmed-active medium and large fields (never small ones), bigger first.
        Other resources list the next best patches in ranking order; areas backed only by scan rows
        get a fainter line than ones seen live.
        """
        pose = self.store.pose
        if pose is None:
            return []
        out = self._spice_alternatives(best, pose) if self.resource.harvest == "drive_over" else self._node_alternatives(best)
        out.sort(key=lambda item: item[0])
        return [d for _, d in out[:limit]]

    def _spice_alternatives(self, best: Area | None, pose) -> list[tuple[tuple, dict]]:
        out = []
        rank = {"large": 0, "medium": 1, "small": 2}
        for t in self.store.items.values():
            a = t.actor
            if not a.is_spice_field or t.status in (Status.OFFLINE, Status.MISSING_IN_RANGE):
                continue
            if t.last_live is None or self.clock - t.last_live > REMEMBER_SPICE_S:
                continue
            if best is not None and math.hypot(a.x - best.x, a.y - best.y) < AREA_RADIUS_CM:
                continue  # that is the main target
            if a.marker in NEVER_TARGET:
                continue  # small fields are not worth the trip
            size = a.spice_size
            style = dict(ALT_STYLE[size])
            kind = "field"
            if self.fresh_blow(a.key):
                style, kind = dict(FRESH_STYLE), "fresh"
            elif (t.fill_trend_per_min() or 0) < 0:
                style["color"] = style["color"].rsplit(",", 1)[0] + f",{HARVESTED_ALPHA})"
                kind = "harvested"
            order = (0 if kind == "fresh" else 1, rank[size], kind == "harvested", pose.dist2d(a.x, a.y))
            out.append((order, {"x": a.x, "y": a.y, "size": size, "kind": kind, **style}))
        return out

    def _node_alternatives(self, best: Area | None) -> list[tuple[tuple, dict]]:
        out = []
        floor = self.resource.min_patch  # only patches worth a stop, in ranking order
        chosen = [best] if best is not None else []
        for rank, a in enumerate(self.top(ROUTED_AREAS)[1 if best is not None else 0:]):
            if len(a.nodes) < floor:
                continue
            if any(math.hypot(a.x - c.x, a.y - c.y) <= PATCH_CM for c in chosen):
                continue  # the same patch as a line already drawn
            chosen.append(a)
            seen = a.live_nodes or any(n.source == "memory" for n in a.nodes)
            style = ALT_STYLE["large"] if seen else ALT_STYLE["small"]
            out.append(((rank,), {"x": a.x, "y": a.y, "size": None, "kind": "area" if seen else "scan", **style}))
        return out

    def status_text(self) -> str:
        if not self.enabled:
            return "recommendations off (Ctrl+Alt+Down)"
        top = self.top(1)
        if self.store.pose is None:
            where = "waiting for position"
        elif top:
            where = f"best area {top[0].distance_m} m away"
        elif self._known_nodes:
            where = "no reachable area"
        elif self.resource.confirmed_only:
            where = "no active field of this size seen yet"
        elif self.max_km > 0:
            where = f"none within {self.max_km:g} km" + (" in the PvP area" if self._pvp_only_here() else "")
        elif self._pvp_only_here():
            where = "none known in the PvP area yet"
        else:
            where = "none known here yet"
        stale = f" · {len(self.scan.stale)} stale hidden" if self.scan.stale else ""
        return where + stale

    def badge(self, now: float | None = None) -> str | None:
        """One short line that fits under the minimap (~45 characters)."""
        if not self.enabled or not self.gate_open():
            return None
        if self.farm is not None:
            return self.farm_notice() or self.farm_line()
        wall = time.time() if now is None else now
        if self.ship_alert and wall - self.ship_alert[0] <= EVENT_SHOW_S:
            return self.ship_alert[1]  # whatever you search: downed ships are rare and short-lived
        event = self.recent_event(wall)
        if event and self.resource.harvest == "drive_over":
            return event  # spice news only while you search spice
        if self.notice and self.resource.harvest != "drive_over" and wall - self.notice[0] <= NOTICE_S:
            return self.notice[1]
        short = SHORT_NAMES.get(self.resource_key, self.resource.name)
        vehicle = {"sandcrawler": "crawler", "ornithopter": "thopter"}.get(self.plan_vehicle, self.plan_vehicle)
        if self.vehicles.override:
            vehicle += "*"  # manually set
        if self.vehicles.current == "foot":
            vehicle = f"on foot, plan {vehicle}"
        return f"{short} · {vehicle} · {self._badge_where()}{self._badge_tail()}"

    def _badge_where(self) -> str:
        top = self.top(1)
        if self.store.pose is None:
            return "waiting"
        if top:
            d = top[0].distance_m or 0
            first = "next" if self.resource.tour else "best"
            where = f"{first} {d / 1000:.1f} km" if d >= 1000 else f"{first} {d} m"
            if self.resource.tour:
                return where + f" · {len(self.top(self.route_stops()))} stops on the route"
            if self.resource.chance:
                return where + f" · {pct(top[0].find_chance())} chance"
            if self.resource.harvest != "drive_over":
                done = self.mined_nodes.near(top[0].x, top[0].y, {m.lower() for m in self.resource.marker_types})
                where += f" · {len(top[0].nodes)} left · {done} mined" if done else f" · {len(top[0].nodes)} nodes"
            return where
        if self._known_nodes:
            return "none reachable"
        if self.resource.confirmed_only:
            return "none active yet"
        if self.max_km > 0:
            return f"none within {self.max_km:g} km"
        return "none in PvP" if self._pvp_only_here() else "none known"

    def _badge_tail(self) -> str:
        if self.lines_enabled and not self.lines_active:
            return " · lines: show 1 resource"
        if self.lines_enabled and not self.lines_here():
            return f" · lines from row {self.lines_from_row}"
        if self.hotspot:
            return f" · hotspot {self.hotspot['cell']}"  # where this resource is richest, reachable or not
        return f" · {len(self.scan.stale)} fixed" if self.scan.stale else ""

    def picker(self) -> dict:
        return {
            "resources": [[r.key, r.name] for r in RESOURCES],
            "vehicles": [[v, "Vehicle: auto" if v == "auto" else f"Vehicle: {v}"] for v in VEHICLE_CHOICES],
            "selected": self.resource_key,
            "vehicle": self.vehicles.override or "auto",
            "status": f"{self.plan_vehicle}{'' if self.vehicles.override else ' (detected)'} · {self.status_text()}"
                      + (f" · hotspot {self.hotspot['cell']} ({self.hotspot['nodes']} nodes)" if self.hotspot else ""),
            "minimap": self.minimap_panel(),
        }
