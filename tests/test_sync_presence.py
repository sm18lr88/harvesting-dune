"""Guild presence lifecycle (docs/SYNC-PROTOCOL.md "Presence"), through the real TLS server with its
clock injected: a member is online while it keeps its heartbeat's promise, late after the deadline,
offline after two more intervals or at once on a goodbye, gone 15 minutes later, and back online on
its next sync. Each detector pass ends at the loop's first wait, which is never slept."""

import asyncio
import json
import threading
import urllib.request
from pathlib import Path

import pytest
from guildsync.api import Api
from guildsync.httpd import make_server
from guildsync.presence import GRACE_S, OFFLINE_KEEP_S
from guildsync.storage import Storage
from helpers import pose, session
from test_robustness import free_port
from test_sync_integration import ALPHA, Player, Stop, issue

from harvesting_dune import syncloop
from harvesting_dune.app import Detector
from harvesting_dune.feed import BusMessage

HERE = Path(__file__).parent
T0 = 1794160000.0
PAUL = {"characterName": "Paul Test", "characterId": 1001, "battlegroupId": ALPHA}
CHANI = {"characterName": "Chani Test", "characterId": 1002, "battlegroupId": ALPHA}
A_AT = (0.0, 0.0)


@pytest.fixture
def guild(tmp_path, monkeypatch):
    """The real server whose clock is `clock[0]`; (url, db path, clock, storage)."""
    async def no_wait(seconds, wake):
        raise Stop

    monkeypatch.setattr(syncloop, "nap", no_wait)
    clock = [T0]
    storage = Storage.open(str(tmp_path / "guildsync.sqlite"))
    now = lambda: clock[0]  # noqa: E731
    server = make_server(storage, str(HERE / "certs" / "test-server.pem"), str(HERE / "certs" / "test-server.key"),
                         "127.0.0.1", free_port(), Api(storage, now), now=now)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"https://127.0.0.1:{server.server_address[1]}", str(tmp_path / "guildsync.sqlite"), clock, storage
    server.shutdown()
    server.server_close()
    storage.close()


def run_pass(p: Player) -> list[tuple[dict, object]]:
    """One pass of the live sync loop; the sync calls it made."""
    before = len(p.calls)
    with pytest.raises(Stop):
        asyncio.run(p.runner.sync_loop())
    return p.calls[before:]


def roster(p: Player) -> dict[str, str]:
    """The guild as this player's roster shows it, without the player's own "you" row."""
    return {m["name"]: m["state"] for m in p.det.peers_snapshot()["members"] if not m["self"]}


def ring(p: Player, at: tuple[float, float]) -> str | None:
    """The colour of the member ring drawn at `at`, or None."""
    rings = [m["color"] for m in p.det.page_state()["alternatives"] if m["kind"] == "member" and (m["x"], m["y"]) == at]
    return rings[0] if rings else None


def row(p: Player, name: str) -> dict:
    [r] = [r for r in p.calls[-1][1]["presence"] if r["characterName"] == name]
    return r


@pytest.fixture
def pair(tmp_path, guild, capsys):
    url, db, clock, storage = guild
    keys = {n: issue(capsys, db, n) for n in ("Paul", "Chani")}
    a = Player(tmp_path / "a", url, keys["Paul"]["token"], PAUL, T0)
    b = Player(tmp_path / "b", url, keys["Chani"]["token"], CHANI, T0)
    pose(a.det, *A_AT, t=T0 + 1)
    pose(b.det, 100_000, 0, t=T0 + 1)
    return a, b, clock, keys


