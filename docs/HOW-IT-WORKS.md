# How harvesting-dune works

This document explains the design of harvesting-dune. For installation and use, read the [README](../README.md).

## Data flow

The information in this table comes from the companion's code and from live data.

| Step | What happens |
|---|---|
| Game → companion | gaming.tools reads the game through its kernel driver `GTDriver`. The game uses BattlEye. The driver reads the object graph of Unreal with offsets that it finds at runtime (`MapActorsReader`, `SpiceFieldMarkerReader`, …). **harvesting-dune never touches the game.** |
| Companion → its pages | The companion sends JSON topics to its pages over WebView2 `chrome.webview`. The topics include `dune.session`, `dune.player` and `dune.map.actors`. `dune.map.actors` contains each resource node, each spice spawn point with `spawned` and `fillRate` 0–1, worm breaches and loot. |
| The bridge | The detector injects `bridge.js` into the companion's pages over the localhost DevTools port. The bridge reads those topics. It sends synthetic `dune.trackedDestination` and `dune.map.route` messages through the page's own listeners. Thus, the companion draws them as its own markers. |
| Map data | The page loads the map file `maps/<map>.d.json`. Each location has its grid cell, and the named shipwrecks (`iconId` `shipwreck`) have their names. The bridge reads this file for the map grid and for the shipwrecks. |
| Scan database | `dune-api-v2.gaming.tools/actors?world&seed` holds crowd scans. Each row has the key `type:x:y`. The page loads it once for each map open and never removes old rows. The detector fetches it through the page's own session. |
| Stale correction | A scan row becomes **confirmed stale** only in one condition. You must be within the detection range. Then, for at least 3 live updates and at least 6 s, no actor is at the row, or the actor reports `spawned:false`. The page's scan fetch then drops the row, and an overlay shows "not spawned". A respawn cancels the correction. The detector saves corrections per map and Coriolis seed. They stay on your PC. The detector uploads nothing. |

**Is premium Map Scan the same thing?** No. Map Scan shows that crowd database. The page loads it once for each map open and never removes a row. A harvested spice field leaves no actor, so its stale marker stays. harvesting-dune removes those markers.

## Module map

The detector is one Python process. Each module has one reason to change. Each data file has exactly one module that writes it.

