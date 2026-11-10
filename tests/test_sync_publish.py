"""What the detector publishes to the guild: strong sightings only, in its partition, once the
character is known, and never without a key."""

import asyncio
import json
from pathlib import Path

from helpers import actors, ore, pose, session, spice

from harvesting_dune import syncloop
from harvesting_dune.app import Detector
from harvesting_dune.feed import BusMessage
from harvesting_dune.peers import PeerMixin
from harvesting_dune.store import Status
from harvesting_dune.sync import SyncClient, SyncServerError

FIXTURES = Path(__file__).parent / "fixtures"
KEY = "gs1_alpha001_Qm9vdHN0cmFwVGVzdEtleUFscGhhMDAx"
DD, LAYOUT = "deepdesert_1", "deepdesert_1_07"
WHO = {"characterName": "Paul Test", "characterId": 1001, "battlegroupId": "prd-srv-test-alpha-erg"}


def ship(x, y):
    return {"id": "s1", "actorType": "BP_SmallShipwreck_01_C", "markerType": "SalvageSteel", "spriteKey": "salvagesteel",
            "x": x, "y": y, "z": 0.0, "reportable": True}


def scan_row(x, y):
    return {"type": "BP_Magnetite_A_Spawner_C", "map_marker_id": "magnetiteore", "x": x, "y": y, "z": 0}


def who(det, **ident):
    det.handle(BusMessage("delta", "dune.trackedSession", ident), 0)


def detector(key=KEY, layout=LAYOUT, ident=WHO, data_dir=None):
    det = Detector(None, data_dir=data_dir, min_rescore_s=0)
    if key:
        det.set_sync_key(key)
    session(det, DD)
    if layout:
        det.map_files[det.store.world] = layout
    who(det, **ident)
    pose(det, 0, 0, t=1)
    return det


NODES = [ore(f"n{i}", 400 * i, 0) for i in range(5)]  # 0-16 m from you


def play(det):
    """A large and a small live field, a medium site that blows, a downed ship, and a node you mine."""
    det.load_scan([scan_row(400, 0)], 200, 1)
    fields = [spice("big", 10_000, 0, "Large", fill=0.7), spice("tiny", 20_000, 0, "Small")]
    actors(det, *fields, spice("site", 50_000, 0, spawned=False), spice("tiny2", 30_000, 0, "Small", spawned=False),
           *NODES, ship(0, 60_000), world=DD, t=2)
    actors(det, *fields, spice("site", 50_000, 0), spice("tiny2", 30_000, 0, "Small"),
           NODES[0], *NODES[2:], ship(0, 60_000), world=DD, t=3)


def by_cls(payload):
    out = {}
    for r in payload["records"]:
        out.setdefault(r["cls"], []).append(r)
    return out


def test_without_a_key_nothing_is_queued_or_published():
    det = detector(key=None)
    play(det)
    assert det.peers.outbox == {} and det.outbox(10) is None


def test_nothing_is_published_before_the_character_name_is_known():
    det = detector(ident={"characterId": 1001, "battlegroupId": WHO["battlegroupId"]})
    play(det)
    assert det.outbox(10) is None


def test_strong_sightings_in_the_deep_desert_layout():
    det = detector()
    play(det)
    payload = det.outbox(10)
    assert payload["identity"] == WHO and payload["partition"] == {"worldId": DD, "layout": LAYOUT}
    got = by_cls(payload)
    assert sorted(got) == ["blow", "mined", "ore", "ship", "spice", "stale"]
    assert all(r["type"] != "spicefieldsmall" for r in payload["records"])
    [large] = [r for r in got["spice"] if r["type"] == "spicefieldlarge"]
    assert large["fill"] == 0.7 and large["size"] == "large" and large["observedAt"] == 10
    assert [(r["type"], r["size"]) for r in got["blow"]] == [("spicefieldmedium", "medium")]
    assert [r["type"] for r in got["ship"]] == ["salvagesteel"]
    assert [r["type"] for r in got["mined"]] == ["magnetiteore"]
    [node] = got["ore"]  # the four live nodes share one 20 m cell
    assert node["type"] == "magnetiteore" and (node["fill"], node["size"], node["rowKey"]) == (None, None, None)
    [stale] = got["stale"]
    assert stale["rowKey"] == "BP_Magnetite_A_Spawner_C:400:0" and stale["type"] == "magnetiteore" and stale["distCm"] == 400
    again = by_cls(det.outbox(20))
    assert sorted(again) == ["spice"]  # notes, the stale row and live ore go once; live fields every time


