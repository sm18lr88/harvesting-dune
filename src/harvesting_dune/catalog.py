# Catalog: resources, their map marker types, minimap extras and respawn defaults.
"""Resource catalog, mirroring the gaming.tools map filter list ("resources" group).

Keys are the companion's marker types. Live actors carry `markerType`/`spriteKey`;
scanned rows carry `type` and `map_marker_id` (a lowercase sprite key).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Resource:
    key: str  # picker id
    name: str
    marker_types: tuple[str, ...]
    harvest: str  # "drive_over" (vehicle harvests the deposit) | "on_foot" (park nearby, cut by hand)
    weight: float = 1.0  # relative value of one node / small field
    # direct only to fields confirmed active by observation; scan rows only as a labelled fallback
    confirmed_only: bool = False
    live_only: bool = False  # never use scan-database rows (things that vanish when taken)
    min_patch: int = 4  # smallest group worth a thin alternative line
    # a search variant of another resource: it shares that resource's marker (and minimap tick box)
    # and only counts actors / scan rows whose actor type starts with one of `actor_types`
    variant_of: str | None = None
    actor_types: tuple[str, ...] = ()
    pvp_only_applies: bool = True  # False: ships are worth it in PvE rows too (the rule is about yields)
    # single places spread over the map: lead to the nearest, then on to the next nearest (one trip)
    tour: bool = False
    # False: the companion always shows its marker and the detector never hides it (downed ships: an
    # alert for any search must be visible); while you search it, the map resources step aside
    minimap_managed: bool = True
    # found only by chance (unique gear, schematics): search areas on the minimap, never a route
    chance: bool = False

    def counts(self, actor_type: str | None) -> bool:
        return not self.actor_types or (actor_type or "").startswith(self.actor_types)


RESOURCES: tuple[Resource, ...] = (
    Resource("spice", "Spice Sand", ("SpiceFieldSmall", "SpiceFieldMedium", "SpiceFieldLarge"), "drive_over"),
    Resource("spice_large", "Spice · Large fields", ("SpiceFieldLarge",), "drive_over", confirmed_only=True),
    Resource("spice_medium", "Spice · Medium fields", ("SpiceFieldMedium",), "drive_over", confirmed_only=True),
    Resource("iron", "Iron Ore", ("MagnetiteOre",), "on_foot"),
    Resource("copper", "Copper Ore", ("AzuriteOre",), "on_foot"),
    Resource("aluminum", "Aluminum Ore", ("BauxiteOre",), "on_foot"),
    Resource("carbon", "Carbon Ore", ("DolomiteRock",), "on_foot"),
    Resource("titanium", "Titanium Ore", ("TitaniumOre",), "on_foot"),
    Resource("stravidium", "Stravidium Mass", ("StravidiumOre",), "on_foot"),
    Resource("jasmium", "Jasmium Crystal", ("JasmiumOre",), "on_foot"),
    Resource("erythrite", "Erythrite Crystal", ("ErythriteOre",), "on_foot"),
    Resource("basalt", "Basalt Stone", ("BasaltOre",), "on_foot"),
    Resource("granite", "Granite Stone", ("RhyoliteOre",), "on_foot"),
    Resource("fuelcell", "Fuel Cell", ("FuelCellWreckage",), "on_foot"),
    Resource("scrap", "Salvaged Metal", ("ScrapMetalWreckage",), "on_foot"),
    # the wrecked buggies and ornithopters at fixed places: cut them for parts (the companion marks
    # them as salvaged metal, like the scrap heaps)
    Resource("wrecks", "Buggy & ornithopter wrecks", ("ScrapMetalWreckage",), "on_foot", min_patch=1, tour=True,
             variant_of="scrap", actor_types=("BP_Ornithopter_GRD_Encounter", "BP_Buggy_GRD_Encounter")),
    # ships shot down over Arrakis (ships.py): three containers each, until a worm swallows the wreck
    Resource("downed_ships", "Downed ships", ("SalvageSteel",), "on_foot", live_only=True, min_patch=1,
             pvp_only_applies=False, tour=True, minimap_managed=False),
    # the named shipwrecks at fixed places ("The Wreck of the Pallas"), from the companion's map data;
    # no actor marker of their own, so the minimap shows the downed-ship family while you search them
    Resource("shipwrecks", "Fixed shipwrecks", ("Shipwreck",), "on_foot", min_patch=1, pvp_only_applies=False,
             tour=True, variant_of="downed_ships"),
    Resource("flour", "Flour Sand", ("FlourSand",), "on_foot"),
    Resource("brittlebush", "Brittle Bush", ("BrittleBush",), "on_foot"),
    Resource("primrose", "Primrose Field", ("PrimroseField",), "on_foot"),
    Resource("agave", "Agave Seeds", ("SaguaroSeed",), "on_foot"),
    # marker names are the companion's own (so the minimap filter toggles its cage markers)
    Resource("cages", "Rare & ultra-rare cages", ("LootContainerUltraRare", "LootContainerRare"), "on_foot",
             live_only=True, min_patch=1),
    # search variant (like the spice size modes): same family on the minimap
    Resource("cages_ultra", "Ultra-rare cages only", ("LootContainerUltraRare",), "on_foot",
             confirmed_only=True, live_only=True, min_patch=1),
)
# Minimap-only extras: not searchable, no lines, never count as "the one shown resource".
# Hidden unless ticked, so clutter the companion shows by default stays off the minimap.
@dataclass(frozen=True, slots=True)
class MinimapExtra:
    key: str
    name: str
    marker_types: tuple[str, ...]


MINIMAP_EXTRAS: tuple[MinimapExtra, ...] = (
    MinimapExtra("buried_treasure", "Buried treasure", ("BuriedTreasure",)),
)
EXTRA_BY_KEY = {e.key: e for e in MINIMAP_EXTRAS}

# Respawn after you mine or gather a node (patch 1.1.0.17: Titanium and Stravidium 30 -> 45 min).
RESPAWN_S = {"titaniumore": 45 * 60, "stravidiumore": 45 * 60}
DEFAULT_RESPAWN_S = 30 * 60


def respawn_s(marker: str) -> float:
    return RESPAWN_S.get(marker, DEFAULT_RESPAWN_S)


LOOTED_HIDE_S = 60 * 60  # a cage you emptied is not a target again for an hour
CAGE_MARKERS = {"lootcontainerultrarare", "lootcontainerrare"}

# Classes for the settings page (ordered). Map resources are grouped here; looted materials
# (itemsources.py) get their class from the item's category in gaming.tools' data.
GROUP_OF = {
    "spice": "Spice", "spice_large": "Spice", "spice_medium": "Spice",
    "iron": "Ore & crystals", "copper": "Ore & crystals", "aluminum": "Ore & crystals", "carbon": "Ore & crystals",
    "titanium": "Ore & crystals", "stravidium": "Ore & crystals", "jasmium": "Ore & crystals", "erythrite": "Ore & crystals",
    "basalt": "Stone", "granite": "Stone",
    "fuelcell": "Salvage", "scrap": "Salvage", "wrecks": "Salvage", "downed_ships": "Salvage",
    "shipwrecks": "Salvage",
    "flour": "Plants & sand", "brittlebush": "Plants & sand", "primrose": "Plants & sand", "agave": "Plants & sand",
    "cages": "Loot cages", "cages_ultra": "Loot cages",
}
ITEM_GROUPS = {  # gaming.tools item category prefix -> class of looted materials
    "items/misc/components": "Components",
    "items/misc/refinedresources": "Refined materials",
    "items/misc/rawresources": "Raw materials",
}  # consumables are never map targets
CHANCE_GROUP = "Uniques & schematics"  # looted by chance from any source, on any map
GROUP_ORDER = ["Spice", "Ore & crystals", "Stone", "Salvage", "Plants & sand", "Loot cages", CHANCE_GROUP,
               "Components", "Refined materials", "Raw materials"]
# items you gather from map nodes: they are the map resources above, not loot targets
MAP_ITEM_IDS = {"spicesand", "stone", "basalt", "bauxiteore", "azuriteore", "magnetiteore", "dolomiterock",
                "jasmiumcrystal", "erythritecrystal", "t6resourcea", "t6resourceb", "oil", "scrapmetal",
                "floursand", "fiberplant", "primrose", "saguaro", "graniteore"}

BY_KEY = {r.key: r for r in RESOURCES}
# the resources with their own tick box on the minimap (size modes and variants share their family's)
_FAMILIES = tuple(r for r in RESOURCES if not r.confirmed_only and r.variant_of is None)
MINIMAP_RESOURCES = tuple(r for r in _FAMILIES if r.minimap_managed)
_BY_MARKER = {m.lower(): r for r in _FAMILIES for m in r.marker_types}
SPICE_SIZE = {"spicefieldsmall": "small", "spicefieldmedium": "medium", "spicefieldlarge": "large"}
# nodes you mine or gather by hand (not spice fields, not loot cages or downed ships)
NODE_MARKERS = {m.lower() for r in RESOURCES if r.harvest == "on_foot" and not r.live_only for m in r.marker_types}
NEVER_TARGET = {"spicefieldsmall"}  # small spice fields are never worth the trip: no target, no line
# live nodes the guild shares (peers.py): ore, crystals and stone, never spice, salvage, plants or loot
ORE_MARKERS = {m.lower() for r in RESOURCES if GROUP_OF.get(r.key) in ("Ore & crystals", "Stone")
               for m in r.marker_types} - NEVER_TARGET


def marker_of(*candidates: str | None) -> str | None:
    """First candidate (markerType, spriteKey, actorType, scan type…) naming a catalog marker."""
    for c in candidates:
        if c and c.lower() in _BY_MARKER:
            return c.lower()
    return None


def resource_of(marker: str | None) -> Resource | None:
    return _BY_MARKER.get(marker) if marker else None
