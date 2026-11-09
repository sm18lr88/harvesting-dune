# harvesting_dune package

Earned its own file: 32 files, ~120 top-level symbols, `Detector` referenced 100+ times; this is where every rule lives.

## OVERVIEW
Flat package: one `Detector` state machine (no I/O except its own data files) plus transport, persistence and Win32 helpers around it.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Detector state / wiring | `app.py` | `class Detector(IngestMixin, ControlsMixin, RankingMixin, OutputsMixin, StatusPageMixin, PeerMixin, CharacterMixin, FarmMixin, GateMixin)` |
| Key gate (no checked key -> nothing drawn, ranked or fetched) | `gate.py` (`GateMixin`, `gate_reason`, `GRACE_S` 72 h, `on_dataset`) | `gate_required` is set only by `cli run`; a 401 -> `key_refused` clears the map; `snapshot()['gate']` -> `viewer.html` key-only view |
| Server datasets (routes, regions) | `dataloop.py` (when: start + daily, ETag/`If-None-Match`) -> `sync.SyncClient.fetch` -> `datacache.py` (files) -> `det.on_dataset` | runner side only; the Detector gets parsed data; builders `scripts/build_routes.py`, `scripts/build_regions.py` (private) |
| New bus topic or payload field | `ingest.py`, `model.py` | `IngestMixin.handle` -> `_handle` routes by topic |
| Settings / picker / hotkey action | `controls.py` | also the minimap filter |
| Candidates, ranking, hotspot | `ranking.py` -> `areas.recommend` | ranking runs off the event loop (worker thread) |
| Terrain, vehicles, route search | `terrain.py` (`TerrainGrid`, `VEHICLES`) | A* with best-first pruning |
| Vehicle detection from motion | `vehicle.py` | speed bands / flight evidence |
| Map grid, PvE/PvP rows | `zones.py` (`fit_grid`, `GridFit`) | |
| Actor lifecycle across snapshots | `store.py` (`ObservationStore`, `Status`) | |
| Resource catalog, respawn defaults | `catalog.py` | `NEVER_TARGET`, `marker_of`, `resource_of` |
| Looted materials, rare cages | `loot.py`, `itemsources.py`, `loot_crawl.js` | "Fallen Shipwreck" rows sit at (0,0,0): never a place, put on seen ships |
| Downed ships (alert, memory, gone/emptied) | `ships.py` (`DownedShips`, `parse_wrecks`) | matched by position: a reloaded ship gets a new actor id |
| Search variants (vehicle wrecks, shipwrecks) | `catalog.Resource.variant_of` / `actor_types` / `tour` | `MINIMAP_RESOURCES` = own tick box; `tour` = nearest-first route stops; `ranking._tour_kept` keeps the current stop unless another is 15 % nearer (`SWITCH_MARGIN`) |
| Named shipwrecks | `bridge.js shipwrecksFromData` -> `runner.load_shipwrecks` -> `app.on_shipwrecks` | from the companion's map data file |
| Chance finds (unique gear, schematics) | `loot_crawl.js` `uniques` -> `itemsources` (`CHANCE_GROUP`, `elsewhere`), `Resource.chance` -> `ranking` (order by `Area.find_chance`), `outputs.chance_marks` -> `bridge.js` rings/dashes/pulse | no cyan route for chance items |
| Farm route mode (ordered sites and stops) | `routes.py` (`parse_routes`, `FarmRun`, done times), `farm.py` (mixin: actions, marks, text), the server's `routes` dataset (none without it; tests: `helpers.datasets`) | off by default; never changes the ranking; frozen order; `route-stop` rings carry `num`; skip = Ctrl+Alt+N |
| Minimap on/off hotkey (Ctrl+Alt+M) | `hotkeys.py` -> `runner.toggle_minimap` -> `bridge.js dd.toggleMinimap` | sends the companion's own `overlay.setWidget*` commands for `dune.minimap`; reads `dune.overlayWidgets` |
| Problem reports | `logsetup.PROBLEMS` -> `statuspage` (`problems`, `about`), `outputs.problem_notice` -> `bridge.js renderProblem` | ERROR+ only |
| Companion transport (DevTools) | `feed.py` (`CompanionHub`, `Page`, `to_bus`) | injects `bridge.js` |
| Live loops, supervision | `runner.py` | failed loops are logged and restarted |
| Settings page server | `server.py`, `viewer.html` | 127.0.0.1:8765 only |
| Guild sync state, publish/receive | `peers.py`, `peerview.py` | in memory only; never ranking nodes, never `store.py`; `peerview._shows_peer_ring`: no ring for a row only our key reported, nor for ore our companion shows live, saw gone or we mined |
| Shared overlay objects (pins) | `objects.py` (`ObjectsMixin`, a base of `PeerMixin`) -> `bridge.js RENDER` | memory only; never `dest`, `route` or `extraActors` |
| Guild sync transport (HTTPS, gzip) | `sync.py` | private CA `sync-ca.pem`; records travel only with their partition |
| Guild sync cadence, wake-ups, backoff | `syncloop.py` -> `runner.sync_loop` | 15/60/180 s by state; `det.sync_wake()` syncs at once; WARNING at most |
| Guild characters (profile, switches, Characters view) | `profile.py` (reduce topics), `characters.py` (mixin), `server.py` `GET /character`, `viewer.html` `sec-chars` | never currencies, session history or bases; no Dune inventory topic exists |
| Guild key entry, shared finds | `server.py` `/sync/key`; `viewer.html` `sec-guild` | the key never enters `snapshot()` or a log |
| CPU priority, EcoQoS, lean mode | `governor.py` | ctypes Win32 |

