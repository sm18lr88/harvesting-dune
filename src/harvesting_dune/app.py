# Detector: the composition root that owns detector state and wires its parts together.
"""Detector core: bus messages -> observations/terrain/vehicle -> area recommendations.

A plain state machine (no I/O apart from its own data files); runner.py wires it to the
companion pages, hotkeys and the settings page. Its behaviour is split by reason to change:

    ingest.py      bus messages in (session, position, actors, mined nodes)
    controls.py    user actions and the minimap filter
    ranking.py     candidates, ranking, hotspot, zones
    outputs.py     what the companion shows (the bridge contract)
    statuspage.py  what the settings page shows (the viewer contract)

Each data file has one writer: settings.py, learning.py, mined.py, worldfiles.py, journal.py,
corrections.py (stale scan rows) and loot.py.
"""

from __future__ import annotations

import asyncio
import collections
import logging
from pathlib import Path

from . import settings
from .areas import Area
from .catalog import BY_KEY, MINIMAP_RESOURCES
from .characters import CharacterMixin, CharacterState
from .controls import ControlsMixin
from .corrections import ScanIndex
from .farm import FarmMixin
from .gate import GateMixin
from .ingest import IngestMixin
from .itemsources import ItemSources
from .journal import Journal
from .learning import Learning
from .loot import LootIndex
from .mined import MinedNodes
from .ships import DownedShips, parse_wrecks
from .model import ActorParser
from .outputs import OutputsMixin
from .peers import PeerMixin, PeerStore
from .ranking import RankingMixin
from .statuspage import StatusPageMixin
from .store import ObservationStore
from .terrain import VEHICLES, TerrainGrid
from .vehicle import VEHICLE_CHOICES, VehicleDetector
from .worldfiles import TerrainFiles, blows_path, load_blows, load_ranges, save_ranges
from .zones import DEFAULT_LAST_PVE_ROW, DEFAULT_LINES_FROM_ROW, DEFAULT_PVP_YIELD, GridFit

__all__ = ["VEHICLE_CHOICES", "Detector"]

log = logging.getLogger(__name__)

SCAN_REFRESH_MIN_S = 60.0


