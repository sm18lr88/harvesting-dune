# Farm: the farm route mode on the detector: actions, the companion's line and rings, and its text.
"""Farm route mode (a mixin of app.Detector; it shares the detector's state).

Off by default, and a separate target like chance finds: the ranking keeps running unchanged, and
while a route runs the companion shows the route's line and rings instead of the ranked target.
routes.py owns the route data and the run's state; this module turns them into page marks, one line
of text for the minimap badge and the target card, and the settings-page rows.
"""

from __future__ import annotations

import logging
import math

from .itemsources import PREFIX, item_id
from .routes import AWAY_END_S, NOTICE_S, FarmRun, Route, Site, Stop, DoneTimes
from .zones import DEFAULT_FIT, cell_name, is_deep_desert

log = logging.getLogger(__name__)

STOP_STYLE = {"color": "rgba(89,221,255,0.95)", "weight": 2.5}  # the companion's route cyan: a route, not a chance find
DONE_STYLE = {"color": "rgba(255,255,255,0.30)", "weight": 1.5}
STOP_RING_CM = 500.0
RINGS_AHEAD = 3


def _km(cm: float) -> str:
    return f"{cm / 100_000:.1f} km" if cm >= 100_000 else f"{round(cm / 100)} m"


class FarmMixin:
    def _farm_setup(self) -> None:
        self.routes: list[Route] = []  # from the server's routes dataset (gate.on_dataset)
        self.farm: FarmRun | None = None
        self.farm_done = DoneTimes(self.data_dir)
        self._farm_text: str | None = None  # the badge text last marked for a push

    # -- actions ------------------------------------------------------------------------------------
    def farm_action(self, action: str, value: str | None) -> bool:
        """False for an unknown route or action, or one that needs a run while none is on."""
        run = self.farm
        match action:
            case "route_start":
                route = next((r for r in self.routes if r.id == value), None)
                if route is None:
                    return False
                self.farm = FarmRun.start(route, self._farm_stops, self.farm_done, self._world_id(), self.store.pose)
                log.info("farm route %s started", route.id)
                self.farm_observe(self.clock)
            case "route_stop" if run is not None:
                self.farm = None
            case "route_skip" if run is not None:
                run.skip(self.clock)
            case "route_repeat" if run is not None:
                run.toggle_repeat(self.clock)
            case _:
                return False
        self.publish_rev += 1  # what the companion shows changed; the ranking did not
        return True

    def _world_id(self) -> str | None:
        return self.store.world[0] if self.store.world else None

    def _farm_stops(self, site: Site) -> tuple[Stop, ...]:
        """A fixed site's stops, or this week's containers for a Deep Desert layout site."""
        if not site.from_items:
            return site.stops
        layout, out = self._item_map(), {}
        for iid in self.farm.route.items if self.farm else ():
            for row in self.items.sources(PREFIX + iid, layout):
                if not self.items.from_ships(row):
                    stop = Stop(row[0], row[1], row[2], "chest", row[3], None)
                    out.setdefault(stop.key, stop)
        return tuple(out.values())

    # -- feed -----------------------------------------------------------------------------------------
    def farm_feed(self, msg, now: float) -> None:
        payload = msg.payload if isinstance(msg.payload, dict) else {}
        if msg.topic == "dune.map.actors" and self._world_key(payload)[0] in (None, self._world_id()) \
                and isinstance(payload.get("actors"), list):
            self.farm_actors(payload["actors"], now)
        if msg.topic in ("dune.session", "dune.player", "dune.map.actors"):
            self.farm_observe(now)

    def farm_observe(self, now: float) -> None:
        run = self.farm
        changed = run.update(self.store.world, self.store.pose, now)
        if run.away_since is not None and now - run.away_since >= AWAY_END_S:
            log.info("farm route %s ended: 10 min outside its maps", run.route.id)
            self.farm, changed = None, True
        # the distance and the notice's end change the minimap text while the run's state stays the same
        text = None if self.farm is None else self.farm_notice() or self.farm_line()
        if changed or text != self._farm_text:
            self._farm_text = text
            self.publish_rev += 1

    def farm_actors(self, raw: list, now: float) -> None:
        """Containers the companion shows emptied (hasLoot false) or full again (true) at the current site's stops."""
        run, hit = self.farm, False
        for a in raw:
            if not (isinstance(a, dict) and isinstance(a.get("x"), (int, float)) and isinstance(a.get("y"), (int, float))):
                continue
            if a.get("hasLoot") is False:
                hit = run.looted(a["x"], a["y"], now) or hit  # every row: one message can show several chests emptied
            elif a.get("hasLoot") is True:
                run.refilled(a["x"], a["y"])
        if hit:
            self.publish_rev += 1

    # -- the companion ------------------------------------------------------------------------------
    def _farm_target(self, run: FarmRun) -> tuple[float, float, float, str] | None:
        pose = self.store.pose
        if run.phase == "site":
            i = run.next_index
            s = run.stops[i]
            return s.x, s.y, s.z, f"stop {run.order.index(i) + 1}/{len(run.order)} · {s.name}"
        if run.phase != "approach":
            return None
        site = run.site
        if site.entrance:
            return *site.entrance, site.name
        if not run.stops or pose is None:  # no stops known yet (this week's layout): the site's marker
            return (*site.center, site.name) if site.center else None
        s = min(run.stops, key=lambda s: math.hypot(s.x - pose.x, s.y - pose.y))  # no entrance known: the nearest stop
        return s.x, s.y, s.z, site.name

    def farm_targets(self, state: dict) -> None:
        """The run's destination, one line to the next stop only, numbered rings ahead and dim rings for done stops."""
        run, state["alternatives"] = self.farm, []
        target = self._farm_target(run) if self.enabled else None
        if target is None:
            return
        x, y, z, label = target
        state["dest"] = {"hasDestination": True, "x": x, "y": y, "z": z, "title": f"Farm route · {run.site.name}", "detail": label}
        if not self.lines_enabled:
            return
        state["route"] = {"mapId": self.scan.map_id or self._world_id(),
                          "stops": [{"x": x, "y": y, "z": z, "houseName": f"Farm route · {label}"}]}
        if run.phase == "site":
            ahead = [i for i in run.order if i not in run.done][:RINGS_AHEAD]
            state["alternatives"] = [
                {"x": run.stops[i].x, "y": run.stops[i].y, "size": None, "kind": "route-stop", "ring": STOP_RING_CM,
                 "num": run.order.index(i) + 1, **STOP_STYLE} for i in ahead
            ] + [{"x": run.stops[i].x, "y": run.stops[i].y, "size": None, "kind": "route-done", "ring": STOP_RING_CM, **DONE_STYLE}
                 for i in sorted(run.done)]

    # -- text ---------------------------------------------------------------------------------------
    def _farm_cell(self, site: Site) -> str:
        fit = self.learning.grid_fit(site.world) or (DEFAULT_FIT if is_deep_desert(site.world) else None)
        return f" (sector {cell_name(site.center[0], site.center[1], fit)})" if fit and site.center else ""

    def farm_line(self) -> str:
        run = self.farm
        site, pose = run.site, self.store.pose
        match run.phase:
            case "travel":
                return f"Farm route: travel to {self.items.region(site.world)}, {site.name}{self._farm_cell(site)}"
            case "wait":
                return f"Farm route: {site.name} ready in {max(1, round((run.wait_until - self.clock) / 60))} min"
        target = self._farm_target(run)
        if target is None:
            return f"Farm route: {site.name}: no stops known this week" if site.from_items else f"Farm route: {site.name}"
        far = f" · {_km(math.hypot(target[0] - pose.x, target[1] - pose.y))}" if pose else ""
        return f"Farm route: {site.name}" + (f" · {target[3].split(' · ')[0]}" if run.phase == "site" else "") + far

    def farm_notice(self) -> str | None:
        run = self.farm
        fresh = run.notice and (run.phase == "wait" or self.clock - run.notice[0] <= NOTICE_S)
        return run.notice[1] if fresh else None

    # -- settings page --------------------------------------------------------------------------------
    def farm_rows(self) -> list[dict]:
        """The routes for the searched item, for the "Farm route" table."""
        iid = item_id(self.resource_key)
        rows = [self._farm_row(r) for r in self.routes if iid in r.items]
        return sorted(rows, key=lambda row: (row["distanceM"] is None, row["distanceM"] or 0))

    def _farm_row(self, r) -> dict:
        pose, world = self.store.pose, self._world_id()
        here = [s.center for s in r.sites if s.world == world and s.center] if pose else []
        near = min((math.hypot(c[0] - pose.x, c[1] - pose.y) for c in here), default=None)
        weekly = any(s.from_items for s in r.sites)
        # [map id, gaming.tools' name or None, Landsraad mission]: the page names it like the other worlds it shows
        worlds = list({s.world: [s.world, self.items.map_names.get(s.world), s.mission] for s in r.sites}.values())
        return {"id": r.id, "name": r.name, "worlds": worlds, "here": any(s.world == world for s in r.sites),
                "sites": len(r.sites), "chests": None if weekly else sum(len(s.stops) for s in r.sites),
                "resetMin": None if r.reset_s is None else round(r.reset_s / 60), "confidence": r.confidence,
                "note": r.note, "distanceM": None if near is None else round(near / 100)}

    def farm_card(self) -> dict | None:
        run = self.farm
        if run is None:
            return None
        return {"id": run.route.id, "name": run.route.name, "phase": run.phase, "line": self.farm_line(),
                "notice": self.farm_notice(), "repeat": run.repeat, "site": run.site.name,
                "done": len(run.done), "stops": len(run.order or run.stops)}
