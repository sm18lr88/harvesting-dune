"""Guild objects on the client: merged by author tag and id, aged on the feed clock, drawn only on
their own map, published only after the server has shown it knows objects, and never a reason for
presence and finds to stop."""

import asyncio
import json
import logging
import re

import pytest
from helpers import CELL_CM, actors, pose, session, spice

from harvesting_dune import logsetup, syncloop
from harvesting_dune.app import Detector
from harvesting_dune.feed import BusMessage
from harvesting_dune.sync import BACKOFF_S, SyncClient, SyncServerError

KEY = "gs1_alpha001_Qm9vdHN0cmFwVGVzdEtleUFscGhhMDAx"
OTHER_KEY = "gs1_bravo002_VGVzdE9ubHlCcmF2b0tleVNlY3JldDAy"
GROUP = "prd-srv-test-alpha-erg"
WHO = {"characterName": "Alpha Test", "characterId": 1001, "battlegroupId": GROUP}
HAGGA, DD, LAYOUT = "survival_1", "deepdesert_1", "deepdesert_1_07"
HAGGA_SCOPE = {"worldId": HAGGA, "layout": HAGGA}
DD_SCOPE = {"worldId": DD, "layout": LAYOUT}
NOW = 1794160000.0


class Stop(Exception):
    pass


def player(tmp_path, journal_dir=None) -> Detector:
    det = Detector(journal_dir, data_dir=tmp_path, min_rescore_s=0)
    det.set_sync_key(KEY)
    session(det, HAGGA)
    det.handle(BusMessage("delta", "dune.trackedSession", WHO), 0)
    pose(det, 0, 0, t=1)
    return det


def obj(oid, kind="pin", cid=1002, name="Beta Test", tag="tagbeta0", x=1000.0, y=2000.0, scope=HAGGA_SCOPE,
        ttl=3600.0, age=10.0, rev=1, body=None, geometry="point"):
    geo = {"type": "point", "x": x, "y": y, "z": 5.0} if geometry == "point" else geometry
    return {"id": oid, "kind": kind, "scope": scope, "geometry": geo, "body": {"label": "IRON HERE"} if body is None else body,
            "ttlS": ttl, "ageS": age, "characterId": cid, "characterName": name, "playerTag": tag, "rev": rev}


def response(objects=None, next_rev=5):
    r = {"now": NOW, "next": next_rev, "keyState": "valid", "presence": [], "sightings": []}
    if objects is not None:
        r["objects"] = list(objects)
    return r


