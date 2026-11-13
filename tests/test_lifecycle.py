"""Spice lifecycle (blows, remembered fields, stale removal) and self-management."""

import ctypes
import json
import os
import shutil
import subprocess
import sys
from ctypes import wintypes

import pytest
from helpers import CELL_CM, M, actors, ore, pose, session, spice

from harvesting_dune.app import Detector
from harvesting_dune.feed import BRIDGE_SCRIPT
from harvesting_dune.governor import (
    Governor,
    cap_directory,
    own_working_set_mb,
    system_free_mb,
)
from harvesting_dune.store import Status


def test_dormant_spawn_point_turning_active_is_a_spice_blow():
    det = Detector(None, min_rescore_s=0)
    session(det, "DD")
    pose(det, 10, 0)
    actors(det, spice(1, 3000 * 100, 0, "Medium", fill=0, spawned=False), t=1)
    assert det.recent_event(1) is None
    actors(det, spice(1, 3000 * 100, 0, "Medium", fill=0.05, spawned=True), t=2)
    assert det.recent_event(2) == "Spice blow! medium field 3.0 km away"
    assert det.badge(now=2).startswith("Spice blow!")
    assert det.recent_event(2 + 91) is None  # shown for 90 s


def test_a_size_search_hears_only_news_of_its_own_size():
    # live 2026-11-11: a large-field search showed "Spice blow! medium field" on the badge
    det = Detector(None, min_rescore_s=0)
    det.ui("resource", "spice_large")
    session(det, "DD")
    pose(det, 10, 0)
    actors(det, spice(1, 3000 * 100, 0, "Medium", fill=0, spawned=False), t=1)
    actors(det, spice(1, 3000 * 100, 0, "Medium", fill=0.05, spawned=True), t=2)
    assert det.recent_event(2) == "Spice blow! medium field 3.0 km away"
    assert "Spice blow" not in det.badge(now=2)
    det.ui("resource", "spice")  # every size
    assert det.badge(now=2).startswith("Spice blow!")


def test_field_coming_into_range_is_not_a_blow():
    det = Detector(None)
    session(det, "DD")
    pose(det, 10, 0)
    actors(det, spice(1, 1000, 0, spawned=True), t=1)
    assert det.recent_event(1) is None


def test_active_field_is_remembered_after_leaving_range_then_forgotten():
    det = Detector(None)
    session(det, "DD")
    pose(det, 10, 0)
    actors(det, spice(7, 20 * CELL_CM, 0, "Medium", fill=0.7), t=100)
    actors(det, t=101)  # out of load range now
    t = det.store.items["7"]
    assert t.status is Status.OUT_OF_RANGE
    mem = det.remembered_spice(200)
    assert mem and mem[0]["spawned"] is True and mem[0]["fillRate"] == 0.7 and mem[0]["reportable"] is False
    assert det.remembered_spice(100 + 46 * 60) == []  # 45 min TTL
    # the store forgets it after the TTL as well (but keeps ordinary actors only 20 min)
    assert det.store.prune(100 + 30 * 60, 20 * 60, 45 * 60) == 0
    assert det.store.prune(100 + 46 * 60, 20 * 60, 45 * 60) == 1


def test_a_loading_screen_keeps_tracked_actors_of_the_same_world():
    det = Detector(None, min_rescore_s=0)
    session(det, "DD", t=0)
    pose(det, 0, 0)
    actors(det, spice("s1", 500 * M, 0, "Large"), t=1)
    actors(det, t=2)  # left the load range: remembered
    session(det, None, t=10)  # loading screen reports no world
    assert det.store.world == (None, 7)  # guild sync still sees the menu
    session(det, "DD", t=20)
    assert [d["id"] for d in det.remembered_spice(21)] == ["dd-mem-s1"]
    session(det, "HB", t=30)  # a different world still starts empty
    assert det.store.items == {}


def test_a_saved_range_marks_a_vanished_node_missing_from_the_first_minute():
    det = Detector(None, min_rescore_s=0)
    det.store.saved_ranges = {"DD": {"magnetiteore": 200 * M}}
    session(det, "DD", t=0)
    pose(det, 0, 0)
    actors(det, ore("o1", 50 * M, 0), t=1)
    actors(det, t=2)  # gone while the player stands 50 m away
    assert det.store.items["o1"].status is Status.MISSING_IN_RANGE


