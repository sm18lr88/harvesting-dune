"""PvP yield bonus, never-small spice targets, remembered resource, alternative lines for ore."""

import json
import math

from helpers import CELL_CM, actors, mountain, ore, pose, session, spice

from harvesting_dune.app import Detector
from harvesting_dune.feed import BusMessage
from harvesting_dune.zones import cell_name, is_pvp, row_of

DD = "deepdesert_1"
PVE_Y = 200_000.0  # row D
PVP_Y = 0.0  # row E


def test_grid_rows_match_the_companion_map():
    # fitted from the companion's own marker grid cells: A5 wreck, B, C, D, F, H rows
    assert row_of(976_802) == "A" and row_of(700_000) == "B" and row_of(500_000) == "C"
    assert row_of(150_000) == "D" and row_of(-250_000) == "F" and row_of(-900_000) == "H"
    assert row_of(5_000_000) == "A" and row_of(-5_000_000) == "I"  # clamped to the grid
    assert not is_pvp(PVE_Y) and is_pvp(PVP_Y)
    assert is_pvp(PVE_Y, last_pve_row="C") and not is_pvp(PVP_Y, last_pve_row="E")


def _two_patches(det, pvp_yield):
    det.pvp_yield = pvp_yield
    det.pvp_only = False  # this weighs PvE against PvP; PvP-only would exclude PvE outright
    det.ui("vehicle", "ornithopter")  # flies: no terrain needed
    session(det, DD)
    pose(det, 0, 120_000, t=1)
    near = [ore(f"n{i}", 400 * i, PVE_Y + 300 * i, marker="StravidiumOre") for i in range(6)]  # 800 m, PvE
    far = [ore(f"f{i}", 400 * i, PVP_Y + 300 * i, marker="StravidiumOre") for i in range(6)]  # 1.2 km, PvP
    actors(det, *near, *far, t=2, world=DD)
    det.ui("resource", "stravidium")
    det.compute(3)
    return det.top(1)[0]


def test_pvp_bonus_can_make_a_farther_pvp_area_the_target():
    best = _two_patches(Detector(None, min_rescore_s=0), pvp_yield=2.5)
    assert is_pvp(best.y) and best.yield_mult == 2.5
    det = Detector(None, min_rescore_s=0)
    assert not is_pvp(_two_patches(det, pvp_yield=1.0).y)  # no bonus: the nearer one wins
    assert "E PvP" in det.describe(det.top(2)[1]) or "D PvE" in det.describe(det.top(1)[0])


def test_small_spice_fields_are_never_a_target_or_a_line():
    det = Detector(None, min_rescore_s=0)
    session(det, "DD")
    pose(det, 10, 0, t=1)
    actors(det, spice("S", 3 * CELL_CM, 0, "Small", fill=1.0), spice("M", 40 * CELL_CM, 0, "Medium", fill=0.7), t=2)
    det.ui("resource", "spice")
    det.compute(3)
    st = det.page_state()
    assert round(st["dest"]["x"]) == round(40 * CELL_CM)
    assert all(a["size"] != "small" for a in st["alternatives"])
    assert all(n.marker != "spicefieldsmall" for a in det.areas for n in a.nodes)


def test_searched_resource_is_remembered_and_follows_the_single_shown_resource(tmp_path):
    det = Detector(None, data_dir=tmp_path)
    det.ui("resource", "stravidium")
    assert Detector(None, data_dir=tmp_path).resource_key == "stravidium"
    # older settings without a saved resource: the one resource on the minimap wins over the default
    saved = json.loads((tmp_path / "settings.json").read_text())
    del saved["resource"]
    (tmp_path / "settings.json").write_text(json.dumps(saved))
    assert Detector(None, data_dir=tmp_path).resource_key == "stravidium"
    saved["minimapFocus"] = True  # ...also from settings saved with the old Focus option
    (tmp_path / "settings.json").write_text(json.dumps(saved))
    assert Detector(None, data_dir=tmp_path).resource_key == "stravidium"


