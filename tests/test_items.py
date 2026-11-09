"""Looted materials (Diamondine Dust...) as search targets, grouped classes on the page."""

import json
import shutil
import subprocess
from importlib import resources

import pytest
from helpers import pose, session

from harvesting_dune.app import Detector
from harvesting_dune.catalog import GROUP_OF, RESOURCES
from harvesting_dune.feed import BusMessage
from harvesting_dune.itemsources import ItemSources

# shape of loot_crawl.js "materials": [map, x, y, z, source, chance, min, max]
CRAWL = {
    "built": 1_790_000_000_000,
    "materials": [
        {"id": "t4mysatarilcomponent1", "name": "Diamondine Dust", "rarity": "Common", "tier": 4,
         "categories": ["items/misc", "items/misc/components"], "sold": [["Zoe", 3500, 15]],
         "drops": [
             # a cluster of five primitive containers 30-40 km away ...
             *[["survival_1", 3_000_000 + i * 15_000, 0, 0, "Primitive Container", 0.81, 1, 8] for i in range(5)],
             # ... versus one ultra-rare cage close by with a small chance
             ["survival_1", 200_000, 0, 0, "Ultra Rare Container", 0.05, 1, 1],
             ["deepdesert_1_03", 10_000, 10_000, 0, "Primitive Container", 0.8, 1, 8],
             ["deepdesert_1_07", 50_000, 50_000, 0, "Primitive Container", 0.8, 1, 8],
         ]},
        {"id": "stone", "name": "Granite Stone", "categories": ["items/misc/rawresources"], "drops": []},  # a map resource
        {"id": "knife", "name": "Knife", "categories": ["items/weapons/shortblades"], "drops": []},  # not a material
    ],
}