def test_a_member_is_online_then_late_then_offline_then_gone_then_back(pair):
    a, b, clock, _ = pair
    for k in range(4):  # 1. A syncs every 15 s: B sees it online with a full ring
        clock[0] = T0 + 15 * k
        run_pass(a)
        run_pass(b)
        assert roster(b)["Paul Test"] == "online" and ring(b, A_AT) == "rgba(255,230,120,0.75)"
    last, promised = clock[0], row(b, "Paul Test")["lastNextS"]
    assert promised == a.calls[-1][0]["presence"]["nextS"] == 60

    clock[0] = last + promised + GRACE_S  # 2. A stops without a goodbye
    run_pass(b)
    assert roster(b)["Paul Test"] == "online"
    clock[0] += 1
    run_pass(b)
    late = ring(b, A_AT)
    assert roster(b)["Paul Test"] == "late" and late is not None and float(late.rsplit(",", 1)[1][:-1]) < 0.75
    [paul] = [m for m in b.det.peers_snapshot()["members"] if m["name"] == "Paul Test"]
    assert paul["ageS"] == round(promised + GRACE_S + 1)  # its age, from the server
    clock[0] = last + 3 * promised + GRACE_S + 1
    run_pass(b)
    assert roster(b)["Paul Test"] == "offline" and ring(b, A_AT) is None
    clock[0] += OFFLINE_KEEP_S
    run_pass(b)
    assert "Paul Test" not in roster(b) and ring(b, A_AT) is None

    run_pass(a)  # 4. A comes back: online on B's next poll, no re-join step
    run_pass(b)
    assert roster(b)["Paul Test"] == "online" and ring(b, A_AT) == "rgba(255,230,120,0.75)"


def test_a_goodbye_takes_the_member_offline_on_the_next_poll(pair, tmp_path):
    a, b, clock, _ = pair
    run_pass(a)
    run_pass(b)
    a.det.ui("sync_pause")  # 3. leaving: B's next poll shows it offline
    [(bye, _)] = run_pass(a)
    assert bye["presence"]["leaving"] is True
    run_pass(b)
    assert roster(b)["Paul Test"] == "offline" and ring(b, A_AT) is None
    a.det.ui("sync_pause")
    run_pass(a)
    run_pass(b)
    assert roster(b)["Paul Test"] == "online"

    b.det.ui("sync_member", "1001")  # 7. hidden by me stays hidden through offline and back
    assert ring(b, A_AT) is None
    a.det.ui("sync_pause")
    run_pass(a)
    a.det.ui("sync_pause")
    run_pass(b)
    assert roster(b)["Paul Test"] == "offline"
    run_pass(a)
    run_pass(b)
    [paul] = [m for m in b.det.peers_snapshot()["members"] if m["name"] == "Paul Test"]
    assert (paul["state"], paul["shown"], ring(b, A_AT)) == ("online", False, None)
    assert Detector(None, data_dir=tmp_path / "b", min_rescore_s=0).sync_hidden_members == {1001}


def test_position_off_key_removed_and_shutdown_each_say_goodbye(pair):
    a, b, clock, _ = pair
    run_pass(a)
    a.det.ui("sync_positions")  # the goodbye drops the ring, the sync right after keeps A listed without one
    (bye, _), (back, _) = run_pass(a)
    assert bye["presence"]["leaving"] and back["position"] is None and not back["presence"]["leaving"]
    run_pass(b)
    assert roster(b)["Paul Test"] == "online" and ring(b, A_AT) is None

    key = a.det.sync_key
    a.det.set_sync_key("")  # the old key says goodbye; then nothing more goes out
    [(bye, _)] = run_pass(a)
    assert bye["presence"]["leaving"] and run_pass(a) == []
    run_pass(b)
    assert roster(b)["Paul Test"] == "offline"

    a.det.set_sync_key(key)
    run_pass(a)
    syncloop.goodbye(a.det, a.runner.sync)  # what Runner.run does when the companion closes
    assert a.calls[-1][0]["presence"]["leaving"]
    run_pass(b)
    assert roster(b)["Paul Test"] == "offline"


def test_a_member_in_a_menu_is_listed_without_a_ring(pair):
    a, b, clock, _ = pair
    run_pass(a)
    session(a.det, None)  # 5. a loading screen or menu: the heartbeat goes on from the last place
    [(beat, _)] = run_pass(a)
    assert beat["presence"]["inGame"] is False and beat["position"] is None and beat["records"] == []
    run_pass(b)
    assert roster(b)["Paul Test"] == "in_menu" and ring(b, A_AT) is None and row(b, "Paul Test")["lastNextS"] == 180


def test_an_old_client_without_presence_still_syncs(pair):
    a, b, clock, keys = pair
    body = {"since": 0, "sentAt": T0, "identity": PAUL, "partition": {"worldId": "survival_1", "layout": "survival_1"},
            "position": {"x": 0.0, "y": 0.0, "z": 0.0, "observedAt": T0}, "records": []}  # 6. no `presence`
    req = urllib.request.Request(f"{a.runner.sync.server}/v1/sync", data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {keys['Paul']['token']}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, context=a.runner.sync.ctx, timeout=10) as resp:
        assert resp.status == 200
    run_pass(b)
    assert roster(b)["Paul Test"] == "online" and row(b, "Paul Test")["lastNextS"] == 60
    clock[0] += 60 + GRACE_S + 1
    run_pass(b)
    assert roster(b)["Paul Test"] == "late"