def test_payload_has_the_contract_request_shape_and_nothing_private():
    det = detector()
    play(det)
    payload = det.outbox(10)
    contract = json.loads((FIXTURES / "sync-contract.json").read_text())
    [case] = [c for c in contract["cases"] if c["name"] == "first-poll"]
    body = case["request"]["body"]
    assert set(payload) | {"since", "sentAt"} == set(body)  # the sync loop adds the cursor and the send time
    for part in ("identity", "partition", "position"):
        assert set(payload[part]) == set(body[part])
    assert all(set(r) == set(body["records"][0]) for r in payload["records"])
    text = json.dumps(payload)
    for word in ('"id"', "currencies", "loot", "vehicle"):
        assert word not in text


def test_position_only_when_the_player_shares_it():
    det = detector()
    assert det.outbox(10)["position"] == {"x": 0.0, "y": 0.0, "z": 10.0, "observedAt": 10}
    det.sync_positions = False
    assert det.outbox(11)["position"] is None


def test_queue_keeps_the_latest_note_per_cell_and_at_most_500():
    det = detector()
    det.peer_note("mined", "magnetiteore", 100, 100, 0, 1)
    det.peer_note("mined", "magnetiteore", 900, 900, 0, 2)  # same 20 m cell: replaces the first
    assert [r["observedAt"] for r in det.peers.outbox.values()] == [2]
    for i in range(600):
        det.peer_note("mined", "magnetiteore", 5000.0 * i, 0, 0, 100 + i)
    assert len(det.peers.outbox) == 500
    assert min(r["observedAt"] for r in det.peers.outbox.values()) == 200  # the oldest went first


def test_records_are_capped_nearest_first_and_the_rest_stays_queued():
    det = detector()
    for i in range(250):
        det.peer_note("mined", "magnetiteore", 5000.0 * (250 - i), 0, 0, i)
    records = det.outbox(1000)["records"]
    assert len(records) == 200 and records[0]["x"] == 5000.0
    assert len(det.peers.outbox) == 50 and len(det.outbox(1001)["records"]) == 50


def test_deep_desert_without_its_layout_publishes_nothing():
    det = detector(layout=None)
    for i in range(10):
        det.peer_note("mined", "magnetiteore", 5000.0 * i, 0, 0, i)
    assert det.partition() is None and det.outbox(10) is None
    assert len(det.peers.outbox) == 10  # kept until the layout is known


def test_paused_sync_drops_notes_and_publishes_nothing():
    det = detector()
    det.sync_enabled = False
    det.peer_note("mined", "magnetiteore", 0, 0, 0, 1)
    assert det.peers.outbox == {} and det.outbox(10) is None


def test_other_worlds_are_their_own_layout_and_notes_stay_in_their_world():
    det = detector()
    det.peer_note("mined", "magnetiteore", 0, 0, 0, 1)
    session(det, "HaggaBasin")
    assert det.partition() == {"worldId": "HaggaBasin", "layout": "HaggaBasin"}
    assert det.outbox(10)["records"] == []  # the Deep Desert note never goes out as a Hagga Basin one


def test_replay_never_drains_the_outbox(monkeypatch):
    drained = []
    monkeypatch.setattr(PeerMixin, "outbox", lambda self, now: drained.append(now))
    recs = [json.loads(line) for line in (FIXTURES / "tracked_session.jsonl").read_text().splitlines()]
    for key in (None, KEY):
        det = Detector(None, min_rescore_s=0)
        if key:
            det.set_sync_key(key)
        for r in recs:  # the cli replay loop
            det.handle(BusMessage(r["type"], r["topic"], r["payload"]), r["t"])
            det.maybe_rescore(r["t"])
        session(det, DD)
        det.map_files[det.store.world] = LAYOUT
        pose(det, 0, 0, t=1)
        play(det)
        det.maybe_rescore(4)
        assert len(det.peers.outbox) == (3 if key else 0)  # blow, ship and mined: queued, not sent
    assert drained == []


def test_only_live_ore_crystal_and_stone_nodes_count_as_ore():
    det = detector()
    cage = {"id": "c1", "actorType": "LootContainer", "markerType": "LootContainerRare", "x": 0, "y": 20_000, "z": 0}
    actors(det, ore("iron", 0, 0), ore("jasmium", 5000, 0, marker="JasmiumOre"), ore("basalt", 10_000, 0, marker="BasaltOre"),
           ore("scrap", 15_000, 0, marker="ScrapMetalWreckage"), ore("bush", 20_000, 0, marker="BrittleBush"),
           ore("fuel", 25_000, 0, marker="FuelCellWreckage"), spice("big", 30_000, 0, "Large"),
           spice("tiny", 35_000, 0, "Small"), cage, world=DD, t=2)
    got = by_cls(det.outbox(10))
    assert sorted(r["type"] for r in got["ore"]) == ["basaltore", "jasmiumore", "magnetiteore"]
    assert [r["type"] for r in got["spice"]] == ["spicefieldlarge"]  # spice fields stay `spice`, never `ore`