def test_ore_alternatives_include_scan_only_areas_with_a_fainter_line():
    det = Detector(None, min_rescore_s=0)
    det.ui("minimap_toggle", "stravidium")
    det.ui("vehicle", "ornithopter")
    session(det, "DD")
    pose(det, 0, 0, t=1)
    actors(det, *[ore(f"a{i}", 30_000 + 300 * i, 0, marker="StravidiumOre") for i in range(6)], t=2)
    det.load_scan([{"type": "StravidiumOre", "map_marker_id": "stravidiumore", "x": -90_000 + 300 * i, "y": 60_000, "z": 0}
                   for i in range(5)], 200, 2)
    det.compute(3)
    alts = det.page_state()["alternatives"]
    assert len(alts) == 1 and alts[0]["kind"] == "scan" and alts[0]["color"].endswith("0.35)")


def test_buried_treasure_is_hidden_on_the_minimap_unless_ticked(tmp_path):
    det = Detector(None, data_dir=tmp_path)
    hidden, managed = det.minimap_filter()
    assert "BuriedTreasure" in hidden and "BuriedTreasure" in managed
    det.ui("minimap_toggle", "iron")  # lines on: iron becomes the one searched resource
    rev = det.minimap_rev
    det.ui("minimap_extra", "buried_treasure")
    assert "BuriedTreasure" not in det.minimap_filter()[0] and det.minimap_rev > rev
    assert det.resource_key == "iron" and det.visible_resources() == ["iron"] and det.lines_active  # search untouched
    assert Detector(None, data_dir=tmp_path).minimap_extras == {"buried_treasure"}
    det.ui("minimap_extra", "buried_treasure")
    assert "BuriedTreasure" in det.minimap_filter()[0]


def test_pvp_only_never_targets_pve_and_still_reaches_far_pvp_rows():
    det = Detector(None, min_rescore_s=0)
    det.ui("minimap_toggle", "stravidium")
    det.ui("vehicle", "buggy")  # ground vehicle: 8 km planning range
    session(det, DD)
    pose(det, 0, 700_000, t=1)  # row B, PvE
    pve = [ore(f"e{i}", 400 * i, 650_000 + 300 * i, marker="StravidiumOre") for i in range(20)]  # rich, 500 m away
    pvp = [ore(f"p{i}", 400 * i, -300_000 + 300 * i, marker="StravidiumOre") for i in range(6)]  # row F, 10 km away
    actors(det, *pve, *pvp, t=2, world=DD)
    det.compute(3)
    assert det.top(1) and all(is_pvp(a.y) for a in det.top(10))  # the far PvP area, never the PvE one
    assert "far: straight-line estimate" in det.top(1)[0].reasons
    det.ui("pvp_only")  # off: PvE is allowed again (and wins, being close and rich)
    det.compute(4)
    assert not is_pvp(det.top(1)[0].y)


def test_pvp_only_says_so_when_nothing_is_known_there():
    det = Detector(None, min_rescore_s=0)
    det.ui("minimap_toggle", "stravidium")
    session(det, DD)
    pose(det, 0, 700_000, t=1)
    actors(det, *[ore(f"e{i}", 400 * i, 650_000, marker="StravidiumOre") for i in range(5)], t=2, world=DD)
    det.compute(3)
    assert not det.top(1) and "none in PvP" in det.badge(now=0) and "PvP" in det.status_text()


def test_grid_cells_match_the_companion_labels():
    # positions of the companion's own H5 / I8 / A1 / A5 markers and cluster centres
    assert cell_name(-103_796, -906_916) == "H5" and cell_name(720_515, -1_000_115) == "I8"
    assert cell_name(-1_150_613, 1_054_799) == "A1" and cell_name(-114_905, 976_802) == "A5"