def test_presence_costs_under_60_bytes_and_no_request(pair):
    a, b, clock, _ = pair
    for k in range(3):
        clock[0] = T0 + 60 * k
        assert len(run_pass(a)) == 1  # 8. one request per sync, as before
    for body, _ in a.calls:
        plain = {k: v for k, v in body.items() if k != "presence"}
        grown = len(json.dumps(body, separators=(",", ":"))) - len(json.dumps(plain, separators=(",", ":")))
        assert 0 < grown <= 60, grown


# -- edge cases (spec section 7) ---------------------------------------------------------------
HAGGA = {"worldId": "survival_1", "layout": "survival_1"}
DD = {"worldId": "deepdesert_1", "layout": "deepdesert_1_07"}


def sync_body(who=PAUL, part=HAGGA, sent_at=T0, **beat) -> dict:
    return {"since": 0, "sentAt": sent_at, "identity": who, "partition": part,
            "position": {"x": 0.0, "y": 0.0, "z": 0.0, "observedAt": sent_at}, "records": [],
            "presence": {"nextS": 60, "inGame": True, "leaving": False, **beat}}


def seen_by_chani(api: Api, key: dict, now: float) -> dict[str, dict]:
    status, reply = api.sync(key, sync_body(CHANI), "127.0.0.1", now)
    assert status == 200
    return {p["characterName"]: p for p in reply["presence"]}


@pytest.fixture
def store(tmp_path):
    st = Storage.open(str(tmp_path / "g.db"))
    yield st, {"id": st.issue_key("Paul")[0]}, {"id": st.issue_key("Chani")[0]}
    st.close()


def test_states_come_from_server_time_only_and_survive_a_restart(tmp_path, store):
    st, paul, chani = store
    api = Api(st, lambda: T0)
    assert api.sync(paul, sync_body(sent_at=T0 - 86_400, nextS=15), "127.0.0.1", T0)[0] == 200  # a client clock a day off
    assert seen_by_chani(api, chani, T0 + 15 + GRACE_S)["Paul Test"]["state"] == "online"
    assert seen_by_chani(api, chani, T0 + 15 + GRACE_S + 1)["Paul Test"]["state"] == "late"
    st.close()
    st = Storage.open(str(tmp_path / "g.db"))  # a server restart: nothing kept in memory
    api = Api(st, lambda: T0)
    assert seen_by_chani(api, chani, T0 + 15 + GRACE_S + 30)["Paul Test"]["state"] == "late"
    assert seen_by_chani(api, chani, T0 + 15 + GRACE_S + 31)["Paul Test"]["state"] == "offline"
    st.close()


@pytest.mark.parametrize(("beat", "field"), [
    ({"inGame": 1}, "presence.inGame"), ({"leaving": "true"}, "presence.leaving"), ({"nextS": 1.5}, "presence.nextS"),
    ({"nextS": True}, "presence.nextS"), ({"extra": 1}, "presence.extra"),
])
def test_presence_fields_are_strict(store, beat, field):
    st, paul, _ = store
    status, problem = Api(st, lambda: T0).sync(paul, sync_body(**beat), "127.0.0.1", T0)
    assert (status, problem["field"]) == (400, field)
    assert st.presence_for(ALPHA, T0) == []  # a refused request stores nothing


@pytest.mark.parametrize(("sent", "stored"), [(1, 5.0), (15, 15.0), (10**9, 600.0)])
def test_next_s_is_clamped(store, sent, stored):
    st, paul, chani = store
    api = Api(st, lambda: T0)
    assert api.sync(paul, sync_body(nextS=sent), "127.0.0.1", T0)[0] == 200
    assert seen_by_chani(api, chani, T0)["Paul Test"]["lastNextS"] == stored


def test_a_null_presence_is_refused(store):
    st, paul, _ = store
    status, problem = Api(st, lambda: T0).sync(paul, {**sync_body(), "presence": None}, "127.0.0.1", T0)
    assert (status, problem["field"]) == (400, "presence")