def test_actors_still_in_the_latest_list_survive_a_quiet_feed():
    det = Detector(None, min_rescore_s=0)
    session(det, "DD", t=0)
    pose(det, 0, 0)
    acts = [ore(f"o{i}", (100 + i) * M, 0) for i in range(3)]
    actors(det, *acts, t=1)
    actors(det, *acts, t=2)
    # real journals show 28-43 min in a world with no new actor list
    assert det.store.prune(2 + 30 * 60, 20 * 60, 45 * 60) == 0
    actors(det, *acts, t=3 + 30 * 60)
    assert det.store._first_seen_distances == {}  # their return is not a first sighting
    # if the feed stops for good, they are still forgotten eventually
    assert det.store.prune(3 + 76 * 60, 20 * 60, 45 * 60) == 3


def test_confirmed_stale_rows_trigger_one_scan_refresh_and_drop_their_overlay():
    det = Detector(None)
    session(det, "DD")
    pose(det, 10, 0)
    det.scan.stale["SpiceField:1:2"] = {"x": 1, "y": 2, "z": 0, "type": "ResourceNode", "sprite": "magnetiteore", "reason": "absent"}
    assert det.scan_refresh_due(1000)
    assert det.page_state()["extraActors"][0]["id"].startswith("dd-stale-")
    det.scan_refreshed(1000)
    assert not det.scan_refresh_due(2000)
    assert det.page_state()["extraActors"] == []  # removed from the page's scan layer: no overlay needed
    det.scan.stale["SpiceField:5:6"] = {"x": 5, "y": 6, "z": 0, "type": "SpiceField", "sprite": "spicefieldsmall", "reason": "absent"}
    assert not det.scan_refresh_due(1030)  # at most once a minute
    assert det.scan_refresh_due(1061)


def test_lean_mode_skips_actor_journaling(tmp_path):
    det = Detector(tmp_path)
    det.lean = True
    session(det, "DD")
    actors(det, spice(1, 1000, 0), t=1)
    lines = next(tmp_path.glob("*.jsonl")).read_text().splitlines()
    assert [json.loads(line)["topic"] for line in lines] == ["dune.session"]


def test_governor_goes_lean_under_memory_pressure_and_recovers(monkeypatch, tmp_path):
    import harvesting_dune.governor as g

    det = Detector(tmp_path)
    gov = Governor(det, tmp_path)
    monkeypatch.setattr(g, "system_free_mb", lambda: 800.0)
    monkeypatch.setattr(g, "own_working_set_mb", lambda: 150.0)
    gov.tick(0)
    assert gov.lean and det.lean
    monkeypatch.setattr(g, "system_free_mb", lambda: 1500.0)
    gov.tick(100)
    assert gov.lean  # hysteresis: not yet
    monkeypatch.setattr(g, "system_free_mb", lambda: 2500.0)
    gov.tick(200)
    assert not gov.lean and not det.lean


def test_journal_folder_is_capped_oldest_first(tmp_path):
    for i in range(5):
        f = tmp_path / f"j{i}.jsonl"
        f.write_bytes(b"x" * 1024 * 1024)
        os.utime(f, (1000 + i, 1000 + i))
    removed = cap_directory(tmp_path, 2.5, keep={tmp_path / "j0.jsonl"})
    assert removed == 3
    assert sorted(p.name for p in tmp_path.iterdir()) == ["j0.jsonl", "j4.jsonl"]


def test_the_governor_forgets_an_unseen_node_after_20_minutes_and_an_active_field_after_45(monkeypatch, tmp_path):
    import harvesting_dune.governor as g

    monkeypatch.setattr(g, "system_free_mb", lambda: 8000.0)  # plenty of memory: lean mode stays off
    monkeypatch.setattr(g, "own_working_set_mb", lambda: 100.0)
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    session(det, "DD", t=0)
    pose(det, 10, 0)
    near = ore("near", 10 * M, 0)
    actors(det, near, ore("o1", 20 * CELL_CM, 0), spice("s1", 20 * CELL_CM, 0, "Medium", fill=0.7), t=100)
    actors(det, near, t=101)  # the node and the active field left the load range
    gov = Governor(det, None)
    gov.tick(100 + 19 * 60)
    assert {"o1", "s1"} <= set(det.store.items)
    gov.tick(100 + 21 * 60)
    assert "o1" not in det.store.items and "s1" in det.store.items
    gov.tick(100 + 46 * 60)
    assert "s1" not in det.store.items


