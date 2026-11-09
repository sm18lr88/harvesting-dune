import time

import pytest
from helpers import CELL_CM, M, mountain, ore, payload, spice

from harvesting_dune.areas import Node, recommend
from harvesting_dune.catalog import BY_KEY, marker_of
from harvesting_dune.model import Pose, parse_actors
from harvesting_dune.store import ObservationStore, Status
from harvesting_dune.terrain import VEHICLES, TerrainGrid

P0 = Pose(0, 0, 0)


# -- catalog / model --------------------------------------------------------------------
def test_catalog_matches_companion_marker_names():
    assert marker_of(None, "magnetiteore") == "magnetiteore"
    assert marker_of("SpiceFieldLarge") == "spicefieldlarge"
    assert marker_of("BP_Something", None) is None
    assert BY_KEY["iron"].marker_types == ("MagnetiteOre",)


def test_parse_actors_matches_companion_shape():
    acts = parse_actors(payload(spice(1, 10, 20, "Large"), {"actorType": "Ore", "x": "bad", "y": 1}, ore(2, 1, 2, 3)))
    assert acts[0].key == "1" and acts[0].spice_size == "large" and acts[0].marker == "spicefieldlarge"
    assert len(acts) == 2 and acts[1].marker == "magnetiteore" and not acts[1].is_spice_field


# -- store ------------------------------------------------------------------------------
def test_store_tracks_fill_and_distinguishes_unloaded_from_missing():
    s = ObservationStore(absence_radius_cm=500 * M)
    s.set_pose(P0)
    s.apply_snapshot(parse_actors(payload(spice(1, 100 * M, 0), spice(2, 2000 * M, 0))), 0)
    s.apply_snapshot(parse_actors(payload(spice(1, 100 * M, 0, fill=0.5))), 60)
    assert s.items["1"].fill_trend_per_min() == pytest.approx(-0.3)
    assert s.items["2"].status is Status.OUT_OF_RANGE
    s.apply_snapshot([], 120)
    assert s.items["1"].status is Status.MISSING_IN_RANGE


def test_detection_range_learned_per_world_after_load_in():
    s = ObservationStore(min_range_samples=2)
    s.set_world("DD", 1)
    s.set_pose(P0)
    s.apply_snapshot(parse_actors(payload(spice(1, 50 * M, 0))), 0)
    s.apply_snapshot(parse_actors(payload(spice(1, 50 * M, 0), spice(2, 900 * M, 0))), 1)
    s.apply_snapshot(parse_actors(payload(spice(1, 50 * M, 0), spice(2, 900 * M, 0), spice(3, 0, 1100 * M))), 2)
    assert s.detection_range_cm("spicefieldmedium") == pytest.approx(1000 * M)
    s.set_world("Hagga", 1)
    assert s.detection_range_cm("spicefieldmedium") is None


# -- terrain ----------------------------------------------------------------------------
def test_driven_cells_are_proven_for_laxer_vehicles():
    g = TerrainGrid()
    g.add_driven(0.5 * CELL_CM, 0.5 * CELL_CM, 0, "sandcrawler")
    assert g.cell_cost((0, 0), VEHICLES["buggy"]) == 1.0
    g2 = TerrainGrid()
    g2.add_driven(0.5 * CELL_CM, 0.5 * CELL_CM, 0, "sandbike")
    assert g2.cell_cost((0, 0), VEHICLES["sandcrawler"]) != 1.0  # a bike track proves nothing for a crawler


def test_no_corner_cutting_through_a_mesa_wall():
    g = TerrainGrid()
    for i in range(-6, 7):
        for j in range(-6, 7):
            g.add_height_sample((20 + i + 0.5) * CELL_CM, (j + 0.5) * CELL_CM, 2000.0 if max(abs(i), abs(j)) <= 3 else 0.0)
    field = g.travel_field((0, 0), VEHICLES["sandcrawler"], {(20, 0)})
    assert (20, 0) not in field
    assert (20, 0) in g.travel_field((0, 0), VEHICLES["buggy"], {(20, 0)})


# -- area recommendation ----------------------------------------------------------------
def nodes(*specs):
    return [Node(m, x, y, z, src, fill) for (m, x, y, z, src, fill) in specs]


def test_big_spice_field_in_mountains_is_excluded_not_penalised():
    g = TerrainGrid()
    mountain(g, 20, 0)
    ns = nodes(("spicefieldlarge", 20.5 * CELL_CM, 0.5 * CELL_CM, 3000, "live", 1.0),
               ("spicefieldsmall", -8 * CELL_CM, 3 * CELL_CM, 0, "live", 0.6))
    res = recommend(BY_KEY["spice"], ns, P0, g, VEHICLES["sandcrawler"], top=0)
    assert res[0].feasible and res[0].x < 0
    mtn = next(a for a in res if a.x > 0)
    assert not mtn.feasible and "route" in mtn.reasons[0]


def test_ore_cluster_on_a_mountain_top_is_excluded_for_ground_vehicles_but_not_flyers():
    g = TerrainGrid()
    mountain(g, 30, 0, radius_cells=6, height=15000.0)  # ~150 m peak, cluster at the top
    top = [("magnetiteore", (30.5 + dx) * CELL_CM, (0.5 + dy) * CELL_CM, 15000, "live", None) for dx, dy in ((0, 0), (0.3, 0.2), (-0.2, 0.3), (0.1, -0.4))]
    low = [("magnetiteore", -10 * CELL_CM, 0, 0, "live", None), ("magnetiteore", -10.3 * CELL_CM, 0.2 * CELL_CM, 0, "live", None)]
    ns = nodes(*top, *low)
    buggy = recommend(BY_KEY["iron"], ns, P0, g, VEHICLES["buggy"], top=0)
    assert buggy[0].feasible and buggy[0].x < 0 and len(buggy[0].nodes) == 2
    assert not next(a for a in buggy if a.x > 0).feasible
    orni = recommend(BY_KEY["iron"], ns, P0, g, VEHICLES["ornithopter"], top=0)
    assert next(a for a in orni if a.x > 0).feasible  # flying: the mountain top is reachable


