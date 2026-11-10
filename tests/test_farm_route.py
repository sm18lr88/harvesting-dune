"""Farm route mode: a real Sheol fly-by replayed (tests/fixtures/sheol-flyby.jsonl: the Deep Desert, the
overland map, then Hagga Basin past the Sheol wrecks, cut from a recorded journal to the session, player
and loot-container messages), plus walking, looting, skipping and stopping on top of it."""

import json
import random
import shutil
from pathlib import Path

import pytest
from helpers import datasets, pose, session
from test_sync_page import run as run_page

from harvesting_dune import hotkeys
from harvesting_dune.app import Detector
from harvesting_dune.feed import BusMessage
from harvesting_dune.routes import parse_routes

FIXTURE = Path(__file__).parent / "fixtures" / "sheol-flyby.jsonl"
SHEOL, SLAG = "slag-sheol-wrecks", "t5radiatedcorecomponent"


def replay(det: Detector, each=lambda: None) -> float:
    if not FIXTURE.is_file():
        pytest.skip("needs tests/fixtures/sheol-flyby.jsonl: a real flight recording, not in the public repository")
    t = 0.0
    for line in FIXTURE.read_text("utf8").splitlines():
        rec = json.loads(line)
        t = rec["t"]
        det.handle(BusMessage(rec["type"], rec["topic"], rec["payload"]), t)
        each()
    return t


def emptied(det: Detector, stop, t: float) -> None:
    """The companion shows the container at this stop emptied (hasLoot false), as it does after looting."""
    det.handle(BusMessage("delta", "dune.map.actors", {"gated": False, "worldId": "survival_1", "coriolisWorldSeed": 1, "actors": [
        {"id": f"c{stop.key}", "actorType": "BP_TemporaryLootContainer_BasicContainer_C", "markerType": "LootContainer",
         "hasLoot": False, "x": stop.x, "y": stop.y, "z": stop.z}]}), t)


def loot_in_order(det: Detector, t: float, n: int) -> None:
    """Stop 1 -> 2 -> ...: one line to the next stop only, numbered rings for the next three, then loot it."""
    run = det.farm
    for k in range(n):
        i = run.next_index
        assert i == run.order[k]
        state = det.page_state()
        assert [(s["x"], s["y"]) for s in state["route"]["stops"]] == [(run.stops[i].x, run.stops[i].y)]
        assert [a["num"] for a in state["alternatives"] if a["kind"] == "route-stop"] == [k + 1, k + 2, k + 3]
        assert sum(a["kind"] == "route-done" for a in state["alternatives"]) == k
        t += 30
        if k % 2:  # shown emptied while you are away does not count ...
            emptied(det, run.stops[i], t)
            assert run.next_index == i
            pose(det, run.stops[i].x + 300, run.stops[i].y, t=t + 1)  # ... until you come within 6 m
        else:
            pose(det, run.stops[i].x + 300, run.stops[i].y, t=t)  # 3 m from the chest ...
            assert run.next_index == i  # ... is not enough: the container must show emptied too
            emptied(det, run.stops[i], t + 1)


def slag_detector(tmp_path) -> Detector:
    det = datasets(Detector(None, data_dir=tmp_path, min_rescore_s=0))
    det.items.ingest({"materials": [{"id": SLAG, "name": "Irradiated Slag", "categories": ["items/misc/components"]}]})
    det.ui("resource", "item:" + SLAG)
    return det


