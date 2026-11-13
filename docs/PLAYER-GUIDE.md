# Player guide

This guide covers everything harvesting-dune does. If you're new, start with the [README](../README.md).

## The controls

You control the tool from three places:

- **The settings page** at http://127.0.0.1:8765. It opens in your browser when the companion starts, unless it's already open.
- **The companion's map window**, which has a small picker at the top right: `Find [resource] [Vehicle: auto]`.
- **The minimap in the game**, which shows the badge (a one-line status), the destination marker and the lines. Hover over the marker to see how many nodes there are, how many of them are live and how many are only known from scans, and how long the round trip takes.

Whenever you change something on the settings page, it shows "Saved", or a red message that explains why the change didn't work.

The settings page has three columns:

- **Left: what you're looking for.** A single search box covers everything, including map resources, parts, materials, unique gear, schematics, downed ships and wrecks. Spelling mistakes are fine, and you can also browse by class. This is the only place where you choose a target. Spice has three buttons: **All**, **Large** and **Medium**.
- **Middle: your target.** This shows what you're searching for, its status, the best place to go and where it drops. It's also where you change vehicle, turn recommendations on or off, set your base and block a place. Below that are the Lines & minimap, Deep Desert and Diagnostics sections.
- **Right: what's around you.** Ships & wrecks, Rare loot nearby, Spice blows, Guild and Characters.

Click a section to open it. Closed sections show a short summary, and the page remembers which ones you had open. In a narrow window, the page switches to two columns.

## Lines on the minimap

- While the lines are on, the minimap shows only one resource: the one you're searching for. The Lines & minimap section then says something like `Minimap shows: Spice Sand`.
- The resource tick boxes only appear when the lines are off, or when the lines are waiting for you to tick exactly one resource. If you tick a different resource while the lines are on, your search switches to it.
- The cyan **main line** runs from you to the best target.
- Up to four **thin lines** lead to the next best areas. In a Large or Medium spice search, they only lead to fields of that size. There's more about this in [Lines on the minimap](HOW-IT-WORKS.md#lines-on-the-minimap).
- The tool remembers your search when it restarts.
- A destination you track yourself in the game, and the Landsraad route mode, always take priority.

## The badge

A typical badge looks like this: <code>Stravidium Mass &middot; thopter &middot; best 1.2 km &middot; 65 nodes &middot; hotspot H5</code>

The badge always names what you're searching for.

- **best 1.2 km** is the distance to the best area.
- **65 nodes** is how many nodes that area has. Once you start mining, it shows your progress instead, for example <code>3 left &middot; 2 mined</code>.
- **hotspot H5** (Deep Desert only) is the grid cell with the most of your resource on the whole map, following the PvP rules. It shows up even when the lines can't reach that far yet.
- Your target stays the same while you travel. It only changes if another place becomes clearly better (by 15%), or if the target is empty or gone.
- When an area is empty, the badge says <code>Area mined out &rarr; next 1.2 km</code>. If the next area becomes clearly better before that happens, it says <code>Move on (2 left here) &rarr; ...</code>.
- While you're searching for spice, the badge also shows news about the field sizes you're searching for, such as `Spice blow! medium field 2.3 km away`. A Large search only tells you about large fields.

## Settings

| Setting | What it does |
|---|---|
| **PvP areas only** (Deep Desert) | On by default. Lines only lead into PvP rows, because yields in PvE are lower. Distant PvP rows count too, with their distance estimated in a straight line. |
| **Last PvE row** | By default, rows A (in the south) to D are PvE and rows E to I are PvP. Change this if the game map shows the border somewhere else. |
| **Lines start in row** (Deep Desert) | Row A by default, so lines appear from the entrance onwards. Choose a later row, such as E, if you only want lines once you're that far in. Until then, the marker stays on the map and the badge says `lines from row E`. |
| **PvP yield &times;** | 2.5 by default. When "PvP areas only" is off, yields in PvP areas are multiplied by this number. |
| **Max distance for lines** (km) | Lines only lead to areas within this straight-line distance. 0 means there's no limit. The hotspot always covers the whole map. |
| **Minimap shows** | Which resources appear on the minimap. The big map and your other companion filters (caves, loot and so on) aren't affected. The tick boxes only appear when the lines are off, or while the lines are waiting for you to tick one. |
| **Extra markers** | Buried treasure stays hidden unless you tick it. It's never searched for and never gets lines. |
| **Vehicle** | "auto" works out your vehicle from the way you move. Choose a vehicle to override it. |