def test_another_key_cannot_heartbeat_for_a_character(store):
    st, paul, chani = store
    api = Api(st, lambda: T0)
    assert api.sync(paul, sync_body(), "127.0.0.1", T0)[0] == 200
    assert api.sync(chani, sync_body(PAUL, leaving=True), "127.0.0.1", T0 + 5)[0] == 403
    assert seen_by_chani(api, chani, T0 + 10)["Paul Test"]["state"] == "online"


def test_a_revoked_key_goes_offline_at_once(store):
    st, paul, chani = store
    api = Api(st, lambda: T0)
    assert api.sync(paul, sync_body(), "127.0.0.1", T0)[0] == 200
    assert st.revoke_key(paul["id"], T0 + 1) == "revoked"
    assert seen_by_chani(api, chani, T0 + 2)["Paul Test"]["state"] == "offline"


def test_offline_rows_stay_15_minutes_then_the_sweep_deletes_them(store):
    st, paul, chani = store
    api = Api(st, lambda: T0)
    assert api.sync(paul, sync_body(leaving=True), "127.0.0.1", T0)[0] == 200
    assert st.sweep(T0 + OFFLINE_KEEP_S)["presence"] == 0
    assert seen_by_chani(api, chani, T0 + OFFLINE_KEEP_S)["Paul Test"]["state"] == "offline"
    assert "Paul Test" not in seen_by_chani(api, chani, T0 + OFFLINE_KEEP_S + 1)
    assert st.sweep(T0 + OFFLINE_KEEP_S + 1)["presence"] == 1


def viewer(world="deepdesert_1", layout="deepdesert_1_07") -> Detector:
    det = Detector(None, min_rescore_s=0)
    det.set_sync_key("gs1_alpha001_Qm9vdHN0cmFwVGVzdEtleUFscGhhMDAx")
    session(det, world)
    det.map_files[det.store.world] = layout
    det.handle(BusMessage("delta", "dune.trackedSession", CHANI), 0)
    pose(det, 0, 0, t=1)
    return det


def test_a_member_switching_world_stays_online_and_leaves_this_minimap(store):
    st, paul, chani = store
    api = Api(st, lambda: T0)
    api.sync(paul, sync_body(part=DD), "127.0.0.1", T0)
    det = viewer()
    det.ingest_peer(api.sync(chani, sync_body(CHANI, part=DD), "127.0.0.1", T0)[1], T0)
    assert [m["kind"] for m in det.page_state()["alternatives"]] == ["member"]
    api.sync(paul, sync_body(part=HAGGA), "127.0.0.1", T0 + 15)
    det.ingest_peer(api.sync(chani, sync_body(CHANI, part=DD), "127.0.0.1", T0 + 16)[1], T0 + 16)
    me, paul_row = det.peers_snapshot()["members"]
    assert (paul_row["state"], paul_row["worldId"]) == ("online", "survival_1")
    assert det.page_state()["alternatives"] == []  # another map: listed, never drawn here


def member_row(cid, name, state, age=5.0, x=1000.0) -> dict:
    return {"characterName": name, "characterId": cid, "battlegroupId": ALPHA, "worldId": "deepdesert_1",
            "layout": "deepdesert_1_07", "x": x, "y": 0.0, "z": 0.0, "ageS": age, "keyId": "beta0002",
            "state": state, "lastNextS": 60.0}


def reply(*rows) -> dict:
    return {"now": T0, "next": 0, "keyState": "valid", "presence": list(rows), "sightings": []}


def test_my_own_row_is_never_drawn_and_always_you():
    det = viewer()
    det.ingest_peer(reply(member_row(1002, "Chani Test", "online"), member_row(1001, "Paul Test", "online", x=2000.0)), T0)
    assert [(m["x"], m["kind"]) for m in det.page_state()["alternatives"]] == [(2000.0, "member")]
    rows = det.peers_snapshot()["members"]
    assert [(r["name"], r["state"], r["self"]) for r in rows] == [("Chani Test", "you", True), ("Paul Test", "online", False)]


