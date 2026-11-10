import asyncio
import json
import shutil
import time

import pytest
from helpers import ore, pose, session

from harvesting_dune import runner
from harvesting_dune.app import Detector
from harvesting_dune.feed import BusMessage
from harvesting_dune.itemsources import ItemSources
from harvesting_dune.loot import MAX_AGE_S, LootIndex, _combine, cage_kind

CRAWL = {
    "built": 1_790_000_000_000,
    "containers": [
        {"map": "survival_1", "x": 1000.0, "y": 2000.0, "z": 0.0, "name": "Ultra Rare Container", "drops": [
            {"id": "belt", "name": "Leap Suspensor Belt", "rarity": "Common", "tier": 2, "chance": 0.1, "min": 1, "max": 1, "price": 4800},
            {"id": "belt", "name": "Leap Suspensor Belt", "rarity": "Common", "tier": 2, "chance": 0.1, "min": 1, "max": 2, "price": 4800},
            {"id": "gun", "name": "Unique Gun", "rarity": "Unique", "tier": 5, "chance": 0.02, "min": 1, "max": 1, "unique": True},
        ]},
        {"map": "survival_1", "x": 9000.0, "y": 0.0, "z": 0.0, "name": "Basic Container", "drops": []},
        {"map": "deepdesert_1_07", "x": 5000.0, "y": 5000.0, "z": 0.0, "name": "Rare Container", "drops": [
            {"id": "servok", "name": "Advanced Servoks", "rarity": "Common", "chance": 1.0, "min": 2, "max": 5}]},
    ],
}


def test_cage_kind_covers_variants():
    assert cage_kind("BP_TemporaryLootContainer_UltraRareContainer_C") == "Ultra Rare"
    assert cage_kind("BP_TemporaryLootContainer_SmallUltraRareContainer_C") == "Ultra Rare"
    assert cage_kind("BP_TemporaryLootContainer_RareContainer_Polar_C") == "Rare"
    assert cage_kind("BP_TemporaryLootContainer_BasicContainer_C") is None


def test_repeated_rolls_combine_into_chance_of_at_least_one():
    merged = {d["id"]: d for d in _combine(CRAWL["containers"][0]["drops"])}
    assert merged["belt"]["chance"] == 0.19 and merged["belt"]["max"] == 3 and merged["gun"]["special"]
    assert _combine(CRAWL["containers"][0]["drops"])[0]["id"] == "gun"  # uniques first


def test_index_keeps_rare_cages_caches_and_matches_every_deep_desert_layout(tmp_path):
    idx = LootIndex(tmp_path)
    assert idx.stale
    assert idx.ingest_crawl(json.dumps(CRAWL)) == 2  # basic container dropped
    again = LootIndex(tmp_path)
    assert len(again.cages) == 2
    assert again.lookup("survival_1", 1000.4, 2000.2).name == "Ultra Rare Container"
    assert again.lookup("survival_1", 5000, 5000) is None
    assert again.lookup("deepdesert_1", 5000, 5000).drops[0]["name"] == "Advanced Servoks"


def test_cages_without_a_position_are_skipped(tmp_path):
    rows = [{"map": "survival_1", "x": None, "y": 0.0, "z": 0.0, "name": "Rare Container", "drops": []},
            {"map": "survival_1", "y": 0.0, "z": 0.0, "name": "Rare Container", "drops": []},  # x left out of the JSON
            CRAWL["containers"][0]]
    idx = LootIndex(tmp_path)
    assert idx.ingest_crawl(json.dumps({"built": 1, "containers": rows})) == 1
    assert LootIndex(tmp_path).lookup("survival_1", 1000, 2000).name == "Ultra Rare Container"


