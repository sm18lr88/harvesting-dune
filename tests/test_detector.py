import json
from pathlib import Path

import pytest
from helpers import CELL_CM, M, actors, ore, pose, session, spice

from harvesting_dune.app import Detector
from harvesting_dune.cli import _LoadIn
from harvesting_dune.corrections import ScanIndex, row_key
from harvesting_dune.feed import BusMessage
from harvesting_dune.model import Pose, parse_actors
from harvesting_dune.sync import SyncClient, SyncServerError
from harvesting_dune.vehicle import VehicleDetector


def drive(det_or_vd, speed_cms, seconds=12, z=0.0, t0=0.0, x0=10.0, ground=None):
    """Feed poses one per second moving along +x."""
    for i in range(seconds + 1):
        p = {"x": x0 + speed_cms * i, "y": 5.0, "z": z}
        if isinstance(det_or_vd, VehicleDetector):
            det_or_vd.on_pose(t0 + i, Pose(p["x"], p["y"], p["z"]), ground)
        else:
            det_or_vd.handle(BusMessage("delta", "dune.player", p), t0 + i)


# -- vehicle detection ------------------------------------------------------------------
def test_on_foot_until_session_says_driving():
    vd = VehicleDetector()
    vd.on_session("Exploration", "CharacterState.State.Exploration  [Sprinting]")
    drive(vd, 2000)  # sprinting fast does not make you a vehicle
    assert vd.current == "foot"


def test_motion_signature_classifies_ground_vehicles():
    for speed, expected in ((800, "sandcrawler"), (1900, "buggy"), (3000, "sandbike")):
        vd = VehicleDetector()
        vd.on_session("Driving", "CharacterState.State.Driving")
        drive(vd, speed, seconds=70)  # a crawler is only called after 60 s without buggy speed
        assert vd.current == expected, (speed, vd.current)
        vd.on_session("Exploration", None)
        assert vd.current == "foot" and vd.plan_vehicle == expected  # keeps planning for it on foot


def test_altitude_above_ground_means_ornithopter():
    vd = VehicleDetector()
    vd.on_session("Driving", None)
    drive(vd, 3500, z=9000, ground=0.0)
    assert vd.current == "ornithopter"


def test_vehicle_name_in_character_state_wins_and_override_beats_everything():
    vd = VehicleDetector()
    vd.on_session("Driving", "CharacterState.State.Driving [Vehicle.Sandcrawler]")
    drive(vd, 3000)
    assert vd.current == "sandcrawler"
    vd.set_override("buggy")
    assert vd.plan_vehicle == "buggy"


def test_detector_learns_terrain_only_from_ground_driving():
    det = Detector(None)
    session(det, "DD", activity="Driving")
    for i in range(20):  # take off: climbing fast, then cruising high
        det.handle(BusMessage("delta", "dune.player", {"x": 10 + 3500 * i, "y": 5, "z": min(i, 6) * 1500}), i)
    assert det.vehicles.current == "ornithopter"
    assert not any(ev.driven_by for ev in det.grid.cells.values())  # no ground evidence, not even before classification
    det_ground = Detector(None)
    session(det_ground, "DD", activity="Driving")
    drive(det_ground, 1900)
    assert any(ev.driven_by == {"buggy"} for ev in det_ground.grid.cells.values())


# -- stale scan rows --------------------------------------------------------------------
R150 = lambda _marker: 15000.0
ROW = {"type": "SpiceField", "map_marker_id": "spicefieldlarge", "x": 50000.7, "y": 1000.2, "z": 0, "metadata": {"fillRate": 0.9}}


def test_scan_rows_whose_marker_fields_are_not_text_are_skipped(tmp_path):
    idx = ScanIndex(tmp_path)
    idx.set_world(("DD", 7))
    idx.load_rows([ROW, {**ROW, "map_marker_id": 5, "x": 90000.0}, {**ROW, "map_marker_id": None, "type": 7, "x": 80000.0}], 200, 0)
    assert [row_key(r) for r in idx.rows] == [row_key(ROW)]


