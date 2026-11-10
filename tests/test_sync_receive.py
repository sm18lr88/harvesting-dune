"""What the guild shared: drawn as aged rings in our partition only, listed on the settings page,
in the ranking only for spice (the newest report of a site wins), and a guild "gone" report never
outlives the node's respawn time."""

import logging

from helpers import CELL_CM, actors, ore, pose, session, spice

from harvesting_dune import logsetup
from harvesting_dune.app import Detector
from harvesting_dune.feed import BusMessage

KEY = "gs1_alpha001_Qm9vdHN0cmFwVGVzdEtleUFscGhhMDAx"
OTHER_KEY = "gs1_bravo002_VGVzdE9ubHlCcmF2b0tleVNlY3JldDAy"
DD, LAYOUT = "deepdesert_1", "deepdesert_1_07"
GROUP = "prd-srv-test-alpha-erg"
WHO = {"characterName": "Paul Test", "characterId": 1001, "battlegroupId": GROUP}


def detector(world=DD, layout=LAYOUT):
    det = Detector(None, min_rescore_s=0)
    det.set_sync_key(KEY)
    session(det, world)
    if layout:
        det.map_files[det.store.world] = layout
    det.handle(BusMessage("delta", "dune.trackedSession", WHO), 0)
    pose(det, 0, 0, t=1)
    return det


def sighting(cls, x, y, age=60.0, marker="spicefieldlarge", world=DD, layout=LAYOUT, observers=("beta0002",), **extra):
    return {"cls": cls, "type": marker, "x": x, "y": y, "z": 0.0, "fill": 0.8, "size": "large", "rowKey": None,
            "distCm": None, "ageS": age, "observerName": "Leto Test", "observerKey": observers[0],
            "observers": list(observers), "rev": 1, "worldId": world, "layout": layout, **extra}


def member(cid, name, x=1000.0, y=0.0, age=5.0, group=GROUP, world=DD, layout=LAYOUT):
    return {"characterName": name, "characterId": cid, "battlegroupId": group, "worldId": world, "layout": layout,
            "x": x, "y": y if x is not None else None, "z": 0.0 if x is not None else None, "ageS": age, "keyId": "beta0002"}


def response(sightings=(), presence=(), next_rev=5):
    return {"now": 1794160000.0, "next": next_rev, "keyState": "valid", "presence": list(presence),
            "sightings": list(sightings)}


def stale_row(x, y, observers=("alpha001", "beta0002"), dist=1500.0, **kw):
    return sighting("stale", x, y, marker="magnetiteore", observers=observers, rowKey=f"BP_Magnetite:{int(x)}:{int(y)}",
                    distCm=dist, **kw)


def test_another_battlegroup_or_layout_never_draws():
    det = detector()
    det.ingest_peer(response(
        sightings=[sighting("spice", 1000, 0, layout="deepdesert_1_06"),
                   sighting("ship", 2000, 0, battlegroupId="prd-srv-other-erg"),
                   stale_row(3000, 0, layout="deepdesert_1_06")],
        presence=[member(2002, "Feyd Other", group="prd-srv-other-erg")]), 100.0)
    assert det.peer_marks() == [] and det.peer_stale_keys() == set()
    snap = det.peers_snapshot()
    assert snap["sightings"] == [] and [m["state"] for m in snap["members"]] == ["you"]  # only ourselves
    assert det.peers.since == 5 and det.peers.key_state == "valid" and det.peers.last_sync == 100.0


def test_server_ages_become_feed_clock_ages_and_rings_fade_out():
    det = detector()
    det.clock = 1000.0
    det.ingest_peer(response(sightings=[sighting("spice", 1000, 0, age=120.0)]), 5.0)
    [ring] = det.page_state()["alternatives"]
    assert ring["kind"] == "peer" and ring["ring"] == 4000
    assert ring["color"] == f"rgba(120,255,140,{round(0.75 - 0.5 * (120 / 2700), 2)})"
    det.clock = 2700 + 1000 - 120
    assert len(det.page_state()["alternatives"]) == 1
    det.clock += 1
    assert det.page_state()["alternatives"] == [] and det.peers.sightings == {}