def test_detector_reports_live_cages_with_loot_and_drops_looted_ones(tmp_path):
    det = Detector(None, data_dir=tmp_path)
    det.loot.ingest_crawl(json.dumps(CRAWL))
    session(det, "survival_1")
    pose(det, 10, 0, 500, t=1)
    cage = {"id": "c1", "actorType": "BP_TemporaryLootContainer_UltraRareContainer_C", "markerType": "LootContainer",
            "spriteKey": "lootcontainer", "x": 1000, "y": 2000, "z": 800}
    det.handle(BusMessage("delta", "dune.map.actors", {"worldId": "survival_1", "coriolisWorldSeed": 7, "actors": [cage]}), 2)
    [c] = det.loot_nearby()
    assert c["kind"] == "Ultra Rare" and c["known"] and c["heightM"] == 3 and c["distanceM"] == 22
    assert c["drops"][0]["name"] == "Unique Gun" and c["specialChance"] == 0.02
    det.handle(BusMessage("delta", "dune.map.actors", {"worldId": "survival_1", "coriolisWorldSeed": 7, "actors": []}), 3)
    assert det.loot_nearby() == []  # looted cages vanish
    json.dumps(det.snapshot())


def test_materials_with_unique_rarity_are_not_highlighted_and_swapped_ranges_are_fixed():
    drops = [
        {"id": "dust", "name": "Spice-infused Plastanium Dust", "rarity": "Unique", "chance": 1.0, "min": 9, "max": 30,
         "categories": ["items/misc", "items/misc/refinedresources"]},
        {"id": "cap", "name": "Particle Capacitor", "rarity": "Common", "chance": 0.5, "min": 30, "max": 11},
        {"id": "cap", "name": "Particle Capacitor", "rarity": "Common", "chance": 0.5, "min": 30, "max": 11},
        {"id": "sch", "name": "Some Schematic", "rarity": "Rare", "chance": 0.05, "min": 1, "max": 1, "schematic": True},
    ]
    by = {d["id"]: d for d in _combine(drops)}
    assert not by["dust"]["special"] and by["sch"]["special"]
    assert (by["cap"]["min"], by["cap"]["max"]) == (11, 60) and by["cap"]["chance"] == 0.75


def _cage(id_, x, y, kind="UltraRareContainer"):
    return {"id": id_, "actorType": f"BP_TemporaryLootContainer_{kind}_C", "markerType": "LootContainer",
            "spriteKey": "lootcontainer", "x": x, "y": y, "z": 0}


def test_cages_are_a_resource_with_lines_to_every_cage_and_skip_looted_ones():
    det = Detector(None, min_rescore_s=0)
    session(det, "survival_1")
    det.ui("resource", "cages")
    pose(det, 10, 0, t=1)
    cages = [_cage("u1", 30_000, 0), _cage("r1", -60_000, 0, "RareContainer"), _cage("r2", 0, 90_000, "RareContainer_Polar")]
    det.handle(BusMessage("delta", "dune.map.actors", {"worldId": "survival_1", "coriolisWorldSeed": 7, "actors": cages}), 2)
    det.compute(3)
    st = det.page_state()
    assert round(st["dest"]["x"]) == 30_000  # ultra rare, closest
    assert len(st["alternatives"]) == 2  # single cages get thin lines (unlike ore)
    assert det.badge(now=0).startswith("Cages R+UR")
    # walk up to the ultra rare and loot it: the game keeps it listed, emptied (docs/HOW-IT-WORKS.md, "Done stop")
    pose(det, 29_500, 0, t=4)
    emptied = [{**cages[0], "hasLoot": False}, *cages[1:]]
    det.handle(BusMessage("delta", "dune.map.actors", {"worldId": "survival_1", "coriolisWorldSeed": 7, "actors": emptied}), 5)
    assert "u1" in det.looted
    det.compute(6)
    assert round(det.page_state()["dest"]["x"]) != 30_000


def test_cages_ignore_scan_database_rows():
    det = Detector(None, min_rescore_s=0)
    session(det, "survival_1")
    pose(det, 10, 0, t=1)
    det.load_scan([{"type": "BP_TemporaryLootContainer_UltraRareContainer_C", "map_marker_id": "lootcontainerultrarare", "x": 5000, "y": 0, "z": 0}], 200, 1)
    det.ui("resource", "cages")
    det.compute(2)
    assert det.page_state()["dest"] is None


