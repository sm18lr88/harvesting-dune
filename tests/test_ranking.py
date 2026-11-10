"""Ranking results applied on the event loop, the move-on notice, hysteresis and area bounds."""

import asyncio
import copy
import math
import threading
import time

import pytest
from helpers import M, actors, ore, pose, session, spice
from test_sync_receive import FAR, response, sighting, target_x
from test_sync_receive import detector as guild_detector

from harvesting_dune import areas as A
from harvesting_dune.app import Detector
from harvesting_dune.areas import Area, Node
from harvesting_dune.catalog import BY_KEY
from harvesting_dune.feed import BusMessage
from harvesting_dune.governor import FORGET_AFTER_S
from harvesting_dune.model import Pose
from harvesting_dune.ranking import ROUTED_AREAS
from harvesting_dune.store import REMEMBER_SPICE_S
from harvesting_dune.terrain import CELL_CM, VEHICLES, TerrainGrid


def test_a_ranking_finishing_after_the_resource_changed_is_dropped(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    session(det, "Arrakeen")
    pose(det, 0, 0, t=1)
    actors(det, spice(1, 400 * M, 0, size="Large", fill=0.9), ore(2, -300 * M, 0), t=2, world="Arrakeen")
    started, go = threading.Event(), threading.Event()
    rank = det._rank

    def held_rank(job):
        started.set()
        go.wait(5)
        return rank(job)

    det._rank = held_rank

    async def run():
        task = asyncio.create_task(det.maybe_rescore_async(10.0))
        assert await asyncio.to_thread(started.wait, 5)
        det.ui("resource", "iron")  # the settings page click lands while the worker ranks spice
        go.set()
        await task

    asyncio.run(run())
    assert det.resource_key == "iron"
    assert det.areas == []


def _area(x, utility, n=5):
    a = Area(x, 0, 0, [Node("magnetiteore", x, 0, 0, "live") for _ in range(n)])
    a.feasible, a.utility, a.distance_m = True, utility, round(abs(x) / 100)
    return a


def test_no_move_on_notice_when_the_current_target_is_kept(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    session(det, "Arrakeen")
    pose(det, 0, 0, t=1)
    det.ui("resource", "iron")
    det.areas = [_area(0, 1.0)]
    det.mined_nodes.record("k1", "magnetiteore", 10 * M, 0, 0, 1.0)
    here, far = _area(50 * M, 1.0), _area(1500 * M, 1.10)  # 10% better: not clearly beaten
    det._apply_ranking((det.store.world, [], None, None, [], det.resource_key, det._scored_at), [far, here], time.perf_counter())
    assert det.areas[0] is here
    assert det.notice is None


def test_a_pruned_current_target_keeps_the_switch_margin(tmp_path):
    res, v, p = BY_KEY["iron"], VEHICLES["ornithopter"], Pose(0, 0, 0)
    nodes = []
    for k in range(14):  # equal clusters at almost the same distance: routing stops before the last ones
        ang, r = k * 2 * math.pi / 14, 1000 * M + 30 * M * k
        nodes += [Node("magnetiteore", r * math.cos(ang) + i * 100, r * math.sin(ang), 0, "live") for i in range(5)]
    out = A.recommend(res, nodes, p, TerrainGrid(), v, top=0, need=ROUTED_AREAS)
    pruned = next(a for a in out if "cannot beat the areas above" in a.reasons)
    prev = copy.deepcopy(pruned)
    prev.reasons = []
    A._evaluate([prev], p, TerrainGrid(), v, None, False, v)
    prev.feasible = True
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    session(det, "Arrakeen")
    pose(det, 0, 0, t=1)
    det.ui("resource", "iron")
    det.areas = [prev]
    det._apply_ranking((det.store.world, [], None, None, [], det.resource_key, det._scored_at), out, time.perf_counter())
    assert math.hypot(det.areas[0].x - prev.x, det.areas[0].y - prev.y) < A.AREA_RADIUS_CM
    assert "kept as your target" in det.areas[0].reasons[0]


def test_the_upper_bound_covers_how_routes_measure_cells():
    v, g = VEHICLES["buggy"], TerrainGrid()
    for i in range(-5, 20):  # a strip you drove: proven, cost 1.0
        for j in (-1, 0, 1):
            g.add_driven((i + 0.5) * CELL_CM, (j + 0.5) * CELL_CM, 0, "buggy")
    p = Pose(0.49 * CELL_CM, 0.5 * CELL_CM, 0)  # near the east edge of its cell
    a = Area(400 * M, 0.5 * CELL_CM, 0, [Node("magnetiteore", 400 * M, 0.5 * CELL_CM, 0, "live") for _ in range(3)])
    bound = A._upper_bound(a, p, v, None, False, v)
    A._evaluate([a], p, g, v, None, False, v)
    assert a.feasible
    assert a.utility <= bound


def test_a_cluster_of_zero_value_nodes_stays_where_its_nodes_are(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    session(det, "Arrakeen")
    pose(det, 5000 * M, 5000 * M, t=1)
    actors(det, spice(1, 5010 * M, 5000 * M, size="Large", fill=0.0), t=2, world="Arrakeen")
    nodes = det.nodes({"spicefieldlarge"}, 2)
    assert len(nodes) == 1 and nodes[0].value() == 0
    (area,) = A._clusters(nodes)
    assert (area.x, area.y) == (5010 * M, 5000 * M)


def _dust_detector(tmp_path, *rows):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.items.ingest({"built": 1_790_000_000_000, "materials": [
        {"id": "dust", "name": "Diamondine Dust", "categories": ["items/misc/components"], "drops": [list(r) for r in rows]}]})
    return det


def _chest(id_, x, y, **extra):
    return {"id": id_, "actorType": "BP_TemporaryLootContainer_BasicContainer_C", "markerType": "LootContainer",
            "spriteKey": "lootcontainer", "x": x, "y": y, "z": 0, **extra}


def _listed(det, t, *acts):
    det.handle(BusMessage("delta", "dune.map.actors", {"worldId": "survival_1", "coriolisWorldSeed": 7, "actors": list(acts)}), t)


def test_one_container_listed_twice_for_an_item_is_one_candidate_worth_both_rows(tmp_path):
    det = _dust_detector(tmp_path, *[["deepdesert_1_07", 50_000, 50_000, 0, "Primitive Container", c, 1, 8] for c in (0.4, 0.8)])
    session(det, "deepdesert_1")
    det.map_files[det.store.world] = "deepdesert_1_07"
    pose(det, 0, 0, t=1)
    nodes = det.item_nodes("item:dust", 2)
    assert [(n.x, n.y) for n in nodes] == [(50_000, 50_000)]
    assert nodes[0].weight == pytest.approx((0.4 + 0.8) * (1 + 8) / 2)


def test_a_container_a_fraction_of_a_centimetre_from_its_source_across_a_cell_edge_is_live(tmp_path):
    # the crawl rounds the game's position: the container at (-908111.56, -906100.1) is stored as (-908112, -906100)
    det = _dust_detector(tmp_path, ["survival_1", -908_112, -906_100, 0, "Primitive Container", 0.8, 1, 8])
    session(det, "survival_1")
    pose(det, -908_112 + 30 * M, -906_100, t=1)
    _listed(det, 2, _chest("p1", -908_111.56, -906_100.1))
    assert [n.source for n in det.item_nodes("item:dust", 3)] == ["live"]


def test_an_emptied_container_stays_no_candidate_for_the_hour_after_the_detector_forgets_it(tmp_path):
    det = _dust_detector(tmp_path, ["survival_1", 100_000, 0, 0, "Primitive Container", 0.8, 1, 8])
    session(det, "survival_1")
    det.ui("resource", "item:dust")
    pose(det, 99_000, 0, t=1)
    _listed(det, 2, _chest("p1", 100_000, 0, hasLoot=False))  # you emptied it
    det.maybe_rescore(2)
    pose(det, 1_000_000, 0, t=3)
    _listed(det, 4)  # 9 km away: unloaded
    later = 4 + FORGET_AFTER_S + 60
    det.store.prune(later, FORGET_AFTER_S, REMEMBER_SPICE_S)  # what the governor does
    assert det.store.items == {}
    assert det.item_nodes("item:dust", later) == []


def test_a_container_the_companion_lists_emptied_away_from_you_is_no_source(tmp_path):
    det = _dust_detector(tmp_path, ["survival_1", 100_000, 0, 0, "Primitive Container", 0.8, 1, 8])
    session(det, "survival_1")
    pose(det, 0, 0, t=1)  # 1 km away: someone else emptied it
    _listed(det, 2, _chest("p1", 100_000, 0, hasLoot=False))
    assert det.item_nodes("item:dust", 3) == []


def test_a_cage_the_companion_lists_emptied_is_no_target_until_it_shows_loot_again(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    session(det, "survival_1")
    det.ui("resource", "cages")
    pose(det, 0, 0, t=1)
    cage = {"id": "u1", "actorType": "BP_TemporaryLootContainer_UltraRareContainer_C", "markerType": "LootContainer",
            "spriteKey": "lootcontainer", "x": 30_000, "y": 0, "z": 0}
    _listed(det, 2, {**cage, "hasLoot": False})  # 300 m away: someone else emptied it
    det.compute(3)
    assert det.page_state()["dest"] is None
    _listed(det, 4, {**cage, "hasLoot": True})
    det.compute(5)
    assert round(det.page_state()["dest"]["x"]) == 30_000


def test_an_expired_active_guild_report_leaves_a_spice_site_to_the_crowd_not_to_an_older_empty_report():
    det = guild_detector()
    det.load_scan([{"type": "BP_SpiceField_Large_C", "map_marker_id": "spicefieldlarge", "x": FAR, "y": 0, "z": 0,
                    "metadata": {"spawned": True, "fillRate": 1}}], 200, 1)
    [row] = det.scan.nodes()
    empty = sighting("stale", FAR, 0, age=REMEMBER_SPICE_S + 15 * 60, rowKey=row.row_key, distCm=1500.0)
    active = sighting("spice", FAR, 0, age=REMEMBER_SPICE_S + 5 * 60)  # newer, but too old to count
    det.ingest_peer(response(sightings=[empty, active]), 10.0)
    assert target_x(det) == FAR