def test_after_my_own_outage_rows_follow_the_server_state_not_my_gap():
    det = viewer()
    det.clock += 3000.0  # we heard nothing for 50 minutes
    det.ingest_peer(reply(member_row(1001, "Paul Test", "online", age=2.0, x=1000.0),
                          member_row(1003, "Leto Test", "late", age=120.0, x=2000.0),
                          member_row(1004, "Feyd Test", "offline", age=1000.0, x=3000.0),  # left 15 min window: still listed
                          member_row(1005, "Irulan Test", "in_menu", age=3.0, x=None)), T0)
    drawn = {m["x"]: m["color"] for m in det.page_state()["alternatives"]}
    assert drawn[1000.0] == "rgba(255,230,120,0.75)" and float(drawn[2000.0].rsplit(",", 1)[1][:-1]) < 0.75
    assert set(drawn) == {1000.0, 2000.0}
    states = [(r["name"], r["state"]) for r in det.peers_snapshot()["members"]]
    assert states == [("Chani Test", "you"), ("Paul Test", "online"), ("Irulan Test", "in_menu"), ("Leto Test", "late"), ("Feyd Test", "offline")]
    det.ingest_peer(reply(member_row(1001, "Paul Test", "online", age=2.0)), T0 + 15)
    assert [r["name"] for r in det.peers_snapshot()["members"]] == ["Chani Test", "Paul Test"]  # gone rows leave on their own


def test_characters_list_follows_presence_order_and_labels():
    det = viewer()
    det.ingest_peer(reply(member_row(1004, "Feyd Test", "offline"), member_row(1001, "Paul Test", "late"),
                          member_row(1003, "Leto Test", "online")), T0)
    rows = det.characters_snapshot()["rows"]
    assert [(r["name"], r["state"]) for r in rows] == [
        ("Chani Test", "you"), ("Leto Test", "online"), ("Paul Test", "late"), ("Feyd Test", "offline")]


# -- one key, two characters on one PC (Leto Test, then Leto Alt), seen by a guildmate -------------------
MAIN = {"characterName": "Leto Test", "characterId": 10000001, "battlegroupId": ALPHA}
ALT = {"characterName": "Leto Alt", "characterId": 10000002, "battlegroupId": ALPHA}


def stored_profiles(storage: Storage) -> dict[int, tuple[str, list]]:
    """The owner's two characters' stored profiles: (key id, skills)."""
    rows = storage._conn.execute("SELECT character_id, key_id, body FROM profiles WHERE character_id IN (?, ?)",
                                 (MAIN["characterId"], ALT["characterId"])).fetchall()
    return {r[0]: (r[1], json.loads(r[2])["skills"]["skills"]) for r in rows}


def test_a_character_switch_on_one_key_shows_two_members_and_keeps_each_profile(tmp_path, guild, capsys):
    url, db, clock, storage = guild
    keys = {n: issue(capsys, db, n) for n in ("Owner", "Chani")}
    o = Player(tmp_path / "o", url, keys["Owner"]["token"], MAIN, T0)
    b = Player(tmp_path / "b", url, keys["Chani"]["token"], CHANI, T0)
    pose(o.det, *A_AT, t=T0 + 1)
    o.det.handle(BusMessage("snapshot", "dune.skillProgress", {"spentByTag": {"Skill.Main.Only": 3}}), T0 + 1)
    run_pass(o)
    run_pass(b)
    assert roster(b) == {"Leto Test": "online"} and stored_profiles(storage)[MAIN["characterId"]][1] == [["Skill.Main.Only", 3]]

    clock[0] += 30  # the main quits; the alt starts on the other Steam account (no topicReset)
    o.det.handle(BusMessage("delta", "dune.status", {"hasGameSession": False}), T0 + 30)
    o.det.handle(BusMessage("delta", "dune.status", {"hasGameSession": True}), T0 + 40)
    o.det.handle(BusMessage("snapshot", "dune.skillProgress", {"spentByTag": {"Skill.Alt.Only": 1}}), T0 + 40)
    o.det.handle(BusMessage("delta", "dune.trackedSession", {"battlegroupId": ALPHA, "activeSeconds": 1}), T0 + 41)
    [(bye, _)] = run_pass(o)  # the main's goodbye, and nothing under a half-known identity
    assert bye["identity"] == MAIN and bye["presence"]["leaving"]
    o.det.handle(BusMessage("delta", "dune.trackedSession", {"characterName": "Leto Alt", "battlegroupId": ALPHA}), T0 + 42)
    assert run_pass(o) == []
    o.det.handle(BusMessage("delta", "dune.trackedSession", ALT), T0 + 43)
    pose(o.det, *A_AT, t=T0 + 43)
    [(first, _)] = run_pass(o)
    assert first["identity"] == ALT

    assert {tuple(sorted(body["identity"].items())) for body, _ in o.calls} == {
        tuple(sorted(MAIN.items())), tuple(sorted(ALT.items()))}  # never a mixed pair
    profiles = stored_profiles(storage)
    owner = keys["Owner"]["keyId"]
    assert profiles == {MAIN["characterId"]: (owner, [["Skill.Main.Only", 3]]),
                        ALT["characterId"]: (owner, [["Skill.Alt.Only", 1]])}  # one key owns both

    run_pass(b)
    assert roster(b) == {"Leto Alt": "online", "Leto Test": "offline"}
    b.det.characters_opened()
    run_pass(b)
    rows = b.det.characters_snapshot()["rows"]
    assert [(r["name"], r["state"], r["player"]) for r in rows] == [
        ("Chani Test", "you", ""), ("Leto Alt", "online", "Leto Alt · Leto Test"), ("Leto Test", "offline", "Leto Alt · Leto Test")]  # one player

    clock[0] += OFFLINE_KEEP_S + 1  # the main ages out; the alt keeps playing
    run_pass(o)
    run_pass(b)
    assert roster(b) == {"Leto Alt": "online"}