def test_shared_sightings_never_change_the_ranking():
    det = detector(world="DD", layout=None)
    actors(det, ore(1, 20 * CELL_CM, 0), ore(2, 20.2 * CELL_CM, 0.1 * CELL_CM), ore(3, -30 * CELL_CM, 0),
           ore(4, 0, 40 * CELL_CM))
    det.ui("resource", "iron")
    det.compute(1)
    areas, before = [(a.x, a.y, a.feasible) for a in det.areas], det.page_state()
    shared = [sighting("spice" if i % 2 else "ship", i * 3000.0, 5000.0, world="DD", layout="DD") for i in range(30)]
    det.ingest_peer(response(sightings=shared), 10.0)
    det.compute(2)
    after = det.page_state()
    assert [(a.x, a.y, a.feasible) for a in det.areas] == areas
    assert after["dest"] == before["dest"] and after["route"] == before["route"]
    grown = after["alternatives"][len(before["alternatives"]):]
    assert after["alternatives"][:len(before["alternatives"])] == before["alternatives"]
    assert len(grown) == 30 and {r["kind"] for r in grown} == {"peer"} and {r["ring"] for r in grown} == {4000, 6000}


def test_shared_data_never_touches_our_own_lifecycle_or_data_files(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.set_sync_key(KEY)
    session(det, DD)
    det.map_files[det.store.world] = LAYOUT
    det.handle(BusMessage("delta", "dune.trackedSession", WHO), 0)
    pose(det, 0, 0, t=1)
    actors(det, ore("own", 5000, 0), world=DD, t=2)
    det.compute(3)

    def state():
        files = {p.relative_to(tmp_path).as_posix(): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
        store = {k: (t.status, t.actor.x, t.actor.y) for k, t in det.store.items.items()}
        return files, store, dict(det.mined_nodes.items), dict(det.scan.stale), list(det.scan.rows)

    before = state()
    det.ingest_peer(response(
        sightings=[sighting("spice", 1000, 0), sighting("ship", 2000, 0), sighting("blow", 3000, 0),
                   sighting("mined", 4000, 0, marker="magnetiteore"), sighting("ore", 6000, 0, marker="azuriteore"),
                   stale_row(10_000, 0), stale_row(20_000, 0)],
        presence=[member(2002, "Leto Test")]), 10.0)
    det.compute(4)
    assert det.peer_marks() and det.peer_stale_keys()  # the guild's data did arrive
    assert state() == before  # but only in the peers layer: no store track, mined node, stale row or file


def test_a_scan_row_is_hidden_only_on_two_observers_with_a_distance_and_nothing_live_there():
    det = detector()
    det.ingest_peer(response(sightings=[stale_row(10_000, 0, observers=("alpha001",)),
                                        stale_row(20_000, 0, dist=None),
                                        stale_row(30_000, 0),
                                        stale_row(40_000, 0)]), 10.0)
    actors(det, ore("live", 40_500, 0), world=DD, t=2)  # we see that node ourselves
    assert det.page_state()["stale"] == ["BP_Magnetite:30000:0"]
    assert det.peer_marks() == []  # stale rows are listed, never ringed


def test_guildmates_are_drawn_unless_hidden_and_never_ourselves():
    det = detector()
    det.ingest_peer(response(presence=[member(1001, "Paul Test"), member(1002, "Chani Test", x=5000.0),
                                       member(1003, "Stilgar Test", x=None),
                                       member(1004, "Gurney Test", world="hagga")]), 10.0)
    assert [(r["kind"], r["x"]) for r in det.peer_marks()] == [("member", 5000.0)]
    snap = {m["name"]: m for m in det.peers_snapshot()["members"]}
    assert snap["Paul Test"]["self"] is True and snap["Chani Test"]["self"] is False
    assert snap["Chani Test"]["distanceM"] == 50 and snap["Chani Test"]["sector"] is not None
    assert snap["Stilgar Test"]["sector"] is None and snap["Gurney Test"]["distanceM"] is None
    det.ui("sync_member", "1002")
    assert det.peer_marks() == [] and snap["Chani Test"]["shown"] is True
    assert {m["name"]: m["shown"] for m in det.peers_snapshot()["members"]}["Chani Test"] is False


def test_a_malformed_row_is_skipped_without_a_problem_report():
    det = detector()
    root = logging.getLogger("harvesting_dune")
    root.addHandler(logsetup.PROBLEMS)
    before = logsetup.PROBLEMS.count
    try:
        bad = sighting("spice", 1000, 0)
        del bad["x"]
        det.ingest_peer(response(sightings=[bad, "junk", sighting("blow", 3000, 0)],
                                 presence=[{"characterId": "x"}]), 10.0)
        det.ingest_peer(["not", "a", "response"], 11.0)
    finally:
        root.removeHandler(logsetup.PROBLEMS)
    assert logsetup.PROBLEMS.count == before
    assert [r["ring"] for r in det.peer_marks()] == [5000]


def test_a_shared_ore_node_is_a_violet_ring_that_fades_over_its_respawn_time():
    det = detector()
    det.clock = 1000.0
    det.ingest_peer(response(sightings=[sighting("ore", 3000, 0, age=600.0, marker="titaniumore", fill=None, size=None)]), 5.0)
    [ring] = det.peer_marks()
    assert (ring["kind"], ring["ring"]) == ("peer", 3000)
    assert ring["color"] == f"rgba(180,150,255,{round(0.75 - 0.5 * (600 / 2700), 2)})"  # Titanium: 45 min
    [row] = det.peers_snapshot()["sightings"]
    assert (row["cls"], row["name"], row["ageS"], row["observer"], row["lifetimeS"]) == ("ore", "Titanium Ore", 600, "Leto Test", 2700)
    det.clock = 1000.0 - 600 + 2700 + 1
    assert det.page_state()["alternatives"] == [] and det.peers_snapshot()["sightings"] == []


def test_our_own_finds_coming_back_from_the_server_draw_no_ring():
    det = detector()  # the server echoes our rows; our other characters share our key id
    det.ingest_peer(response(sightings=[sighting("ore", 3000, 0, marker="titaniumore", observers=("alpha001",)),
                                        sighting("spice", 9000, 0, observers=("alpha001",)),
                                        sighting("ore", 6000, 0, marker="titaniumore", observers=("alpha001", "beta0002"))]), 5.0)
    assert [(r["kind"], r["x"]) for r in det.peer_marks()] == [("peer", 6000)]


def test_a_guild_ore_ring_gives_way_to_what_our_own_companion_shows():
    det = detector()
    actors(det, ore("live", 3000, 0), world=DD, t=2)  # the companion marks this node itself
    det.mined_nodes.record("mined", "magnetiteore", 6000, 0, 0, 2)  # we mined this one: nothing is there
    det.ingest_peer(response(sightings=[sighting("ore", x, 0, marker="magnetiteore") for x in (3000, 6000, 9000)]), 5.0)
    assert [r["x"] for r in det.peer_marks()] == [9000]  # only the node we cannot see


def test_a_gone_report_hides_the_scan_row_only_until_the_node_can_have_respawned():
    det = detector()
    det.clock = 1000.0
    det.ingest_peer(response(sightings=[stale_row(30_000, 0, age=60.0)]), 10.0)  # Iron: 30 min respawn
    assert det.peer_stale_keys() == {"BP_Magnetite:30000:0"}
    assert det.page_state()["stale"] == ["BP_Magnetite:30000:0"]
    det.clock = 1000.0 - 60 + 1800 - 1
    assert det.peer_stale_keys() == {"BP_Magnetite:30000:0"}
    det.clock += 1
    assert det.peer_stale_keys() == set()  # it has had time to grow back: the scan row shows again
    assert det.page_state()["stale"] == []


def test_a_gone_report_of_a_marker_that_never_respawns_hides_until_the_report_expires():
    det = detector()
    det.clock = 1000.0
    det.ingest_peer(response(sightings=[sighting("stale", 30_000, 0, marker="salvagesteel", observers=("alpha001", "beta0002"),
                                                 rowKey="BP_Ship:30000:0", distCm=1500.0)]), 10.0)
    det.clock += 3 * 3600  # far past any node respawn time, inside the ship report's 24 h
    assert det.peer_stale_keys() == {"BP_Ship:30000:0"}


def test_our_own_eyes_win_over_a_fresh_gone_report():
    det = detector()
    det.ingest_peer(response(sightings=[stale_row(30_000, 0, age=0.0)]), 10.0)
    assert det.peer_stale_keys() == {"BP_Magnetite:30000:0"}
    actors(det, ore("live", 30_200, 0), world=DD, t=2)
    assert det.peer_stale_keys() == set() and det.page_state()["stale"] == []


FAR = 300_000.0  # 3 km: beyond what our own companion loads


def target_x(det, resource="spice_large"):
    det.ui("resource", resource)
    det.compute(det.clock)
    dest = det.page_state()["dest"]
    return None if dest is None else round(dest["x"])


def test_a_guildmates_active_large_field_is_the_target_when_nobody_saw_it_later():
    det = detector()
    det.ingest_peer(response(sightings=[sighting("spice", FAR, 0, age=60.0)]), 10.0)
    assert target_x(det) == FAR
    assert "a guildmate saw it active" in det.page_state()["dest"]["detail"]


def test_a_crowd_site_a_guildmate_saw_empty_is_no_target_until_someone_sees_it_active():
    det = detector()
    det.load_scan([{"type": "BP_SpiceField_Large_C", "map_marker_id": "spicefieldlarge", "x": FAR, "y": 0, "z": 0,
                    "metadata": {"spawned": True, "fillRate": 1}}], 200, 1)
    assert target_x(det) == FAR  # only the crowd database knows it: the fallback target
    [row] = det.scan.nodes()
    det.ingest_peer(response(sightings=[sighting("stale", FAR, 0, age=600.0, rowKey=row.row_key, distCm=1500.0)]), 10.0)
    assert target_x(det) is None  # the crowd row has no time: any report beats it
    det.ingest_peer(response(sightings=[sighting("spice", FAR, 0, age=1200.0)]), 11.0)
    assert target_x(det) is None  # seen active before it was seen empty
    det.ingest_peer(response(sightings=[sighting("spice", FAR, 0, age=5.0)]), 12.0)
    assert target_x(det) == FAR  # seen active again since


def test_our_own_later_look_beats_a_guildmates_report_and_loses_to_a_newer_one():
    det = detector()
    actors(det, spice("site", FAR, 0, "Large", fill=0, spawned=False), world=DD, t=500)  # we see it empty
    det.ingest_peer(response(sightings=[sighting("spice", FAR, 0, age=100.0)]), 10.0)  # active at t=400
    assert target_x(det) is None
    actors(det, world=DD, t=520)  # out of our load range now: our last look was at t=500
    assert target_x(det) is None
    det.ingest_peer(response(sightings=[sighting("spice", FAR, 0, age=5.0)]), 11.0)  # active at t=515
    assert target_x(det) == FAR


def test_a_guildmates_empty_report_ends_our_older_memory_of_an_active_field():
    det = detector()
    actors(det, spice("site", FAR, 0, "Large", fill=0.9), world=DD, t=100)
    actors(det, world=DD, t=110)  # remembered: active at t=100, now out of range
    assert target_x(det) == FAR
    det.load_scan([{"type": "BP_SpiceField_Large_C", "map_marker_id": "spicefieldlarge", "x": FAR, "y": 0, "z": 0}], 200, 110)
    [row] = det.scan.nodes()
    det.ingest_peer(response(sightings=[sighting("stale", FAR, 0, age=5.0, rowKey=row.row_key, distCm=1500.0)]), 12.0)
    assert target_x(det) is None


def test_a_large_search_never_leads_to_a_guildmates_medium_field():
    det = detector()
    det.ingest_peer(response(sightings=[sighting("spice", FAR, 0, marker="spicefieldmedium", size="medium")]), 10.0)
    assert target_x(det) is None and det.nodes({"spicefieldlarge"}) == []
    assert target_x(det, "spice_medium") == FAR


def test_a_new_character_on_another_battlegroup_draws_none_of_the_old_ones_finds():
    det = detector()
    det.ingest_peer(response(sightings=[sighting("ship", 1000, 0)], presence=[member(1002, "Chani Test")]), 10.0)
    assert det.peer_marks()
    det.handle(BusMessage("delta", "dune.trackedSession",
                          {"characterName": "Feyd Other", "characterId": 2002, "battlegroupId": "prd-srv-test-other-erg"}), 20)
    assert det.peer_marks() == [] and det.peers_snapshot()["sightings"] == []


def test_a_new_key_draws_and_ranks_nothing_the_old_key_received():
    det = detector()
    det.ingest_peer(response(sightings=[sighting("spice", FAR, 0, observers=("alpha001",))]), 10.0)  # our own find, echoed
    assert det.peer_marks() == []
    det.set_sync_key(OTHER_KEY)
    assert det.peer_marks() == [] and det.peer_spice_reports() == []


def pin(oid, cid, name, rev, tag="tagalpha"):
    return {"id": oid, "kind": "pin", "scope": {"worldId": DD, "layout": LAYOUT},
            "geometry": {"type": "point", "x": 1000.0, "y": 0.0, "z": 0.0}, "body": {"label": oid.upper()},
            "ttlS": 3600, "ageS": 1, "characterId": cid, "characterName": name, "playerTag": tag, "rev": rev}


def test_a_same_key_alts_pin_becomes_ours_when_a_later_reply_shows_our_tag():
    det = detector()
    det.ingest_peer({**response(), "objects": [pin("alt1", 1009, "Paul Alt", rev=1)]}, 10.0)
    det.ingest_peer({**response(), "objects": [pin("own1", 1001, "Paul Test", rev=2)]}, 11.0)
    assert sorted(m["id"] for m in det.peers_snapshot()["objects"]["mine"]) == ["alt1", "own1"]


def test_a_guildmates_absurd_coordinates_are_skipped_and_the_settings_page_still_renders():
    det = detector()
    huge = 1.7e308
    det.ingest_peer(response(sightings=[sighting("spice", huge, huge)],
                             presence=[member(1002, "Chani Test", x=huge, y=huge)]), 10.0)
    snap = det.peers_snapshot()
    assert snap["sightings"] == []
    assert {m["name"]: m["distanceM"] for m in snap["members"]}["Chani Test"] is None


class Counted:
    """A loaded actor that counts how often its marker is read."""

    def __init__(self, actor, reads) -> None:
        self._actor, self._reads = actor, reads

    def __getattr__(self, name):
        if name == "marker":
            self._reads.append(1)
        return getattr(self._actor, name)


def test_drawing_guild_ore_rings_reads_each_loaded_actor_once_not_once_per_row():
    det = detector()
    actors(det, *[ore(f"n{i}", 5000.0 * i, 50_000) for i in range(200)], world=DD, t=2)
    det.ingest_peer(response(sightings=[sighting("ore", 5000.0 * i, -50_000, marker="magnetiteore") for i in range(100)]), 10.0)
    reads = []
    for t in det.store.items.values():
        t.actor = Counted(t.actor, reads)
    assert {r["kind"] for r in det.peer_marks()} == {"peer"}
    assert len(reads) <= 200
