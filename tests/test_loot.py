import json

from helpers import pose, session

from harvesting_dune.app import Detector
from harvesting_dune.feed import BusMessage
from harvesting_dune.loot import LootIndex, _combine, cage_kind

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
    # walk up to the ultra rare and loot it: it vanishes while you stand next to it
    pose(det, 29_500, 0, t=4)
    det.handle(BusMessage("delta", "dune.map.actors", {"worldId": "survival_1", "coriolisWorldSeed": 7, "actors": cages[1:]}), 5)
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