| Module | Responsibility |
|---|---|
| `app.py` | The `Detector` class. It owns the detector state and connects the parts below. |
| `ingest.py` | Bus messages in: session, position, actors, spice blows, mined nodes. |
| `controls.py` | Actions from the settings page, the picker and the hotkeys. The minimap filter. |
| `ranking.py` | Candidate nodes, ranking (routing runs in a worker thread), the hotspot, zones. |
| `outputs.py` | What the companion shows. This is the contract with `bridge.js`. |
| `statuspage.py` | What the settings page shows. This is the contract with `viewer.html`. |
| `areas.py`, `terrain.py` | Area clusters, session scoring, terrain and route search. |
| `vehicle.py` | Vehicle detection. |
| `ships.py` | Downed ships: alert, memory, gone and emptied. |
| `routes.py`, `farm.py` | Farm routes: the `routes` dataset, the run you are on, and its page marks and text. `routes.py` writes `data\route-done-<map>-<seed>.json`. |
| `gate.py` | The key gate: the detector works only with a guild key that the server accepted in the last 72 h. It writes no file. |
| `datacache.py` | Writes `data\datasets\` (the server's datasets, their ETags and the last key check). |
| `dataloop.py` | When to ask the server for its datasets: on start, then at most once a day. |
| `zones.py` | The map grid and the PvE and PvP rows. |
| `settings.py` | Writes `data\settings.json`. |
| `learning.py` | Writes `data\learned.json`. |
| `mined.py` | Writes `data\mined-<map>-<seed>.json`. |
| `worldfiles.py` | Writes `data\terrain-<map>-<seed>.json`, `data\blows-<map>-<seed>.json` and `data\ranges.json`. |
| `corrections.py` | Writes `data\corrections-<map>-<seed>.json` and `data\scan-<map>-<seed>.json`. |
| `journal.py` | Writes `data\journals\*.jsonl`. |
| `peers.py` | Guild sync state: what the tool publishes and what the guild shared. It writes no file. |
| `sync.py` | The HTTPS link to the guild server: one request, gzip both ways. It also makes the dataset GETs. |
| `syncloop.py` | When to talk to the guild server: the cadence, wake-ups, retries and backoff. |
| `profile.py`, `characters.py` | Your character profile for the guild, and the guild's profiles for the Characters section. They write no file. |
| `feed.py`, `runner.py`, `server.py` | The transport to the companion, the live loops, and the settings page server. |

The guild server lives in `server/guildsync/`. It never imports the detector package. `storage.py` keeps keys, members, sightings and character profiles in one SQLite file. `httpd.py` serves HTTPS and limits the request rate. `api.py` checks each request and builds the answer. `keysctl.py` issues, lists and revokes guild keys. The admin runs it over SSH. There is no admin web endpoint. See [GUILD-SYNC-ADMIN.md](GUILD-SYNC-ADMIN.md).

## Recommendation logic

1. The nodes of the selected resource come from three sources:
   - live actors (certain),
   - actors that you saw earlier,
   - scan rows (likely). Spice rows are the least likely.

   The tool ignores spice spawn points that are not active.
2. The tool groups the nodes into areas of approximately 500 m.
3. Each vehicle must pass a **feasibility gate** on the learned terrain:
   - For spice, the vehicle must harvest spice and must drive onto the field.
   - For all other resources, the vehicle needs a parking spot within 150 m that it can drive to. The walk and the climb count as time.
   - Ornithopters ignore the terrain.

   The tool **excludes** a rich area on a mountain that the vehicle cannot reach. It does not only give it a lower score.
4. The tool ranks the areas:
   - **Spice:** value per minute of the complete trip (out, harvest, back to base). The harvester load limits the value.
   - **Nodes that you mine by hand:** the quantity that you can mine in a 30 min session, which includes the trip out and back. A **patch** is the area plus all areas within 800 m, because you can walk between them. Neighbour areas count 0.8 times. Each second of travel also costs a small penalty. A dense island keeps you busy for the complete session. A moderate spot becomes empty after a few minutes. Thus, the island wins, even if it is farther away.

   The routing uses exact A* search with best-first pruning, off the main loop. The time depends on the terrain. In the Deep Desert it is a few milliseconds. In Hagga Basin with little mapped terrain it is approximately 1 s. A new best area must be 15% better than the current area to replace it. If only the route search from where you are now fails for the current area (for example in flight, or behind a ridge), the area stays the target: it was reachable a moment ago.

The tool learns the terrain on 50 m cells from these sources:

- the tracks of your ground vehicle, but only after the tool identified the vehicle with confidence,
- your height on foot,
- the height of each actor that the tool sees. Ore on rock outcrops shows mountains that you did not visit.

The tool keeps the terrain per map and per Deep Desert layout.

## Ships, wrecks and parts

| Kind | Where the tool finds it |
|---|---|
| **Downed ship** | Live actor `BP_SmallShipwreck_01_C`, marker `SalvageSteel`. Its three containers are `BP_SmallWreck_LootContainer_*`. The game loads the hull from approximately 0.5 to 0.9 km away. A ship that loads again gets a new actor id, so the tool matches ships by position (100 m). |
| **Named shipwreck** | The companion's map data (see Data flow). The bridge sends the map id of the file with the wrecks. The tool uses them only when that map is the map you are on. |
| **Buggy or ornithopter wreck** | Actors and scan rows of type `BP_Buggy_GRD_Encounter_Spawner_C` and `BP_Ornithopter_GRD_Encounter_Spawner_C`. They use the salvaged-metal marker, so this search is a variant of Salvaged Metal that counts only these types. |

- **Downed ships:** an alert on the badge for 90 s. The ship stays a target for 30 min (estimate) after the game unloads it. It is gone when you are within 400 m and the game does not show it. It is emptied when you opened all three of its containers. The tool counts the containers by place, because a container that loads again gets a new actor id. The companion always shows downed ships on the minimap; the tool never hides them.
- **One trip, nearest first:** for these single places, the tool does not rank by value. The target is the nearest place, then always the nearest one left from there. The route shows up to 5 stops in that order, and no thin lines.
- **Parts from downed ships:** gaming.tools lists the loot of a downed ship ("Fallen Shipwreck", PvE and PvP variants) at the map origin, because the ship has no fixed place. The tool never uses that position. It puts that loot on each downed ship that it saw. In the Deep Desert, the loot is the same in every weekly layout, so the tool does not wait for the layout to be found.
- **Parts in named shipwrecks:** a container within 200 m of a shipwreck's map marker shows under the name of the wreck on the part's card.

## Chance finds: unique gear and schematics

- **Data:** the loot crawl (`loot_crawl.js`, run in a companion page once a week) reads every item's drop locations from gaming.tools. It keeps materials, and also unique and schematic items with every drop location on every map, plus the map names. For unique gear, the drop locations are on its schematic (`schematic.dropLocations`): the game drops the schematic, and you craft the item. Schematics that a vendor sells (`schematicSoldBy`) and contract rewards (`rewardFrom`) are kept too, without lines. A new cache version makes the next run rebuild the data.
- **Ranking:** for a chance item, the tool orders the places by the chance to find at least one: 1 - (1 - p1) x (1 - p2) x ... over the sources in a place. It does not order them by expected amount per trip.
- **On the minimap:** no route line, because a route looks certain. Each likely place gets a gold ring. Its radius is 40 m plus up to 160 m for a chance of 25 % or more. One dashed gold line points from you to the most likely place. The rings and the line pulse slowly: every 0.9 s they become fainter and the rings wider, then back.
- **Other regions:** the item card shows the best chance in each other region. All Deep Desert layouts count as one region.

## Farm routes

- **Data:** the routes do not ship with the tool. The guild server holds them as the `routes` dataset (see "Key gate and datasets"). Without it there are no routes. `scripts\build_routes.py` builds the dataset file from research rows (`uv run python scripts\build_routes.py <runs-research.json> [<folder>]`, default `dist\datasets`). Every position comes from the gaming.tools item data or the companion's map markers. The script never makes one up: a place without a position is left out. Deep Desert places that move every week keep only their item ids. The tool reads this week's containers from the item data for the current layout.
- **Checks:** the tool checks each row when it loads the dataset. It skips a bad row with a warning.
- **Off by default:** a route is a separate target, like chance finds. The ranking keeps running and does not change. While a route runs, the companion shows the route instead of the ranked target.
- **Wrong map:** the badge and the target card say where to go. There are no lines.
- **Right map:** the line leads to the site's entrance. No entrance is known yet, so it leads to the nearest stop of the site.
- **At the site** (within 150 m of a stop): the stop order is fixed once. It is the sourced order, or else nearest-next from where you entered the site. It never changes while you walk. One line leads to the next stop only. Rings with numbers (drawn as line strokes) show the next 3 stops. Dim rings show the done stops.
- **Done stop:** you came within 6 m, and the companion shows its container emptied (`hasLoot` false). In the recorded journals, an emptied container stays on the map with `hasLoot` false; it does not vanish. `Ctrl+Alt+N` skips a stop. A skipped stop is not saved.
- **Done times** are saved per map and Coriolis seed. After a restart, the tool does not send you to a chest you emptied within the reset time.
- **Site done:** the card shows for example `Wreck of the Ourea done: next Wreck of the Leto (1.2 km) / reset time unknown`. The tool goes on to the next site. With **Repeat this site** it waits for the reset and starts the same order again. When a route is done, it goes back to the first site whose reset has passed, or it waits.
- **Reset:** only Testing Stations have a source (45 min, one player report). Elsewhere the reset is unknown, and the tool uses its own one-hour guess for waits.
- **End:** Stop route, or 10 minutes outside the route's maps.

## Key gate and datasets

The tool works only with a guild key. The server decides if a key is good. The gate is the data that the server keeps back, because a public program cannot be forced to check anything.

- **No key, a rejected key or an expired key:** the tool draws nothing on the companion's map. There is no line, no ring, no badge and no picker. It ranks nothing and fetches no dataset. The settings page shows only the key field and one line that says why. The companion itself keeps working as usual.
- **A valid key:** everything works. The tool uses the datasets that it has on disk.
- **The check:** the dataset list (`GET /v1/data`) is the key check. The tool asks for it on start, and then at most once a day. A failed check is tried again after 1, 5, 15, 30 and 60 minutes. A sync that the server accepts also counts as a check.
- **Offline grace:** if the server cannot be reached, the tool keeps working for 72 h after the last good check. After that, it stops like "no key". The settings page says "Server unreachable for N h". The last check is saved in `data\datasets\index.json` (at most once an hour), so a restart keeps the grace.
- **A 401** (the key is revoked or expired) stops the tool at once. The map clears on the next push (within 2 s). The tool asks nothing more until you save a new key.
- **Datasets:** `routes` (farm routes) and `regions` (names for the instance maps that gaming.tools does not name). The tool fetches a dataset only when its ETag in the list differs from the saved copy, and then sends `If-None-Match`. So a normal day costs one small request. `datacache.py` saves each dataset in `data\datasets\<name>.json`.
- **Revoked keys keep old data:** data that the tool already downloaded stays on that PC. A revoked key gets no new data and no guild sync.
- **Builders:** `scripts\build_routes.py` and `scripts\build_regions.py` write the dataset files. The admin puts them in the server's datasets folder. They are private: they stay out of the public repository.
- **What stays public:** the resource catalog and its marker names, the respawn defaults from the patch notes, the downed-ship thresholds, and the default Deep Desert grid. The tool needs them to read the companion, or anybody can get them again from the companion or the patch notes.

## Vehicle detection

The game field `dune.session.activity == "Driving"` tells the tool *if* you are in a vehicle. The game never tells *which* vehicle. Thus, the tool uses your motion.

One driving session is one vehicle, because you cannot change vehicles without getting out.

- Any evidence of flight makes the complete session an ornithopter, carriers included. This is the evidence of flight:
  - a take-off climb (40 m within 5 s),
  - hovering,
  - a speed above approximately 126 km/h,
  - a position far above the ground around you (80 m).
- Without evidence of flight, the fastest sustained speed of the session gives the vehicle. Up to approximately 41 km/h, it is a sandcrawler. Above approximately 90 km/h, it is a sandbike. Between these speeds, it is a buggy.
- The tool shows "sandcrawler" only after 60 s without buggy speed. A buggy that you use to mine between nodes often moves slowly for more than 15 s.

A short, slow drive looks the same as a sandcrawler. In that case, select the vehicle on the settings page to override the detection.

## Lines on the minimap

- **Deep Desert start row:** the setting "Lines start in row" (default A, the entrance) hides the lines until your position is in that row or a row further north. The destination marker stays.
- **Main line:** the companion's own cyan route line, from you to the best target. The bridge sends it again after approximately 30 m of movement, because the companion never draws it again as you move.
- **Alternative lines:** thin and fainter, up to 4.
  - For spice: only fields that the tool confirmed as active, never unconfirmed scan rows. The color shows the size: large is faint cyan, **medium is amber**, small is faint white. A fresh blow is magenta. A field that somebody harvests is fainter.
  - For other resources: the next best patches in ranking order, one line per patch. An area that only the scan database knows gets a fainter line than an area that you saw live.
  - The lines show even when there is no main target, for example when no active large field is known yet.
- **How the bridge draws the lines:** the companion's map engine (gtmap) sets `this._createdLayers = []` in its constructor. A setter that the bridge installs at page start catches that. Thus, the bridge gets the minimap's map instance and draws into its own `createStaticLayer()`, with the same renderer and zoom. The bridge **calibrates** the transform from world to map against the page's own markers (each marker stores its world point and its map point). The route line is the fallback. Thus, the bridge assumes nothing about the orientation.

## Spice in the Deep Desert

- **Spice blows:** the game sends all spice spawn points within its load range (approximately 4.2 km, learned), also the empty ones (`spawned:false`). An empty point that becomes active is a blow. The badge shows `Spice blow! <size> field <n> km away` for 90 s, and the ranking runs again immediately. Blows of small fields get no message. The game sends nothing from beyond the load range. Thus, the tool cannot detect blows there. The companion also reads no explosion actor.
- **Remembered fields:** active fields that you saw stay on the companion map for 45 min after they leave the load range. The tool draws them from your observation and marks them as not reportable.
- **Empty sites are never targets:** the game sends every spice site, active or not. A scan row can be on a site that the game reports as `spawned:false`. In that case, the tool drops the row as a target immediately and removes it from the map. This needs no learned range. The tool also hides empty sites on both maps, because the companion draws them like fields.
- **Removing stale fields:** after the tool confirms rows as stale, the page reloads its scan layer without those rows, at most once a minute. Thus, they really disappear. Until then, a "not spawned" overlay covers them.

## What the tool learns from your play

The tool saves these values in `data\learned.json`. Each value is used after it has 5 samples. Until then, a default applies.

- **Respawn time per resource:** the time that nodes you mined needed to appear again. The tool uses the lower quartile, because the tool sees each node only when it is already back. The default is 45 min for Titanium and Stravidium, and 30 min for the other resources.
- **Your mining pace per resource:** the seconds between nodes that you or your group take one after the other near you. It sets how much fits in a 30 min session. The tool uses the median of the last 8 samples. Thus, the pace changes within a few nodes when the conditions change, for example when a second player mines with you. The file keeps the last 50 samples as history. The default is 25 s per node.
- **Map grids:** the tool fits the grid of each map at each start from the companion's own map data. Each location in that data has its grid cell. A new fit replaces the stored grid only if it puts at least as many of the companion's markers into their own cell.

The weekly Coriolis layouts need no special action. The tool keeps scan data, corrections, mined nodes, terrain and spice blows per map and layout. The rankings and the hotspot come from the live data.

## Guild sync

The wire format is in [SYNC-PROTOCOL.md](SYNC-PROTOCOL.md).

Data flow:

1. The detector sees a strong sighting. The peers mixin puts it in the outbox. One note is kept per class, type and 20 m cell. A live ore, crystal or stone node goes out once. It goes again when it changes, when it was gone and is live again, or 20 min later if it is still there.
2. The "guild sync" loop (`syncloop.py`) sends the outbox. It sends every 15 s while you move or have finds to send, every 60 s while nothing changes, and every 180 s in the menus. Entering a world, a new key or opening the guild lists sends at once. It also sends your name, server and, if shared, position. Bodies over 1 KB travel gzipped.
3. The server stores the rows. It answers with what the guild sent since the last poll.
4. `ingest_peer` merges the answer in memory. The rows become rings on the map and lists on the settings page.

**Partition:** each row carries its battlegroup, world and Coriolis layout. The tool shows a row only in the same world and layout.

**Ages:** the server sends each row's age, not a clock time. The detector turns the age into feed-clock time. Thus, replays and the live feed age rows the same way. Rings fade with age. A guildmate's ring is full while they are online, faded when they are late, and gone when they leave (the server says which; see SYNC-PROTOCOL.md "Presence").

**Failures:** a failed sync waits 30 s, 1 min, 2 min, 5 min, then 10 min. It logs a warning at most once a minute. The rest of the detector keeps running. Finds that did not go out are sent with the next sync, but only in the same world.

**Characters:** the companion's skill, schematic, achievement, Landsraad, stats and session topics are kept in memory (`characters.py`). `profile.py` cuts them down to what the companion's own pages show. The profile goes up only when it changed, at most every 5 min. The guild's profiles come down only while the Characters section is open. They are never part of a sync answer.

What never happens:

- Guild rows never become ranking nodes. They never enter the observation store, the scan index or a file under `data\`.
- A guild "stale" row hides a scan row only with two distinct observers, a distance, and no live actor there. Your own eyes win.
- For a node that respawns, the guild row hides the scan row only until the respawn time of that node has passed since the newest report. Then the scan row shows again, even if the server still keeps the report. Markers that never respawn, like downed ships, stay hidden until the report expires.
- The tool never sends actor ids, currencies, vehicles or a position history. This session's loot goes only in the character profile, and only while you share this session. The key never appears in a log, a page or the snapshot.

## Robustness

- **Saved data survives a hard stop:** the tool writes each file to a temporary file first. Then it replaces the old file in one step. This applies to settings, corrections, terrain, blows, ranges, the loot index, learned values and mined nodes. Thus, a crash during a write never destroys the file. The tool ignores a file with unexpected contents and logs a warning. It does not stop.
- **Terrain and "Set base here" stay** per map and layout (`data\terrain-<map>-<seed>.json`). The tool saves the terrain every minute and at exit.
- **Corrections expire:** a confirmed-stale row of a node stays hidden for the respawn time of that resource. That time is the learned value, or else 45 min for Titanium and Stravidium and 30 min for others. A spice site stays hidden for 3 h. After that, the row shows again. If the node is still absent when you come near, the tool confirms it again.
- **The tool restarts failed background loops:** if one loop fails (feed, scan refresh, loot index, …), the tool logs the error and restarts that loop. The other loops continue to draw lines. If a scan download fails, the tool keeps the rows that it has. It tries again after 30 s, 1 min, 2 min, and so on, up to 10 min.
- **Clean exit** (`--exit-when-companion-gone`): after the companion refused connections for that time, the detector saves the terrain, clears the pages and exits. This also works when a settings page is open.
- **Problems are reported, the companion is not touched:** every error (from every source, also from `bridge.js` in a companion page) goes into a list of the last 20. The settings page shows them with a report to copy. The companion's map window shows a small notice for 30 min after an error. If the detector fails 3 times quickly, the watcher shows a Windows message once per companion session. The code of the bridge catches its own errors, so a failure never reaches the companion's own code.
- **Settings page:** the server answers only `127.0.0.1` and `localhost` and refuses changes from other websites. Each change shows "Saved" or the reason why it failed. A tab that shows an old version reloads itself.

## Logs

The detector writes all warnings and errors to `data\logs\errors.log`. It writes everything to `data\logs\detector.log`. Each log keeps its last 5 files. The limit is 2 MB for each `detector.log` file and 1 MB for each `errors.log` file.

These sources also go into the logs:

- errors in background tasks, in threads and outside of all handlers,
- Python warnings,
- errors in `bridge.js` inside the companion's pages,
- script errors on the settings page,
- errors in the watcher (in `data\autorun.log`).

If the same warning repeats, the detector logs it at most one time each minute. The next message tells how many repeats it did not log.

## Resource use

- The detector runs at below-normal CPU priority with Windows EcoQoS (efficiency cores). The game always gets the CPU first.
- **Lean mode** starts when free system memory is below 1.2 GB (it ends above 2 GB), or when the detector uses more than 400 MB. In lean mode, the detector writes no actor journal, sends smaller overlays, drops its caches and runs the garbage collector.
- The detector forgets actors that it did not see for 20 min. Active spice fields stay for 45 min. The journal folder has a limit of 300 MB. The oldest files go first.
- In menus, on the overland map and in places with nothing to track, the detector checks every 2 s instead of every 0.5 s.
- The detection range for stale confirmation is learned **per actor type** (plants approximately 0.6 km, spice approximately 4.2 km). A safety brake refuses to mark a large part of the rows nearby as stale at the same time.

## Autorun

`scripts\install.ps1` runs `scripts\install-autorun.ps1`. That script does two things:

1. It gives each companion start a DevTools port on the localhost.
2. It registers a hidden logon task. The task starts the detector with the companion and opens the settings page.

When the companion closes, the detector saves its data and exits. If it does not exit within 30 s, the watcher stops it. If the detector fails, the watcher starts it again after 10 s. If it fails again and again, the wait increases to 5 min. The watcher keeps the log of the failed run as `data-run.prev.log`. The log of the watcher is `data\autorun.log`. To remove autorun, use the option `-Uninstall`.

## Run the tool by hand (development)

Use this procedure without autorun, for example when you change the code.

```powershell
uv sync                                                      # includes pytest
powershell -File scripts\start-companion-feed.ps1 -Restart   # companion with a localhost DevTools port
uv run harvesting-dune run                                     # add --resource iron and similar options
uv run pytest                                                # tests (the bridge tests need Node.js)
```

If the detector stops, the pages remove everything that it showed within approximately 20 s.

**WARNING:** While the DevTools port is open, any program on this PC can control the companion's pages. To close the port, restart the companion in the usual way.

## Known limits

- The vehicle speed bands, capacities, harvest rates and spice field yields are estimates. Adjust them with the data in `data\journals`.
- The bridge draws route stops only when the page's map id matches. The detector learns that id from the page's own scan fetch.
- Live actors stop while the companion hides the minimap, for example in combat.
