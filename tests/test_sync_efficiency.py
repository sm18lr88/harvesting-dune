"""Guild sync stays cheap (docs/SYNC-PROTOCOL.md "Cost"): requests per hour follow what the player
does, a wake-up syncs at once, an unchanged answer redraws nothing, every query a sync runs
on the server uses an index, and a restart refetches a bounded set of shared objects."""

import asyncio
import json
import time
from collections import Counter

import pytest
from guildsync.api import Api
from guildsync.storage import Storage
from helpers import pose, session

from harvesting_dune import syncloop
from harvesting_dune.app import Detector
from harvesting_dune.feed import BusMessage
from harvesting_dune.sync import SyncClient

KEY = "gs1_alpha001_Qm9vdHN0cmFwVGVzdEtleUFscGhhMDAx"
OTHER_KEY = "gs1_bravo002_VGVzdE9ubHlCcmF2b0tleVNlY3JldDAy"
GROUP = "prd-srv-test-alpha-erg"
WHO = {"characterName": "Paul Test", "characterId": 1001, "battlegroupId": GROUP}
HAGGA = "survival_1"
NOW = 1794160000.0


class Stop(Exception):
    pass


def player(tmp_path) -> Detector:
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.set_sync_key(KEY)
    session(det, HAGGA)
    det.handle(BusMessage("delta", "dune.trackedSession", WHO), 0)
    pose(det, 0, 0, t=1)
    return det


def answer(det: Detector, path: str) -> dict:
    match path:
        case "/v1/sync":
            return {"now": NOW, "next": det.peers.since, "keyState": "valid", "presence": [], "sightings": []}
        case "/v1/profiles":
            return {"now": NOW, "next": 0, "ids": [], "profiles": []}
        case _:
            return {"ok": True}


def syncs_in_an_hour(det: Detector, monkeypatch, each_wait=lambda clock: None) -> int:
    """Run the loop over one simulated hour (each wait advances the clock, nothing really waits)."""
    client, calls, clock = SyncClient(server="https://127.0.0.1:9"), [], [0.0]

    def exchange(key, body, path="/v1/sync"):
        calls.append(path)
        return answer(det, path)

    async def nap(seconds, wake):
        clock[0] += seconds
        if clock[0] >= 3600:
            raise Stop
        each_wait(clock[0])
        return seconds, False

    client.exchange = exchange
    monkeypatch.setattr(syncloop, "nap", nap)
    with pytest.raises(Stop):
        asyncio.run(syncloop.poll(det, client))
    return calls.count("/v1/sync")


def test_requests_per_hour_follow_what_the_player_does(tmp_path, monkeypatch):
    moving = player(tmp_path / "moving")
    step = iter(range(1, 10_000))
    assert syncs_in_an_hour(moving, monkeypatch, lambda clock: pose(moving, next(step) * 20_000.0, 0, t=clock)) == 240
    assert syncs_in_an_hour(player(tmp_path / "calm"), monkeypatch) == 60
    away = player(tmp_path / "away")  # synced in a world, then a menu or loading screen: the heartbeat only
    # 0 s in the world (promising 60 s), 60 s as promised, then every 180 s: 21 in the hour
    assert syncs_in_an_hour(away, monkeypatch, lambda clock: session(away, None)) == 21
    for name, change in (("nokey", lambda d: d.set_sync_key("")), ("paused", lambda d: d.ui("sync_pause")),
                         ("rejected", lambda d: setattr(d.peers, "key_state", "invalid")),
                         ("closed", lambda d: d.handle(BusMessage("delta", "dune.status", {"hasGameSession": False}), 2))):
        det = player(tmp_path / name)
        change(det)
        assert syncs_in_an_hour(det, monkeypatch) == 0, name


def test_a_wake_up_syncs_at_once_instead_of_at_the_next_tick(tmp_path):
    det = player(tmp_path)
    client = SyncClient(server="https://127.0.0.1:9")

    async def main() -> list[str]:
        loop, calls, arrived = asyncio.get_running_loop(), [], asyncio.Queue()

        def exchange(key, body, path="/v1/sync"):  # a worker thread, like the real request
            kind = "goodbye" if body.get("presence", {}).get("leaving") else path
            calls.append(kind)
            loop.call_soon_threadsafe(arrived.put_nowait, kind)
            return answer(det, path)

        async def next_sync() -> None:  # bounded far below the 15 s tick: only a wake-up gets here in time
            while await asyncio.wait_for(arrived.get(), 5) != "/v1/sync":
                pass

        client.exchange = exchange
        task = asyncio.create_task(syncloop.poll(det, client))
        try:
            await next_sync()  # the first sync after start
            det.ui("guild_view", "guild")
            await next_sync()
            session(det, "deepdesert_1")  # entering a world (its layout not known yet: nothing to publish)
            det.map_files[det.store.world] = "deepdesert_1_07"
            session(det, HAGGA)
            await next_sync()
            det.set_sync_key(OTHER_KEY)
            await next_sync()
            synced = calls.count("/v1/sync")
            det.ui("guild_view", "characters")  # the profile fetch may ride on the pass already running
            while await asyncio.wait_for(arrived.get(), 5) != "/v1/profiles":
                pass
        finally:
            task.cancel()
        return synced, calls

    synced, calls = asyncio.run(main())
    assert synced == 4 and calls.count("/v1/profiles") == 1
    assert calls.count("goodbye") == 1  # the replaced key said goodbye before the new one synced