def detector(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.items.ingest(CRAWL)
    return det


def test_index_keeps_materials_groups_them_and_skips_map_resources(tmp_path):
    src = ItemSources(tmp_path)
    assert src.ingest(CRAWL) == 1
    again = ItemSources(tmp_path)  # cached
    [g] = again.picker("survival_1")
    assert g["group"] == "Components" and g["items"] == [["item:t4mysatarilcomponent1", "Diamondine Dust", 6, False, False]]
    assert again.resource("item:t4mysatarilcomponent1").min_patch == 1


def test_every_map_resource_has_a_class():
    assert all(GROUP_OF.get(r.key) for r in RESOURCES)


def test_deep_desert_layout_is_matched_from_the_containers_you_see(tmp_path):
    src = ItemSources(tmp_path)
    src.ingest(CRAWL)
    assert src.layout_for(("deepdesert_1", 7), [(50_000, 50_000), (50_010, 49_990)]) == "deepdesert_1_07"
    assert src.layout_for(("survival_1", 7), []) == "survival_1"


def test_the_companions_map_file_names_the_layout_before_any_container_is_seen(tmp_path):
    det = detector(tmp_path)
    session(det, "deepdesert_1")
    pose(det, 0, 0, t=1)
    det.ui("resource", "item:t4mysatarilcomponent1")
    assert det.item_nodes(det.resource_key, 1) == []  # only buried treasure around: no layout from containers
    det.on_shipwrecks({"map": "deepdesert_1_03", "wrecks": []})  # the minimap loaded this week's map file
    assert [(n.x, n.y) for n in det.item_nodes(det.resource_key, 2)] == [(10_000, 10_000)]
    assert det.snapshot()["itemInfo"]["sources"][0]["count"] == 1


def test_selecting_a_material_leads_to_the_best_cluster_and_shows_its_sources(tmp_path):
    det = detector(tmp_path)
    session(det, "survival_1")
    pose(det, 10, 0, t=1)
    det.ui("minimap_toggle", "item:t4mysatarilcomponent1")
    assert det.resource_key == "item:t4mysatarilcomponent1"
    assert det.visible_resources() == [] and det.lines_active  # map resources step aside
    det.vehicles.set_override("ornithopter")
    det.compute(2)
    st = det.page_state()
    assert st["dest"]["x"] > 2_900_000  # five containers at 81% beat one cage at 5%
    assert "source" in st["dest"]["detail"]
    info = det.snapshot()["itemInfo"]
    assert info["sources"][0]["name"] == "Primitive Container" and info["sources"][0]["count"] == 5
    assert info["vendors"][0]["name"] == "Zoe"
    json.dumps(det.snapshot())


def test_emptied_container_is_no_longer_a_source(tmp_path):
    det = detector(tmp_path)
    session(det, "survival_1")
    det.ui("minimap_toggle", "item:t4mysatarilcomponent1")
    pose(det, 199_000, 0, t=1)
    cage = {"id": "u", "actorType": "BP_TemporaryLootContainer_UltraRareContainer_C", "markerType": "LootContainer",
            "spriteKey": "lootcontainer", "x": 200_000, "y": 0, "z": 0}
    det.handle(BusMessage("delta", "dune.map.actors", {"worldId": "survival_1", "coriolisWorldSeed": 7, "actors": [cage]}), 2)
    assert any(n.source == "live" and n.x == 200_000 for n in det.item_nodes(det.resource_key, 2))
    det.handle(BusMessage("delta", "dune.map.actors", {"worldId": "survival_1", "coriolisWorldSeed": 7, "actors": []}), 3)
    assert not any(n.x == 200_000 for n in det.item_nodes(det.resource_key, 3))


def test_an_item_target_survives_restarts_and_unknown_items_fall_back(tmp_path):
    det = detector(tmp_path)
    det.ui("resource", "item:t4mysatarilcomponent1")
    again = Detector(None, data_dir=tmp_path)
    assert again.resource_key == "item:t4mysatarilcomponent1" and again.resource.name == "Diamondine Dust"
    det.ui("resource", "item:nope")  # unknown: ignored
    assert det.resource_key == "item:t4mysatarilcomponent1"
    det.ui("next_resource")  # cycling from an item goes back to the map resources
    assert det.resource_key == RESOURCES[0].key


SHIP_CRAWL = {"built": 1, "materials": [
    {"id": "t1explorationcomponent", "name": "Holtzman Actuator", "categories": ["items/misc/components"],
     "drops": [
         # gaming.tools lists the crashing ships at the map origin: they have no fixed place
         *[["survival_1", 0, 0, 0, "Fallen Shipwreck", 0.96, 40, 111] for _ in range(4)],
         ["survival_1", 500_000, 0, 0, "Basic Container", 0.5, 1, 4],  # 5 km away, poor
         ["survival_1", -400_000, 8_000, 0, "Basic Container", 0.5, 1, 4],  # inside a named shipwreck
     ]}]}
SHIP = {"id": "wreck-1", "actorType": "BP_SmallShipwreck_01_C", "markerType": "SalvageSteel", "spriteKey": "salvagesteel",
        "x": 150_000, "y": 0, "z": 0, "reportable": True}


def _actors(det, t, *acts):
    det.handle(BusMessage("delta", "dune.map.actors", {"worldId": "survival_1", "coriolisWorldSeed": 7, "actors": list(acts)}), t)


def test_parts_from_downed_ships_go_to_a_seen_ship_never_to_the_map_origin(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.items.ingest(SHIP_CRAWL)
    session(det, "survival_1")
    pose(det, 10, 0, t=1)
    det.ui("minimap_toggle", "item:t1explorationcomponent")
    det.vehicles.set_override("ornithopter")
    [g] = det.items.picker("survival_1")
    assert g["items"] == [["item:t1explorationcomponent", "Holtzman Actuator", 2, True, False]]  # 2 fixed places + ships
    assert all(abs(n.x) > 1000 for n in det.item_nodes(det.resource_key, 1))  # nothing at the origin
    _actors(det, 2, SHIP)
    det.compute(3)
    assert round(det.page_state()["dest"]["x"]) == 150_000  # the downed ship: 4 x 96% x ~75
    det.on_shipwrecks({"map": "survival_1", "wrecks": [["The Wreck of the Pallas", -400_000, 0, 0]]})
    src = {s["where"]: s for s in det.snapshot()["itemInfo"]["sources"]}
    assert src["ships"]["count"] == 1 and src["ships"]["nearestM"] == 1500 and src["ships"]["name"] == "Downed ships"
    assert src["wreck"]["name"] == "The Wreck of the Pallas (Basic Container)" and src["fixed"]["count"] == 1


UNIQUE_CRAWL = {"built": 1, "materials": [], "mapNames": {"cb_survivalchallenge_station_15": "Imperial Testing Station No. 15"},
                "uniques": [{"id": "regiscontainer2", "name": "Upgraded Regis Spice Container", "rarity": "Unique",
                             "categories": ["items/gear"], "drops": [
                                 # six near you: more expected in total (0.18), but only 16.7 % to find one
                                 *[["survival_1", 100_000 + 2_000 * i, 0, 0, "Rare Container", 0.03, 1, 1] for i in range(6)],
                                 ["survival_1", -800_000, 0, 0, "Ultra Rare Container", 0.17, 1, 1],  # far: most likely
                                 ["cb_survivalchallenge_station_15", 1000, 0, 0, "Ultra Rare Container", 0.2, 1, 1],
                                 ["deepdesert_1_03", 5, 5, 0, "Rare Container", 0.04, 1, 1],
                                 ["deepdesert_1_07", 5, 5, 0, "Rare Container", 0.06, 1, 1]]}]}


def test_unique_items_are_chance_finds_with_search_areas_and_other_regions(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.items.ingest(UNIQUE_CRAWL)
    session(det, "survival_1")
    pose(det, 0, 0, t=1)
    det.vehicles.set_override("ornithopter")
    det.ui("minimap_toggle", "item:regiscontainer2")
    assert det.resource.chance and det.lines_active
    det.compute(2)
    st = det.page_state()
    assert st["route"] is None and st["dest"]["x"] == -800_000  # the most likely place, not the nearest
    line, *rings = st["alternatives"]
    assert line["kind"] == "chance" and line["dash"] and line["x"] == -800_000
    assert [r["kind"] for r in rings] == ["chance-area", "chance-area"] and all(r["pulse"] for r in rings)
    assert rings[0]["ring"] > rings[1]["ring"]  # 17 % against 16.7 %: the bigger ring
    assert "17% chance" in (det.badge(now=0) or "")
    info = det.snapshot()["itemInfo"]
    assert info["chance"] and [(e["region"], e["bestChance"]) for e in info["elsewhere"]] == [
        ("Imperial Testing Station No. 15", 0.2), ("Deep Desert", 0.06)]
    [g] = [g for g in det.items.picker("survival_1") if g["group"] == "Uniques & schematics"]
    assert g["items"][0][4] is True  # tagged "chance" on the settings page
    assert ItemSources(tmp_path).items["regiscontainer2"]["chance"]  # kept in the cache


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_item_search_forgives_typos_and_ranks_the_closest_names_first():
    html = resources.files("harvesting_dune").joinpath("viewer.html").read_text("utf8")
    code = html[html.index("// fuzzy:start"):html.index("// fuzzy:end")] + r"""
const names = ['Upgraded Regis Spice Container', 'Holtzman Actuator', 'Improved Holtzman Actuator', 'Mechanical Parts',
  'Diamondine Dust', 'Iron Ore'];
const rank = (q) => names.map((n) => [fuzzy(q, n), n]).filter(([s]) => s >= MATCH).sort((a, b) => b[0] - a[0]).map(([, n]) => n);
console.log(JSON.stringify({ regis: rank('regis'), holz: rank('holzman actuater'), dimon: rank('dimondine'), none: rank('xqzv') }));
"""
    out = subprocess.run(["node", "-"], input=code, capture_output=True, text=True, timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout.splitlines()[0])
    assert r["regis"] == ["Upgraded Regis Spice Container"]
    assert r["holz"] == ["Holtzman Actuator", "Improved Holtzman Actuator"]
    assert r["dimon"] == ["Diamondine Dust"] and r["none"] == []


# gaming.tools' shape: unique gear keeps its drop places on its schematic (you craft the item from it)
CDN_ITEMS = {
    "dust": {"name": "Diamondine Dust", "categories": ["items/misc/components/x"], "rarity": "Common", "dropLocations": [
        {"map": {"id": "survival_1", "name": "Hagga Basin"}, "locations": [{"x": 1, "y": 2, "z": 3, "name": "Rare Container", "chance": 0.5}]}]},
    "drill": {"name": "Regis Burst Drillshot", "categories": ["items/weapons"], "rarity": "Unique", "isSchematic": False,
              "schematic": {"id": "s1", "dropLocations": [{"map": {"id": "cb_ecolab_bronze_green_089", "name": "Ecolab 89"},
                                                        "locations": [{"x": 100, "y": 0, "z": 0, "name": "Ultra Rare Container", "chance": 0.09}]}]}},
    "regis": {"name": "Upgraded Regis Spice Container", "categories": ["items/vehicles"], "rarity": "Unique", "schematic": {"id": "s2"},
              "schematicSoldBy": [{"entity": {"name": "Landsraad Vehicle Vendor"}, "price": 185000, "stockAmount": 1}]},
    "jacket": {"name": "Sentinel Jacket", "categories": ["items/garment"], "rarity": "Unique", "schematic": {"id": "s3"},
               "rewardFrom": [{"mainCategoryId": "contracts", "name": "Target the Top"}]},
    "nosource": {"name": "Nowhere Boots", "categories": ["items/garment"], "rarity": "Unique", "schematic": {"id": "s4"}},
}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_crawl_finds_unique_gear_through_its_schematic_vendors_and_rewards(tmp_path):
    from harvesting_dune.loot import CRAWL_SCRIPT

    code = r"""
const enc = (v) => { const arr = []; const put = (x) => { const i = arr.length; arr.push(null);
  arr[i] = Array.isArray(x) ? x.map(put) : x && typeof x === 'object' ? Object.fromEntries(Object.entries(x).map(([k, y]) => [k, put(y)])) : x; return i; };
  put(v); return arr; };
const ITEMS = DATA;
const ents = Object.keys(ITEMS).map((id) => ({ id, mainCategoryId: 'items', name: ITEMS[id].name }));
globalThis.fetch = async (url) => { const p = new URL(url).pathname; const m = p.match(/\/items\/([^/]+)\.d\.json$/);
  const body = p.endsWith('/entities.d.json') ? ents : m && ITEMS[m[1]] ? { id: m[1], ...ITEMS[m[1]] } : null;
  return { ok: !!body, json: async () => enc(body) }; };
eval(SCRIPT).then((s) => { console.log(s); process.exit(0); });
""".replace("DATA", json.dumps(CDN_ITEMS)).replace("SCRIPT", json.dumps(CRAWL_SCRIPT))
    out = subprocess.run(["node", "-"], input=code, capture_output=True, text=True, timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    crawl = json.loads(out.stdout.splitlines()[0])
    uniques = {u["id"]: u for u in crawl["uniques"]}
    assert set(uniques) == {"drill", "regis", "jacket"}  # an item with no source at all is left out
    assert uniques["drill"]["via"] == "schematic" and uniques["drill"]["drops"][0][:5] == ["cb_ecolab_bronze_green_089", 100, 0, 0, "Ultra Rare Container"]
    assert crawl["mapNames"]["cb_ecolab_bronze_green_089"] == "Ecolab 89"
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.items.ingest(crawl)
    session(det, "survival_1")
    rows = {r[1]: r for g in det.items.picker("survival_1") for r in g["items"]}
    assert rows["Regis Burst Drillshot"][4] is True and rows["Upgraded Regis Spice Container"][4] is False  # sold, not luck
    det.ui("minimap_toggle", "item:drill")
    info = det.snapshot()["itemInfo"]
    assert info["chance"] and info["via"] == "schematic" and info["elsewhere"][0]["region"] == "Ecolab 89"
    det.ui("minimap_toggle", "item:regis")
    info = det.snapshot()["itemInfo"]
    assert not info["chance"] and info["via"] == "schematic" and info["vendors"][0]["name"] == "Landsraad Vehicle Vendor"
    det.ui("minimap_toggle", "item:jacket")
    assert det.snapshot()["itemInfo"]["reward"] == ["Target the Top"]


def test_consumables_are_never_targets_even_from_an_old_cache(tmp_path):
    crawl = {"built": 1, "materials": [
        {"id": "water", "name": "Water Pack", "categories": ["items/utility/consumables"],
         "drops": [["survival_1", 1, 1, 0, "Basic Container", 0.9, 1, 1]]}]}
    src = ItemSources(tmp_path)
    assert src.ingest(crawl) == 0
    # a cache written before consumables were dropped
    src._set(1, {"water": {"name": "Water Pack", "group": "Consumables", "sold": []}}, {"survival_1": {"water": [(1, 1, 0, "x", 0.9, 1, 1)]}})
    assert not src.items and not src.is_item("item:water")
    det = Detector(None, data_dir=tmp_path)
    det.ui("resource", "item:water")
    assert det.resource_key != "item:water"
