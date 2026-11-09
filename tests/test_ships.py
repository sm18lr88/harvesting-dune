"""Downed ships, named shipwrecks and buggy / ornithopter wrecks as places to search."""

from helpers import pose, session

from harvesting_dune.app import Detector
from harvesting_dune.feed import BusMessage

DD = "deepdesert_1"
SHIP_X, SHIP_Y = 0.0, 290_000.0  # row D: a PvE row


def ship(id_="s1", x=SHIP_X, y=SHIP_Y):
    return {"id": id_, "actorType": "BP_SmallShipwreck_01_C", "markerType": "SalvageSteel", "spriteKey": "salvagesteel",
            "x": x, "y": y, "z": 0.0, "reportable": True}


def container(id_, x, y):
    return {"id": id_, "actorType": "BP_SmallWreck_LootContainer_FlatPanel_C", "markerType": "LootContainer",
            "spriteKey": "lootcontainer", "x": x, "y": y, "z": 0.0}


def wreckage(id_, actor_type, x, y):
    return {"id": id_, "actorType": actor_type, "markerType": "ScrapMetalWreckage", "spriteKey": "scrapmetalwreckage",
            "resourceItemId": "scrapmetal", "x": x, "y": y, "z": 0.0, "reportable": True}


def feed(det, t, *acts, world=DD):
    det.handle(BusMessage("delta", "dune.map.actors", {"worldId": world, "coriolisWorldSeed": 7, "actors": list(acts)}), t)


def dd_detector():
    det = Detector(None, min_rescore_s=0)
    det.ui("vehicle", "ornithopter")
    session(det, DD)
    return det


def test_a_downed_ship_is_announced_remembered_and_a_target():
    det = dd_detector()
    det.ui("resource", "iron")
    pose(det, SHIP_X, SHIP_Y + 90_000, t=1)  # 900 m away
    feed(det, 100, ship())
    assert det.badge(now=100) == "Downed ship! 0.9 km away"  # whatever you search
    assert det.badge(now=200) != "Downed ship! 0.9 km away"  # for 90 s
    assert "SalvageSteel" not in det.minimap_filter()[1]  # the companion always shows downed ships
    det.ui("resource", "downed_ships")
    assert det.visible_resources() == [] and det.lines_active  # the map resources step aside
    det.compute(101)
    assert det.page_state()["dest"]["y"] == SHIP_Y  # in a PvE row: PvP-only is about yields, not ships
    pose(det, SHIP_X, SHIP_Y + 190_000, t=102)  # 1.9 km: the game unloads it
    feed(det, 103)
    det.compute(104)
    st = det.page_state()
    assert st["dest"]["y"] == SHIP_Y and "remembered" not in st["dest"]["detail"]
    assert [a["id"] for a in st["extraActors"] if a["id"].startswith("dd-ship-")] == ["dd-ship-s1"]
    [row] = det.snapshot()["ships"]
    assert row["distanceM"] == 1900 and not row["loaded"] and row["zone"] == "D PvE"


def report(det, x=SHIP_X, y=SHIP_Y):
    """A downed ship another player reported (gaming.tools scan database row)."""
    det.scan.load_rows([{"world": DD, "seed": 7, "type": "BP_SmallShipwreck_01_C", "map_marker_id": "salvagesteel",
                         "x": x, "y": y, "z": 0.0}], 200, 0)


def test_a_ship_other_players_reported_is_a_target_far_beyond_the_load_range():
    det = dd_detector()
    det.ui("resource", "downed_ships")
    report(det)
    pose(det, SHIP_X, SHIP_Y + 500_000, t=1)  # 5 km: the game shows a hull only within ~0.9 km
    det.compute(2)
    st = det.page_state()
    assert st["dest"]["y"] == SHIP_Y and "not yet confirmed live" in st["dest"]["detail"]
    assert det.snapshot()["shipsReported"] == 1


def test_a_reported_ship_where_you_saw_one_gone_is_never_a_target():
    det = dd_detector()
    det.ui("resource", "downed_ships")
    report(det)
    pose(det, SHIP_X, SHIP_Y + 90_000, t=1)
    feed(det, 2, ship())
    pose(det, SHIP_X, SHIP_Y + 30_000, t=3)  # 300 m and no hull: a worm took it
    feed(det, 4)
    det.compute(5)
    assert det.page_state()["dest"] is None and det.snapshot()["shipsReported"] == 0