class Detector(IngestMixin, ControlsMixin, RankingMixin, OutputsMixin, StatusPageMixin, PeerMixin, CharacterMixin, FarmMixin,
               GateMixin):
    def __init__(self, journal_dir: Path | None, data_dir: Path | None = None, min_rescore_s: float = 1.5) -> None:
        self.data_dir = data_dir
        self.settings_path = (data_dir / "settings.json") if data_dir else None
        self.store = ObservationStore()
        self.journal = Journal(journal_dir)
        self.learning = Learning(data_dir / "learned.json" if data_dir else None)
        self.mined_nodes = MinedNodes(data_dir)
        # Terrain and base are per world (map + Coriolis seed): Arrakeen, Hagga Basin,
        # the Deep Desert and each Deep Desert layout have unrelated coordinates.
        self.terrain = TerrainFiles(data_dir)
        self._bases: dict[tuple | None, tuple[float, float]] = {}
        self.vehicles = VehicleDetector()
        self.scan = ScanIndex(data_dir, ttl_for=self.respawn_for)
        self.loot = LootIndex(data_dir)
        self.ships = DownedShips()  # crashed ships seen on this map, until a worm takes them
        self.shipwrecks: dict[tuple, list[tuple[str, float, float, float]]] = {}  # world -> named wrecks (name, x, y, z)
        self.map_files: dict[tuple, str] = {}  # world -> the companion's map file ("deepdesert_1_01": the layout)
        # feed state
        self._session_world: tuple | None = None
        self.world_name: str | None = None
        self.activity: str | None = None
        self.character_state: str | None = None
        self.map_scan_seed: int | None = None
        self.identity: dict = {}  # dune.trackedSession: characterName, characterId, battlegroupId
        self.in_game: bool | None = None  # dune.status hasGameSession; None until it says
        self.connected = False
        self.clock = 0.0
        self._actor_parser = ActorParser()
        self._last_pose_t: float | None = None
        self._observed_at = 0.0
        self._last_actor_count = 0
        self._last_mine: dict[str, tuple[float, float, float]] = {}  # marker -> (t, x, y)
        self.blows: dict[str, dict] = {}  # site key -> {x, y, z, size, t}: spice blows seen this cycle
        self._blows_world: tuple | None = None
        self.items = ItemSources(data_dir)  # looted materials: where each drops
        self.looted: dict[str, float] = {}  # cage key -> when it vanished right next to you
        self.events: collections.deque[tuple[float, str]] = collections.deque(maxlen=20)
        self.notice: tuple[float, str] | None = None  # "area mined out -> next" for the badge
        self.ship_alert: tuple[float, str] | None = None  # "Downed ship! 1.2 km away", for any resource
        # ranking state
        self.areas: list[Area] = []
        self.hotspot: dict | None = None  # the grid cell where the searched resource is richest
        self._known_nodes = 0  # candidate nodes for the selected resource at the last ranking
        self.score_ms: float | None = None
        self.min_rescore_s = min_rescore_s
        self._scored_at = 0.0
        self._scored_key: tuple | None = None
        self._dirty = True
        self._last_published: str | None = None
        # output state
        self.publish_rev = 0  # bumps whenever what the companion should show changes
        self.minimap_rev = 0
        self.listeners: set[asyncio.Queue] = set()  # open settings pages
        self.settings_url = "http://127.0.0.1:8765/"  # the CLI sets the real port
        self.lean = False  # set by the governor when memory is short
        self._scan_refreshed_at = -1e18
        # settings (settings.py reads and writes them). With lines on the minimap always shows
        # exactly the searched resource (a rule, not an option); a fresh install starts that way.
        self.resource_key = "spice"
        self.enabled = True
        self.minimap_hidden: set[str] = {r.key for r in MINIMAP_RESOURCES if r.key != "spice"}
        self.minimap_extras: set[str] = set()  # minimap-only extras you ticked; everything else hidden
        self.lines_enabled = True
        self.last_pve_row = DEFAULT_LAST_PVE_ROW
        self.lines_from_row = DEFAULT_LINES_FROM_ROW  # Deep Desert: lines appear once you reach this row
        self.pvp_yield = DEFAULT_PVP_YIELD
        self.pvp_only = True  # Deep Desert: never direct you into PvE rows (reduced yields)
        self.max_km = 0.0  # lines only to areas within this straight-line distance; 0 = no limit
        # guild sync (peers.py)
        self.sync_key: str | None = None
        self.sync_enabled = True
        self.sync_positions = True  # share my position: on by default, each player can switch it off
        self.sync_hidden_members: set[int] = set()  # guildmates (character ids) not drawn on my minimap
        self.share_character = True  # characters.py: my profile goes to the guild
        self.share_session = True  # ...with this session's XP, dailies and loot
        self.peers = PeerStore()
        self.chars = CharacterState()
        self._farm_setup()  # farm.py: routes, the run (off by default), stop done-times
        self._gate_setup()  # gate.py: off here; the live run requires a checked key
        self.sync_wake = lambda: None  # the running sync loop replaces it: "sync now"
        settings.load(self)
        self._follow_single_visible()  # lines on and one resource shown: search for exactly that
        self.store.saved_ranges = load_ranges(self._ranges_path())

    # -- per-world state -------------------------------------------------------------------
    @property
    def grid(self) -> TerrainGrid:
        return self.terrain.grid(self.store.world)

    def save_terrain(self, now: float, force: bool = False) -> int:
        return self.terrain.save(now, force)

    @property
    def base(self) -> tuple[float, float] | None:
        return self._bases.get(self.store.world)

    @property
    def resource(self):
        # a looted material (item:<id>) resolves through the item index; an unknown key (index not
        # built yet) falls back to spice so nothing downstream ever sees a missing resource
        return BY_KEY.get(self.resource_key) or self.items.resource(self.resource_key) or BY_KEY["spice"]

    @property
    def plan_vehicle(self) -> str:
        v = self.vehicles.plan_vehicle
        # scouting spice from an ornithopter/sandbike: plan the trip for the harvesting vehicle
        if self.resource.harvest == "drive_over" and not VEHICLES[v].harvests_spice and not self.vehicles.override:
            return self.vehicles.last_harvester
        return v

    @property
    def journal_path(self) -> str | None:
        return self.journal.path

    def _changed(self) -> None:
        self._dirty = True
        self.publish_rev += 1

    changed = _changed  # for the governor: something outside the feed changed what to show

    def _enter_world(self, world: tuple) -> None:
        if self.store.set_world(*world):
            log.info("world changed to %s", world)
            self.ships.reset()
            self.areas = []
            self.hotspot = None
            self._last_pose_t = None
            self._scored_key = None
            self.publish_rev += 1
            self.sync_wake()  # the guild sees us in the new world at once
        self.scan.set_world(world)
        if not self.blows or self._blows_world != world:
            self._blows_world = world
            self.blows = load_blows(blows_path(self.data_dir, world))
        self.mined_nodes.set_world(world)
        self.farm_done.set_world(world)
        self._dirty = True

    def _ranges_path(self) -> Path | None:
        return self.data_dir / "ranges.json" if self.data_dir else None

    def save_ranges(self) -> None:
        save_ranges(self._ranges_path(), self.store)

    # -- learned values ----------------------------------------------------------------------
    def respawn_for(self, marker: str) -> float:
        return self.learning.respawn_for(marker)

    def node_seconds(self, markers: set[str]) -> float:
        return self.learning.node_seconds(markers)

    def grid_fit(self, world_id: str | None = None) -> GridFit | None:
        return self.learning.grid_fit(world_id or (self.store.world[0] if self.store.world else None))

    def grid_fit_wanted(self) -> str | None:
        """World id to fit from the companion's markers (once per run and map), or None."""
        world_id = self.store.world[0] if self.store.world else None
        return world_id if self.learning.grid_fit_wanted(world_id) else None

    def on_grid_samples(self, world_id: str, samples: list, fit: GridFit | None = None) -> bool:
        changed = self.learning.adopt_grid(world_id, samples, fit)
        if changed:
            self._changed()
        return changed is not None

    def shipwrecks_wanted(self) -> bool:
        return bool(self.store.world and self.store.world[0]) and self.store.world not in self.shipwrecks

    def on_shipwrecks(self, rows: object) -> bool:
        """Named shipwrecks read from the companion's map data (untrusted page data: checked)."""
        world = self.store.world
        wrecks = parse_wrecks(rows, world[0] if world else None)
        if wrecks is None or not world:
            return False  # another map's file (or junk): try the next page, or again next minute
        self.shipwrecks[world] = wrecks
        self.map_files[world] = rows["map"]  # checked by parse_wrecks: this world's map file
        log.info("map %s: %s named shipwrecks", rows["map"], len(wrecks))
        self._changed()
        return True

    # -- nodes you mined -------------------------------------------------------------------------
    @property
    def mined(self) -> dict[str, dict]:
        return self.mined_nodes.items

    def mined_near(self, x: float, y: float, markers: set[str]) -> int:
        return self.mined_nodes.near(x, y, markers)

    def expire_mined(self, now: float) -> int:
        return self.mined_nodes.expire(now, self.respawn_for)

    # -- scan database ----------------------------------------------------------------------
    def scan_request(self) -> tuple[str, int] | None:
        """(map id, seed) to fetch from the scan database, or None if not ready."""
        world = self.store.world
        if world is None or world[0] is None or self.map_scan_seed in (None, 0):
            return None
        return (self.scan.map_id or world[0], self.map_scan_seed)

    def on_scan_world(self, map_id: str | None) -> None:
        if map_id and map_id != self.scan.map_id:
            self.scan.map_id = map_id
            self.scan.fetched_at = None  # refetch under the page's own map id

    def load_scan(self, rows: list[dict], status: int, now: float) -> None:
        self.scan.load_rows(rows, status, now)
        log.info("scan database: %s usable rows (HTTP %s)", len(self.scan.rows), status)
        self._changed()

    def scan_refresh_due(self, now: float) -> bool:
        """Time to make the companion reload its scan layer without newly confirmed stale rows."""
        return self.scan.refresh_pending and now - self._scan_refreshed_at >= SCAN_REFRESH_MIN_S

    def scan_refreshed(self, now: float) -> None:
        self._scan_refreshed_at = now
        self.scan.mark_refreshed()
        self.publish_rev += 1  # overlay markers for those rows are no longer needed