def test_replay_sheol_route_travel_line_frozen_order_loot_and_site_done(tmp_path):
    det = slag_detector(tmp_path)
    assert SHEOL in [r["id"] for r in det.snapshot()["farmRoutes"]]
    assert det.farm is None and not (det.badge() or "").startswith("Farm route")  # off by default
    det.ui("route_start", SHEOL)
    travel, orders = set(), set()

    def watch():
        if det.store.world and det.store.world[0] == "deepdesert_1" and det.store.pose:
            state = det.page_state()
            travel.add((det.badge(), state["route"], state["dest"], len(state["alternatives"])))
        orders.add(tuple(det.farm.order or ()))

    t = replay(det, watch)
    assert travel == {("Farm route: travel to Hagga Basin, Wreck of the Ourea", None, None, 0)}  # wrong map: no lines
    orders.discard(())
    assert det.snapshot()["farm"]["line"].startswith("Farm route: Wreck of the Ourea")  # the target card says the same
    assert len(orders) == 1  # fixed once, when the fly-by reached the wreck
    loot_in_order(det, t, 3)
    run = det.farm
    assert run.next_index == run.order[3]
    assert len(json.loads(next(tmp_path.glob("route-done-survival_1-*.json")).read_text())) == 3
    while run.next_index is not None and run.site.name == "Wreck of the Ourea":
        det.ui("route_skip")
    card = det.snapshot()["farm"]
    assert card["notice"].startswith("Wreck of the Ourea done: next ") and card["notice"].endswith("/ reset time unknown")
    assert card["site"] != "Wreck of the Ourea"  # default: on to the next site


def test_order_never_changes_while_walking(tmp_path):
    for seed in range(4):
        det = slag_detector(tmp_path / str(seed))
        det.ui("route_start", SHEOL)
        t = replay(det)
        run, rng = det.farm, random.Random(seed)
        frozen, site = list(run.order), run.pos
        for _ in range(400):
            t += 1
            s = run.stops[rng.randrange(len(run.stops))]
            pose(det, s.x + rng.uniform(-20000, 20000), s.y + rng.uniform(-20000, 20000), t=t)
            if rng.random() < 0.3:
                pose(det, s.x, s.y, t=t)
                emptied(det, s, t)
            if run.pos != site:
                break  # the site is done: the next one gets its own order
            assert run.order == frozen


def test_skip_hotkey_advances_and_stop_route_clears_every_mark(tmp_path):
    det = datasets(Detector(None, data_dir=tmp_path, min_rescore_s=0))
    session(det, "survival_1", seed=1)
    det.ui("route_start", SHEOL)
    first = det.farm.site.stops[0]
    pose(det, first.x, first.y, t=1)
    run = det.farm
    order = list(run.order)
    skip = next(name for vk, name in hotkeys.BINDINGS.values() if vk == hotkeys.VK_N)
    det.ui(skip)
    assert run.next_index == order[1] and run.done == {order[0]}
    state = det.page_state()
    assert state["route"]["stops"][0]["x"] == run.stops[order[1]].x
    assert [a["kind"] for a in state["alternatives"]].count("route-done") == 1
    det.ui("route_stop")
    state = det.page_state()
    assert det.farm is None and state["route"] is None and state["dest"] is None
    assert not any(a["kind"].startswith("route") for a in state["alternatives"])
    assert not (det.badge() or "").startswith("Farm route") and det.snapshot()["farm"] is None


def test_route_mode_leaves_the_ranking_alone(tmp_path):
    det = datasets(Detector(None, data_dir=tmp_path, min_rescore_s=0))
    det.areas, det.resource_key = ["ranked"], "iron"
    det.ui("route_start", SHEOL)
    det.ui("route_skip")
    det.ui("route_stop")
    assert det.areas == ["ranked"] and det.resource_key == "iron"


def test_a_restart_does_not_send_you_back_to_a_chest_you_just_emptied(tmp_path):
    det = datasets(Detector(None, data_dir=tmp_path, min_rescore_s=0))
    session(det, "survival_1", seed=1)
    det.ui("route_start", SHEOL)
    s = det.farm.site.stops[0]
    pose(det, s.x, s.y, t=1)
    emptied(det, s, 2)
    det2 = datasets(Detector(None, data_dir=tmp_path, min_rescore_s=0))
    session(det2, "survival_1", seed=1)
    det2.ui("route_start", SHEOL)
    pose(det2, s.x, s.y, t=3)
    assert det2.farm.done == {det2.farm.stops.index(s)}


