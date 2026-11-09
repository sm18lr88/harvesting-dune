"""Nodes you mine: tracked, hidden until respawn, and the target moves on when an area is done."""

from helpers import actors, ore, pose, session

from harvesting_dune.app import Detector
from harvesting_dune.catalog import respawn_s
from harvesting_dune.corrections import row_key


def _setup(tmp_path=None):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.ui("minimap_toggle", "stravidium")
    det.ui("vehicle", "ornithopter")
    session(det, "DD")
    pose(det, 0, 0, t=1)
    return det


def _row(x, y):
    return {"type": "BP_Stravidium_A_Spawner_C", "map_marker_id": "stravidiumore", "x": x, "y": y, "z": 0}


NEAR = [ore(f"n{i}", 400 * i, 0, marker="StravidiumOre") for i in range(5)]  # 0-16 m from you
FAR = [ore(f"f{i}", 300_000 + 300 * i, 0, marker="StravidiumOre") for i in range(3)]  # 3 km, smaller


def test_a_node_you_mine_is_tracked_hidden_and_excluded(tmp_path):
    det = _setup(tmp_path)
    det.load_scan([_row(400, 0)], 200, 1)
    actors(det, *NEAR, *FAR, t=2)
    actors(det, NEAR[0], *NEAR[2:], *FAR, t=3)  # n1 vanished right next to you (4 m)
    assert list(det.mined) == ["n1"]
    assert row_key(_row(400, 0)) in det.scan.stale and det.scan.stale[row_key(_row(400, 0))]["ttl"] == respawn_s("stravidiumore")
    assert all(not (n.x == 400 and n.y == 0) for n in det.nodes({"stravidiumore"}))  # neither actor nor scan row
    again = Detector(None, data_dir=tmp_path)
    session(again, "DD")
    assert list(again.mined) == ["n1"]  # survives a restart


def test_unloading_many_nodes_at_once_is_not_mining():
    det = _setup()
    actors(det, *NEAR, *FAR, t=2)
    actors(det, t=3)  # the whole list drops (map unload / flicker)
    assert det.mined == {}


def test_nodes_far_from_you_vanishing_are_not_yours():
    det = _setup()
    actors(det, *NEAR, *FAR, t=2)
    actors(det, *NEAR, *FAR[1:], t=3)  # someone else, 1.5 km away
    assert det.mined == {}


def test_mined_nodes_come_back_after_the_respawn_time_or_when_the_game_shows_them():
    det = _setup()
    actors(det, *NEAR, *FAR, t=2)
    actors(det, NEAR[0], *NEAR[2:], *FAR, t=3)
    assert "n1" in det.mined
    assert det.expire_mined(3 + respawn_s("stravidiumore") - 10) == 0
    actors(det, *NEAR, *FAR, t=200)  # the game shows it again (early respawn)
    assert det.mined == {}
    actors(det, NEAR[0], *NEAR[2:], *FAR, t=201)
    assert det.expire_mined(201 + respawn_s("stravidiumore") + 1) == 1 and det.mined == {}


def test_when_an_area_is_mined_out_the_target_moves_on_and_says_so():
    det = _setup()
    actors(det, *NEAR, *FAR, t=2)
    det.compute(2)
    assert det.top(1)[0].x < 10_000  # the nodes right here first
    actors(det, *NEAR[1:], *FAR, t=3)  # you mine them one by one
    actors(det, *NEAR[2:], *FAR, t=3.5)
    det.compute(3)
    assert det.top(1)[0].x < 10_000 and "3 left · 2 mined" in det.badge(now=3)
    for k in (3, 4, 5):  # and the rest
        actors(det, *NEAR[k:], *FAR, t=3 + k / 10)
    det.compute(4)
    assert det.top(1)[0].x > 100_000
    assert det.badge().startswith("Area mined out") and "3.0 km" in det.badge()
    assert len(det.mined) == 5


def test_mining_in_a_sparse_spot_still_counts():
    det = _setup()
    actors(det, *NEAR[:3], t=2)  # only three nodes loaded around you
    actors(det, *NEAR[1:3], t=3)
    assert list(det.mined) == ["n0"]



def test_mining_pace_is_learned_from_consecutive_mines(tmp_path):
    det = _setup(tmp_path)
    actors(det, *NEAR, *FAR, t=2)
    for k in range(1, 5):  # one node every 20 s, all close together
        actors(det, *NEAR[k:], *FAR, t=2 + 20 * k)
    assert det.learning.data["pace"]["stravidiumore"] == [20.0, 20.0, 20.0]


def test_a_node_seen_again_teaches_its_respawn_time_and_mined_nodes_follow_the_map(tmp_path):
    det = _setup(tmp_path)
    actors(det, *NEAR, *FAR, t=2)
    actors(det, NEAR[0], *NEAR[2:], *FAR, t=3)  # n1 mined
    actors(det, *NEAR, *FAR, t=903)  # back after 900 s
    assert det.learning.data["respawn"]["stravidiumore"] == [900.0]
    actors(det, NEAR[0], *NEAR[2:], *FAR, t=904)  # mined again
    session(det, "survival_1", t=905)  # another map: its own (empty) records
    assert det.mined == {}
    session(det, "DD", t=906)  # back: the DD records are loaded again
    assert list(det.mined) == ["n1"]



def test_moving_within_the_same_island_is_not_moving_on():
    """Live case: 'Move on (98 left here) -> next 194 m' inside the H5 island."""
    det = _setup()
    here = [ore(f"h{i}", 400 * (i % 4), 400 * (i // 4), marker="StravidiumOre") for i in range(8)]  # at you
    part2 = [ore(f"p{i}", 40_000 + 300 * i, 0, marker="StravidiumOre") for i in range(30)]  # 400 m on, same island
    actors(det, *here, *part2, t=2)
    det.compute(2)
    det.areas = [a for a in det.areas if a.x < 10_000] + [a for a in det.areas if a.x >= 10_000]  # target: here
    for k in range(1, 4):
        actors(det, *here[k:], *part2, t=2 + k)  # mine three here; the target shifts to part 2
    det.compute(6)
    assert det.top(1)[0].x > 30_000 and det.notice is None



def test_a_node_your_partner_takes_nearby_also_counts():
    det = _setup()
    partner = ore("q", 4500, 0, marker="StravidiumOre")  # 45 m from you
    stranger = ore("z", 9000, 0, marker="StravidiumOre")  # 90 m: too far to say
    actors(det, *NEAR, partner, stranger, *FAR, t=2)
    actors(det, *NEAR, stranger, *FAR, t=3)
    actors(det, *NEAR, *FAR, t=4)
    assert list(det.mined) == ["q"]