def priority_class() -> int:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.GetPriorityClass.argtypes = [wintypes.HANDLE]
    k32.GetPriorityClass.restype = wintypes.DWORD
    return k32.GetPriorityClass(k32.GetCurrentProcess())


@pytest.mark.skipif(os.name != "nt", reason="Windows APIs")
def test_windows_probes_work():
    before = priority_class()
    assert system_free_mb() > 0 and own_working_set_mb() > 0
    # in a child process: the policy is process-wide, and Windows passes it on to every process pytest starts later
    probe = subprocess.run([sys.executable, "-c", "from harvesting_dune.governor import apply_process_policy as a; print(a())"],
                           capture_output=True, text=True, timeout=60, check=False, creationflags=subprocess.CREATE_NO_WINDOW)
    assert probe.returncode == 0, probe.stderr
    assert "below-normal priority" in probe.stdout  # at least the priority change applies
    assert priority_class() == before



@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_bridge_scan_refresh_clears_then_restores_the_session():
    code = r"""
const listeners = [];
const wv = { postMessage() {}, addEventListener: (_, f) => listeners.push(f), dispatchEvent: (e) => { for (const f of [...listeners]) f(e); } };
globalThis.window = globalThis; globalThis.chrome = { webview: wv };
globalThis.location = { pathname: '/dune/minimap' };
globalThis.document = { readyState: 'loading', getElementById: () => null };
globalThis.__harvestingDuneEmit = () => {}; globalThis.fetch = async () => new Response('[]');
eval(SCRIPT);
const seeds = [];
wv.addEventListener('message', (e) => { const m = e.data; if (m.topic === 'dune.session') seeds.push(m.payload.mapScanSeed); });
const before = __dd.refreshScan();
wv.dispatchEvent({ data: { type: 'delta', topic: 'dune.session', payload: { worldId: 'deepdesert_1', mapScanSeed: 7 } } });
const r = __dd.refreshScan();
setTimeout(() => { console.log(JSON.stringify({ before, r, seeds })); process.exit(0); }, 100);
""".replace("SCRIPT", json.dumps(BRIDGE_SCRIPT))
    out = subprocess.run(["node", "-"], input=code, capture_output=True, text=True, timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout.splitlines()[0])
    assert res["before"] == "no-session" and res["r"] == "ok"
    assert res["seeds"] == [7, 0, 7]  # native, cleared (layer drops), restored (layer refetches)


def test_spice_is_planned_for_the_harvester_while_scouting_in_an_ornithopter():
    det = Detector(None, min_rescore_s=0)
    session(det, "DD", activity="Driving")
    det.vehicles.current, det.vehicles.last_harvester = "ornithopter", "sandcrawler"
    assert det.plan_vehicle == "sandcrawler"
    assert det.badge(now=0).startswith("Spice · crawler · ")
    det.ui("resource", "iron")
    assert det.plan_vehicle == "ornithopter"  # ore can be flown to


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_bridge_hides_empty_spawn_points_but_forwards_them():
    code = r"""
const listeners = [], emitted = [];
const wv = { postMessage() {}, addEventListener: (_, f) => listeners.push(f), removeEventListener() {}, dispatchEvent: (e) => { for (const f of [...listeners]) f(e); } };
globalThis.window = globalThis; globalThis.chrome = { webview: wv };
globalThis.location = { pathname: '/dune/minimap' };
globalThis.document = { readyState: 'loading', getElementById: () => null };
globalThis.__harvestingDuneEmit = (s) => emitted.push(JSON.parse(s));
const rows = [{ type: 'BP_SpiceField_Small_C', map_marker_id: 'spicefieldsmall', x: 1, y: 1, z: 0, metadata: { spawned: false, fillRate: 0 } },
              { type: 'BP_SpiceField_Small_C', map_marker_id: 'spicefieldsmall', x: 2, y: 2, z: 0, metadata: { spawned: true, fillRate: 1 } },
              { type: 'ResourceNode', map_marker_id: 'magnetiteore', x: 3, y: 3, z: 0 }];
globalThis.fetch = async () => new Response(JSON.stringify(rows));
eval(SCRIPT);
__dd.primary = true;
const seen = [];
wv.addEventListener('message', (e) => { if (e.data.topic === 'dune.map.actors') seen.push(e.data.payload.actors.map((a) => a.id)); });
wv.dispatchEvent({ data: { type: 'delta', topic: 'dune.map.actors', payload: { actors: [
  { id: 'empty', markerType: 'SpiceFieldSmall', spawned: false, fillRate: 0 },
  { id: 'real', markerType: 'SpiceFieldSmall', spawned: true, fillRate: 1 },
  { id: 'ore', markerType: null, spriteKey: 'magnetiteore' }] } } });
(async () => {
  const kept = await (await fetch('https://dune-api-v2.gaming.tools/actors?world=w&seed=1')).json();
  const fwd = emitted.filter((m) => m.topic === 'dune.map.actors').map((m) => m.payload.actors.map((a) => a.id));
  console.log(JSON.stringify({ seen, fwd, kept: kept.map((r) => r.x) })); process.exit(0);
})();
""".replace("SCRIPT", json.dumps(BRIDGE_SCRIPT))
    out = subprocess.run(["node", "-"], input=code, capture_output=True, text=True, timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout.splitlines()[0])
    assert r["seen"] == [["real", "ore"]]  # the map never draws the empty site
    assert r["fwd"] == [["empty", "real", "ore"]]  # the detector still gets it (blow detection)
    assert r["kept"] == [2, 3]  # scanned empty sites are dropped too


