# Vehicle: works out which vehicle you are in from the session and your motion.
"""Automatic vehicle detection.

Signals, strongest first:
  1. manual override (hotkey), until cleared
  2. a vehicle name inside the session's `characterState` string, if the game puts one there
  3. `dune.session.activity == "Driving"` (definitive in/out of vehicle) plus motion.

You cannot change vehicles without leaving "Driving", so one driving session is one vehicle.
Flight evidence therefore settles the whole session as an ornithopter (scout, assault or
carrier): a take-off climb, hovering, cruising faster than any ground vehicle, or sitting far
above the surrounding ground. A single bump never counts: a buggy cresting a dune used to be
called an ornithopter by one vertical-speed spike, and "the lowest ground within 1 km" put a
buggy on a dune 25 m "in the air". Without flight evidence, the fastest sustained speed seen
in the session separates sandcrawler / buggy / sandbike: a buggy creeping around a node field
looks like a crawler for a while, but a crawler never reaches buggy speed.

Thresholds were checked against today's recorded sessions (flights show evidence within
0-7 s of boarding; slow ground sessions show none).
"""

from __future__ import annotations

import statistics
from collections import deque

from .model import Pose

NAME_HINTS = (
    ("sandcrawler", "sandcrawler"),
    ("harvester", "sandcrawler"),
    ("ornithopter", "ornithopter"),
    ("sandbike", "sandbike"),
    ("buggy", "buggy"),
    ("treadwheel", "buggy"),
)
VEHICLE_CHOICES = ("auto", "sandcrawler", "buggy", "sandbike", "ornithopter")  # manual override choices
CRAWLER_MAX_CMS = 1150.0  # ~41 km/h sustained
SANDBIKE_MIN_CMS = 2500.0  # ~90 km/h sustained
CRAWLER_AFTER_S = 60.0  # this long without buggy speed makes it a crawler (a buggy mining between nodes creeps for 15 s+)
FLYING_MIN_CRUISE_CMS = 3500.0  # ~126 km/h sustained: no ground vehicle cruises this fast
FLYING_ALTITUDE_CM = 8000.0  # 80 m above the typical ground around you (dunes stay below this)
CLIMB_CM, CLIMB_WINDOW_S = 4000.0, 5.0  # 40 m up within 5 s: a take-off, not a dune
HOVER_MAX_CMS, HOVER_MIN_VZ_CMS = 140.0, 200.0  # < 5 km/h across while >= 2 m/s up or down
HOVER_SAMPLES = 2
BOARDING_IGNORE_S = 2.0  # getting in moves you a few metres: not evidence of anything
MIN_SPEED_DT_S = 0.8  # measure speed over >= 0.8 s spans: update timing jitter fakes spikes
BOARDING_RADIUS_CM = 150000.0  # the boarding height is a ground reference within 1.5 km