def test_a_crawl_without_materials_is_not_repeated_until_the_index_ages(tmp_path, monkeypatch):
    class Page:
        crawls = 0

        async def evaluate(self, expr, await_promise=False, timeout=15):
            Page.crawls += 1
            return json.dumps({**CRAWL, "built": int(time.time() * 1000), "items": 500, "failed": 0})  # no materials

    class Hub:
        primary, on_attach, pages = Page(), [], {}

    waited, real_sleep = [0.0], asyncio.sleep

    async def sleep(s):  # the loop's own waits, counted instead of spent
        waited[0] += s
        if waited[0] > 600:
            raise asyncio.CancelledError
        await real_sleep(0)

    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    monkeypatch.setattr(runner.asyncio, "sleep", sleep)
    try:
        asyncio.run(runner.Runner(det, Hub()).refresh_loot_index())
    except asyncio.CancelledError:
        pass
    assert Page.crawls == 1 and det.items.items == {}


def test_ultra_rare_only_mode_ignores_rare_cages():
    det = Detector(None, min_rescore_s=0)
    session(det, "survival_1")
    pose(det, 10, 0, t=1)
    cages = [_cage("r1", 5_000, 0, "RareContainer"), _cage("u1", 80_000, 0)]
    det.handle(BusMessage("delta", "dune.map.actors", {"worldId": "survival_1", "coriolisWorldSeed": 7, "actors": cages}), 2)
    det.ui("resource", "cages_ultra")
    det.compute(3)
    assert round(det.page_state()["dest"]["x"]) == 80_000 and det.page_state()["alternatives"] == []
    assert det.visible_resources() == ["cages"]  # focus shows the whole cage family on the minimap


def _crawled_index(tmp_path, cdn_items: dict) -> LootIndex:
    from test_items import _run_crawl

    idx = LootIndex(tmp_path)
    idx.ingest_crawl(_run_crawl(cdn_items))
    return idx


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_cage_table_lists_the_unique_schematic_that_drops_there(tmp_path):
    # gaming.tools keeps unique gear's drop places on its schematic, never on the item itself
    drill = {"drill": {"name": "Regis Burst Drillshot", "categories": ["items/weapons"], "rarity": "Unique",
                       "schematic": {"id": "s1", "dropLocations": [{"map": {"id": "survival_1", "name": "Hagga Basin"}, "locations": [
                           {"x": 1000, "y": 2000, "z": 0, "name": "Ultra Rare Container", "chance": 0.09}]}]}}}
    cage = _crawled_index(tmp_path, drill).lookup("survival_1", 1000, 2000)
    assert cage is not None and [(d["name"], d["special"]) for d in cage.drops] == [("Regis Burst Drillshot", True)]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_small_ultra_rare_cage_gets_its_loot_table(tmp_path):
    dust = {"dust": {"name": "Diamondine Dust", "categories": ["items/misc/components/x"], "rarity": "Common", "dropLocations": [
        {"map": {"id": "deepdesert_1_07", "name": "Deep Desert"}, "locations": [
            {"x": 5000, "y": 5000, "z": 0, "name": "Small Ultra Rare", "chance": 0.5}]}]}}
    cage = _crawled_index(tmp_path, dust).lookup("deepdesert_1", 5000, 5000)
    assert cage is not None and [d["name"] for d in cage.drops] == ["Diamondine Dust"]


def _list(det, t, *acts):
    det.handle(BusMessage("delta", "dune.map.actors", {"worldId": "survival_1", "coriolisWorldSeed": 7, "actors": list(acts)}), t)