def test_an_unchanged_answer_redraws_nothing_and_still_ages_members(tmp_path):
    det = player(tmp_path)
    member = {"characterName": "Chani Test", "characterId": 1002, "battlegroupId": GROUP, "worldId": HAGGA,
              "layout": HAGGA, "x": 5000.0, "y": 0.0, "z": 0.0, "ageS": 2.0}
    resp = {"now": NOW, "next": 4, "keyState": "valid", "presence": [member], "sightings": []}
    det.ingest_peer(resp, time.time())
    drawn = det.publish_rev
    det.clock += 40.0
    det.ingest_peer(resp, time.time())
    assert det.publish_rev == drawn and det.peers.members[1002]["seen_at"] == det.clock - 2.0
    det.ingest_peer({**resp, "presence": [{**member, "x": 9000.0}]}, time.time())
    assert det.publish_rev == drawn + 1


def test_every_query_of_a_sync_and_a_profile_fetch_uses_an_index(tmp_path):
    st = Storage.open(str(tmp_path / "guild.db"))
    api, key = Api(st, lambda: NOW), {"id": st.issue_key("Alpha Test")[0]}
    body = {"since": 0, "sentAt": NOW, "identity": WHO, "partition": {"worldId": HAGGA, "layout": HAGGA},
            "position": {"x": 0.0, "y": 0.0, "z": 0.0, "observedAt": NOW},
            "records": [{"cls": "ore", "type": "magnetiteore", "x": 100.0, "y": 0.0, "z": 0.0, "observedAt": NOW,
                         "fill": None, "size": None, "rowKey": None, "distCm": None}],
            "objects": [pin("p1", 7200)]}
    profile = {"status": None, "skills": None, "stats": None, "recipes": None, "landsraad": None, "achievements": None, "session": None}
    statements: list[str] = []
    st._conn.set_trace_callback(statements.append)
    assert api.sync(key, body, "127.0.0.1", NOW)[0] == 200
    assert api.sync(key, body, "127.0.0.1", NOW)[0] == 200  # the update path of the sighting too
    assert api.profile(key, {"identity": WHO, "profile": profile}, "127.0.0.1", NOW)[0] == 200
    assert api.profiles(key, {"identity": WHO, "since": 0}, "127.0.0.1", NOW)[0] == 200
    st._conn.set_trace_callback(None)
    queries = [s for s in statements if s.split()[0].upper() in ("SELECT", "UPDATE", "DELETE") or " WHERE " in s]
    assert queries
    for sql in queries:
        plan = [row[3] for row in st._conn.execute("EXPLAIN QUERY PLAN " + sql)]
        assert not [p for p in plan if p.startswith("SCAN")], (sql, plan)
    st.close()


def pin(oid: str, ttl: int) -> dict:
    return {"id": oid, "kind": "pin", "scope": {"worldId": HAGGA, "layout": HAGGA},
            "geometry": {"type": "point", "x": 100.0, "y": 0.0, "z": 0.0}, "body": {"label": "IRON HERE"}, "ttlS": ttl}


def test_a_restart_refetch_of_objects_is_bounded_by_the_caps(tmp_path):
    st = Storage.open(str(tmp_path / "guild.db"))
    api = Api(st, lambda: NOW)

    def sync(n: int, since: int = 0, objects: list | None = None) -> dict:
        who = {"characterName": f"Alpha Test {n}", "characterId": 3000 + n, "battlegroupId": GROUP}
        body = {"since": since, "sentAt": NOW, "identity": who, "partition": {"worldId": HAGGA, "layout": HAGGA},
                "position": None, "records": [], **({"objects": objects} if objects else {})}
        status, resp = api.sync({"id": keys[n]}, body, "127.0.0.1", NOW)
        assert status == 200, resp
        return resp

    keys = [st.issue_key(f"Alpha Test {n}")[0] for n in range(17)]
    for n in range(15):  # every key at its live cap: the battlegroup at its cap
        sync(n, objects=[pin(f"p{i}", 7200) for i in range(20)])
    for batch in range(5):  # a sixteenth key's 100 deletes: its row ceiling
        sync(15, objects=[pin(f"d{batch}_{i}", 0) for i in range(20)])
    since, requests, got, size = 0, 0, Counter(), 0
    while True:
        resp = sync(16, since)
        requests += 1
        got.update((o["playerTag"], o["id"]) for o in resp["objects"])
        size += len(json.dumps(resp["objects"]))
        since = resp["next"]
        if len(resp["objects"]) < 200:
            break
    assert requests <= 3 and len(got) == 400 and set(got.values()) == {1}
    assert size < 300_000
    st.close()