The companion keeps a separate list of minimap filters that it gives you no control over, so the minimap reloads for a second whenever it needs to apply a change.

Why use `Ctrl+Alt+M` rather than the companion's own "Toggle Minimap" key? The companion's key follows its widget visibility settings, so it can't show the minimap in situations where those settings hide it. `Ctrl+Alt+M` changes the settings themselves, so the minimap is either shown in every activity or turned off.

## Farm routes

Choose an item on the settings page. If players farm it along a fixed route, a **Farm route** table lists those routes with their world, sites, chests, reset time, confidence and distance. Press **Start** to begin.

- If you're on the wrong map, the minimap tells you where to go, for example `Farm route: travel to Hagga Basin, Wreck of the Ourea`. There are no lines until you get there.
- Once you're on the right map, the line takes you to the site.
- At the site, a line points to the next chest, and numbered rings mark the next three chests. Dimmed rings are chests you've already emptied.
- A chest counts as done once you've been within 6 m of it and the companion shows it as emptied. Press `Ctrl+Alt+N` to skip a chest.
- The order never changes while you're walking the route.
- When you finish a site, the route moves on to the next one. **Repeat this site** waits for the reset and then runs the same order again.
- **Stop route** ends the route. It also ends by itself after 10 minutes outside the route's maps, and it's always off when the tool starts.
- "data only" means no player guide confirms the route. "order: nearest" means no guide gives an order, so you simply go to the nearest chest.

## Guild sync

Guild sync shares what you see with the rest of your guild. To set it up:

1. Open the **Guild** section on the settings page.
2. Paste your key and press **Save key** (or Enter).
3. Next to the field, you'll see whether the key is being checked, whether you're connected (as your character), whether the key was rejected, or whether the server can't be reached.

After that, the key is never shown again. You'll just see "key saved", with **Change** and **Remove** buttons.

Without a key, or with a key that's been rejected or has expired, the tool draws nothing on the companion's map, and the settings page only shows the key field and the reason. If the server can't be reached, the tool keeps working for 72 hours after the last successful check. For the details, see [Key gate and datasets](HOW-IT-WORKS.md#key-gate-and-datasets).

**What you share:** your character name and server, your strong sightings (active spice fields, spice blows, downed ships, live ore, crystal and stone nodes, mined nodes, and scan entries confirmed as stale), and your position. Position sharing is on by default. Turn off **Share my position** to stop it.

**What you get:**

- Rings on the map: green for spice, pink for a spice blow, orange for a downed ship, violet for a live ore node and yellow for a guildmate.
- There's no ring for finds that only you shared, since they're already on your map. There's also no ore ring where your own companion shows the node, where it has seen the node gone, or where you mined it.
- Rings fade as they get older. An ore ring disappears once that ore's respawn time has passed, but as long as the node is still there, the guildmate who can see it confirms it every 20 minutes, so the ring stays. A guildmate's ring is solid while they're playing and fades when their updates arrive late. Guildmates who are in the menus have no ring. When a guildmate leaves the game, their ring disappears and the list shows "left 4 min ago" for 15 minutes.
- A list of the guildmates who are online. Untick a guildmate to hide them on the minimap.
- **Shared finds** on the settings page, showing what was found, how long ago, and who saw it.