def test_repeat_waits_for_the_reset_then_restarts_the_same_order(tmp_path):
    det = datasets(Detector(None, data_dir=tmp_path, min_rescore_s=0))
    station = next(r for r in det.routes if r.id == "testing-station-024")
    world = station.sites[0].world
    session(det, world, seed=1)
    det.ui("route_start", station.id)
    det.ui("route_repeat")
    s = station.sites[0].stops[0]
    pose(det, s.x, s.y, t=10)
    order = list(det.farm.order)
    for _ in order:
        det.ui("route_skip")
    assert det.snapshot()["farm"]["notice"] == "Imperial Testing Station No. 24 done: repeat here in 45 min"
    pose(det, s.x, s.y, t=11)
    assert det.farm.phase == "wait" and det.page_state()["route"] is None  # waiting for the reset: no line
    pose(det, s.x, s.y, t=10 + 45 * 60 + 1)
    assert det.farm.phase == "site" and det.farm.order == order and not det.farm.done


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_settings_page_lists_the_routes_and_the_route_you_are_on(tmp_path):
    det = slag_detector(tmp_path)
    session(det, "survival_1", seed=1)
    pose(det, -300000, 160000, t=1)
    det.ui("route_start", SHEOL)
    log = run_page(det.snapshot(), """
      page.render(SNAP);
      log.farm = el("farm").innerHTML;
      fire("click", { closest: () => ({ dataset: { route: "diamondine-mysa-tarill" } }) });
      fire("click", { closest: () => ({ dataset: { farm: "route_skip" } }) });
      await tick();
      log.calls = calls.map((c) => c.url).filter((u) => u.includes("route_"));
      const old = { ...SNAP }; delete old.farm; delete old.farmRoutes;
      page.render(old); log.old = el("farm").innerHTML;
    """)
    farm = log["farm"]
    assert "Farm route: Wreck of the Ourea" in farm and "Stop route" in farm and "Repeat this site" in farm
    assert '<tr class="sel"><td>Sheol wrecks <span class="note">order: nearest</span>' in farm  # one short note, no badge
    assert 'data-route="slag-sheol-wrecks"' not in farm and 'data-route="slag-tyche"' in farm  # Start on the others
    assert log["calls"] == ["/control?action=route_start&value=diamondine-mysa-tarill", "/control?action=route_skip"]
    assert log["old"] == ""  # an older detector without the keys: nothing, and no error


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_card_agrees_with_the_routes_and_worlds_have_readable_names(tmp_path):
    """No drop rows for the item on this map (index not built yet), but a farm route here: the card says so."""
    det = slag_detector(tmp_path)
    session(det, "survival_1", seed=1)
    pose(det, -300000, 160000, t=1)
    log = run_page(det.snapshot(), """
      page.render(SNAP);
      log.meta = el("tMeta").textContent; log.info = el("itemInfo").innerHTML; log.farm = el("farm").innerHTML;
    """)
    assert "not on this map" not in log["meta"] and "does not drop anywhere" not in log["info"]
    assert "Drops in chests here: see Farm route" in log["info"]
    assert "<td>Hagga Basin</td>" in log["farm"] and "<td>instance (Landsraad mission)</td>" in log["farm"]
    assert "Overland" not in log["farm"]
    det.handle(BusMessage("snapshot", "dune.session", {"worldId": "deepdesert_1", "coriolisWorldSeed": 1}), 2)
    log = run_page(det.snapshot(), 'page.render(SNAP); log.info = el("itemInfo").innerHTML;')
    assert "does not drop anywhere" in log["info"]  # no route on this map: the plain line stays


def test_route_ends_after_10_min_away_but_not_on_the_way_to_an_instance(tmp_path):
    det = datasets(Detector(None, data_dir=tmp_path, min_rescore_s=0))
    session(det, "survival_1", seed=1, t=0)
    det.ui("route_start", "testing-station-024")
    pose(det, 0, 0, t=700)  # walking to the station in Hagga Basin
    assert det.farm is not None and det.farm.phase == "travel"
    session(det, "deepdesert_1", seed=1, t=701)
    pose(det, 0, 0, t=1300)
    assert det.farm is not None
    pose(det, 0, 0, t=1302)
    assert det.farm is None and not (det.badge() or "").startswith("Farm route")