## CONTRACTS (change both sides together)
- `outputs.py` <-> `bridge.js`: `page_state()` keys `dest`, `route`, `alternatives`, `stale`, `extraActors`, `problem`, `objects` (documented in the `outputs.py` docstring).
- `statuspage.py` <-> `viewer.html`: `snapshot()` keys are **only ever added**, never renamed or removed; old open tabs must keep working until they reload.
- `snapshot()` key `peers` (guild sync status, roster, shared finds; key shown only as set or not); key `characters` (switches and rows only; details via `GET /character?id=`); `page_state()` `alternatives` kinds `peer` / `member` are rings only.
- `model.py` parsers <-> companion payloads (`dune.session`, `dune.player`, `dune.map.actors`).

## DATA FILES: ONE WRITER EACH (under `data\`)
Objects (pins) are memory-only: `objects.py` writes no file, so this list is unchanged.
| File | Sole writer |
|------|-------------|
| `settings.json` | `settings.py` |
| `learned.json` | `learning.py` |
| `mined-<map>-<seed>.json` | `mined.py` |
| `terrain-*`, `blows-*`, `ranges.json` | `worldfiles.py` |
| `corrections-*`, `scan-*` | `corrections.py` |
| `journals\*.jsonl` | `journal.py` |
| loot index / `loot-rare.json` | `loot.py` |
| `route-done-<map>-<seed>.json` | `routes.py` |
| `datasets\index.json`, `datasets\<name>.json` | `datacache.py` |

Guild sync writes no file; the key lives in `settings.json` (settings.py). The dataset index keeps only the key id (public part), never the secret.

Per-world files are keyed by map + Coriolis seed; never share coordinates across worlds.

## CONVENTIONS
- Every module opens with `# <Name>: <one-line role>` then a docstring explaining the why; keep both when adding modules.
- New behaviour goes into the mixin matching its reason to change, not into `app.py`.
- Win32 calls go through `ctypes` with a `sys.platform` guard (see `hotkeys.py`); no extra runtime deps beyond `websockets`.

## ANTI-PATTERNS
- Plain `write_text`/`json.dump` for persisted state: use `fsutil.write_json` / `try_write_json` (atomic replace; the process gets hard-killed).
- Raising on a bad file or payload: `read_json` ignores bad shapes with a warning; ingest must never take the detector down on one odd message.
- Treating an actor missing from a snapshot as depleted: only `missing_in_range` well inside the learned load range counts (`store.py`).
- Ranking on the event loop thread.
- Making `spicefieldsmall` or consumables targets (`catalog.NEVER_TARGET`).
- Settings code that un-hides markers or turns lines off on its own (`settings.py`).
- Bridge code that can throw into the companion's own call path: wrap hooks in `try { ... } catch (err) { report(...) }`.
- Ships and items ageing on wall time in ranking: use the feed clock (`self.clock`), or replays expire them.
- Shipping server-held research in the package (routes, region names): it goes in a dataset built by `scripts/build_*.py`; the module keeps only a neutral public fallback.
- Drawing, ranking or fetching a dataset while `gate_open()` is False; tests that import `gate.GRACE_S` instead of stating 72 h follow a wrong value.