Guild data only affects the ranking for spice (see [Spice size modes](#spice-size-modes)). For everything else, it just adds rings and lists. A scan entry is only hidden once two guildmates have confirmed that it's stale.

**Your own eyes always win.** If you can see that a node is live, no guild report will hide it. Because nodes grow back, a report only hides a node until its respawn time is over. Downed ships never come back, so a report about one hides it until the report expires.

**Pins** are short labels, such as "IRON HERE", that appear on your guild's minimaps. Press `Ctrl+Alt+P`, or type a label in the **Guild** section and click **Drop pin**. To change the label the hotkey uses, edit the field after "Ctrl+Alt+P drops". The pin goes on the destination you've set in the companion, or where you're standing if you haven't set one. Guildmates on the same map and layout see a ring with your label. Pins last for two hours. Your own pins are listed on the settings page, and **Delete** removes a pin for everyone. Hiding a guildmate also hides their pins. Pins never affect the lines or the ranking.

**Pause sync** stops all sending and receiving. Your key stays saved.

**Characters** lists your guild's characters, with alts grouped under the player they belong to. Click a character to see their skills, stats, schematics, Landsraad progress, achievements and current session. To find out who can craft something, type in the schematics box (spelling mistakes are fine). Your own row shows what the guild can see about you.

- **Share my character** is on by default. If you turn it off, the server deletes your profile.
- **Share this session** is on by default. If you turn it off, your XP, dailies and loot from this session stay private.
- Your Solari, past sessions and bases are never shared.
- Your inventory can't be shared, because the companion doesn't show it.
- Any part of a profile that the companion hasn't sent yet shows "not reported yet".

The tool only contacts the server as often as it needs to: every 15 seconds while you're moving, every minute while you're standing still, every 3 minutes while you're in the menus, and never without a key.

## Spice size modes

**Spice &middot; Large fields** and **Spice &middot; Medium fields** only send you to active fields of that size. That applies to the main line and to the thin lines.

For each spice site, the most recent report decides whether the field is active:

- **Your companion:** the field is live right now, or you've seen it active. This is remembered for 45 minutes.
- **A guildmate:** they've seen it active. This counts for 45 minutes.
- **The gaming.tools scan database:** its entries have no timestamp, so any other report counts as newer.

A few more rules:

- If the most recent report says the site is empty, it isn't a target. That includes a scan entry that a guildmate has seen empty.
- If your companion is showing the site right now, only your companion's view counts.
- The game sometimes reports the same site again under a new actor id. The new report replaces the old one, so an empty site drops out as soon as your companion shows it as empty.
- If no active field of the size you want is known, the tool falls back to scan entries and marks them as unconfirmed.
- When you're scouting in an ornithopter, targets are ranked by flying distance with no range limit, as long as the field suits your harvester.
- Small spice fields are never a target and never get a line.

## Classes and looted materials

Below the search box, you can browse by class. Each class has its own color: Spice, Ore & crystals, Stone, Salvage, Plants & sand, Loot cages, Uniques & schematics, Components and Refined materials.

**Looted materials**, such as Diamondine Dust, don't grow on the map. They drop from containers and corpses, and the tool knows every place they drop from gaming.tools' item data.

- You'll find them under **Components** and **Refined materials**. Consumables are never targets.
- Each material shows how many places on your map drop it. A greyed-out material doesn't drop on this map. You can still choose it, and its card lists the regions where it does drop.
- When you choose one, the lines lead to the best cluster of containers that drop it. Map resources are hidden while a material is selected.
- Sources are ranked by the amount you can expect (chance &times; average quantity) per minute of the whole trip. A container the game is showing you counts in full. One you've emptied, or one that's missing when you're right next to it, doesn't count.
- The material's card shows the kinds of containers, the best chance, the amount, the nearest place and any vendors who sell it.
- In the Deep Desert, the tool reads the weekly layout from the map file the companion loads. Until it has that, it works out the layout from the containers the game shows you.

## Parts, downed ships and wrecks

Search for a part in the left column, such as Holtzman Actuator, Mechanical Parts, Diamondine Dust or Carbide blade parts. Part of the name is enough, and spelling mistakes are fine.

- Parts tagged **ships** also drop from downed ships.
- The part's card has three groups, **Downed ships**, **Fixed shipwrecks** and **Containers & corpses**, each with the best chance, the amount, the average amount per source and the nearest place.
- The lines lead to the best sources. A downed ship only counts once the game has shown it to you.

Three searches in the **Salvage** class take you on a trip: first to the nearest place, then to the next nearest, and so on. The route shows up to five stops in that order. Your current stop stays the target unless another one is clearly closer (by 15%).

| Search | What it finds |
|---|---|
| **Downed ships** | Ships that the game shoots down at random places in Hagga Basin and the Deep Desert. Each one has three containers and stays until a sandworm takes the wreck. |
| **Fixed shipwrecks** | The named wrecks that are always there, such as The Wreck of the Pallas, read from the companion's own map. |
| **Buggy & ornithopter wrecks** | Wrecked vehicles in fixed places that you can cut up for parts. Scrap heaps aren't included. |

The **Ships & wrecks** section lists the downed ships you've seen and the fixed shipwrecks on your map.

About downed ships:

- The game only shows a downed ship when you're within about 0.5 to 0.9 km of it. As soon as one appears, a red ring flashes on your minimap, whatever you're searching for. If you're searching for downed ships or an item they drop, the badge also says `Downed ship! 0.9 km away` for 90 seconds.
- You only get ships that your own game has shown you. Other players report ships to gaming.tools as well, but those reports have no timestamp and ships don't last long, so the tool never sends you to one. The settings page just counts them. When no ship is known, the badge says `none known`.
- A ship is remembered for 30 minutes after you leave it. That figure is an estimate.
- The tool forgets a ship if you come within 400 m and the game no longer shows it (a sandworm got it), or once you've opened all its containers.
- Downed ships are targets in PvE rows too. "PvP areas only" is about yields, not ships.

## Unique gear and schematics (chance finds)

You don't find unique gear itself. You find its schematic and craft the item. Schematics only drop by chance, and the tool knows every place on every map that can drop them: Hagga Basin, the Deep Desert, the Imperial Testing Stations, the dungeons and the story maps. The data comes from gaming.tools.

- Some schematics don't drop at all and are sold by a vendor instead (the Upgraded Regis Spice Container, for example), and some items are contract rewards. The card tells you when that's the case, and these items get no lines.
- Search for the item. `regis` finds the Regis container, and `holzman actuater` finds the Holtzman Actuator.
- Chance finds are tagged **chance**. The card shows the places on your map and the best chance in each other region, such as a testing station.
- Since nothing is certain, there's no cyan route. Instead, pulsing gold rings mark the likely places (the bigger the ring, the better the chance), and a dashed gold line points to the most likely one.
- Places are ranked by the chance of finding at least one of the item. The badge shows that chance, for example <code>best 5.3 km &middot; 17% chance</code>.

## Rare and ultra-rare cages

- **Rare & ultra-rare cages** leads you to both kinds, with an ultra-rare cage counting three times as much. **Ultra-rare cages only** skips the rare ones.
- **Rare loot nearby** shows what each cage near you can drop, and the chance of getting at least one of each item. The game decides what's inside a cage when you open it, so no tool can show you the real contents in advance.

## Nodes you mine

A node counts as taken when it disappears within 60 m of you, either on its own or together with a few others. When lots of nodes vanish at once, that's just the game unloading an area. It doesn't matter whether you, your group or a stranger took the node: as far as the ranking is concerned, it's gone.

- Mined nodes aren't ranked, and their map markers are hidden.
- They come back after their respawn time, which is 45 minutes for Titanium and Stravidium and 30 minutes for everything else, or sooner if the game shows them again.
- The tool learns the real respawn times as you play. See [What the tool learns from your play](HOW-IT-WORKS.md#what-the-tool-learns-from-your-play).
- The records are kept separately for each map in `data\mined-*.json`.

## Hotkeys on a mouse button

The hotkeys are listed in the [README](../README.md#using-it). To put one on a mouse button, open Logitech G HUB, select the button, choose **System > Keyboard shortcut** (or **Keystroke**) and press the keys. Other mouse software works in much the same way.

## Privacy and security

**Please note:** while the companion is running, any program on your PC can use its DevTools port `127.0.0.1:9231` to control the companion's pages. Other computers can't connect to it. Uninstalling the tool removes this setting.

- Everything the tool learns (terrain, corrections, journals) is stored in `data\`.
- Nothing is sent anywhere until you enter a guild key. After that, the tool sends your character name, your server and your strong sightings. It also sends your position and your character profile (skills, stats, schematics, Landsraad, achievements and the current session) unless you turn those off. All of it goes only to the guild server at 158.69.179.64, over HTTPS with the certificate checked. Your key is stored in `data\settings.json`.
- The settings page only accepts connections from `127.0.0.1` and `localhost`, and it refuses changes that come from other websites.

If something goes wrong, see [Troubleshooting](TROUBLESHOOTING.md).