def own_routes(tmp_path, *routes) -> Detector:
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    assert det.on_dataset("routes", {"routes": list(routes)})
    return det


def site(name, world, x=None, center=None, weekly=False) -> dict:
    d = {"name": name, "world": world, "fromItems": weekly,
         "stops": [] if x is None else [{"x": x, "y": 0, "z": 0, "kind": "chest", "name": f"{name} chest"}]}
    return d | ({"center": {"x": center, "y": 0, "z": 0}} if center is not None else {})


TWO = {"id": "two", "items": ["x"], "resetS": 600,
       "sites": [site("A", "survival_1", 0, center=0), site("B", "survival_1", 500000, center=500000)]}


def test_deep_desert_route_survives_10_min_in_hagga_basin_on_the_way(tmp_path):
    det = own_routes(tmp_path, {"id": "dd", "items": ["x"], "sites": [site("DD wreck", "deepdesert_1", 100000, center=100000)]})
    session(det, "survival_1", seed=1, t=1000)
    det.ui("route_start", "dd")
    pose(det, 0, 0, t=1601)  # still flying to the Deep Desert exit
    assert det.farm is not None and det.farm.phase == "travel"


def test_skip_moves_on_from_a_weekly_site_with_no_stops_known(tmp_path):
    det = own_routes(tmp_path, {"id": "w", "items": ["x"], "resetS": 3600, "sites": [
        site("Weekly", "deepdesert_1", weekly=True), site("Fixed", "deepdesert_1", 0, center=0)]})
    session(det, "survival_1", seed=1, t=1)  # started away: the dataset's site order
    det.ui("route_start", "w")
    session(det, "deepdesert_1", seed=1, t=2)
    pose(det, 0, 0, t=3)
    assert det.farm.site.name == "Weekly" and det.farm.stops == ()
    det.ui("route_skip")
    assert det.farm.site.name == "Fixed"


def test_repeat_turned_on_while_waiting_for_the_next_site_shows_that_sites_stops(tmp_path):
    det = own_routes(tmp_path, TWO)
    session(det, "survival_1", seed=1, t=1)
    det.ui("route_start", "two")
    pose(det, 0, 0, t=2)
    det.ui("route_skip")  # A done
    pose(det, 500000, 0, t=3)
    det.ui("route_skip")  # B done: both within the reset, wait for A
    assert det.farm.phase == "wait" and det.farm.site.name == "A"
    det.ui("route_repeat")
    pose(det, 0, 0, t=700)  # the wait ends
    pose(det, 0, 0, t=701)
    assert [s.name for s in det.farm.stops] == ["A chest"] and det.page_state()["dest"]["x"] == 0


def test_repeat_turned_off_while_waiting_to_repeat_moves_on(tmp_path):
    det = own_routes(tmp_path, TWO)
    session(det, "survival_1", seed=1, t=1)
    det.ui("route_start", "two")
    det.ui("route_repeat")
    pose(det, 0, 0, t=2)
    det.ui("route_skip")  # A done: wait to repeat it
    assert det.farm.phase == "wait" and det.farm.site.name == "A"
    det.ui("route_repeat")
    assert det.farm.phase != "wait" and det.farm.site.name == "B"


def test_a_new_coriolis_seed_resolves_a_weekly_sites_stops_again(tmp_path):
    det = own_routes(tmp_path, {"id": "w", "items": ["slag"], "resetS": 3600,
                                "sites": [site("Row A", "deepdesert_1", weekly=True)]})
    det.items.by_map = {"week1": {"slag": [(1000.0, 0.0, 0.0, "Basic Container")]},
                        "week2": {"slag": [(4_000_000.0, 0.0, 0.0, "Basic Container")]}}
    det._item_map = lambda: {1: "week1", 2: "week2"}[det.store.world[1]]
    session(det, "deepdesert_1", seed=1, t=1)
    det.ui("route_start", "w")
    pose(det, 100, 0, t=2)
    assert [(s.x, s.y) for s in det.farm.stops] == [(1000.0, 0.0)]
    session(det, "deepdesert_1", seed=2, t=3)  # the weekly reset: a new layout on the same map
    pose(det, 4_000_100.0, 0, t=4)
    assert [(s.x, s.y) for s in det.farm.stops] == [(4_000_000.0, 0.0)] and det.farm.phase == "site"