def test_size_modes_direct_to_confirmed_active_fields_of_that_size_only():
    det = Detector(None, min_rescore_s=0)
    session(det, "DD")
    pose(det, 10, 0, t=1)
    actors(det,
           spice("big-near-empty", 5 * CELL_CM, 0, "Large", fill=0, spawned=False),  # an empty site
           spice("med", 8 * CELL_CM, 0, "Medium", fill=0.9),
           spice("big", 30 * CELL_CM, 0, "Large", fill=0.8), t=2)
    det.load_scan([{"type": "BP_SpiceField_Large_C", "map_marker_id": "spicefieldlarge", "x": 3 * CELL_CM, "y": 0, "z": 0}], 200, 2)
    det.ui("resource", "spice_large")
    det.compute(3)
    best = det.top(1)[0]
    assert round(best.x) == round(30 * CELL_CM)  # the live large field, not the closer unverified scan row
    assert "Large" in det.page_state()["dest"]["title"]
    det.ui("resource", "spice_medium")
    det.compute(4)
    assert round(det.top(1)[0].x) == round(8 * CELL_CM)


def test_size_mode_says_when_no_active_field_of_that_size_is_known():
    det = Detector(None, min_rescore_s=0)
    session(det, "DD")
    pose(det, 10, 0, t=1)
    actors(det, spice("s", 5 * CELL_CM, 0, "Small", fill=1.0), t=2)
    det.ui("resource", "spice_large")
    det.compute(3)
    assert det.page_state()["dest"] is None
    assert det.status_text() == "no active field of this size seen yet"



def test_a_site_reported_again_under_a_new_id_replaces_what_we_remembered():
    det = Detector(None, min_rescore_s=0)
    session(det, "DD")
    pose(det, 10, 0, t=1)
    actors(det, spice("old", 30 * CELL_CM, 0, "Large", fill=1.0), t=2)
    actors(det, t=3)  # unloaded: remembered as an active field
    det.ui("resource", "spice_large")
    det.compute(4)
    assert det.page_state()["dest"] is not None
    actors(det, spice("new", 30 * CELL_CM, 0, "Large", fill=0, spawned=False), t=5)  # same site, new id, empty
    det.compute(6)
    assert det.page_state()["dest"] is None and "old" not in det.store.items


def test_a_dormant_site_back_active_under_a_new_id_is_a_spice_blow():
    det = Detector(None, min_rescore_s=0)
    session(det, "DD")
    pose(det, 10, 0)
    actors(det, spice("old", 3000 * 100, 0, "Medium", fill=0, spawned=False), t=1)
    actors(det, t=2)
    actors(det, spice("new", 3000 * 100, 0, "Medium", fill=0.05, spawned=True), t=3)
    assert det.recent_event(3) == "Spice blow! medium field 3.0 km away"


def test_a_spawn_point_out_of_range_is_never_a_candidate():
    det = Detector(None, min_rescore_s=0)
    session(det, "DD")
    pose(det, 10, 0, t=1)
    actors(det, spice("empty", 5 * CELL_CM, 0, "Large", fill=0, spawned=False), t=2)
    actors(det, t=3)  # now out of load range
    assert det.nodes({"spicefieldlarge"}) == []