def test_no_response_ever_carries_a_keys_admin_label(tmp_path, guild, capsys):
    url, db, clock, storage = guild
    label = "Real Name Label"
    keys = {"a": issue(capsys, db, label), "b": issue(capsys, db, "Another " + label)}
    a = Player(tmp_path / "a", url, keys["a"]["token"], PAUL, T0)
    b = Player(tmp_path / "b", url, keys["b"]["token"], CHANI, T0)
    for p in (a, b):
        p.det.handle(BusMessage("snapshot", "dune.skillProgress", {"spentByTag": {"Skills.Ability.CablePull": 1}}), T0)
        p.det.characters_opened()
        run_pass(p)
        run_pass(p)
    answers = [resp for p in (a, b) for _, resp in p.calls] + [resp for p in (a, b) for _, _, resp in p.profile_calls]
    assert {path for p in (a, b) for path, _, _ in p.profile_calls} == {"/v1/profile", "/v1/profiles"}
    assert answers and not [r for r in answers if label in json.dumps(r)]


def test_one_key_plays_any_number_of_characters_and_they_share_one_tag(store):
    st, paul, chani = store
    api = Api(st, lambda: T0)
    alts = [{"characterName": f"Paul Alt {n}", "characterId": 5000 + n, "battlegroupId": ALPHA} for n in range(4)]
    blank = {"status": None, "skills": None, "stats": None, "recipes": None, "landsraad": None, "achievements": None, "session": None}
    for who in alts:
        assert api.sync(paul, sync_body(who), "127.0.0.1", T0)[0] == 200
        assert api.profile(paul, {"identity": who, "profile": blank}, "127.0.0.1", T0)[0] == 200
    assert api.profile(chani, {"identity": CHANI, "profile": blank}, "127.0.0.1", T0)[0] == 200
    status, reply = api.profiles(chani, {"identity": CHANI, "since": 0}, "127.0.0.1", T0)
    tags = {p["characterName"]: p["playerTag"] for p in reply["profiles"]}
    assert status == 200 and len({tags[w["characterName"]] for w in alts}) == 1
    assert tags["Chani Test"] != tags["Paul Alt 0"] and paul["id"] not in json.dumps(reply)



def test_records_staged_by_one_character_never_go_out_as_another():
    from harvesting_dune.sync import SyncClient

    client, part = SyncClient(server="https://127.0.0.1:9"), {"worldId": "survival_1", "layout": "survival_1"}
    seen = {"cls": "ore", "type": "magnetiteore", "x": 0.0, "y": 0.0, "z": 0.0, "observedAt": T0,
            "fill": None, "size": None, "rowKey": None, "distCm": None}
    assert client.stage([seen], (1001, part)) == [seen]  # sent, not acknowledged yet
    assert client.stage([], (1001, part)) == [seen]
    assert client.stage([], (1002, part)) == []  # another character: dropped, not sent as its own