def test_a_reported_ship_you_reach_and_do_not_see_is_dropped_and_the_route_moves_on():
    det = dd_detector()
    det.ui("resource", "downed_ships")
    far_y = SHIP_Y + 300_000  # a second reported ship 3 km farther north
    det.scan.load_rows([{"type": "BP_SmallShipwreck_01_C", "map_marker_id": "salvagesteel", "x": SHIP_X, "y": y,
                         "z": 0.0} for y in (SHIP_Y, far_y)], 200, 0)
    pose(det, SHIP_X, SHIP_Y - 500_000, t=1)
    det.compute(1)
    assert det.page_state()["dest"]["y"] == SHIP_Y
    # live: hovering 410 m up, 54 m from the reported place; the game shows no hull there
    pose(det, SHIP_X, SHIP_Y - 5_400, z=41_000, t=2)
    feed(det, 3)  # snapshots come ~20 s apart: flying past, one is all you get in range
    det.compute(15)
    assert det.page_state()["dest"]["y"] == far_y and det.snapshot()["shipsReported"] == 1
    assert det.scan.expire(15 + 4 * 3600) == 0  # a worm took it: it does not come back like a node
    assert det.snapshot()["shipsReported"] == 1


def test_the_next_ship_stays_the_target_until_another_is_clearly_nearer():
    det = dd_detector()
    det.ui("resource", "downed_ships")
    north, south = SHIP_Y + 60_000, SHIP_Y - 64_000
    det.scan.load_rows([{"type": "BP_SmallShipwreck_01_C", "map_marker_id": "salvagesteel", "x": SHIP_X, "y": y,
                         "z": 0.0} for y in (north, south)], 200, 0)
    pose(det, SHIP_X, SHIP_Y, t=1)
    det.compute(1)
    assert det.page_state()["dest"]["y"] == north
    # live (journal-20261108-124343): 70 m of flight made the other ship 20 m nearer and the line flipped
    pose(det, SHIP_X, SHIP_Y - 3_000, t=2)
    det.compute(2)
    assert det.page_state()["dest"]["y"] == north
    pose(det, SHIP_X, SHIP_Y - 20_000, t=3)  # now 440 m against 800 m
    det.compute(3)
    assert det.page_state()["dest"]["y"] == south


def test_the_next_ship_stays_the_target_when_the_ranking_leaves_it_unrouted(monkeypatch):
    from harvesting_dune import ranking

    det = dd_detector()
    det.ui("resource", "downed_ships")
    near, far = SHIP_Y + 60_000, SHIP_Y + 500_000
    det.scan.load_rows([{"type": "BP_SmallShipwreck_01_C", "map_marker_id": "salvagesteel", "x": SHIP_X, "y": y,
                         "z": 0.0} for y in (near, far)], 200, 0)
    pose(det, SHIP_X, SHIP_Y, t=1)
    det.compute(1)
    assert det.page_state()["dest"]["y"] == near
    rank = ranking.recommend

    def pruned(*args, **kw):  # live: more valuable PvP ships filled the routed set
        areas = rank(*args, **kw)
        for a in areas:
            if a.y == near:
                a.feasible, a.reasons = False, ["cannot beat the areas above"]
        return areas

    monkeypatch.setattr(ranking, "recommend", pruned)
    pose(det, SHIP_X, SHIP_Y + 3_000, t=2)
    det.compute(2)
    st = det.page_state()
    assert st["dest"]["y"] == near and "kept as your next stop" in st["dest"]["detail"]


def test_a_reported_ship_the_game_shows_stays():
    det = dd_detector()
    det.ui("resource", "downed_ships")
    report(det)
    pose(det, SHIP_X, SHIP_Y - 5_400, t=1)
    feed(det, 2)  # the hull loads a moment late
    feed(det, 24, ship())
    assert not det.scan.stale


def test_a_ship_loaded_again_under_a_new_id_is_the_same_ship():
    det = dd_detector()
    pose(det, SHIP_X, SHIP_Y + 90_000, t=1)
    feed(det, 2, ship("a"))
    det.ship_alert = None
    feed(det, 3)
    feed(det, 4, ship("b", SHIP_X + 20, SHIP_Y))
    assert len(det.ships.ships) == 1 and det.ship_alert is None


def test_a_ship_missing_up_close_is_gone():
    det = dd_detector()
    det.ui("resource", "downed_ships")
    pose(det, SHIP_X, SHIP_Y + 90_000, t=1)
    feed(det, 2, ship())
    pose(det, SHIP_X, SHIP_Y + 30_000, t=3)  # 300 m: it would be loaded
    feed(det, 4)
    det.compute(5)
    assert det.page_state()["dest"] is None and det.snapshot()["ships"][0]["gone"]


