"""PvP yield bonus, never-small spice targets, remembered resource, alternative lines for ore."""

import json

from helpers import CELL_CM, actors, ore, pose, session, spice

from harvesting_dune.app import Detector
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