def test_flying_scout_is_directed_to_far_large_fields_the_harvester_can_work():
    from harvesting_dune.areas import Node, recommend
    from harvesting_dune.catalog import BY_KEY
    from harvesting_dune.model import Pose
    from harvesting_dune.terrain import VEHICLES, TerrainGrid

    far = [Node("spicefieldlarge", 22_000 * 100, 0, 0, "live", 0.9)]
    ground = recommend(BY_KEY["spice_large"], far, Pose(0, 0, 0), TerrainGrid(), VEHICLES["sandcrawler"], top=0)
    assert not ground[0].feasible and "planning range" in ground[0].reasons[0]
    flying = recommend(BY_KEY["spice_large"], far, Pose(0, 0, 0), TerrainGrid(), VEHICLES["sandcrawler"], top=0, travel=VEHICLES["ornithopter"])
    assert flying[0].feasible and flying[0].trip_s < 22_000 * 100 * 2 / VEHICLES["ornithopter"].loaded_speed_cms + 1000


def test_minimap_filter_hide_list_and_persistence(tmp_path):
    det = Detector(None, data_dir=tmp_path)
    det.ui("lines")  # lines off: the minimap filter is a free multi-select
    det.minimap_hidden = set()
    det.ui("minimap_toggle", "primrose")
    det.ui("minimap_toggle", "brittlebush")
    hidden, managed = det.minimap_filter()
    assert hidden == ["BrittleBush", "BuriedTreasure", "PrimroseField"] and "MagnetiteOre" in managed  # extras hidden by default
    again = Detector(None, data_dir=tmp_path)
    assert again.minimap_hidden == {"primrose", "brittlebush"} and not again.lines_enabled
    det.ui("lines")  # lines back on: only the searched resource is shown, and it follows the search
    det.ui("resource", "spice_large")
    hidden, _ = det.minimap_filter()
    assert "SpiceFieldLarge" not in hidden and {"MagnetiteOre", "PrimroseField"} <= set(hidden)
    det.ui("resource", "spice_medium")
    rev = det.minimap_rev
    det.ui("next_resource")  # to Iron: the minimap switches with the search
    assert det.minimap_rev > rev and det.visible_resources() == ["iron"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_bridge_minimap_filter_keeps_players_own_choices():
    code = r"""
const store = { 'dune.companion.minimap.deselectedMarkerTypes': JSON.stringify(['Cave', 'LootContainerRare', 'BrittleBush']) };
globalThis.localStorage = { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = v; } };
let reloads = 0;
globalThis.window = globalThis;
globalThis.chrome = { webview: { postMessage() {}, addEventListener() {}, removeEventListener() {}, dispatchEvent() {} } };
globalThis.location = { pathname: '/dune/minimap', reload: () => reloads++ };
globalThis.document = { readyState: 'complete', getElementById: () => null };
globalThis.__harvestingDuneEmit = () => {}; globalThis.fetch = async () => new Response('[]');
eval(SCRIPT);
const managed = ['BrittleBush', 'PrimroseField', 'MagnetiteOre'];
const r1 = __dd.setMinimapHidden(['PrimroseField'], managed);
const r2 = __dd.setMinimapHidden(['PrimroseField'], managed);
setTimeout(() => { console.log(JSON.stringify({ r1, r2, reloads, list: JSON.parse(store['dune.companion.minimap.deselectedMarkerTypes']) })); process.exit(0); }, 100);
""".replace("SCRIPT", json.dumps(BRIDGE_SCRIPT))
    out = subprocess.run(["node", "-"], input=code, capture_output=True, text=True, timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout.splitlines()[0])
    assert r["r1"] == "reloading" and r["r2"] == "unchanged" and r["reloads"] == 1
    assert r["list"] == ["Cave", "LootContainerRare", "PrimroseField"]  # own choices kept, BrittleBush shown again


def test_targeting_mode_keeps_far_fields_for_ground_vehicles():
    from harvesting_dune.areas import Node, recommend
    from harvesting_dune.catalog import BY_KEY
    from harvesting_dune.model import Pose
    from harvesting_dune.terrain import VEHICLES, TerrainGrid

    far = [Node("spicefieldlarge", 12_800 * 100, 0, 0, "scan", None)]
    res = recommend(BY_KEY["spice_large"], far, Pose(0, 0, 0), TerrainGrid(), VEHICLES["sandcrawler"], top=0, allow_far=True)
    assert res[0].feasible and res[0].reasons[0] == "far: straight-line estimate"


def _large_scan_row(x, y):
    return {"type": "BP_SpiceField_Large_C", "map_marker_id": "spicefieldlarge", "x": x, "y": y, "z": 0}


def test_scan_row_on_a_dormant_site_is_never_a_target_even_without_a_learned_range():
    """Replays the live failure: a large-field scan row sat exactly on a site reporting spawned:false,
    and only one large field had ever been seen, so no large-field range was learned."""
    det = Detector(None, min_rescore_s=0)
    session(det, "DD")
    pose(det, 10, 0, t=1)
    det.load_scan([_large_scan_row(914400, -304800)], 200, 1)
    det.ui("resource", "spice_large")
    det.compute(1)
    assert det.page_state()["dest"] is not None  # before the site is loaded, the row is the only lead
    actors(det, spice("site", 914400, -304800, "Large", fill=0, spawned=False), t=2)
    det.compute(3)
    assert det.page_state()["dest"] is None  # the game says the site is empty: never lead you there
    for t in (4, 7, 11):
        det._observed_at = -1e9
        actors(det, spice("site", 914400, -304800, "Large", fill=0, spawned=False), t=t)
    assert any(k.startswith("BP_SpiceField_Large_C:914400") for k in det.scan.stale)  # removed from the map too


def test_rare_spice_size_borrows_the_family_range_then_saved_ranges(tmp_path):
    det = Detector(None, data_dir=tmp_path)
    session(det, "DD")
    pose(det, 10, 0, t=1)
    actors(det, t=1)  # load-in
    for i in range(6):
        actors(det, *[spice(f"s{j}", 2500 * 100 + j, 0, "Small", fill=0, spawned=False) for j in range(i + 1)], t=2 + i)
    assert det.store.absence_radius_cm("spicefieldsmall") is not None
    assert det.store.absence_radius_cm("spicefieldlarge") == det.store.absence_radius_cm("spicefieldsmall")
    det.save_ranges()
    fresh = Detector(None, data_dir=tmp_path)
    session(fresh, "DD")
    assert fresh.store.absence_radius_cm("spicefieldsmall") is not None  # works from the first minute


def test_spice_alternatives_are_confirmed_active_fields_bigger_first():
    det = Detector(None, min_rescore_s=0)
    session(det, "DD")
    pose(det, 10, 0, t=1)
    actors(det,
           spice("L1", 30 * CELL_CM, 0, "Large", fill=0.9),
           spice("M1", 10 * CELL_CM, 0, "Medium", fill=0.8),
           spice("M2", -12 * CELL_CM, 0, "Medium", fill=0.8),
           spice("S1", 3 * CELL_CM, 0, "Small", fill=1.0),
           spice("L2", -45 * CELL_CM, 0, "Large", fill=0.7),
           spice("E1", 5 * CELL_CM, 0, "Large", fill=0, spawned=False), t=2)  # empty site: never a line
    det.load_scan([{"type": "BP_SpiceField_Large_C", "map_marker_id": "spicefieldlarge", "x": -40 * CELL_CM, "y": 0, "z": 0}], 200, 2)
    det.ui("resource", "spice_large")
    det.compute(3)
    st = det.page_state()
    assert round(st["dest"]["x"]) == round(30 * CELL_CM)
    # large mode lines only large fields: no medium or small, no scan row, no empty site
    assert [a["size"] for a in st["alternatives"]] == ["large"]
    det.ui("resource", "spice")
    det.compute(4)
    # the all-sizes mode still lists medium and large, bigger first, never small
    assert [a["size"] for a in det.page_state()["alternatives"]] == ["large", "large", "medium"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_bridge_draws_alternative_lines_in_a_calibrated_gtmap_layer():
    code = r"""
const listeners = [];
const wv = { postMessage() {}, addEventListener: (_, f) => listeners.push(f), removeEventListener() {}, dispatchEvent: (e) => { for (const f of [...listeners]) f(e); } };
globalThis.window = globalThis; globalThis.chrome = { webview: wv };
globalThis.location = { pathname: '/dune/minimap' };
globalThis.document = { readyState: 'loading', getElementById: () => null };
globalThis.__harvestingDuneEmit = () => {}; globalThis.fetch = async () => new Response('[]');
eval(SCRIPT);   // document start: the capture hook is installed before the map exists
// --- a tiny gtmap look-alike -------------------------------------------------------------
class StaticLayer { constructor() { this.vectors = new Map(); this.n = 0; }
  addPolyline(points, style) { this.vectors.set(++this.n, { type: 'polyline', points, style }); }
  clearVectors() { this.vectors.clear(); } }
class GtMap { constructor() { this._createdLayers = []; const self = this;
    this.view = { translate: (x, y, t) => t === 'flipVertical' ? { x: x / 100, y: -y / 100 } : { x: x / 100, y: y / 100 } };
    this.layers = { createStaticLayer() { const l = new StaticLayer(); self._createdLayers.push(l); return l; }, addLayer() {} }; }
  requestRender() {} }
const map = new GtMap();
// a marker layer as the page fills it: map point + world data (calibration reference)
const markers = { _items: new Map([['a-1', { id: 'a-1', point: { x: 1234, y: -567 }, data: { x: 123400, y: 56700 } }]]) };
map._createdLayers.push(markers);
wv.dispatchEvent({ data: { type: 'delta', topic: 'dune.player', payload: { x: 0, y: 0, z: 0 } } });
__dd.set({ route: { mapId: 'DD', stops: [{ x: 500000, y: 200000, z: 0 }] },
           alternatives: [{ x: 100000, y: 100000, color: 'rgba(255,176,59,0.60)', weight: 2 }, { x: -300000, y: 0, color: 'rgba(255,255,255,0.35)', weight: 1.5 }] });
setTimeout(() => {
  const alt = __dd.altLayer;
  const lines = alt ? [...alt.vectors.values()].map((v) => ({ end: v.points[1], style: v.style })) : [];
  console.log(JSON.stringify({ captured: __dd.maps.length, transform: __dd.altTransform, lines, own: Object.prototype.hasOwnProperty.call(map, '_createdLayers') }));
  process.exit(0);
}, 80);
""".replace("SCRIPT", json.dumps(BRIDGE_SCRIPT))
    out = subprocess.run(["node", "-"], input=code, capture_output=True, text=True, timeout=30, check=False)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout.splitlines()[0])
    assert r["captured"] == 1 and r["own"] is True
    assert r["transform"] == "flipVertical"  # matched against the page's own route line
    assert r["lines"] == [
        {"end": {"x": 1000, "y": -1000}, "style": {"color": "rgba(255,176,59,0.60)", "weight": 2}},
        {"end": {"x": -3000, "y": 0}, "style": {"color": "rgba(255,255,255,0.35)", "weight": 1.5}},
    ]


def test_size_mode_draws_no_line_to_fields_of_another_size():
    det = Detector(None, min_rescore_s=0)
    session(det, "DD")
    pose(det, 10, 0, t=1)
    actors(det, spice("M1", 10 * CELL_CM, 0, "Medium", fill=0.8), t=2)
    det.ui("resource", "spice_large")
    det.compute(3)
    st = det.page_state()
    assert st["dest"] is None and st["alternatives"] == []
    det.ui("resource", "spice_medium")
    det.compute(4)
    assert det.page_state()["alternatives"] == []  # the medium field is the target itself


def test_blow_is_caught_through_the_companions_list_flicker(tmp_path):
    """Live data: sites drop out of the actor list and come back; a site seen empty, then missing
    for a while, then active is a blow."""
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    session(det, "DD")
    pose(det, 10, 0, t=1)
    actors(det, spice("s", 1400 * 100, 0, "Medium", fill=0, spawned=False), t=2)
    actors(det, t=3)  # flickers out of the list
    actors(det, spice("s", 1400 * 100, 0, "Medium", fill=1.0, spawned=True), t=4)
    assert det.recent_event(4) == "Spice blow! medium field 1.4 km away"
    assert det.fresh_blow("s") is not None
    actors(det, t=5)
    actors(det, spice("s", 1400 * 100, 0, "Medium", fill=1.0, spawned=True), t=6)
    assert len(det.events) == 1  # an active field flickering back is not a second blow
    again = Detector(None, data_dir=tmp_path)
    session(again, "DD")
    assert "s" in again.blows  # remembered across restarts for this Coriolis cycle


def test_fresh_blow_and_harvested_fields_get_distinct_lines():
    det = Detector(None, min_rescore_s=0)
    session(det, "DD")
    pose(det, 10, 0, t=1)
    actors(det, spice("fresh", 20 * CELL_CM, 0, "Medium", fill=0, spawned=False), spice("h", -10 * CELL_CM, 0, "Medium", fill=0.9), t=2)
    actors(det, spice("fresh", 20 * CELL_CM, 0, "Medium", fill=1, spawned=True), spice("h", -10 * CELL_CM, 0, "Medium", fill=0.5),
           spice("L", 0, 4 * CELL_CM, "Large", fill=1), t=62)  # the main target
    det.ui("resource", "spice")
    det.compute(63)
    kinds = {a["kind"]: a for a in det.page_state()["alternatives"]}
    assert kinds["fresh"]["color"].startswith("rgba(255,70,200") and kinds["harvested"]["color"].endswith(",0.25)")
    assert det.page_state()["alternatives"][0]["kind"] == "fresh"  # fresh blows first


def test_with_lines_on_exactly_one_resource_is_shown_and_searched():
    det = Detector(None, min_rescore_s=0)
    assert det.visible_resources() == ["spice"] and det.lines_active  # fresh install: lines work at once
    det.ui("minimap_toggle", "titanium")  # lines on: ticking one selects it alone
    assert det.visible_resources() == ["titanium"] and det.resource_key == "titanium" and det.lines_active
    det.ui("minimap_toggle", "stravidium")
    assert det.visible_resources() == ["stravidium"] and det.resource_key == "stravidium"
    det.ui("resource", "spice_large")  # choosing what to search moves the minimap with it
    assert det.visible_resources() == ["spice"] and det.resource_key == "spice_large"
    det.ui("lines")
    det.ui("minimap_toggle", "iron")  # lines off: free multi-select
    assert det.visible_resources() == ["spice", "iron"] and not det.lines_active
    assert "lines" not in det.badge(now=0)
    det.ui("lines")  # back on: narrowed to the searched resource again
    assert det.visible_resources() == ["spice"] and det.lines_active


def test_resources_added_later_start_hidden_for_existing_settings(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"minimapHidden": ["iron"], "minimapFocus": False, "lines": True}))
    det = Detector(None, data_dir=tmp_path)
    assert "cages" in det.minimap_hidden and "iron" in det.minimap_hidden and "spice" not in det.minimap_hidden