def test_a_ship_you_emptied_is_no_longer_a_target():
    det = dd_detector()
    det.ui("resource", "downed_ships")
    xs = (SHIP_X + 3_000, SHIP_X - 3_000, SHIP_X + 9_000)
    first = [container(f"c{i}", x, SHIP_Y) for i, x in enumerate(xs)]
    again = [container(f"r{i}", x, SHIP_Y) for i, x in enumerate(xs)]  # loaded again: new ids, same places
    pose(det, SHIP_X, SHIP_Y + 6_000, t=1)  # 60 m away: unloading is not opening
    feed(det, 2, ship(), *first)
    feed(det, 3, ship())
    feed(det, 4, ship(), *again)
    t = 5
    for i, x in enumerate(xs):
        det.compute(t)
        assert det.page_state()["dest"]  # a target until the last container is opened
        pose(det, x, SHIP_Y + 500, t=t + 1)
        feed(det, t + 2, ship(), *again[i + 1:])  # it vanished right next to you: you opened it
        t += 3
    det.compute(t)
    assert det.page_state()["dest"] is None and det.snapshot()["ships"][0]["emptied"]


def test_buggy_and_ornithopter_wrecks_leave_the_scrap_heaps_out():
    det = Detector(None, min_rescore_s=0)
    det.ui("vehicle", "ornithopter")
    session(det, "survival_1")
    pose(det, 0, 0, t=1)
    heap = wreckage("h", "BP_ScrapMetal_A_Spawner_C", 30_000, 0)
    orni = wreckage("o", "BP_Ornithopter_GRD_Encounter_Spawner_C", 200_000, 0)
    feed(det, 2, heap, orni, world="survival_1")
    det.load_scan([{"type": "BP_Buggy_GRD_Encounter_Spawner_C", "map_marker_id": "scrapmetalwreckage", "x": -300_000.0,
                    "y": 0.0, "z": 0.0},
                   {"type": "BP_ScrapMetal_B_Spawner_C", "map_marker_id": "scrapmetalwreckage", "x": 0.0, "y": 50_000.0,
                    "z": 0.0}], 200, 3)
    det.ui("resource", "wrecks")
    assert det.visible_resources() == ["scrap"] and det.lines_active  # the minimap shows salvage markers
    det.compute(4)
    targets = {round(a.x) for a in det.areas}
    assert targets == {200_000, -300_000}  # the ornithopter (live) and the buggy (scan row); no heaps
    assert det.page_state()["dest"]["x"] == 200_000


def test_scattered_wrecks_are_one_trip_nearest_first():
    det = Detector(None, min_rescore_s=0)
    det.ui("vehicle", "ornithopter")
    session(det, "survival_1")
    pose(det, 0, 0, t=1)
    xs = [100_000, -150_000, 300_000, 10_000_000]  # the last one is 100 km out
    feed(det, 2, *[wreckage(f"o{i}", "BP_Ornithopter_GRD_Encounter_Spawner_C", x, 0) for i, x in enumerate(xs)],
         world="survival_1")
    det.ui("resource", "wrecks")
    det.compute(3)
    st = det.page_state()
    # nearest (1 km), then on from there: 300k is 2 km further, -150k would be 2.5 km back
    assert [s["x"] for s in st["route"]["stops"]][:3] == [100_000, 300_000, -150_000]
    assert st["alternatives"] == [] and "next 1.0 km" in (det.badge(now=0) or "")


def test_named_shipwrecks_from_the_map_data_are_targets():
    det = Detector(None, min_rescore_s=0)
    det.ui("vehicle", "ornithopter")
    session(det, "survival_1")
    pose(det, -48_000, -60_000, t=1)
    assert det.shipwrecks_wanted()
    # the map window shows another map than the one you play on: not this world's wrecks
    assert not det.on_shipwrecks({"map": "deepdesert_1_07", "wrecks": [["Elsewhere", 1.0, 2.0, 3.0]]})
    assert not det.on_shipwrecks([["no map id", 1.0, 2.0, 3.0]]) and det.shipwrecks_wanted()
    assert det.on_shipwrecks({"map": "survival_1", "wrecks": [["The Wreck of the Pallas", -48_675.0, -70_247.0, 4_799.0],
                                                            ["bad", "x", 1, 2], "junk"]})
    assert not det.shipwrecks_wanted() and not det.on_shipwrecks("nope")
    det.ui("resource", "shipwrecks")
    det.compute(2)
    assert det.page_state()["dest"]["x"] == -48_675.0
    assert det.snapshot()["shipwrecks"] == [{"name": "The Wreck of the Pallas", "distanceM": 103, "zone": None}]