def test_ore_reachable_by_parking_nearby_and_walking():
    g = TerrainGrid()
    # small rocky knoll (one cell) the buggy cannot enter, ore on top; sand all around
    g.add_height_sample(10.5 * CELL_CM, 0.5 * CELL_CM, 0)
    g.add_height_sample(10.5 * CELL_CM + 10, 0.5 * CELL_CM, 6000)
    res = recommend(BY_KEY["iron"], nodes(("magnetiteore", 10.5 * CELL_CM, 0.5 * CELL_CM, 3000, "live", None)), P0, g, VEHICLES["buggy"], top=0)
    assert res[0].feasible


def test_spice_needs_a_spice_harvesting_vehicle():
    res = recommend(BY_KEY["spice"], nodes(("spicefieldsmall", 1000, 0, 0, "live", 0.5)), P0, TerrainGrid(), VEHICLES["sandbike"], top=0)
    assert not res[0].feasible and "cannot harvest spice" in res[0].reasons[0]


def test_live_fields_beat_equally_placed_scan_rows():
    ns = nodes(("spicefieldmedium", 20 * CELL_CM, 0, 0, "scan", 0.9), ("spicefieldmedium", -20 * CELL_CM, 0, 0, "live", 0.9))
    res = recommend(BY_KEY["spice"], ns, P0, TerrainGrid(), VEHICLES["buggy"], top=0)
    assert res[0].x < 0 and "not yet confirmed live" in res[1].reasons


def test_pruned_ranking_equals_exhaustive_and_is_fast():
    from harvesting_dune.areas import _clusters, _evaluate, _patches

    g = TerrainGrid()
    mountain(g, 15, 15, 5)
    mountain(g, -20, 5, 4)
    specs = [("magnetiteore", (i % 9 - 4) * 12 * CELL_CM + (i * 37 % 11) * 900, (i // 9 - 3) * 12 * CELL_CM, 0, "scan", None) for i in range(60)]
    base = (-10 * CELL_CM, 0)
    t0 = time.perf_counter()
    res = recommend(BY_KEY["iron"], nodes(*specs), P0, g, VEHICLES["buggy"], base=base, top=3)
    elapsed = time.perf_counter() - t0
    every = _clusters(nodes(*specs))
    _patches(every)  # same scoring as recommend(): each area counts its patch
    _evaluate(every, P0, g, VEHICLES["buggy"], base, drive_over=False)
    exhaustive = sorted((a for a in every if a.feasible), key=lambda a: -a.utility)[:3]
    assert [(round(a.x), round(a.y)) for a in res] == [(round(a.x), round(a.y)) for a in exhaustive]
    assert elapsed < 1.0, elapsed  # generous: may run while the game is loading the CPU


def test_areas_beyond_planning_range_are_labelled_not_called_blocked():
    far = nodes(("magnetiteore", 12_000 * M, 0, 0, "scan", None))
    res = recommend(BY_KEY["iron"], far, P0, TerrainGrid(), VEHICLES["buggy"], top=0)
    assert not res[0].feasible and "planning range" in res[0].reasons[0]
    assert recommend(BY_KEY["iron"], far, P0, TerrainGrid(), VEHICLES["ornithopter"], top=0)[0].feasible


def test_astar_route_cost_equals_dijkstra_on_random_terrain():
    import random

    rng = random.Random(7)
    for trial in range(6):
        g = TerrainGrid()
        for _ in range(900):
            i, j = rng.randint(-25, 25), rng.randint(-25, 25)
            g.add_height_sample((i + 0.5) * CELL_CM, (j + 0.5) * CELL_CM, rng.choice((0, 0, 0, 1500, 3000, 6000)))
        if trial % 2:  # some proven tracks change the heuristic floor
            for i in range(-10, 10):
                g.add_driven((i + 0.5) * CELL_CM, 0.5 * CELL_CM, 0, "buggy")
        v = VEHICLES["buggy"]
        start = (0.5 * CELL_CM, 0.5 * CELL_CM)
        field = g.travel_field(start, v, max_cells=40)
        for _ in range(15):
            goal = ((rng.randint(-20, 20) + 0.5) * CELL_CM, (rng.randint(-20, 20) + 0.5) * CELL_CM)
            r = g.route(start, goal, v, max_cells=40, max_detour=1e9)
            d = field.get((int(goal[0] // CELL_CM), int(goal[1] // CELL_CM)))
            assert (r is None) == (d is None)
            if r:
                assert r[0] == pytest.approx(d[0])


def test_a_real_haul_a_short_flight_away_beats_a_tiny_cluster_next_to_you():
    """Live case: 3 granite nodes 83 m away were chosen over big clusters."""
    tiny = [("rhyoliteore", 83 * M + i * 500, 0, 0, "live", None) for i in range(3)]
    big = [("rhyoliteore", 2_000 * M + (i % 5) * 1500, (i // 5) * 1500, 0, "live", None) for i in range(20)]
    res = recommend(BY_KEY["granite"], nodes(*tiny, *big), P0, TerrainGrid(), VEHICLES["ornithopter"], top=0)
    assert len(res[0].nodes) == 20