def test_routes_dataset_bad_rows_are_skipped_with_a_warning(caplog):
    good = {"id": "ok", "items": ["x"], "sites": [{"name": "A", "world": "survival_1",
                                                   "stops": [{"x": 1, "y": 2, "z": 3, "kind": "chest"}, {"x": "bad", "y": 2, "kind": "chest"}]}]}
    rows = [good, {"id": 5, "sites": []}, {"id": "nosites"}, {"id": "nan", "sites": [{"name": "B", "world": "w", "stops": [{"x": float("nan"), "y": 0, "kind": "chest"}]}]}, "junk"]
    routes = parse_routes({"version": 1, "routes": rows})
    assert [r.id for r in routes] == ["ok"] and len(routes[0].sites[0].stops) == 1
    assert sum("routes dataset" in m for m in caplog.messages) == 6  # 4 bad routes, 2 bad stops
    assert parse_routes("junk") == [] and parse_routes({"routes": {}}) == []


def test_no_routes_without_the_dataset(tmp_path):
    """The routes are server-held: a detector that never got the dataset has none, and says nothing odd."""
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    assert det.routes == [] and det.snapshot()["farmRoutes"] == []
    det.ui("route_start", SHEOL)
    assert det.farm is None
    assert not det.on_dataset("routes", {"routes": []}) and det.routes == []  # an empty or bad dataset changes nothing
    assert len(datasets(det).routes) == 4  # the fixture loads whole


def containers(det: Detector, stops, t: float, has_loot: bool = False) -> None:
    """One actors message that shows every container at these stops, emptied or with loot."""
    det.handle(BusMessage("delta", "dune.map.actors", {"gated": False, "worldId": "survival_1", "coriolisWorldSeed": 1, "actors": [
        {"id": f"c{s['x']}", "actorType": "BP_TemporaryLootContainer_BasicContainer_C", "markerType": "LootContainer",
         "hasLoot": has_loot, "x": s["x"], "y": s["y"], "z": s["z"]} for s in stops]}), t)


def yard(*xs) -> dict:
    """One Hagga Basin site with a chest at each x, in this order."""
    return {"id": "yard", "items": ["x"], "resetS": 600, "sites": [{
        "name": "Yard", "world": "survival_1", "center": {"x": 0, "y": 0, "z": 0},
        "stops": [{"x": x, "y": 0, "z": 0, "kind": "chest", "name": f"chest {x}"} for x in xs]}]}


FOUR = yard(0, 1000, 2000, 3000)  # 10 m apart: nearest-next from x=0 is the dataset order


def started(tmp_path, route) -> Detector:
    det = own_routes(tmp_path, route)
    session(det, "survival_1", seed=1, t=1)
    det.ui("route_start", route["id"])
    return det


def test_one_actors_message_closes_every_stop_it_shows_emptied(tmp_path):
    det = started(tmp_path, FOUR)
    stops = FOUR["sites"][0]["stops"]
    pose(det, 0, 0, t=2)  # the order freezes here
    pose(det, 0, 0, t=2.5)
    pose(det, 1000, 0, t=3)  # came to the first two chests
    containers(det, stops[:2], t=4)  # both looted between two actors messages
    assert det.farm.done == {0, 1}


def test_an_emptied_container_closes_only_its_own_stop_not_a_neighbour_84_cm_away(tmp_path):
    route = yard(0, 84)
    det = started(tmp_path, route)
    pose(det, 40, 0, t=2)  # the order freezes here
    pose(det, 40, 0, t=2.5)  # within 6 m of both chests
    containers(det, route["sites"][0]["stops"][:1], t=3)
    assert (det.farm.done, det.farm.next_index) == ({0}, 1)  # the full neighbour is next