def test_objects_are_merged_by_tag_and_id_and_unknown_kinds_are_kept_silently(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(logsetup, "_last", {})
    det = player(tmp_path)
    with caplog.at_level(logging.WARNING):
        det.ingest_peer(response([obj("p1"), obj("p1", tag="taggamma", cid=1003, name="Gamma Test"),
                                  obj("b1", kind="base", body={"name": "HOME"}),
                                  obj("g1", geometry={"type": "blob", "points": []})]), NOW)
        assert not [r for r in caplog.records if "skipped" in r.getMessage()]
        bad = obj("x1")
        del bad["id"]
        det.ingest_peer(response([bad]), NOW)
    assert [r.levelno for r in caplog.records if "skipped a object" in r.getMessage()] == [logging.WARNING]
    assert set(det.peers.objects) == {("tagbeta0", "p1"), ("taggamma", "p1"), ("tagbeta0", "b1"), ("tagbeta0", "g1")}
    assert det.snapshot()["peers"]["objects"]["all"] == 4
    drawn = det.page_state()["objects"]
    assert sorted(o["id"] for o in drawn) == ["tagbeta0:b1", "tagbeta0:p1", "taggamma:p1"]  # no blob geometry
    assert det.peers.objects_seen


def test_page_state_objects_carry_id_point_body_mine_and_author(tmp_path):
    det = player(tmp_path)
    det.ingest_peer(response([]), NOW)
    before = {k: v for k, v in det.page_state().items() if k != "objects"}
    det.ingest_peer(response([obj("p1", age=10.0, ttl=3600.0),
                              obj("m1", cid=1001, name="Alpha Test", tag="tagalpha", age=4.0, body={"label": "MINE"})]), NOW)
    state = det.page_state()
    assert {k: v for k, v in state.items() if k != "objects"} == before  # dest, route, alternatives, stale, extraActors
    assert state["objects"] == [
        {"id": "tagalpha:m1", "kind": "pin", "x": 1000.0, "y": 2000.0, "z": 5.0, "body": {"label": "MINE"},
         "mine": True, "by": "Alpha Test"},
        {"id": "tagbeta0:p1", "kind": "pin", "x": 1000.0, "y": 2000.0, "z": 5.0, "body": {"label": "IRON HERE"},
         "mine": False, "by": "Beta Test"},
    ]
    assert det.peers.my_tag == "tagalpha"
    det.enabled = False  # recommendations off: pins hide with everything else
    assert det.page_state()["objects"] == []


def test_a_pin_ageing_on_the_feed_clock_does_not_change_page_state(tmp_path):
    det = player(tmp_path)
    det.ingest_peer(response([obj("p1")]), NOW)
    first = json.dumps(det.page_state())
    pose(det, 0, 0, t=det.clock + 30)
    assert json.dumps(det.page_state()) == first  # an unchanged pin never forces a push and a redraw


def test_a_same_key_alts_pin_before_our_own_in_one_reply_is_still_ours(tmp_path):
    det = player(tmp_path)
    det.ingest_peer(response([obj("alt1", cid=1009, name="Alpha Alt", tag="tagalpha", rev=1),
                              obj("own1", cid=1001, name="Alpha Test", tag="tagalpha", rev=2)]), NOW)
    assert {o["id"]: o["mine"] for o in det.peers.objects.values()} == {"alt1": True, "own1": True}
    assert det.delete_object("alt1")


def test_remembered_spice_in_page_state_ages_on_the_feed_clock(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    session(det, "DD")
    pose(det, 10, 0)
    actors(det, spice(7, 20 * CELL_CM, 0, "Medium", fill=0.7), t=100)
    actors(det, t=101)  # out of load range now, still within the 45 min memory on the feed clock
    assert "dd-mem-7" in [a["id"] for a in det.page_state()["extraActors"]]


def test_objects_out_of_scope_are_kept_and_drawn_on_arrival(tmp_path):
    det = player(tmp_path)
    det.ingest_peer(response([obj("dd", scope=DD_SCOPE), obj("all", scope=None)]), NOW)
    assert [o["id"] for o in det.page_state()["objects"]] == ["tagbeta0:all"]
    session(det, DD, t=2)
    det.map_files[det.store.world] = LAYOUT
    assert sorted(o["id"] for o in det.page_state()["objects"]) == ["tagbeta0:all", "tagbeta0:dd"]


def test_objects_expire_on_the_feed_clock_and_a_tombstone_removes_one_at_once(tmp_path):
    det = player(tmp_path)
    det.ingest_peer(response([obj("short", ttl=60.0), obj("long", ttl=3600.0)]), NOW)
    pose(det, 0, 0, t=det.clock + 61)
    assert [o["id"] for o in det.page_state()["objects"]] == ["tagbeta0:long"]
    rev = det.publish_rev
    det.ingest_peer(response([obj("long", ttl=0.0, rev=9)]), NOW)
    assert det.page_state()["objects"] == [] and det.publish_rev > rev


def test_a_hidden_members_objects_are_not_drawn(tmp_path):
    det = player(tmp_path)
    det.ingest_peer(response([obj("p1"), obj("p2", cid=1003, tag="taggamma", name="Gamma Test")]), NOW)
    det.ui("sync_member", "1002")
    assert [o["id"] for o in det.page_state()["objects"]] == ["taggamma:p2"]
    assert det.snapshot()["peers"]["objects"]["all"] == 2  # still known, just not drawn


def test_publish_stages_a_pin_at_the_destination_else_the_pose_and_refuses_without_either(tmp_path):
    det = player(tmp_path)
    oid = det.publish_object("pin", {"label": "IRON HERE"})
    up = det.peers.objects_out[-1]
    assert set(up) == {"id", "kind", "scope", "geometry", "body", "ttlS"}
    assert up["id"] == oid and re.fullmatch(r"[A-Za-z0-9_-]{12}", oid)
    assert up["scope"] == det.partition() == HAGGA_SCOPE and up["ttlS"] == 7200
    assert up["geometry"] == {"type": "point", "x": 0.0, "y": 0.0, "z": 10.0}
    assert det.snapshot()["peers"]["objects"]["where"] == "position"
    det.peers.published_at = 0.0
    det.handle(BusMessage("delta", "dune.trackedDestination", {"hasDestination": True, "x": 500.0, "y": 600.0, "z": 7.0}), 2)
    det.publish_object("pin", {"label": "DEST"})
    assert det.peers.objects_out[-1]["geometry"] == {"type": "point", "x": 500.0, "y": 600.0, "z": 7.0}
    assert det.snapshot()["peers"]["objects"]["where"] == "destination"

    blank = Detector(None, data_dir=tmp_path / "blank", min_rescore_s=0)
    assert blank.publish_object("pin", {"label": "X"}) is None
    assert blank.peers.objects_note == "needs a guild key and a tracked character"
    blank.set_sync_key(KEY)
    session(blank, HAGGA)
    blank.handle(BusMessage("delta", "dune.trackedSession", WHO), 0)
    assert blank.publish_object("pin", {"label": "X"}) is None and blank.peers.objects_note == "no position yet"
    session(blank, DD, t=1)  # Deep Desert without its layout yet
    pose(blank, 0, 0, t=2)
    assert blank.publish_object("pin", {"label": "X"}) is None and blank.peers.objects_note == "no map yet"
    blank.peers.key_state = "invalid"
    assert blank.publish_object("pin", {"label": "X"}) is None
    assert blank.peers.objects_note == "the guild key was refused: enter a new one" and blank.peers.objects_out == []


def test_publishing_is_paced_and_capped(tmp_path):
    det = player(tmp_path)
    assert det.publish_object("pin", {"label": "A"}) is not None
    assert det.publish_object("pin", {"label": "B"}) is None and det.peers.objects_note == "wait a moment"
    det.peers.published_at = 0.0
    det.ingest_peer(response([obj(f"m{i}", cid=1001, name="Alpha Test", tag="tagalpha") for i in range(19)]), NOW)
    assert det.publish_object("pin", {"label": "C"}) is None  # 19 live + 1 staged
    assert det.peers.objects_note == "you already have 20 live pins: delete one first"
    det.peers.objects_out.clear()
    note = det.publish_object("note", {"text": "hello"}, geometry=None, scope=None)
    assert note is not None and det.peers.objects_out[-1]["geometry"] is None and det.peers.objects_out[-1]["scope"] is None
    assert det.peers.objects_note is None


def loop_run(det, monkeypatch, exchange, stop_after):
    """Run the sync loop with a fake exchange; events are ("sync", body) and ("nap", seconds)."""
    client, events = SyncClient(server="https://127.0.0.1:9"), []

    def fake(key, body, path="/v1/sync"):
        if path != "/v1/sync":
            return {"now": NOW, "next": 0, "ids": [], "profiles": []} if path == "/v1/profiles" else {"ok": True}
        events.append(("sync", json.loads(json.dumps(body))))
        if sum(e[0] == "sync" for e in events) > stop_after:
            raise Stop
        return exchange(body, len([e for e in events if e[0] == "sync"]))

    async def nap(seconds, wake):
        events.append(("nap", seconds))
        if len(events) > 200:
            raise Stop
        return seconds, False

    client.exchange = fake
    monkeypatch.setattr(syncloop, "nap", nap)
    with pytest.raises(Stop):
        asyncio.run(syncloop.poll(det, client))
    return events


def test_objects_are_sent_only_after_the_server_has_shown_the_key(tmp_path, monkeypatch):
    det = player(tmp_path)
    det.publish_object("pin", {"label": "IRON HERE"})
    old = loop_run(det, monkeypatch, lambda body, n: response(None), stop_after=3)
    assert [e for e in old if e[0] == "sync" and "objects" in e[1]] == [] and len(det.peers.objects_out) == 1
    assert not det.peers.objects_seen

    new = loop_run(det, monkeypatch, lambda body, n: response([]), stop_after=2)
    syncs = [e[1] for e in new if e[0] == "sync"]
    assert "objects" not in syncs[0] and syncs[1]["objects"][0]["body"] == {"label": "IRON HERE"}
    assert det.peers.objects_out == []


def test_a_refused_upload_is_dropped_and_the_sync_goes_on_without_backoff(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(logsetup, "_last", {})
    det = player(tmp_path)
    det.ingest_peer(response([]), NOW)
    det.publish_object("pin", {"label": "IRON HERE"})

    def refuse_once(body, n):
        if n == 1:
            raise SyncServerError(400, "invalid-field objects[0].kind")
        return response([])

    events = loop_run(det, monkeypatch, refuse_once, stop_after=2)
    assert [e[0] for e in events[:2]] == ["sync", "sync"]  # the next exchange in the same pass, no wait
    assert "objects" in events[0][1] and "objects" not in events[1][1]
    assert det.peers.objects_out == [] and det.peers.objects_note == "the server refused the pin"
    assert BACKOFF_S[0] not in [e[1] for e in events if e[0] == "nap"]

    assert [r.levelno for r in caplog.records if "refused the staged objects" in r.getMessage()] == [logging.WARNING]
    det.objects_refused("object-cap objects[0]")
    assert det.peers.objects_note == "the server refused the pin: too many live objects"

    def bad_since(body, n):
        raise SyncServerError(400, "invalid-field since")

    other = loop_run(det, monkeypatch, bad_since, stop_after=1)
    assert other[1] == ("nap", BACKOFF_S[0])  # any other 400 still backs off


def test_an_object_cap_refusal_keeps_the_staged_deletes(tmp_path):
    det = player(tmp_path)
    det.ingest_peer(response([obj("old1", cid=1001, name="Alpha Test", tag="tagalpha")]), NOW)
    assert det.delete_object("old1")
    det.publish_object("pin", {"label": "NEW"})
    det.objects_refused("object-cap objects[1]")
    assert [(u["id"], u["ttlS"]) for u in det.peers.objects_out] == [("old1", 0)]  # deletes always pass the cap
    det.objects_refused("invalid-field objects[0].kind")
    assert det.peers.objects_out == []  # any other refusal may be the delete itself: never resent in a loop



def test_a_cut_objects_page_forces_the_next_sync_at_once(tmp_path, monkeypatch):
    det = player(tmp_path)
    page = [obj(f"o{i}", rev=i) for i in range(200)]
    events = loop_run(det, monkeypatch, lambda body, n: response(page if n == 1 else []), stop_after=2)
    assert [e[0] for e in events[:2]] == ["sync", "sync"]


def test_delete_unstages_or_stages_a_tombstone_and_hides_the_row_at_once(tmp_path):
    det = player(tmp_path)
    staged = det.publish_object("pin", {"label": "A"})
    assert det.delete_object(staged) and det.peers.objects_out == []
    det.ingest_peer(response([obj("m1", cid=1001, name="Alpha Test", tag="tagalpha"), obj("p1")]), NOW)
    assert det.delete_object("p1") is False  # not ours
    rev = det.publish_rev
    assert det.delete_object("m1")
    assert [o["id"] for o in det.page_state()["objects"]] == ["tagbeta0:p1"] and det.publish_rev > rev
    tomb = det.peers.objects_out[-1]
    assert tomb["id"] == "m1" and tomb["ttlS"] == 0 and set(tomb) == {"id", "kind", "scope", "geometry", "body", "ttlS"}


def test_the_destination_is_read_in_memory_only(tmp_path):
    det = player(tmp_path, journal_dir=tmp_path / "journals")
    dest = {"hasDestination": True, "x": 123457.5, "y": 123457.5, "z": 123457.5, "title": "secret"}
    det.handle(BusMessage("delta", "dune.trackedDestination", dest), 2)
    assert det.native_dest == dest
    det.ingest_peer(response([]), NOW)
    det.publish_object("pin", {"label": "X"})  # staged for the server only after the explicit action
    text = json.dumps(det.snapshot()) + json.dumps(det.page_state())
    assert "123457.5" not in text
    det.handle(BusMessage("delta", "dune.trackedDestination", {"hasDestination": False}), 3)
    assert det.native_dest is None
    det.handle(BusMessage("delta", "dune.trackedDestination", dest), 4)
    det.handle(BusMessage("topicReset", "dune.trackedDestination", None), 5)
    assert det.native_dest is None
    written = "".join(p.read_text("utf8") for p in (tmp_path / "journals").glob("*.jsonl"))
    assert written and "trackedDestination" not in written


def test_objects_reset_with_the_key_and_the_identity_but_the_capability_memory_stays(tmp_path):
    det = player(tmp_path)
    det.ingest_peer(response([obj("p1")]), NOW)
    det.publish_object("pin", {"label": "A"})
    det.set_sync_key(OTHER_KEY)
    assert det.peers.objects == {} and det.peers.objects_out == [] and det.peers.objects_seen
    det.ingest_peer(response([obj("p1")]), NOW)
    det.handle(BusMessage("delta", "dune.status", {"hasGameSession": False}), 5)
    assert det.peers.objects == {} and det.peers.objects_seen


def test_an_unchanged_objects_answer_redraws_nothing(tmp_path):
    det = player(tmp_path)
    rows = [obj("p1"), obj("p2", x=5000.0)]
    det.ingest_peer(response(rows), NOW)
    rev = det.publish_rev
    det.ingest_peer(response(rows), NOW + 15)
    assert det.publish_rev == rev
    det.ingest_peer(response([obj("p2", x=6000.0, rev=2)]), NOW + 30)
    assert det.publish_rev > rev