class VehicleDetector:
    def __init__(self, window_s: float = 20.0) -> None:
        self.override: str | None = None
        self.driving = False
        self.name_hint: str | None = None
        self.current = "foot"
        self.last_vehicle = "buggy"  # what to plan for while on foot
        self.last_harvester = "sandcrawler"  # last vehicle seen that can harvest spice
        self._window_s = window_s
        self._samples: deque[tuple[float, float, float, float, float | None]] = deque()  # t, x, y, z, ground_z
        self._pending: str | None = None
        self._motion_confirmed = False
        self.stats_hint: str | None = None
        self._last_foot: tuple[float, float, float] | None = None  # x, y, z while on foot
        self._boarding: tuple[float, float, float] | None = None
        self._session_t0: float | None = None
        self._new_session()  # per-session state: flight evidence, hovers, cruise speeds

    def on_session(self, activity: str | None, character_state: str | None) -> bool:
        driving = (activity or "").lower() == "driving" or "driving" in (character_state or "").lower()
        hint = None
        low = (character_state or "").lower()
        for needle, vehicle in NAME_HINTS:
            if needle in low:
                hint = vehicle
                break
        changed = driving != self.driving or hint != self.name_hint
        if driving and not self.driving:
            self._boarding = self._last_foot
        if driving != self.driving:
            self._new_session()
        self.driving, self.name_hint = driving, hint
        if not driving:
            self.stats_hint = None
        return self._update() or changed

    def _new_session(self) -> None:
        self._samples.clear()
        self._motion_confirmed = False
        self._pending = None
        self._session_t0 = None
        self._session_flying = False
        self._hovers = 0
        self._cruise_max = 0.0  # fastest sustained ground speed this session
        self._cruise_windows = 0
        self._cruise_since: float | None = None

    def on_stats_names(self, names: list[str]) -> bool:
        """Vehicle names found in the companion's raw stats (if it exposes any while driving)."""
        hint = None
        for name in names:
            low = name.lower()
            hint = next((veh for needle, veh in NAME_HINTS if needle in low), None)
            if hint:
                break
        if hint == self.stats_hint:
            return False
        self.stats_hint = hint
        return self._update()

    def on_pose(self, t: float, pose: Pose, ground_z: float | None) -> bool:
        if not self.driving:
            self._last_foot = (pose.x, pose.y, pose.z)
        if self.driving:
            if ground_z is None and self._boarding is not None:
                bx, by, bz = self._boarding
                if ((pose.x - bx) ** 2 + (pose.y - by) ** 2) ** 0.5 <= BOARDING_RADIUS_CM:
                    ground_z = bz
            if self._session_t0 is None:
                self._session_t0 = t
            prev = self._samples[-1] if self._samples else None
            # 1 s resolution: companion updates arrive in bursts, and tiny spans fake huge speeds
            if prev is None or t - prev[0] >= 1.0:
                self._samples.append((t, pose.x, pose.y, pose.z, ground_z))
                while self._samples and t - self._samples[0][0] > self._window_s:
                    self._samples.popleft()
                if not self._session_flying and self._flight_evidence():
                    self._session_flying = True
        return self._update()

    def _flight_evidence(self) -> bool:
        """Anything a ground vehicle cannot do, judged on the newest 1 s step and recent window."""
        s = self._samples
        if len(s) < 2 or self._session_t0 is None or s[-1][0] - self._session_t0 < BOARDING_IGNORE_S:
            return False
        (t0, x0, y0, z0, _), (t1, x1, y1, z1, _) = s[-2], s[-1]
        dt = t1 - t0
        if dt > 0:
            h = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5 / dt
            if h < HOVER_MAX_CMS and abs(z1 - z0) / dt >= HOVER_MIN_VZ_CMS:
                self._hovers += 1  # straight up or down without moving across
                if self._hovers >= HOVER_SAMPLES:
                    return True
        recent = [p for p in s if t1 - p[0] <= CLIMB_WINDOW_S]
        if recent and z1 - min(p[3] for p in recent) >= CLIMB_CM:
            return True  # take-off climb
        return self._classify_motion() == "ornithopter"  # fast cruise, or far above the ground

    def set_override(self, vehicle: str | None) -> None:
        self.override = vehicle
        self._update()

    @property
    def confident(self) -> bool:
        """True when the current vehicle is known, not just assumed from last time."""
        if self.current == "foot":
            return True
        return bool(self.override or self.name_hint or self.stats_hint or self._motion_confirmed)

    @property
    def plan_vehicle(self) -> str:
        if self.override:
            return self.override
        return self.current if self.current != "foot" else self.last_vehicle

    def _window_cruise(self) -> float | None:
        """Median moving speed over the recent window (cm/s), or None while parked or too new."""
        s = list(self._samples)
        if len(s) < 6 or s[-1][0] - s[0][0] < 5:
            return None
        speeds = []
        anchor = s[0]
        for cur in s[1:]:
            dt = cur[0] - anchor[0]
            if dt < MIN_SPEED_DT_S:
                continue
            speeds.append(((cur[1] - anchor[1]) ** 2 + (cur[2] - anchor[2]) ** 2) ** 0.5 / dt)
            anchor = cur
        moving = [v for v in speeds if v > 300]
        return statistics.median(moving) if len(moving) >= 3 else None

    def _classify_motion(self) -> str | None:
        """Flight from the window alone: far above the ground, or cruising too fast for the ground."""
        s = self._samples
        alts = [z - g for (_, _, _, z, g) in s if g is not None]
        if len(s) >= 6 and len(alts) >= 5 and statistics.median(alts) > FLYING_ALTITUDE_CM:
            return "ornithopter"
        cruise = self._window_cruise()
        return "ornithopter" if cruise is not None and cruise >= FLYING_MIN_CRUISE_CMS else None

    @staticmethod
    def _ground_type(cruise: float) -> str:
        if cruise <= CRAWLER_MAX_CMS:
            return "sandcrawler"
        if cruise >= SANDBIKE_MIN_CMS:
            return "sandbike"
        return "buggy"

    def _update(self) -> bool:
        before = self.current
        if not self.driving:
            self.current = "foot"
        elif self.name_hint or self.stats_hint:
            self.current = self.name_hint or self.stats_hint
        elif self._session_flying:
            self.current = "ornithopter"  # the whole session: a carrier cruising level stays one
            self._motion_confirmed = True
        else:
            cruise = self._window_cruise()
            if cruise is not None and cruise < FLYING_MIN_CRUISE_CMS:
                self._cruise_max = max(self._cruise_max, cruise)
                self._cruise_windows += 1
                if self._cruise_since is None:
                    self._cruise_since = self._samples[-1][0]
            moving_s = self._samples[-1][0] - self._cruise_since if self._cruise_since is not None and self._samples else 0.0
            # one session is one vehicle: its fastest sustained speed says which. Buggy or bike
            # speed settles it at once; "sandcrawler" only after CRAWLER_AFTER_S without ever
            # getting faster, so pulling away slowly never flashes it.
            if self._cruise_windows >= 3 and (self._cruise_max > CRAWLER_MAX_CMS or moving_s >= CRAWLER_AFTER_S):
                self.current = self._ground_type(self._cruise_max)
                self._motion_confirmed = True
            elif self.current == "foot":
                # until motion says otherwise, assume the last vehicle (not yet confident)
                self.current = self.last_vehicle
        if self.current != "foot":
            self.last_vehicle = self.current
        if self.current in ("buggy", "sandcrawler") and self.confident:
            self.last_harvester = self.current  # only once identified, never from a boarding guess
        return self.current != before
