# harvesting-dune

harvesting-dune is a helper for Dune: Awakening that works alongside the [gaming.tools](https://gaming.tools) companion app. You choose a resource, and it finds the best place to farm it that your vehicle can reach, then draws a line to it on the companion's map and on your in-game minimap. It also hides scan entries it knows are out of date, such as spice fields that have already been harvested.

Besides map resources, it can find parts, looted materials, schematics for unique gear, loot cages, downed ships and wrecks. It can also guide you along farm routes and share finds with your guild.

harvesting-dune is not a hack or a cheat. It does not modify the game, read its memory or interact with the game client in any way. It works with the information that the gaming.tools companion app displays and, if you use guild sync, shares that information with your guild through the guild's sync server.

## Requirements

- Windows 10 or 11.
- The Dune companion from [gaming.tools](https://gaming.tools).
- An internet connection during the install, which downloads uv and Python by itself.
- A guild key from your guild admin. The tool doesn't draw anything without one.

## Install

1. Download `harvesting-dune-<version>.zip` from the [Releases page](https://github.com/sm18lr88/harvesting-dune/releases).
2. Right-click the file, choose **Extract All**, and double-click `Install.cmd` in the extracted folder. You don't need admin rights, and the first install takes a few minutes.
3. Paste your guild key when you're asked for it and press Enter.
4. Close the companion (tray icon > Exit) and open it again.

From then on, the tool starts and stops with the companion, and it updates itself after you close the game. The setup program, installing from a cloned folder, how updates work and how to uninstall are all covered in [Installing](https://github.com/sm18lr88/harvesting-dune/blob/master/docs/INSTALL.md).

## Using it

1. Start the companion. The settings page opens in your browser at http://127.0.0.1:8765.
2. In the left column, search for what you want to farm.
3. Follow the cyan line on your minimap. Thinner lines lead to the next best spots.

The badge under the minimap shows what you're searching for, the vehicle it's planning for and how far away the best area is, for example <code>Stravidium Mass &middot; thopter &middot; best 1.2 km &middot; 65 nodes &middot; hotspot H5</code>. The tool works out your vehicle from the way you move. If it gets it wrong, choose your vehicle on the settings page.

| Hotkey | What it does |
|---|---|
| `Ctrl+Alt+Right` / `Ctrl+Alt+Left` (arrow keys) | Switches to the next or previous resource. |
| `Ctrl+Alt+Up` | Changes the vehicle: auto, sandcrawler, buggy, sandbike, ornithopter. |
| `Ctrl+Alt+Down` | Turns recommendations on or off. |
| `Ctrl+Alt+L` | Turns the lines on or off. |
| `Ctrl+Alt+M` | Shows or hides the companion's minimap, whatever you're doing: on foot, in combat, in any vehicle or while building. |
| `Ctrl+Alt+N` | Skips to the next stop on a farm route. |
| `Ctrl+Alt+P` | Drops a guild pin with your preset label. |

## More information

- [Player guide](https://github.com/sm18lr88/harvesting-dune/blob/master/docs/PLAYER-GUIDE.md): every feature and setting in detail, and what is shared with your guild.
- [Troubleshooting](https://github.com/sm18lr88/harvesting-dune/blob/master/docs/TROUBLESHOOTING.md): what to do when something goes wrong, and where the logs are. If the tool breaks, the companion keeps working normally.
- [Privacy and security](https://github.com/sm18lr88/harvesting-dune/blob/master/docs/PLAYER-GUIDE.md#privacy-and-security): while the companion is running, any program on your PC can control the companion's pages through its DevTools port. Please read this section.
- [How harvesting-dune works](https://github.com/sm18lr88/harvesting-dune/blob/master/docs/HOW-IT-WORKS.md) and [Development](https://github.com/sm18lr88/harvesting-dune/blob/master/docs/DEVELOPMENT.md): for anyone who wants to look under the hood or contribute.

## License

Source-available, not open source: PolyForm Noncommercial 1.0.0 (see LICENSE).
You may use, change and share it for noncommercial purposes.
The license covers this code only.

Not affiliated with Funcom or gaming.tools. Dune is a trademark of its owners.