def test_a_session_message_without_its_coriolis_seed_keeps_the_frozen_order_and_skips(tmp_path):
    det = started(tmp_path, FOUR)
    pose(det, 0, 0, t=2)
    det.ui("route_skip")
    det.ui("route_skip")
    frozen = list(det.farm.order)
    det.handle(BusMessage("snapshot", "dune.session", {"worldId": "survival_1"}), 3)  # after a loading screen
    session(det, "survival_1", seed=1, t=4)  # the same seed, 3 s later
    pose(det, 3000, 0, t=5)  # now standing at the far end of the site
    assert (det.farm.order, det.farm.done) == (frozen, set(frozen[:2]))


def test_time_on_the_overland_map_on_the_way_to_an_instance_does_not_end_the_run(tmp_path):
    det = own_routes(tmp_path, {"id": "inst", "items": ["x"], "sites": [site("Station", "dungeon_1", 0, center=0)]})
    session(det, "survival_1", seed=1, t=1)
    det.ui("route_start", "inst")
    session(det, "overmap", seed=1, t=2)
    pose(det, 0, 0, t=3)
    pose(det, 0, 0, t=2 + 700)  # 11 minutes over the overland map
    assert det.farm is not None and det.farm.phase == "travel"


def test_the_minimap_text_is_pushed_again_when_the_distance_to_the_stop_changes(tmp_path):
    det = started(tmp_path, FOUR)
    pose(det, 0, 0, t=2)
    rev = det.publish_rev
    pose(det, -20000, 0, t=3)  # 200 m from the same next stop
    assert det.publish_rev > rev


def test_the_minimap_text_is_pushed_again_when_the_site_done_notice_runs_out(tmp_path):
    det = started(tmp_path, TWO)
    pose(det, 0, 0, t=2)
    det.ui("route_skip")  # A done: the notice shows
    rev = det.publish_rev
    pose(det, 0, 0, t=2 + 121)  # same place, after the notice's 120 s
    assert det.publish_rev > rev


def test_a_stop_that_shows_loot_again_is_not_done_when_you_reach_it(tmp_path):
    det = started(tmp_path, FOUR)
    stops = FOUR["sites"][0]["stops"]
    pose(det, 0, 0, t=2)
    containers(det, stops[1:2], t=3)  # shown emptied while you are 10 m away ...
    containers(det, stops[1:2], t=4, has_loot=True)  # ... then full again
    pose(det, 1000, 0, t=5)
    assert 1 not in det.farm.done


def test_a_late_emptied_report_during_the_repeat_wait_does_not_restart_the_wait(tmp_path):
    det = started(tmp_path, TWO)
    det.ui("route_repeat")
    pose(det, 0, 0, t=2)
    det.ui("route_skip")  # A's only chest closed with Next stop at t=2: repeat at 2 + resetS
    containers(det, TWO["sites"][0]["stops"], t=100)
    assert det.farm.wait_until == 2 + TWO["resetS"]


def test_a_routes_row_with_non_list_sources_loads_without_sources():
    row = {"id": "s", "items": ["x"], "sources": 5, "sites": [site("A", "survival_1", 0)]}
    assert [(r.id, r.sources) for r in parse_routes({"routes": [row]})] == [("s", ())]


def test_a_400_digit_number_in_a_routes_row_is_skipped_not_raised():
    big = 10 ** 400
    row = {"id": "b", "items": ["x"], "resetS": big, "sites": [{"name": "A", "world": "survival_1", "stops": [
        {"x": big, "y": 0, "kind": "chest"}, {"x": 1, "y": 2, "kind": "chest"}]}]}
    routes = parse_routes({"routes": [row]})
    assert [(r.reset_s, [(s.x, s.y) for s in r.sites[0].stops]) for r in routes] == [(None, [(1.0, 2.0)])]


def test_an_unknown_route_or_a_route_action_without_a_run_is_refused(tmp_path):
    det = own_routes(tmp_path, TWO)
    assert det.ui("route_start", "no-such-route") is False
    assert det.ui("route_skip") is False and det.ui("route_start", "two") is True
