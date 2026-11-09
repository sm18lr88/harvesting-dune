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
