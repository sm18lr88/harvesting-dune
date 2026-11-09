# harvesting-dune

## Overview

harvesting-dune adds "Where do I harvest?" to the **gaming.tools companion map** for Dune: Awakening.

You select a resource. The tool finds the best **area that you can reach** with your vehicle. The companion's own minimap and map show that area as a destination and a route.

The tool also hides scan-database entries that it confirmed as stale. An example is spice that somebody harvested.

The tool reads only the data that the companion already shows. It never touches the game.

For the design and the technical details, read [How harvesting-dune works](docs/HOW-IT-WORKS.md).

## Requirements

- **Windows 10 or Windows 11**.
- The **Dune companion** from [gaming.tools](https://gaming.tools), installed.
- An internet connection for the install. The installer gets uv and Python by itself.

## Install

The easiest way is the ZIP file. You can also use the installer program or this folder. The result is the same.

### Install from the ZIP file (recommended)

1. On the [Releases page](https://github.com/sm18lr88/harvesting-dune/releases), download `harvesting-dune-<version>.zip`.
2. Right-click the ZIP file and select **Extract All**. Do not open `Install.cmd` inside the ZIP file.
3. In the extracted folder, double-click `Install.cmd`. You do not need administrator rights.
4. Wait. The first install downloads Python. This takes a few minutes.
5. The installer asks for your **guild key**. The key is required. Your guild admin gives it to you. Paste it and press Enter. The key stays hidden. The installer checks the key with the server. If the server cannot be reached, the key is saved and checked when the detector starts.
6. Close the companion (tray icon > Exit) and open it again.

What `Install.cmd` does:

- It copies the tool to `%LOCALAPPDATA%\Programs\harvesting-dune`. After the install, you can delete the ZIP file and the extracted folder.
- It finds uv (the Python package manager). If uv is missing, it installs uv for you. It tries winget, then the uv installer, then a direct download that it checks.
- uv installs its own Python. The installer never uses or changes another Python on your PC.
- It makes the detector start and stop with the companion.
- It writes everything to a log file. The window shows where the log file is.

If something fails, the window stays open. It tells you the reason and how to fix it. Do the fix, then double-click `Install.cmd` again. You can run it as often as you like.

**Update:** download the new ZIP file, extract it, and double-click `Install.cmd`. The new version replaces the old one. Your data and your guild key stay.

**Uninstall:** double-click `Uninstall.cmd` in the extracted folder, or in `%LOCALAPPDATA%\Programs\harvesting-dune`. Your learned data stays in the `data` folder. To remove everything, delete that folder after the uninstall.

### Install with the installer program

1. On the [Releases page](https://github.com/sm18lr88/harvesting-dune/releases), download `harvesting-dune-setup-<version>.exe`.
2. Run the file. You do not need administrator rights.
3. Keep "Start harvesting-dune together with the gaming.tools companion" selected.
4. If the companion is open, also select "Restart the companion now".
5. Wait until the window with the setup steps closes. The first installation downloads Python.

The installer puts the tool in `%LOCALAPPDATA%\Programs\harvesting-dune`. To remove the tool, use "Installed apps" in the Windows settings. The uninstaller removes autorun and the program files. It keeps your learned data in the `data` folder.

If the installer shows an error, read `data\install.log` in the installation folder.

### Install from this folder

1. Get this folder. Use one of these methods:
   - Run `git clone https://github.com/sm18lr88/harvesting-dune`.
   - Download the folder as a ZIP file. Extract it to a permanent location.

   The autorun task runs the tool from this folder. Thus, do not use a temporary folder.
2. Open PowerShell in this folder.
3. Run this command:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -InstallUv
   ```

   - The option `-InstallUv` installs uv with winget if uv is not installed.
   - If the companion is open, add the option `-RestartCompanion`. This option restarts the companion immediately. The DevTools port opens only when the companion starts.
4. Open the gaming.tools companion as usual.

When the companion starts, the detector also starts. The detector opens its settings page at http://127.0.0.1:8765. When you close the companion, the detector stops.

The installer installs nothing for all users of the PC.

## Use

### Where to find the controls

- **Settings page:** http://127.0.0.1:8765 in your web browser. When the companion starts, the page opens in your default browser. If a settings page is already open, no second page opens.
- **Companion map window:** a small picker at the top right: `Find [resource] [Vehicle: auto]`.
- **Minimap overlay in the game:** a one-line status text (the badge), the destination marker and the lines.
  When you hover over the destination marker, it shows the node count, how many nodes are live or only scanned, and the round-trip time.

Every change on the settings page shows "Saved", or a red message that tells you why the change failed.

The settings page has three parts:

- **What are you looking for?** One search for everything: map resources, parts and materials, unique gear and schematics, downed ships and wrecks. Typos are fine. Pick a result to make it your target. Below the search, you can also browse everything by class.
- **Your target:** a card shows what you search, its status, the best place and where it drops. Here you also change the vehicle, turn the recommendations on or off, set your base and block a place.
- **Sections:** the other settings are in sections. Click a section to open it: Minimap & lines, Deep Desert, Ships & wrecks, Rare loot nearby, Spice blows and Diagnostics. A closed section shows a short summary. The page remembers which sections are open.

### Lines on the minimap

- If lines are on, the minimap shows exactly one resource. That resource is the searched resource. If you tick a different resource, the search changes to that resource.
- The **main line** is cyan. It goes from you to the best target.
- Up to 4 **thin lines** go to the next best areas. For more details, read [Lines on the minimap](docs/HOW-IT-WORKS.md#lines-on-the-minimap).
- The tool remembers the searched resource after a restart.
- Your own tracked destination in the game and the Landsraad route mode always have priority.

### The badge

An example of the badge: `Stravidium Mass · thopter · best 1.2 km · 65 nodes · hotspot H5`.

- **best 1.2 km:** the distance to the best area.
- **65 nodes:** the number of nodes in that area. After you mine nodes there, the badge shows the progress, for example `3 left · 2 mined`.
- **hotspot H5:** in the Deep Desert, the grid cell with the most of the searched resource on the complete map. PvP rules apply. The hotspot shows even if the lines cannot reach it yet.
- The target stays the same while you travel to it. It changes only when a new place is clearly better (15 %), or when the target is empty or gone.
- When an area is empty, the badge shows `Area mined out → next 1.2 km`. When the next area is clearly better before that, the badge shows `Move on (2 left here) → …`.
- While you search spice, the badge also shows spice news, for example `Spice blow! medium field 2.3 km away`.

### Hotkeys

| Keys | Action |
|---|---|
| `Ctrl+Alt+→` / `Ctrl+Alt+←` | Select the next or the previous resource. |
| `Ctrl+Alt+↑` | Change the vehicle: auto, sandcrawler, buggy, sandbike, ornithopter. |
| `Ctrl+Alt+↓` | Turn the recommendations on or off. |
| `Ctrl+Alt+L` | Turn the lines on or off. |
| `Ctrl+Alt+M` | Show or hide the companion's minimap, whatever you do: on foot, in combat, in any vehicle, while building. |
| `Ctrl+Alt+N` | Farm route: skip to the next stop. |

The companion's own "Toggle Minimap" key keeps its widget visibility settings, so it cannot show the minimap where those settings hide it. `Ctrl+Alt+M` changes those settings: it shows the minimap in every activity, or turns it off.

To use a mouse button: in Logitech G HUB, select the button, choose **System > Keyboard shortcut** (or **Keystroke**), and press `Ctrl+Alt+M`. Other mouse software works the same way. `Ctrl+Alt+N` (next stop on a farm route) works the same way.

### Farm route

Pick an item on the settings page. If someone farms it on a fixed route, a **Farm route** table lists the routes: world, sites, chests, reset time, confidence and distance. Press **Start** on one.

- In the wrong map, the minimap says where to go, for example `Farm route: travel to Hagga Basin, Wreck of the Ourea`. There are no lines.
- In the right map, the line leads to the site.
- At the site, one line leads to the next chest. Rings with numbers show the next 3 chests. Dim rings show the chests you emptied.
- A chest counts as done when you came within 6 m and the companion shows it emptied. `Ctrl+Alt+N` skips a chest.
- The order never changes while you walk.
- When a site is done, the tool goes on to the next site. **Repeat this site** waits for the reset and then starts the same order again.
- **Stop route** ends it. The route also ends after 10 minutes outside its maps. It is off when the tool starts.
- "data only" means no player guide confirms the route. "order: nearest" means no guide gives an order: the tool goes to the nearest chest next.

### Settings

| Setting | What it does |
|---|---|
| **PvP areas only** (Deep Desert) | On by default. The lines only go into PvP rows, because PvE yields are lower. Far PvP rows also count. The tool estimates them in a straight line. |
| **PvE rows: A to …** | Rows A (south) to D are PvE by default. Rows E to I are PvP. Change the last PvE row if the game map shows a different border. |
| **Lines start in row** (Deep Desert) | Row A by default: the lines show from the entrance. Select a later row, for example E, to see the lines only when you are that many rows into the Deep Desert. Before that row, the destination marker stays and the badge shows `lines from row E`. |
| **PvP yield ×** | 2.5 by default. If "PvP areas only" is off, PvP areas count their yield × this value. |
| **Max distance for lines** (km) | The lines only go to areas within this straight-line distance. 0 means no limit. The hotspot cell always looks at the complete map. |
| **Minimap shows** | The resources on the minimap. The big map does not change. Your other filter choices in the companion (caves, loot, …) stay as they are. |
| **Also on the minimap** | Buried treasure is hidden unless you tick it. It is not searched and gets no lines. |
| **Vehicle** | "auto" detects the vehicle from your motion. Select a vehicle to override the detection. |

The companion keeps a separate filter list for the minimap but has no control for it. Thus, the minimap page reloads for one second to apply a change.

### Guild sync

Guild sync shares what you see with your guild. Your guild admin gives you a guild key.

1. Open the settings page and find the **Guild** section.
2. Paste the key and press **Save** (or Enter).
3. The page shows the result next to the field: checking, connected as your character, key rejected, or server unreachable.

The page never shows the key again. It shows only "key saved", with **Change** and **Remove**.

What the tool shares:

- your character name and your server,
- strong sightings: active spice fields, spice blows, downed ships, live ore, crystal and stone nodes, mined nodes and scan rows that the tool confirmed as stale,
- your position. This is on by default. Turn off **Share my position** to stop it.

What you see from the guild:

- Rings on the map. Green is spice, pink is a spice blow, orange is a downed ship, violet is a live ore node, yellow is a guildmate.
- A ring fades as it gets older. An ore ring goes away after the respawn time of that ore. While the node is still there, the guildmate who sees it confirms it every 20 min, so the ring stays. A guildmate ring is full while they play and faded when their syncs are late. It goes away when they leave the game. The list then says "left 4 min ago" for 15 min.
- A list of guildmates online. Untick a guildmate to hide that guildmate on the minimap.
- **Shared finds** on the settings page: what each find is, how old it is, and who saw it.

Guild data never changes the ranking. It only draws rings and lists. A scan row is hidden only when two guildmates confirmed it as stale.

Your own eyes always win. If you see a node live, a guild report never hides it. A node grows back after its respawn time. So a guild report hides a node only until that time has passed. Then the node shows again. A downed ship never comes back, so its report hides it until the report expires.

**Pause sync** stops all sending and receiving. Your key stays saved.

**Characters** (a section of the settings page) lists the characters of your guild. Click one to see its skills, stats, schematics, Landsraad progress, achievements and this session. Type in the schematics box to find who can craft something. Typos are fine. Your own row shows what the guild sees of you.

- **Share my character** is on by default. Turn it off and the server deletes your profile.
- **Share this session** is on by default. Turn it off and your XP, dailies and loot of this session stay private.
- The tool never shares your Solari, your past sessions or your bases.
- The companion app does not show your inventory, so the tool cannot share it.
- A part says "not reported yet" until the companion app has sent it.

The tool talks to the guild server only as often as it must: every 15 s while you move, every minute while you stand still, every 3 min in the menus, and never without a key.

### Spice size modes

**Spice · Large fields** and **Spice · Medium fields** send you only to fields of that size that the tool saw as active. The tool saw them live, or remembers them for 45 min.

- If no such field is known, the tool uses scan-database rows and labels them as unconfirmed.
- While you fly the scout ornithopter, the tool ranks targets by flight distance with no range limit. The field must only suit your harvester.
- Small spice fields are never a target and never get a line.

### Classes and looted materials

On the settings page, below the search, you can browse everything by class. Each class has its own color: Spice, Ore & crystals, Stone, Salvage, Plants & sand, Loot cages, Uniques & schematics, Components and Refined materials.

**Looted materials** (for example Diamondine Dust) do not grow on the map. They drop from containers and corpses. The tool knows every drop location from gaming.tools' item data.

- The materials are in two classes: **Components** and **Refined materials**. Consumables are never targets.
- Each material shows how many places drop it on your current map. A material that does not drop there is greyed out. You can still pick it: its card then shows the other regions that drop it.
- If you pick a material, the lines go to the best cluster of containers that drop it. The map resources are hidden while a material is selected.
- The tool ranks each source by its expected amount (chance × average quantity) per minute of the complete trip. A container that the game shows you counts fully. A container that you emptied, or that is not there when you are next to it, does not count.
- A card shows where the material drops (container kinds, best chance, amount, nearest place) and the vendors who sell it.
- In the Deep Desert, the tool reads the current weekly layout from the map file that the companion loads. Until then, it finds the layout from the containers that the game shows you.

### Parts, downed ships and wrecks

Use the search **What are you looking for?** at the top of the settings page to look for a part, for example Holtzman Actuator, Mechanical Parts, Diamondine Dust or Carbide blade parts. Type a part of the name, then pick the part. Typos are fine.

- A part with the tag **ships** also drops from downed ships.
- The card of the part has three groups: **Downed ships**, **Fixed shipwrecks** and **Containers & corpses**. Each row shows the best chance, the amount, the average amount for each source and the nearest place.
- The lines lead to the best sources. A downed ship counts only after the game showed it to you.

The section **Ships & wrecks** has three searches. Each one leads you on one trip: first to the nearest place, then on to the next nearest. The route on the map shows up to 5 stops in that order.

| Search | What it finds |
|---|---|
| **Downed ships** | Ships that the game shoots down at random places, in Hagga Basin and in the Deep Desert. Each has three containers, until a sandworm takes the wreck. |
| **Fixed shipwrecks** | Always there: the named wrecks at fixed places, for example The Wreck of the Pallas. The tool reads them from the companion's own map. |
| **Buggy & ornithopter wrecks** | The wrecked vehicles at fixed places that you cut for parts. The scrap heaps are not included. |

About downed ships:

- The game shows a downed ship only within approximately 0.5 to 0.9 km. When it does, the badge shows `Downed ship! 0.9 km away` for 90 s, whatever resource you search.
- Farther away, the tool uses the downed ships that other players reported to gaming.tools (its scan database). "Lead me to downed ships" includes them, marked "not yet confirmed live". When you come close and the game shows no ship there, the tool drops it.
- The tool remembers the ship for 30 min after you leave. This time is an estimate.
- The tool forgets the ship when you come within 400 m and the game does not show it any more (a sandworm took it), or when you opened all its containers.
- Downed ships are targets in PvE rows too. "PvP areas only" is about yields, not about ships.

### Unique gear and schematics (chance finds)

You do not find unique gear itself. You find its schematic, and then you craft the item. Schematics drop only by chance. The tool knows every place that can drop them, on every map: Hagga Basin, the Deep Desert, the Imperial Testing Stations, the dungeons and the story maps. The data comes from gaming.tools.

- A few schematics do not drop: a vendor sells them, for example the schematic of the Upgraded Regis Spice Container. Some items are contract rewards. The card tells you this. These items get no lines.

- Search for the item on the settings page. Typos are fine: `regis` finds the Regis container, `holzman actuater` finds the Holtzman Actuator.
- The item has the tag **chance**. Its card shows the places on your map and the best chance in each other region, for example a testing station.
- The minimap shows no cyan route, because nothing is certain. Instead, a pulsing gold ring marks each likely place. A bigger ring means a better chance. A dashed gold line points to the most likely place.
- The tool orders the places by the chance to find at least one item there. The badge shows that chance, for example `best 5.3 km · 17% chance`.

### Rare and ultra-rare cages

- **Rare & ultra-rare cages** leads you to both kinds of cage. An ultra-rare cage counts 3 times. **Ultra-rare cages only** ignores rare cages.
- The section "Rare loot nearby" shows what each nearby cage can drop, with the chance to get at least one of each item. The game rolls the contents when you open a cage. Thus, no tool can show the real contents in advance.

### Nodes that you mine

The tool records a node as taken if the node disappears within 60 m of you. It must disappear alone or with a few others. Many nodes that disappear at the same time are the game unloading an area. You, a player in your group or another player can take the node. For the ranking, the node is gone in all three cases.

- The tool does not rank a mined node, and hides its map marker.
- The node returns after its respawn time: 45 min for Titanium and Stravidium, 30 min for other resources. The node also returns if the game shows it again.
- The tool learns the real respawn times from your play. For details, read [What the tool learns from your play](docs/HOW-IT-WORKS.md#what-the-tool-learns-from-your-play).
- The tool keeps the records per map in `data\mined-*.json`.

## Uninstall

If you installed from the ZIP file, double-click `Uninstall.cmd` (see [Install from the ZIP file](#install-from-the-zip-file-recommended)).

If you installed from this folder:

1. Run this command in the tool folder:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Uninstall
   ```

2. Restart the companion. This closes the DevTools port.
3. Delete the folder.

## Privacy and security

**WARNING:** While the companion runs, any program on your PC can use the DevTools port `127.0.0.1:9231` to control the companion's pages. Other computers cannot connect to it. To remove this setting, uninstall the tool.

- The tool keeps your learned data (terrain, corrections, journals) in the folder `data\`.
- The tool sends nothing until you enter a guild key. Then it sends your character name, your server, strong sightings and, unless you turn them off, your position and your character profile (skills, stats, schematics, Landsraad, achievements and this session). It sends them only to the guild server at 158.69.179.64. The connection is HTTPS, and the tool checks the server's certificate. The key is kept in `data\settings.json`.
- The settings page answers only `127.0.0.1` and `localhost`. It refuses changes from other websites. Thus, a web page in your browser cannot change your settings.

## Troubleshooting

If something in harvesting-dune fails, the gaming.tools companion keeps working as usual. The tool tells you about the problem:

- **Settings page:** a red box at the top shows the last problems. Press **Copy report** and send the text to the developer.
- **Companion map window:** a small notice at the bottom right tells you to open the settings page. Press **Hide** to close it until the page reloads.
- **Windows message:** if the detector fails 3 times in a row and cannot start again, a message tells you how to make a report.

1. Run the doctor:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1
   ```

   The doctor checks each part: the companion, the DevTools setting and port, the autorun task, the watcher, the detector and the settings page. For each problem, it shows the fix.
2. Do the first fix that the doctor shows.
3. Run the doctor again.

To save the result to share it, add the option `-Report`. The doctor saves the report in `data\`.

| Log | Contents |
|---|---|
| `data\logs\errors.log` | All warnings and errors, from every part of the tool. Read this log first. |
| `data\logs\detector.log` | Everything that the detector logs. The last 5 files stay (2 MB each). |
| `data\install.log` | The output of the last install. |
| `data\autorun.log` | If the watcher saw the companion and its DevTools port. |
| `data-run.log` | The log of the detector. |
| `data-run.prev.log` | The log of the previous detector run, after a restart. |

If the log shows "No DevTools feed on port 9231", the companion started before the installation. Close the companion (tray icon > Exit), and open it again.

## License

The repository has no license file yet.

## Contributing

1. Install the development tools, including pytest:

   ```powershell
   uv sync
   ```

2. Before you send a change, run the tests. The bridge tests need Node.js.

   ```powershell
   uv run pytest
   ```

For the design, the module boundaries and how to run the tool by hand, read [How harvesting-dune works](docs/HOW-IT-WORKS.md).

### Build the installer

- **On your PC:** install [Inno Setup 6](https://jrsoftware.org/isinfo.php). Then run `iscc /DAppVersion=1.2.3 installer\harvesting-dune.iss`. The installer goes to `dist\`.
- **The ZIP file:** run `powershell -ExecutionPolicy Bypass -File installer\build-zip.ps1 -Version 1.2.3`. The file goes to `dist\`. `installer\test-zip.ps1` installs it, installs it again and uninstalls it.
- **On GitHub:** publish a release. The "Release installer" workflow builds the installer and the ZIP file, tests them, and attaches them to the release. To build a test installer without a release, run the workflow by hand in the Actions tab.
- Each push also runs the "CI" workflow: the tests, the lint check and a check of the PowerShell scripts.

## License

Source-available, not open source: PolyForm Noncommercial 1.0.0 (see LICENSE).
You may use, change and share it for noncommercial purposes.
The license covers this code only.

Not affiliated with Funcom or gaming.tools. Dune is a trademark of its owners.