def test_scan_row_confirmed_stale_only_after_repeated_absence_nearby(tmp_path):
    idx = ScanIndex(tmp_path)
    idx.set_world(("DD", 7))
    idx.load_rows([ROW, {"type": "Junk", "map_marker_id": "xx", "x": 1, "y": 1, "z": 1}], 200, 0)
    assert len(idx.rows) == 1 and row_key(ROW) == "SpiceField:50000:1000"
    far = Pose(-900000, 0, 0)
    for t in range(0, 20, 2):
        idx.observe(far, [], t, R150)  # far away: no evidence either way
    assert not idx.stale
    near = Pose(45000, 0, 0)
    idx.observe(near, [], 30, R150)
    idx.observe(near, [], 32, R150)
    assert not idx.stale  # two snapshots are not enough
    assert idx.observe(near, [], 37, R150)
    assert idx.stale[row_key(ROW)]["reason"] == "absent"
    assert not idx.nodes()
    # persisted per world + seed, and restored
    again = ScanIndex(tmp_path)
    again.set_world(("DD", 7))
    assert row_key(ROW) in again.stale
    other_seed = ScanIndex(tmp_path)
    other_seed.set_world(("DD", 8))
    assert not other_seed.stale


def test_not_spawned_actor_marks_row_stale_and_respawn_restores_it(tmp_path):
    idx = ScanIndex(None)
    idx.set_world(("DD", 7))
    idx.load_rows([ROW], 200, 0)
    near = Pose(45000, 0, 0)
    dead = parse_actors({"actors": [spice(9, 50100, 1100, "Large", spawned=False)]})
    for t in (0, 3, 7):
        idx.observe(near, dead, t, R150)
    assert idx.stale[row_key(ROW)]["reason"] == "not spawned"
    assert idx.overlay_actors(near) == []  # spice: the stale row is removed, not greyed out
    alive = parse_actors({"actors": [spice(9, 50100, 1100, "Large")]})
    assert idx.observe(near, alive, 20, R150) and not idx.stale


ORE_ROW = {"type": "MagnetiteOre", "map_marker_id": "magnetiteore", "x": 1000.0, "y": 0.0, "z": 0}


def test_absence_counts_only_consecutive_snapshots_in_range():
    idx = ScanIndex(None)
    idx.load_rows([ORE_ROW], 200, 0)
    near, far = Pose(0, 0, 0), Pose(900000, 0, 0)
    idx.observe(near, [], 0, R150)
    idx.observe(near, [], 3, R150)  # a fly-by before the node loaded
    idx.observe(far, [], 20, R150)
    assert not idx.observe(near, [], 3600, R150) and not idx.stale  # one snapshot on a later visit


def test_unstaling_a_row_the_page_already_hid_asks_for_a_reload():
    idx = ScanIndex(None)
    idx.load_rows([ORE_ROW], 200, 0)
    near = Pose(0, 0, 0)
    for t in (0, 3, 7):
        idx.observe(near, [], t, R150)
    idx.mark_refreshed()
    assert not idx.refresh_pending
    alive = parse_actors({"actors": [ore("o1", 1000.0, 0.0)]})
    assert idx.observe(near, alive, 100, R150) and not idx.stale
    assert idx.refresh_pending


def test_corrections_file_with_non_text_sprite_still_draws_the_overlay(tmp_path):
    entry = {"x": 1, "y": 2, "z": 0, "confirmedAt": 0}
    data = {"MagnetiteOre:1:2": {**entry, "sprite": 7}, "MagnetiteOre:3:4": {**entry, "type": [1]}}
    (tmp_path / "corrections-DD-7.json").write_text(json.dumps(data))
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    session(det, "DD", t=0)
    assert len(det.scan.stale) == 2  # kept: they still hide their rows until they expire
    assert len(det.scan.overlay_actors(None)) == 2
    det.page_state()


STRAV_ROW = {"type": "BP_Stravidium_A_Spawner_C", "map_marker_id": "stravidiumore", "x": 10000.0, "y": 0.0, "z": 0}