def test_badge_names_the_hotspot_cell_even_when_the_line_points_elsewhere():
    det = Detector(None, min_rescore_s=0)
    det.ui("minimap_toggle", "stravidium")
    det.ui("vehicle", "ornithopter")
    session(det, DD)
    pose(det, -100_000, -700_000, t=1)  # row G, PvP
    near = [ore(f"n{i}", -100_000 + 400 * i, -690_000, marker="StravidiumOre") for i in range(5)]  # G5, next to you
    hot = [ore(f"h{i}", -104_000 + 300 * (i % 8), -907_000 + 300 * (i // 8), marker="StravidiumOre") for i in range(40)]  # H5
    actors(det, *near, *hot, t=2, world=DD)
    det.compute(3)
    assert det.hotspot["cell"] == "H5" and det.hotspot["nodes"] == 40
    assert "hotspot H5" in det.badge(now=0) and "hotspot H5" in det.picker()["status"]
    det.ui("resource", "iron")  # another resource: the old hotspot is not shown
    assert det.hotspot is None and "hotspot" not in det.badge(now=0)


def test_a_minimap_switch_drops_the_old_hotspot_and_node_count_until_the_next_ranking():
    det = Detector(None, min_rescore_s=0)
    det.ui("minimap_toggle", "stravidium")
    det.ui("vehicle", "ornithopter")
    session(det, DD)
    pose(det, -100_000, -700_000, t=1)
    hot = [ore(f"h{i}", -104_000 + 300 * (i % 8), -907_000 + 300 * (i // 8), marker="StravidiumOre") for i in range(40)]
    actors(det, *hot, t=2, world=DD)
    det.compute(3)
    assert det.snapshot()["hotspot"]["cell"] == "H5"
    det.ui("minimap_toggle", "iron")  # lines on: iron becomes the search; no iron is known anywhere
    # nothing ranks again here (with no pose, or a closed gate, this would last for ever)
    snap = det.snapshot()
    assert snap["resource"] == "iron" and snap["hotspot"] is None
    assert "none known" in snap["status"] and "hotspot" not in det.picker()["status"]
    assert "hotspot" not in det.badge(now=0)


def test_max_distance_limits_lines_but_not_the_hotspot(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.ui("minimap_toggle", "stravidium")
    det.ui("vehicle", "ornithopter")
    session(det, DD)
    pose(det, -100_000, -700_000, t=1)  # row G, PvP
    near = [ore(f"n{i}", -100_000 + 400 * i, -560_000, marker="StravidiumOre") for i in range(5)]  # 1.4 km
    far = [ore(f"h{i}", -104_000 + 300 * (i % 8), -907_000 + 300 * (i // 8), marker="StravidiumOre") for i in range(40)]  # ~2 km, rich
    actors(det, *near, *far, t=2, world=DD)
    det.compute(3)
    assert det.top(1)[0].y < -800_000  # no limit: the rich far area wins
    det.ui("max_km", "1.5")
    det.compute(4)
    assert det.top(1) and all(a.distance_m <= 1500 for a in det.top(10))
    assert det.hotspot["cell"] == "H5"  # still names the best place overall
    det.ui("max_km", "0.5")
    det.compute(5)
    assert not det.top(1) and "none within 0.5 km" in det.badge(now=0)
    assert Detector(None, data_dir=tmp_path).max_km == 0.5
    for bad in ("nan", "inf", "x"):
        det.ui("max_km", bad)
    assert det.max_km == 0.5


def test_deep_desert_lines_start_in_the_chosen_row(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.ui("minimap_toggle", "stravidium")
    det.ui("vehicle", "ornithopter")
    session(det, DD)
    pose(det, 0, 700_000, t=1)  # row B, just past the entrance
    actors(det, *[ore(f"p{i}", 400 * i, PVP_Y + 300 * i, marker="StravidiumOre") for i in range(6)], t=2, world=DD)
    det.compute(3)
    st = det.page_state()
    assert st["route"] and st["dest"]  # default row A: lines from the entrance on, as before
    det.ui("lines_row", "E")
    det.compute(4)
    st = det.page_state()
    assert st["route"] is None and not st.get("alternatives") and st["dest"]  # the target stays, its lines wait
    assert "lines from row E" in (det.badge(now=0) or "")
    pose(det, 0, 20_000, t=5)  # row E
    det.compute(6)
    assert det.page_state()["route"] and "lines from row" not in (det.badge(now=0) or "")
    det.ui("lines_row", "Z")  # not a row: ignored
    assert det.lines_from_row == "E" and det.snapshot()["zones"]["linesFromRow"] == "E"
    assert Detector(None, data_dir=tmp_path).lines_from_row == "E"
    saved = json.loads((tmp_path / "settings.json").read_text())
    saved["linesFromRow"] = "EE"  # a malformed file keeps the default
    (tmp_path / "settings.json").write_text(json.dumps(saved))
    assert Detector(None, data_dir=tmp_path).lines_from_row == "A"


def test_lines_row_applies_only_in_the_deep_desert():
    det = Detector(None, min_rescore_s=0)
    det.ui("minimap_toggle", "stravidium")
    det.ui("vehicle", "ornithopter")
    det.ui("lines_row", "I")
    session(det, "survival_1")
    pose(det, 0, 700_000, t=1)
    actors(det, *[ore(f"p{i}", 400 * i, 650_000 + 300 * i, marker="StravidiumOre") for i in range(6)], t=2, world="survival_1")
    det.compute(3)
    assert det.page_state()["route"]



def _island(prefix, cx, cy, n, spread_m=1000):
    """n Stravidium nodes over a ~1 km island (split into several 500 m areas by clustering)."""
    side = int(n ** 0.5) + 1
    step = spread_m * 100 / side
    return [ore(f"{prefix}{i}", cx + step * (i % side), cy + step * (i // side), marker="StravidiumOre") for i in range(n)]


def test_a_dense_island_beats_a_moderate_spot_nearer_to_you():
    """Live case (H5): it first sent the player to a moderate spot, then to the island next door."""
    det = Detector(None, min_rescore_s=0)
    det.pvp_only = False
    det.ui("minimap_toggle", "stravidium")
    det.ui("vehicle", "ornithopter")
    session(det, "DD")
    pose(det, 0, 0, t=1)
    moderate = _island("m", 100_000, 0, 10, spread_m=150)  # 10 nodes, 1 km away
    island = _island("i", 500_000, 0, 110)  # 110 nodes over 1 km, 5 km away
    actors(det, *moderate, *island, t=2)
    det.compute(3)
    best = det.top(1)[0]
    assert best.x > 400_000, det.describe(best)
    assert "in the patch" in det.describe(best)
    assert all(abs(a["x"] - best.x) > 50_000 or abs(a["y"] - best.y) > 50_000
               for a in det.page_state()["alternatives"])  # no second line into the same island


def test_the_moderate_spot_still_wins_when_the_island_is_out_of_reach():
    det = Detector(None, min_rescore_s=0)
    det.pvp_only = False
    det.ui("minimap_toggle", "stravidium")
    det.ui("vehicle", "buggy")
    session(det, "DD")
    pose(det, 0, 0, t=1)
    actors(det, *_island("m", 30_000, 0, 10, spread_m=150), *_island("i", 4_000_000, 0, 110), t=2)
    det.compute(3)
    assert det.top(1)[0].x < 100_000  # 40 km by buggy: nothing left of a 30 min session



def test_grid_is_fitted_from_the_companions_own_markers():
    from harvesting_dune.zones import DEFAULT_FIT, GridFit, fit_grid

    # markers as the companion draws them, on a grid shifted and scaled against the fallback
    true = GridFit(900_000.0, -250_000.0, -1_000_000.0, 250_000.0)
    samples = []
    for r in range(9):
        for c in range(9):
            for k in range(3):
                x = true.col_1 + c * true.col_step + (k - 1) * 70_000
                y = true.row_a + r * true.row_step + (1 - k) * 60_000
                samples.append((f"{'ABCDEFGHI'[r]}{c + 1}", x, y))
    samples.append(("Z9", 1, 2))  # junk is ignored
    fit = fit_grid(samples)
    assert fit is not None and abs(fit.row_step - true.row_step) < 1 and abs(fit.col_1 - true.col_1) < 1
    assert cell_name(true.col_1 + 4 * true.col_step, true.row_a + 7 * true.row_step, fit) == "H5"
    assert fit_grid(samples[:10]) is None  # too few to trust
    # wrong labels everywhere: refused, the fallback stays
    assert fit_grid([(f"A{1 + i % 9}", i * 1000.0, i * 777.0) for i in range(60)]) is None
    det = Detector(None)
    session(det, DD)
    assert det.grid_fit() == DEFAULT_FIT and det.grid_fit_wanted() == DD
    assert det.on_grid_samples(DD, samples) and det.grid_fit() == fit and det.grid_fit_wanted() is None


def test_respawn_time_and_mining_pace_are_learned_from_play(tmp_path):
    from harvesting_dune.areas import NODE_HARVEST_S
    from harvesting_dune.catalog import respawn_s

    det = Detector(None, data_dir=tmp_path)
    assert det.respawn_for("stravidiumore") == respawn_s("stravidiumore")
    for v in (1500, 1600, 1700, 1800, 2400):
        det.learning.learn("respawn", "stravidiumore", v)
    assert det.respawn_for("stravidiumore") == 1600  # the lower quartile of what you saw
    assert det.node_seconds({"stravidiumore"}) == NODE_HARVEST_S
    for v in (12, 14, 15, 16, 40):
        det.learning.learn("pace", "stravidiumore", v)
    assert det.node_seconds({"stravidiumore"}) == 15
    again = Detector(None, data_dir=tmp_path)
    assert again.respawn_for("stravidiumore") == 1600 and again.node_seconds({"stravidiumore"}) == 15


def test_grid_fit_on_the_real_companion_markers():
    """Regression: a line through marker positions put 28% of them in the wrong cell."""
    from pathlib import Path

    from harvesting_dune.zones import GridFit, fit_agreement, fit_grid

    markers = json.loads((Path(__file__).parent / "dd07_grid_markers.json").read_text())
    fit = fit_grid(markers)
    assert fit is not None and fit_agreement(fit, markers) >= 0.99
    # a worse grid (fitted from a lopsided subset) never replaces a better one
    det = Detector(None)
    session(det, DD)
    det.on_grid_samples(DD, markers, fit)
    skewed = GridFit(fit.row_a, fit.row_step * 0.94, fit.col_1, fit.col_step * 0.98, 600)
    det.learning.fitted_this_run.clear()
    det.on_grid_samples(DD, markers, skewed)
    assert det.grid_fit() == fit



def test_mining_pace_follows_the_latest_mines(tmp_path):
    """Solo yesterday, two on a buggy today: the pace adapts within a few mines."""
    det = Detector(None, data_dir=tmp_path)
    for _ in range(40):
        det.learning.learn("pace", "stravidiumore", 20.0)  # a long solo history
    assert det.node_seconds({"stravidiumore"}) == 20
    for _ in range(5):
        det.learning.learn("pace", "stravidiumore", 10.0)  # a partner joins
    assert det.node_seconds({"stravidiumore"}) == 10
    assert len(det.learning.data["pace"]["stravidiumore"]) == 45  # the history stays on file


def test_a_bad_grid_in_learned_json_falls_back_to_the_default(tmp_path):
    from harvesting_dune.zones import DEFAULT_FIT

    for bad in ('"x"', "null", "1e999"):  # 1e999 parses to infinity
        (tmp_path / "learned.json").write_text(
            '{"grids": {"%s": {"row_a": %s, "row_step": -270300, "col_1": 0, "col_step": 270300, "samples": 30}}}'
            % (DD, bad)
        )
        det = Detector(None, data_dir=tmp_path)
        session(det, DD)
        assert det.grid_fit() == DEFAULT_FIT and not is_pvp(PVE_Y, fit=det.grid_fit())


def test_grid_fit_gives_up_on_degenerate_markers_instead_of_raising():
    from harvesting_dune.zones import fit_grid

    level = [(f"A{1 + i % 3}", (i % 3) * 300000.0, 0.0) for i in range(8)]  # border midpoints come out level
    level += [(f"B{1 + i % 3}", (i % 3) * 300000.0, 10.0) for i in range(4)]
    level += [(f"C{1 + i % 3}", (i % 3) * 300000.0, 0.0 if i < 4 else 100.0) for i in range(8)]
    assert fit_grid(level) is None
    huge = [(f"{'ABC'[i % 3]}{1 + (i // 3) % 3}", 1.7e308 if i % 2 else -1.0, 1.7e308 * (1 if i % 3 else -1)) for i in range(30)]
    assert fit_grid(huge) is None  # finite, but the sums overflow to NaN


def test_two_far_apart_bumps_do_not_make_a_buggy_an_ornithopter():
    from harvesting_dune.model import Pose
    from harvesting_dune.vehicle import VehicleDetector

    v = VehicleDetector()
    v.on_session("Driving", None)
    x = z = 0.0
    for s in range(150):
        if s in (30, 100):
            z += 300.0  # stopped for 1 s while the body jolts 3 m
        else:
            x += 1500.0
        v.on_pose(1000.0 + s, Pose(x, 0.0, z), None)
    assert v.current == "buggy"


def test_ground_vehicle_confirmation_does_not_depend_on_the_pose_rate():
    from harvesting_dune.model import Pose
    from harvesting_dune.vehicle import VehicleDetector

    def confirmed_after(step):
        v = VehicleDetector()
        v.on_session("Driving", None)
        for i in range(int(30 / step)):
            v.on_pose(1000.0 + i * step, Pose(i * step * 1500.0, 0.0, 0.0), None)
            if v.confident:
                return i * step
        return None

    assert confirmed_after(0.25) == confirmed_after(1.0) is not None


def test_a_base_beyond_the_route_window_keeps_a_near_area_reachable_by_ground():
    det = Detector(None, min_rescore_s=0)
    det.ui("vehicle", "buggy")
    session(det, "HaggaBasin")
    pose(det, 0, 900_000, t=1)  # 9 km away: beyond the 8 km window a route search covers around its start
    det.ui("base")
    pose(det, 0, 0, t=2)
    actors(det, *(ore(f"o{i}", 60_000 + 300 * i, 0) for i in range(4)), t=3, world="HaggaBasin")
    det.ui("resource", "iron")
    det.compute(4)
    assert det.areas and det.areas[0].feasible and abs(det.areas[0].x - 60_000) < 2_000


def test_a_terrain_number_too_big_for_an_integer_skips_only_its_cell(tmp_path):
    (tmp_path / "terrain-HaggaBasin-7.json").write_text(
        '{"version": 1, "cellCm": %s, "cells": [[1e999, 0, 0, 0, 0, 0, [], []], [2, 3, 100, 300, 400, 2, [], []]]}'
        % json.dumps(CELL_CM)
    )
    det = Detector(None, data_dir=tmp_path)
    session(det, "HaggaBasin")
    assert set(det.grid.cells) == {(2, 3)}


def test_a_grid_number_too_big_for_a_float_falls_back_to_the_default(tmp_path):
    from harvesting_dune.zones import DEFAULT_FIT

    (tmp_path / "learned.json").write_text(
        '{"grids": {"%s": {"row_a": %s, "row_step": -270300, "col_1": 0, "col_step": 270300, "samples": 30}}}'
        % (DD, "9" * 400)
    )
    det = Detector(None, data_dir=tmp_path)
    session(det, DD)
    assert det.grid_fit() == DEFAULT_FIT


def _iron(x, y, weight=None):
    from harvesting_dune.areas import Node

    return Node("magnetiteore", x, y, 0.0, "scan", weight=weight)


def _rank_iron(nodes, **kw):
    from harvesting_dune.areas import recommend
    from harvesting_dune.catalog import BY_KEY
    from harvesting_dune.model import Pose
    from harvesting_dune.terrain import VEHICLES, TerrainGrid

    return recommend(BY_KEY["iron"], nodes, Pose(0, 0, 0), TerrainGrid(), VEHICLES["ornithopter"], top=0, **kw)


def _straight(a):
    return (a.x**2 + a.y**2) ** 0.5


def test_a_nearest_first_ranking_routes_the_nearest_area_before_richer_ones():
    rich = [_iron(500_000 + 60_000 * i, 60_000 * j) for i in range(4) for j in range(3) for _ in range(5)]
    near = _iron(50_000, 0)
    out = _rank_iron([*rich, near], need=1, order=_straight)
    assert out[0].feasible and out[0].nodes == [near]


def test_a_most_likely_first_ranking_routes_the_likeliest_place_before_richer_patches():
    patch = [_iron(300_000 + 50_000 * i, 50_000 * j, weight=0.04) for i in range(4) for j in range(3)]
    likeliest = _iron(-300_000, 0, weight=0.17)
    out = _rank_iron([*patch, likeliest], need=1, order=lambda a: -a.find_chance())
    assert out[0].feasible and out[0].nodes == [likeliest]


def test_an_uncapped_ranking_returns_every_area():
    spots = [_iron(60_000 * i, 60_000 * j) for i in range(19) for j in range(2)]
    assert len(_rank_iron(spots, cap=False)) == len(spots)



def _hull(id_, x, y):
    return {"id": id_, "actorType": "BP_SmallShipwreck_01_C", "markerType": "SalvageSteel", "spriteKey": "salvagesteel",
            "x": x, "y": y, "z": 0.0, "reportable": True}


def test_a_ship_tour_starts_at_the_ship_nearest_to_you(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.ui("vehicle", "ornithopter")
    session(det, DD)
    det.ui("resource", "downed_ships")
    pose(det, 0.0, 290_000.0, t=1)  # row D
    actors(det, _hull("seen", 0.0, 350_000.0), t=2, world=DD)  # 600 m from you
    det.load_scan([{"type": "BP_SmallShipwreck_01_C", "map_marker_id": "salvagesteel", "x": -600_000.0 + 100_000.0 * i,
                    "y": -200_000.0, "z": 0.0} for i in range(13)], 200, 2)  # reported ships 5 km away, PvP row F
    det.compute(3)
    first = det.page_state()["route"]["stops"][0]
    assert (first["x"], first["y"]) == (0.0, 350_000.0)


def test_a_chance_find_leads_to_its_most_likely_place(tmp_path):
    town = [["survival_1", 300_000 + 55_000 * i, 55_000 * j, 0, "Rare Container", 0.04, 1, 1] for i in range(5) for j in range(5)]
    crawl = {"built": 1, "materials": [], "uniques": [{
        "id": "regiscontainer2", "name": "Upgraded Regis Spice Container", "rarity": "Unique", "categories": ["items/gear"],
        "drops": [["survival_1", -300_000, 0, 0, "Ultra Rare Container", 0.17, 1, 1], *town]}]}
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.items.ingest(crawl)
    session(det, "survival_1")
    pose(det, 0, 0, t=1)
    det.ui("vehicle", "ornithopter")
    det.ui("minimap_toggle", "item:regiscontainer2")
    det.compute(2)
    assert det.page_state()["dest"]["x"] == -300_000  # the 17 % place, not one of the 25 places at 4 %


def test_the_target_keeps_its_switch_margin_on_a_map_with_more_areas_than_are_reported(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.ui("vehicle", "ornithopter")
    session(det, "survival_1")
    det.ui("resource", "iron")
    acts = []

    def patch(cx, cy, n):
        acts.extend(ore(f"o{len(acts) + i}", cx + 300.0 * (i % 5), cy + 300.0 * (i // 5)) for i in range(n))

    patch(480_000.0, 0.0, 20)  # the target: 4.8 km east
    for j in range(12):  # 12 equal patches 5 km away to the west
        ang = math.radians(100 + j * 160 / 11)
        patch(500_000.0 * math.cos(ang), 500_000.0 * math.sin(ang), 20)
    for j in range(25):  # 25 single rocks around you: 38 areas in all
        patch(-120_000.0 + 60_000.0 * (j % 5), -120_000.0 + 60_000.0 * (j // 5), 1)
    pose(det, 0, 0, t=1)
    actors(det, *acts, t=2, world="survival_1")
    det.compute(3)
    first = det.top(1)[0]
    assert first.x > 400_000
    pose(det, -25_000.0, 0, t=4)  # 250 m west: the western patches gain a little, far less than 15 %
    det.compute(5)
    assert det.top(1)[0].x > 400_000 and len(det.areas) <= 30


def test_a_target_kept_while_flying_is_not_kept_once_motion_says_buggy(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    session(det, "survival_1", activity="Driving")
    det.ui("resource", "iron")
    for i in range(20):  # take off and cruise: motion says ornithopter
        det.handle(BusMessage("delta", "dune.player", {"x": 10 + 3500 * i, "y": 5, "z": min(i, 6) * 1500}), i)
    assert det.vehicles.current == "ornithopter"
    mountain(det.grid, 60, 0, radius_cells=6, height=15000.0)  # a 150 m rock
    top = [ore(f"t{i}", (60.5 + dx) * CELL_CM, (0.5 + dy) * CELL_CM, z=15000.0)
           for i, (dx, dy) in enumerate(((0, 0), (0.3, 0.2), (-0.2, 0.3), (0.1, -0.4)))]
    low = [ore("l0", -10 * CELL_CM, 0, z=10.0), ore("l1", -10.3 * CELL_CM, 0.2 * CELL_CM, z=10.0)]
    actors(det, *top, *low, t=20, world="survival_1")
    det.maybe_rescore(21)
    assert det.top(1)[0].x > 0  # flying: the four rocks on the mountain top
    session(det, "survival_1", t=22)  # landed
    pose(det, 66_510, 5, z=10.0, t=23)
    session(det, "survival_1", activity="Driving", t=25)  # a new driving session
    for i in range(13):
        det.handle(BusMessage("delta", "dune.player", {"x": 66_510 - 1900 * i, "y": 5, "z": 10.0}), 26 + i)
    assert (det.vehicles.current, det.vehicles.override) == ("buggy", None)
    det.maybe_rescore(40)
    assert det.top(1)[0].x < 0  # the low rocks: no buggy climbs the mountain


def test_pvp_only_texts_stay_quiet_for_a_search_it_does_not_apply_to(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.ui("vehicle", "ornithopter")
    session(det, DD)
    det.ui("resource", "downed_ships")  # ships are searched in PvE rows too
    pose(det, 0, 290_000.0, t=1)
    det.compute(2)
    assert det.pvp_only and not det.top(1)
    assert "PvP" not in det.status_text() and "PvP" not in det.badge(now=2)


def test_spice_lines_never_point_into_pve_rows_with_pvp_only_on(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.ui("resource", "spice")
    session(det, DD)
    pose(det, 0, PVP_Y, t=1)
    actors(det, spice("L", 0, PVE_Y, "Large", fill=0.9), t=2, world=DD)  # 2 km away, in a PvE row
    det.compute(3)
    assert det.pvp_only and det.page_state()["alternatives"] == []


def test_spice_lines_never_go_beyond_the_max_distance(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.ui("resource", "spice")
    det.ui("max_km", "1")
    session(det, "DD")
    pose(det, 10, 0, t=1)
    actors(det, spice("M", 50 * CELL_CM, 0, "Medium", fill=0.8), t=2)  # 2.5 km away
    det.compute(3)
    assert det.page_state()["alternatives"] == []


def test_ranking_goes_on_after_the_wall_clock_steps_back(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.ui("vehicle", "ornithopter")
    det.ui("minimap_toggle", "stravidium")
    session(det, "DD", t=1000)
    pose(det, 0, 0, t=1000)
    actors(det, *[ore(f"n{i}", 300_000 + 300 * i, 0, marker="StravidiumOre") for i in range(5)], t=1000)
    assert det.maybe_rescore(1000)
    pose(det, 50_000, 0, t=400)  # 500 m on, after the PC clock was set back 10 minutes
    assert det.maybe_rescore(400)


def test_the_mined_out_notice_ages_on_the_feed_clock(tmp_path):
    from harvesting_dune.outputs import NOTICE_S

    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.ui("minimap_toggle", "stravidium")
    det.ui("vehicle", "ornithopter")
    session(det, "DD")
    pose(det, 0, 0, t=1)
    near = [ore(f"n{i}", 400 * i, 0, marker="StravidiumOre") for i in range(5)]
    far = [ore(f"f{i}", 300_000 + 300 * i, 0, marker="StravidiumOre") for i in range(3)]
    actors(det, *near, *far, t=2)
    det.compute(2)
    for k in range(1, 6):  # you mine them one by one
        actors(det, *near[k:], *far, t=2 + k / 10)
    det.compute(3)
    assert det.badge(now=3).startswith("Area mined out")
    assert not det.badge(now=3 + NOTICE_S + 1).startswith("Area mined out")


def test_a_spice_field_on_rock_is_too_rough_for_a_harvester_but_one_on_open_sand_is_not():
    from harvesting_dune.areas import Area, Node, _field_too_rough
    from harvesting_dune.terrain import VEHICLES, TerrainGrid

    buggy = VEHICLES["buggy"]
    def field_at(x, y):
        return Area(x, y, 0.0, [Node("spicefieldmedium", x, y, 0.0, "live", 0.8)])
    grid = TerrainGrid()
    for i in range(-4, 5):
        for j in range(-4, 5):
            grid.block((i + 0.5) * CELL_CM, (j + 0.5) * CELL_CM, "buggy")  # every cell under the field
    assert _field_too_rough(field_at(0.5 * CELL_CM, 0.5 * CELL_CM), grid, buggy) == "only 0% of the field is drivable by buggy"
    assert _field_too_rough(field_at(200 * CELL_CM, 200 * CELL_CM), grid, buggy) is None


def test_a_node_counts_as_mined_by_its_own_key_or_by_its_spot_but_not_by_another_marker():
    from harvesting_dune.mined import MinedNodes
    from harvesting_dune.ranking import _you_mined

    mined = MinedNodes(None)
    assert not _you_mined(mined, "magnetiteore", 1000.0, 0.0, "k1")  # nothing mined yet
    mined.record("k1", "magnetiteore", 1000.0, 0.0, 0.0, 1.0)
    assert _you_mined(mined, "magnetiteore", 1200.0, 0.0)  # a scan row at the same spot
    assert _you_mined(mined, "magnetiteore", 90_000.0, 0.0, "k1")  # the same actor, wherever it is drawn
    assert not _you_mined(mined, "magnetiteore", 90_000.0, 0.0, "k2")
    assert not _you_mined(mined, "stravidiumore", 1000.0, 0.0)