def test_ore_goes_once_per_change_and_a_burst_spreads_over_polls():
    det = detector()
    actors(det, *[ore(f"n{i}", 2500.0 * (i + 1), 0) for i in range(250)], world=DD, t=2)
    first = det.outbox(10)["records"]
    assert len(first) == 200 and {r["cls"] for r in first} == {"ore"}
    assert len(det.outbox(11)["records"]) == 50  # the rest of the burst, next poll
    assert det.outbox(12)["records"] == []  # nothing changed: nothing re-sent
    actors(det, *[ore(f"n{i}", 2500.0 * (i + 1), 0) for i in range(250)], ore("new", -5000, 0), world=DD, t=3)
    assert [(r["cls"], r["x"]) for r in det.outbox(13)["records"]] == [("ore", -5000)]


def test_a_live_ore_node_is_confirmed_again_every_20_minutes_and_only_then():
    det = detector()
    actors(det, ore("iron", 20_000, 0), world=DD, t=2)
    assert [r["cls"] for r in det.outbox(10)["records"]] == ["ore"]
    assert det.outbox(10 + 1199)["records"] == []
    assert [r["observedAt"] for r in det.outbox(10 + 1200)["records"]] == [1210]
    assert det.outbox(10 + 1201)["records"] == []


def test_a_stale_row_already_published_is_not_sent_again_when_we_come_back_to_its_map(tmp_path):
    det = detector(data_dir=tmp_path)
    play(det)
    assert [r["rowKey"] for r in by_cls(det.outbox(10))["stale"]] == ["BP_Magnetite_A_Spawner_C:400:0"]
    session(det, "HaggaBasin", t=20)
    det.outbox(21)
    session(det, DD, t=30)  # the map's corrections file is read again
    assert "stale" not in by_cls(det.outbox(31))


def test_a_live_ore_node_that_only_left_the_load_range_is_not_sent_again_when_it_loads_again():
    det = detector()
    actors(det, ore("iron", 20_000, 0), world=DD, t=2)
    assert [r["cls"] for r in det.outbox(10)["records"]] == ["ore"]
    pose(det, 2_000_000, 0, t=20)
    actors(det, world=DD, t=21)
    assert [t.status for t in det.store.items.values()] == [Status.OUT_OF_RANGE]
    det.outbox(22)
    pose(det, 0, 0, t=30)
    actors(det, ore("iron", 20_000, 0), world=DD, t=31)
    assert det.outbox(40)["records"] == []


def staged_labels(det):
    return [u["body"]["label"] for u in det.peers.objects_out if u["ttlS"] > 0]


def objects_reply(rows=()):
    return {"next": 1, "keyState": "valid", "presence": [], "sightings": [], "objects": list(rows)}


class PinWhileRefused(SyncClient):
    """The player drops a second pin while the request with the first one is on the wire; the server
    then refuses that request for the guild's object cap."""

    def __init__(self, det) -> None:
        super().__init__(server="https://127.0.0.1:1")
        self.det = det

    def exchange(self, key, body, path="/v1/sync"):
        self.det.peers.published_at = 0.0  # the second pin comes after the publish gap
        self.det.publish_object("pin", {"label": "SECOND"})
        raise SyncServerError(400, "object-cap objects[0]")


def test_a_refusal_drops_only_the_pins_of_the_refused_request():
    det = detector()
    det.ingest_peer(objects_reply(), 5.0)  # the server knows objects
    det.publish_object("pin", {"label": "FIRST"})
    assert asyncio.run(syncloop._sync(det, PinWhileRefused(det), KEY, syncloop.Pace())) is False
    assert staged_labels(det) == ["SECOND"]


def test_the_guild_cap_counts_the_battlegroups_objects_on_every_map():
    det = detector()
    hagga = {"worldId": "survival_1", "layout": "survival_1"}
    rows = [{"id": f"o{i}", "kind": "pin", "scope": hagga, "geometry": {"type": "point", "x": 1000.0, "y": 0.0, "z": 0.0},
             "body": {"label": "X"}, "ttlS": 3600, "ageS": 1, "characterId": 2000 + i, "characterName": "Leto Test",
             "playerTag": f"tag{i}", "rev": i + 1} for i in range(300)]
    det.ingest_peer(objects_reply(rows), 5.0)
    assert det.publish_object("pin", {"label": "ONE MORE"}) is None
    assert staged_labels(det) == [] and "300" in det.peers.objects_note


def test_a_stale_row_from_an_earlier_run_is_not_published_again(tmp_path):
    first = detector(data_dir=tmp_path)
    play(first)
    assert "stale" in by_cls(first.outbox(10))
    det = detector(data_dir=tmp_path)  # the next start reads the map's corrections file
    pose(det, 0, 0, t=100)
    actors(det, ore("n0", 0, 0), world=DD, t=101)
    assert "stale" not in by_cls(det.outbox(110))