def test_a_cage_that_leaves_the_list_together_with_another_actor_next_to_you_is_not_emptied():
    det = Detector(None, min_rescore_s=0)
    session(det, "survival_1")
    pose(det, 0, 0, t=1)
    far = [ore(f"o{i}", 50_000 + i * 1_000, 0) for i in range(4)]
    _list(det, 2, _cage("u1", 1_000, 0), ore("near", 1_500, 0), *far)
    _list(det, 3, *far)  # two actors left at once: the list unloading, not a cage you opened
    assert "u1" not in det.looted


def test_a_cage_the_game_shows_full_again_is_no_longer_emptied():
    det = Detector(None, min_rescore_s=0)
    session(det, "survival_1")
    pose(det, 0, 0, t=1)
    other = ore("o1", 50_000, 0)
    _list(det, 2, _cage("u1", 1_000, 0), other)
    _list(det, 3, other)  # it vanished alone next to you
    _list(det, 4, {**_cage("u1", 1_000, 0), "hasLoot": True}, other)
    assert "u1" not in det.looted


def test_a_cage_you_emptied_leaves_the_rare_loot_nearby_card():
    det = Detector(None, min_rescore_s=0)
    session(det, "survival_1")
    pose(det, 0, 0, t=1)
    other = ore("o1", 50_000, 0)
    _list(det, 2, {**_cage("u1", 1_000, 0), "hasLoot": True}, other)
    _list(det, 3, {**_cage("u1", 1_000, 0), "hasLoot": False}, other)  # the game keeps it listed, emptied
    assert "u1" in det.looted and det.loot_nearby() == []


def test_a_container_the_game_shows_emptied_next_to_you_is_no_longer_a_source():
    det = Detector(None, min_rescore_s=0)
    det.items.ingest({"built": 1_790_000_000_000, "materials": [
        {"id": "dust", "name": "Diamondine Dust", "categories": ["items/misc/components"],
         "drops": [["survival_1", 100_000, 0, 0, "Primitive Container", 0.8, 1, 8]]}]})
    session(det, "survival_1")
    pose(det, 99_000, 0, t=1)
    chest = {"id": "p1", "actorType": "BP_TemporaryLootContainer_BasicContainer_C", "markerType": "LootContainer",
             "spriteKey": "lootcontainer", "x": 100_000, "y": 0, "z": 0, "hasLoot": False}
    _list(det, 2, chest)
    assert det.item_nodes("item:dust", 3) == []


def test_a_vendor_row_whose_price_or_stock_is_no_number_is_dropped(tmp_path):
    src = ItemSources(tmp_path)
    src.ingest({"built": 1_790_000_000_000, "materials": [
        {"id": "dust", "name": "Diamondine Dust", "categories": ["items/misc/components"], "drops": [],
         "sold": [["Text price", "3500", 15], ["Text stock", 100, "lots"], ["Flag price", True, 1], ["Kept", 900, None]]}]})
    assert [v["name"] for v in src.info("item:dust", "survival_1")["vendors"]] == ["Kept"]


def test_a_cage_index_built_by_a_clock_running_far_ahead_is_stale(tmp_path):
    idx = LootIndex(tmp_path)
    idx.ingest_crawl(json.dumps({**CRAWL, "built": (time.time() + MAX_AGE_S) * 1000}))
    assert idx.stale


def test_a_crawled_drop_whose_amounts_are_text_is_left_out():
    good = {"id": "belt", "name": "Belt", "rarity": "Common", "chance": 0.5, "min": 1, "max": 2}
    text = {"id": "dust", "name": "Dust", "rarity": "Common", "chance": 0.5, "min": "1", "max": "2"}
    assert [d["id"] for d in _combine([good, text, text])] == ["belt"]


def test_a_loot_index_file_with_an_infinite_coordinate_is_ignored(tmp_path):
    (tmp_path / "loot-rare.json").write_text('{"version": 3, "built": 1790000000000, "cages": [{"map": "survival_1", '
                                             '"x": 1e999, "y": 0, "z": 0, "name": "Rare Container", "drops": []}]}')
    assert LootIndex(tmp_path).cages == []