def _cage(id_, x, y):
    return {"id": id_, "actorType": "BP_TemporaryLootContainer_UltraRareContainer_C", "markerType": "LootContainer",
            "spriteKey": "lootcontainer", "x": x, "y": y, "z": 0}


def test_an_ore_node_the_game_loads_again_under_a_new_id_is_one_node(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    session(det, "DD")
    pose(det, 0, 0, t=1)
    actors(det, ore("19636D1D030:195DFD17120:10", 400 * M, 0), t=2)
    actors(det, t=3)  # unloaded
    actors(det, ore("19636D1D030:195E69B31A0:10", 400 * M, 0), t=4)  # loaded again: only the middle of the id changed
    assert [(n.x, n.source) for n in det.nodes({"magnetiteore"})] == [(400 * M, "live")]


def test_an_empty_actor_list_after_a_full_one_is_no_proof_that_anything_left(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.store.saved_ranges = {"DD": {"magnetiteore": 200 * M}}
    session(det, "DD", t=0)
    pose(det, 0, 0)
    nodes = [ore(f"o{i}", (50 + i) * M, 0) for i in range(6)]
    actors(det, *nodes, t=1)
    actors(det, t=2)  # the list drops out while a menu is open
    assert {k: t.status for k, t in det.store.items.items()} == {f"o{i}": Status.LIVE for i in range(6)}


def test_a_container_next_to_you_is_not_emptied_when_the_whole_list_drops_out(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    session(det, "DD")
    pose(det, 0, 0, t=1)
    actors(det, _cage("c1", 10 * M, 0), ore("o1", 300 * M, 0), ore("o2", 310 * M, 0), t=2)
    actors(det, t=3)
    assert det.looted == {}


def test_a_container_next_to_you_is_not_emptied_when_the_list_unloads_an_area(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    session(det, "DD")
    pose(det, 0, 0, t=1)
    far = [ore(f"o{i}", (300 + i) * M, 0) for i in range(6)]
    actors(det, _cage("c1", 10 * M, 0), *far, t=2)
    actors(det, far[0], t=3)  # the cage and five more left at once
    assert det.looted == {}


def test_a_container_the_game_shows_emptied_keeps_that_fact(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    session(det, "DD")
    pose(det, 0, 0, t=1)
    actors(det, {**_cage("c1", 10 * M, 0), "hasLoot": True}, t=2)
    actors(det, {**_cage("c1", 10 * M, 0), "hasLoot": False}, t=3)
    assert det.store.items["c1"].actor.has_loot is False