def test_a_mined_node_in_a_cluster_keeps_its_row_hidden_while_its_neighbours_stay(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.store.saved_ranges = {"DD": {"stravidiumore": 600 * M}}
    session(det, "DD")
    pose(det, 10400, 500, t=1)  # 5 m from the node you mine
    rows = [{**STRAV_ROW, "x": 10000.0 + 400 * i} for i in range(5)]  # nodes 4 m apart, each with its own row
    nodes = [ore(f"n{i}", r["x"], 0, marker="StravidiumOre") for i, r in enumerate(rows)]
    det.load_scan(rows, 200, 1)
    actors(det, *nodes, t=2)
    actors(det, nodes[0], *nodes[2:], t=3)  # n1 vanished next to you: you mined it
    assert det.scan.stale[row_key(rows[1])]["reason"] == "mined"
    actors(det, nodes[0], *nodes[2:], t=5.5)  # the next list, unchanged
    assert row_key(rows[1]) in det.scan.stale


def spice_rows_on_dormant_sites(n, x0=100000.0, step=20000.0):
    rows = [{"type": "BP_SpiceField_Small_C", "map_marker_id": "spicefieldsmall", "x": x0 + step * i, "y": 0.0, "z": 0}
            for i in range(n)]
    dead = parse_actors({"actors": [spice(f"s{i}", r["x"], r["y"], "Small", fill=0, spawned=False) for i, r in enumerate(rows)]})
    return rows, dead


def test_many_rows_the_game_reports_not_spawned_are_all_removed_without_a_learned_range():
    rows, dead = spice_rows_on_dormant_sites(25)
    idx = ScanIndex(None)
    idx.load_rows(rows, 200, 0)
    for t in range(0, 61, 2):
        idx.observe(Pose(0, 0, 0), dead, t, lambda _m: None)
    assert set(idx.stale) == {row_key(r) for r in rows}


def test_a_row_the_game_reports_not_spawned_beyond_the_learned_range_is_removed():
    rows, dead = spice_rows_on_dormant_sites(1, x0=400000.0)  # 4 km away, outside a 2.52 km confirm radius
    idx = ScanIndex(None)
    idx.load_rows(rows, 200, 0)
    for t in (0, 3, 7, 10):
        idx.observe(Pose(0, 0, 0), dead, t, lambda _m: 252000.0)
    assert row_key(rows[0]) in idx.stale


def test_a_row_seen_alive_in_a_pass_the_safety_brake_stops_is_saved_as_alive(tmp_path):
    first = {**ORE_ROW, "x": 0.0, "y": 14000.0}
    others = [{**ORE_ROW, "x": -6000.0 + 400.0 * i, "y": -10000.0} for i in range(30)]
    idx = ScanIndex(tmp_path)
    idx.set_world(("DD", 7))
    idx.load_rows([first, *others], 200, 0)
    here = Pose(0, 0, 0)
    others_alive = parse_actors({"actors": [ore(f"o{i}", r["x"], r["y"]) for i, r in enumerate(others)]})
    for t in (0, 3, 7):
        idx.observe(here, others_alive, t, R150)  # the first node is gone: its row goes stale
    assert row_key(first) in idx.stale
    for t in (100, 103):
        idx.observe(here, [], t, R150)  # the other 30 drop out of the list
    idx.observe(here, parse_actors({"actors": [ore("first", 0.0, 14000.0)]}), 107, R150)  # the first is back
    assert idx.braked == 1
    again = ScanIndex(tmp_path)
    again.set_world(("DD", 7))
    assert row_key(first) not in again.stale


def test_a_scan_row_whose_metadata_is_text_is_still_ranked_and_shown(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    session(det, "DD")
    pose(det, 10, 0, t=1)
    good = {**ORE_ROW, "x": 20000.0}
    det.load_scan([good, {**good, "x": 90000.0, "metadata": "unknown"}], 200, 1)
    det.ui("resource", "iron")
    det.compute(2)
    det.snapshot()
    assert len(det.nodes({"magnetiteore"})) == 2


# -- detector end to end (no I/O) -------------------------------------------------------
def test_the_target_stays_when_only_the_route_from_here_fails(monkeypatch):
    from harvesting_dune import areas as areas_mod

    det = Detector(None, min_rescore_s=0)
    session(det, "DD")
    pose(det, 10, 0)
    near_x = 20 * CELL_CM
    actors(det, ore(1, near_x, 0), ore(2, near_x + 0.2 * CELL_CM, 0.1 * CELL_CM), ore(3, -60 * CELL_CM, 0), ore(4, -60.2 * CELL_CM, 0))
    det.ui("resource", "iron")
    det.vehicles.set_override("buggy")
    det.compute(1)
    assert abs(det.page_state()["dest"]["x"] - near_x) < 50_000
    route = areas_mod._access
    # in flight 4 km away the route search found no way to the target it chose a moment ago
    monkeypatch.setattr(areas_mod, "_access", lambda a, *rest: None if abs(a.x - near_x) < 50_000 else route(a, *rest))
    pose(det, 30 * CELL_CM, 5 * CELL_CM, t=2)
    det.compute(2)
    st = det.page_state()
    assert abs(st["dest"]["x"] - near_x) < 50_000  # not the next place across the map
    assert "kept as your target" in st["dest"]["detail"]


def test_recommendation_payload_for_the_companion_map():
    det = Detector(None, min_rescore_s=0)
    session(det, "DD")
    pose(det, 10, 0)
    actors(det, ore(1, 20 * CELL_CM, 0), ore(2, 20.2 * CELL_CM, 0.1 * CELL_CM), ore(3, -30 * CELL_CM, 0), ore(4, 0, 40 * CELL_CM))
    det.ui("resource", "iron")
    det.compute(1)
    st = det.page_state()
    assert st["dest"]["hasDestination"] and st["dest"]["title"].startswith("Iron Ore · best for")
    assert "2 nodes (2 live)" in st["dest"]["detail"]
    assert len(st["route"]["stops"]) == 1  # one main line; the other good areas get thin lines
    assert st["alternatives"] == []  # single rocks never get a line: patches need >= 4 nodes
    assert st["route"]["mapId"] == "DD"
    json.dumps(st)
    assert det.badge().startswith("Iron Ore · on foot, plan buggy · best ") and det.badge().endswith(" nodes")


def test_single_target_still_gets_a_route_line_for_direction():
    det = Detector(None, min_rescore_s=0)
    session(det, "DD")
    pose(det, 10, 0)
    actors(det, spice(1, 40 * CELL_CM, 0, "Large", fill=0.9))
    det.ui("resource", "spice_large")
    det.compute(1)
    st = det.page_state()
    assert st["dest"] and st["route"] and len(st["route"]["stops"]) == 1


def test_switching_resource_clears_the_old_recommendation_immediately():
    det = Detector(None, min_rescore_s=0)
    session(det, "DD")
    pose(det, 10, 0)
    actors(det, spice(1, 20 * CELL_CM, 0))
    det.compute(1)
    assert det.page_state()["dest"]
    rev = det.publish_rev
    det.ui("next_resource")
    assert det.resource_key == "spice_large" and det.page_state()["dest"] is None and det.publish_rev > rev
    det.ui("resource", "iron")
    det.compute(2)
    assert det.page_state()["dest"] is None and det.status_text() == "none known here yet"


def test_hysteresis_keeps_current_target_unless_clearly_beaten():
    det = Detector(None, min_rescore_s=0)
    session(det, "DD")
    pose(det, 10, 0)
    actors(det, ore(1, 20 * CELL_CM, 0), ore(2, -21 * CELL_CM, 0))
    det.ui("resource", "iron")
    det.compute(1)
    first = det.top(1)[0].x
    pose(det, -1 * CELL_CM, 0, t=2)  # now slightly closer to the other one
    det.compute(3)
    assert det.top(1)[0].x == first


def test_scan_rows_feed_recommendations_and_dedupe_with_live():
    det = Detector(None, min_rescore_s=0)
    session(det, "DD")
    pose(det, 10, 0)
    actors(det, spice(1, 20 * CELL_CM, 0, "Large"))
    assert det.scan_request() == ("DD", 7)
    det.on_scan_world("deepdesert_1")
    assert det.scan_request() == ("deepdesert_1", 7)
    det.load_scan([
        {"type": "SpiceField", "map_marker_id": "spicefieldlarge", "x": 20 * CELL_CM + 500, "y": 300, "z": 0},  # same field as live
        {"type": "SpiceField", "map_marker_id": "spicefieldmedium", "x": -40 * CELL_CM, "y": 0, "z": 0},
    ], 200, 1)
    ns = det.nodes()
    assert sorted(n.source for n in ns) == ["live", "scan"]


def test_toggle_off_clears_companion_display_and_badge():
    det = Detector(None, min_rescore_s=0)
    session(det, "DD")
    pose(det, 10, 0)
    actors(det, spice(1, 20 * CELL_CM, 0))
    det.compute(1)
    det.ui("toggle")
    assert det.page_state()["dest"] is None and det.badge() is None


# -- map transitions --------------------------------------------------------------------
def test_terrain_base_and_stale_state_do_not_leak_between_maps():
    det = Detector(None, min_rescore_s=0)
    session(det, "HaggaBasin")
    pose(det, 500, 500, 9000, t=1)
    det.ui("base")
    hagga_cells = set(det.grid.cells)
    session(det, "Arrakeen", t=2)
    assert not det.grid.cells and det.base is None and det.store.pose is None and not det.areas
    session(det, "HaggaBasin", t=3)
    assert set(det.grid.cells) == hagga_cells and det.base == (500, 500)


def test_leftover_actor_list_from_previous_map_is_ignored():
    det = Detector(None)
    session(det, "DD")
    session(det, "Arrakeen", t=1)
    actors(det, spice(1, 1000, 0), t=2, world="DD")
    assert det.store.world == ("Arrakeen", 7) and not det.store.items


def test_cities_menus_and_junk_never_break_anything():
    det = Detector(None, min_rescore_s=0)
    session(det, "Arrakeen")
    pose(det, 0, 0, 0, t=1)  # loading screen origin
    assert det.store.pose is None
    pose(det, 1200, -300, 40, t=2)
    for junk in (None, "oops", {"actors": "nope"}, {"actors": [None, 5, {"x": "a"}]}, []):
        for topic in ("dune.map.actors", "dune.player", "dune.session"):
            det.handle(BusMessage("delta", topic, junk), 3)
    det.handle(BusMessage("topicReset", "dune.map.actors", None), 4)
    det.maybe_rescore(5)
    json.dumps(det.snapshot())
    json.dumps(det.page_state())
    session(det, "Arrakeen", t=6)
    pose(det, 1300, -300, 40, t=7)
    assert det.store.world == ("Arrakeen", 7) and det.store.pose is not None


def test_handler_exception_is_contained(monkeypatch):
    det = Detector(None)
    monkeypatch.setattr(det, "_on_pose", lambda *_: 1 / 0)
    pose(det, 1, 1)
    session(det, "Arrakeen")
    assert det.store.world == ("Arrakeen", 7)


def test_journal_records_changes_only(tmp_path):
    det = Detector(tmp_path, min_rescore_s=0)
    session(det, "DD")
    for t in (1, 2):
        actors(det, spice(1, 30 * CELL_CM, 0), t=t)
    lines = next(tmp_path.glob("*.jsonl")).read_text().splitlines()
    assert [json.loads(line)["topic"] for line in lines] == ["dune.session", "dune.map.actors"]


def test_real_ornithopter_landing_is_not_mistaken_for_a_sandbike():
    """Recorded live: 350 m up, 140-270 km/h, descending to a stop; characterState was empty."""
    poses = json.loads((Path(__file__).parent / "ornithopter_landing.json").read_text())
    det = Detector(None)
    session(det, "deepdesert_1", activity="Driving", state=None)
    det.character_state = None
    for t, x, y, z in poses:
        det.handle(BusMessage("delta", "dune.player", {"x": x, "y": y, "z": z}), t)
    assert det.vehicles.current == "ornithopter"
    assert not any(ev.driven_by for ev in det.grid.cells.values())


def test_raw_stats_vehicle_names_give_an_exact_answer():
    det = Detector(None)
    session(det, "DD", activity="Driving")
    det.handle(BusMessage("delta", "dune.rawStats", {"groups": [{"name": "Character", "entries": [{"name": "CurrentHealth", "value": "1"}]},
                                                                 {"name": "Vehicle: Sandcrawler Mk2", "entries": []}]}), 1)
    assert det.vehicles.current == "sandcrawler" and det.vehicles.confident
    session(det, "DD", activity="Exploration", t=2)
    assert det.vehicles.current == "foot" and det.vehicles.stats_hint is None


def test_hovering_carrier_high_above_known_sand_is_an_ornithopter():
    """Live case: a carrier hovering at ~1 km/h, 406 m up; spawn sites around sit at ~50 m."""
    det = Detector(None)
    session(det, "DD", activity="Driving")
    actors(det, *[spice(f"s{i}", 20000 * (i - 2), 30000, "Small", fill=0, spawned=False, z=5000) for i in range(5)], t=0)
    for i in range(12):
        det.handle(BusMessage("delta", "dune.player", {"x": 10 + i * 30, "y": 5, "z": 40600}), i)
    assert det.vehicles.current == "ornithopter"



def bumpy(vd, speed_cms, seconds, t0=0.0, x0=10.0, bump_every=4, bump_cm=900.0):
    """A ground vehicle over dunes: a sharp 9 m rise and fall every few seconds."""
    for i in range(seconds + 1):
        z = bump_cm if i % bump_every == 1 else 0.0
        vd.on_pose(t0 + i, Pose(x0 + speed_cms * i, 5.0, z), 0.0)


def test_a_buggy_over_dunes_is_never_called_an_ornithopter():
    vd = VehicleDetector()
    vd.last_vehicle = "ornithopter"  # the last session was a flight
    vd.on_session("Driving", None)
    bumpy(vd, 1900, 30)
    assert vd.current == "buggy" and vd.confident


def test_one_driving_session_is_one_vehicle_a_level_carrier_stays_an_ornithopter():
    vd = VehicleDetector()
    vd.on_session("Driving", None)
    for i in range(8):  # take off: 60 m up in 4 s
        vd.on_pose(float(i), Pose(10.0 + 50 * i, 5.0, min(i, 4) * 1500.0), 0.0)
    assert vd.current == "ornithopter"
    for i in range(8, 70):  # then cruise level at ~110 km/h, like a loaded carrier
        vd.on_pose(float(i), Pose(10.0 + 3050 * i, 5.0, 6000.0), None)
    assert vd.current == "ornithopter"
    vd.on_session("Exploration", None)
    vd.on_session("Driving", None)  # a new session: judged afresh
    bumpy(vd, 1900, 20, t0=100.0)
    assert vd.current == "buggy"


def test_getting_in_does_not_count_as_hovering():
    vd = VehicleDetector()
    vd.on_session("Driving", None)
    vd.on_pose(0.0, Pose(0.0, 0.0, 0.0), 0.0)
    vd.on_pose(1.0, Pose(20.0, 0.0, 400.0), 0.0)  # boarding: 4 m up, no horizontal motion
    drive(vd, 1900, t0=2.0, z=400.0, ground=0.0)
    assert vd.current == "buggy"


def test_spice_news_stays_off_the_badge_while_searching_ore():
    det = Detector(None, min_rescore_s=0)
    det.ui("minimap_toggle", "stravidium")
    session(det, "DD")
    pose(det, 10, 0, t=1)
    actors(det, spice("m", 20 * CELL_CM, 0, "Medium", fill=0, spawned=False), t=2)
    actors(det, spice("m", 20 * CELL_CM, 0, "Medium", fill=1, spawned=True), t=62)
    assert det.events and "Spice blow" not in det.badge(now=63)
    det.ui("resource", "spice")
    assert "Spice blow" in det.badge(now=63)


def test_small_spice_blows_are_never_announced():
    det = Detector(None, min_rescore_s=0)
    session(det, "DD")
    pose(det, 10, 0, t=1)
    actors(det, spice("s", 20 * CELL_CM, 0, "Small", fill=0, spawned=False), t=2)
    actors(det, spice("s", 20 * CELL_CM, 0, "Small", fill=1, spawned=True), t=62)
    assert not det.events and "s" not in str(det.badge(now=63)).lower().split("·")[0].replace("spice", "")



def test_a_buggy_creeping_between_nodes_is_not_a_sandcrawler():
    vd = VehicleDetector()
    vd.on_session("Driving", None)
    drive(vd, 1900, seconds=12)  # drove over at buggy speed
    drive(vd, 600, seconds=40, t0=13.0, x0=10.0 + 1900 * 12)  # then creeps around a node field
    assert vd.current == "buggy"



def test_pulling_away_slowly_never_flashes_sandcrawler():
    vd = VehicleDetector()
    vd.on_session("Driving", None)
    seen = set()
    for i in range(20):  # 8 s slow, then buggy speed
        vd.on_pose(float(i), Pose(10.0 + sum(700 if k < 8 else 1900 for k in range(i)), 5.0, 0.0), 0.0)
        seen.add(vd.current)
    assert "sandcrawler" not in seen and vd.current == "buggy"



def test_a_buggy_creeping_between_nodes_for_a_while_never_shows_as_a_crawler():
    """Live case (15:40): 16 s at up to 41 km/h while mining from the buggy, then faster."""
    vd = VehicleDetector()
    vd.last_vehicle = "buggy"
    vd.on_session("Driving", None)
    seen, x = set(), 10.0
    for i in range(50):
        x += 700 if i < 35 else 1900  # 35 s creeping, then buggy speed
        vd.on_pose(float(i), Pose(x, 5.0, 0.0), 0.0)
        seen.add(vd.current)
    assert "sandcrawler" not in seen and vd.current == "buggy"


# -- per-world files and the world of a message --------------------------------------------
def test_a_session_with_no_world_writes_no_per_world_file(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    session(det, None, seed=None, t=0)  # the menus: no map yet
    pose(det, 10, 0, t=0)
    actors(det, spice(1, 3000 * M, 0, "Medium", fill=0, spawned=False), t=1, world=None, seed=None)
    actors(det, spice(1, 3000 * M, 0, "Medium", fill=0.05), t=2, world=None, seed=None)
    det.load_scan([ORE_ROW], 200, 3)
    assert det.recent_event(2).startswith("Spice blow!")  # the blow and the rows were handled ...
    assert sorted(p.name for p in tmp_path.glob("*None*")) == []  # ... but no file stands for "no world"


def test_a_loading_screen_into_the_same_world_keeps_the_scan_rows(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    session(det, "DD", t=0)
    det.load_scan([ORE_ROW], 200, 1)
    session(det, None, t=10)  # loading screen reports no world
    session(det, "DD", t=20)
    assert [row_key(r) for r in det.scan.rows] == [row_key(ORE_ROW)]
    assert det.scan.fetched_at == 1  # not downloaded again before the regular refresh


def test_a_different_world_after_a_loading_screen_drops_the_scan_rows(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    session(det, "DD", t=0)
    det.load_scan([ORE_ROW], 200, 1)
    session(det, None, t=10)
    session(det, "HB", t=20)
    assert det.scan.rows == [] and det.scan.fetched_at is None


def test_a_dataset_from_the_guild_server_holding_nan_is_refused(monkeypatch):
    client = SyncClient(server="https://127.0.0.1:1")
    monkeypatch.setattr(client, "_open", lambda _req: (b'{"routes": [{"x": NaN}]}', {}))
    with pytest.raises(SyncServerError):
        client.fetch("key", "/v1/data/routes")


def test_replay_load_in_ignores_a_session_world_that_is_not_text():
    load_in, seen = _LoadIn(), set()
    feed = [
        BusMessage("snapshot", "dune.session", {"worldId": "DD"}),
        BusMessage("delta", "dune.player", {"x": 0, "y": 0, "z": 0}),
        BusMessage("delta", "dune.map.actors", {"actors": [spice("a", 1000, 0)]}),  # the load-in list
        BusMessage("delta", "dune.session", {"worldId": 5}),  # not a world name: no map change
        BusMessage("delta", "dune.map.actors", {"actors": [spice("a", 1000, 0), spice("b", 2000 * M, 0)]}),
    ]
    fields = [f for msg in feed for f in load_in.new_fields(msg, seen)]
    assert [f["dist_m"] for f in fields] == [2000]


def test_a_farm_container_list_whose_world_is_not_text_still_closes_its_stop(tmp_path):
    route = {"id": "yard", "items": ["x"], "resetS": 600, "sites": [{
        "name": "Yard", "world": "survival_1", "center": {"x": 0, "y": 0, "z": 0},
        "stops": [{"x": x, "y": 0, "z": 0, "kind": "chest", "name": f"chest {x}"} for x in (0, 1000)]}]}
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    assert det.on_dataset("routes", {"routes": [route]})
    session(det, "survival_1", seed=1, t=1)
    det.ui("route_start", "yard")
    pose(det, 0, 0, t=2)
    pose(det, 0, 0, t=2.5)
    det.handle(BusMessage("delta", "dune.map.actors", {"gated": False, "worldId": 12, "coriolisWorldSeed": 1, "actors": [
        {"id": "c0", "actorType": "BP_TemporaryLootContainer_BasicContainer_C", "markerType": "LootContainer",
         "hasLoot": False, "x": 0, "y": 0, "z": 0}]}), 3)
    assert det.farm.done == {0}
