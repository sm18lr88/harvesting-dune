"""Shared objects end to end: two detectors and `python -m guildsync` over TLS, from a pin action to the
other minimap's ring and strokes; other kinds need no server change; delete and revoke reach the other
detector within one poll; a pin waits for its map; a server without objects keeps the sync flowing."""

import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

import guildsync.objects
import pytest
from guildsync import keysctl
from guildsync.api import Api
from guildsync.httpd import make_server
from guildsync.problems import invalid
from guildsync.storage import Storage
from helpers import pose, session
from test_bridge import draw
from test_robustness import free_port
from test_sync_integration import ALPHA, OTHER, Player, Stop, assert_contract_request, issue, ok_response_keys

from harvesting_dune import objects, syncloop
from harvesting_dune.sync import SyncRejected

ROOT = Path(__file__).parent.parent
CERTS = ROOT / "tests" / "certs"
A_WHO = {"characterName": "Alpha Test", "characterId": 1001, "battlegroupId": ALPHA}
B_WHO = {"characterName": "Beta Test", "characterId": 1002, "battlegroupId": ALPHA}
C_WHO = {"characterName": "Gamma Test", "characterId": 2001, "battlegroupId": OTHER}
A_AT, B_AT = (20_000.0, 5_000.0), (100_000.0, 0.0)


@pytest.fixture
def guild_process(tmp_path):
    """The guild server as it runs live: its own process, TLS with the test certificates, a free port."""
    db = str(tmp_path / "guildsync.sqlite")
    (tmp_path / "datasets").mkdir()
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-m", "guildsync", "--db", db, "--cert", str(CERTS / "test-server.pem"),
                             "--key", str(CERTS / "test-server.key"), "--host", "127.0.0.1", "--port", str(port)],
                            cwd=ROOT / "server", stdin=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    ready, lines = threading.Event(), []

    def read_stderr() -> None:
        for line in proc.stderr:
            lines.append(line.rstrip())
            if "listening on" in line:
                ready.set()

    threading.Thread(target=read_stderr, daemon=True).start()
    try:
        assert ready.wait(20), lines
        yield f"https://127.0.0.1:{port}", db
    finally:
        proc.terminate()
        proc.wait(10)
        print("guildsync stderr tail:", *lines[-5:], sep="\n")


@pytest.fixture
def guild(guild_process, capsys, monkeypatch, tmp_path):
    """Players on the guild process; each sync_once is one pass of the real loop, and pins are not paced."""
    async def no_wait(seconds, wake):
        raise Stop

    monkeypatch.setattr(syncloop, "nap", no_wait)
    monkeypatch.setattr(objects, "PUBLISH_GAP_S", 0.0)
    url, db = guild_process
    t0 = time.time()

    def player(who: dict, at: tuple[float, float]) -> Player:
        key = issue(capsys, db, who["characterName"])
        p = Player(tmp_path / who["characterName"], url, key["token"], who, t0)
        p.key_id = key["keyId"]
        pose(p.det, *at, t=t0 + 1)
        return p

    return player, url, db


def known(p: Player) -> Player:
    """The first sync of a process sends no objects; after it the player knows the server takes them."""
    p.sync_once()
    assert p.det.peers.objects_seen
    return p


def checked(*players: Player) -> None:
    for p in players:
        for body, answer in p.calls:
            assert_contract_request(body)
            assert isinstance(answer, SyncRejected) or frozenset(answer) in ok_response_keys()


def test_a_pin_travels_from_one_detector_to_the_other_and_is_drawn_as_a_ring_and_strokes(guild):
    player, _, _ = guild
    a, b, c = player(A_WHO, A_AT), player(B_WHO, B_AT), player(C_WHO, A_AT)
    a.det.sync_positions = b.det.sync_positions = False  # only the pin is a ring on these maps
    assert a.det.publish_object("pin", {"label": "IRON HERE"})
    a.sync_once()
    assert "objects" not in a.calls[-1][0] and len(a.det.peers.objects_out) == 1  # the first answer of the process
    a.sync_once()
    assert [u["body"] for u in a.calls[-1][0]["objects"]] == [{"label": "IRON HERE"}] and a.det.peers.objects_out == []
    b.sync_once()
    [seen] = b.det.page_state()["objects"]
    assert (seen["kind"], seen["body"], seen["mine"], seen["by"]) == ("pin", {"label": "IRON HERE"}, False, "Alpha Test")
    assert (seen["x"], seen["y"]) == A_AT
    [own] = a.det.page_state()["objects"]
    assert own["mine"] is True and own["id"] == seen["id"]
    drawn = draw(b.det.page_state())
    assert len([p for p in drawn if p[0] == 33]) == 1 and len(drawn) - 1 > 8
    assert c.sync_once()["objects"] == []  # another battlegroup
    checked(a, b, c)


def test_openness_a_base_and_a_positionless_note_pass_with_zero_server_change(guild):
    player, _, _ = guild
    a, b = known(player(A_WHO, A_AT)), player(B_WHO, B_AT)
    b.det.sync_positions = False
    assert a.det.publish_object("pin", {"label": "IRON HERE"})
    assert a.det.publish_object("base", {"name": "Sietch Test"})
    assert a.det.publish_object("note", {"text": "meet at the blow"}, geometry=None, scope=None)
    a.sync_once()
    b.sync_once()
    assert len(b.det.peers.objects) == 3 and b.det.snapshot()["peers"]["objects"]["all"] == 3
    state = b.det.page_state()
    assert sorted(o["kind"] for o in state["objects"]) == ["base", "pin"]
    pin_only = {**state, "objects": [o for o in state["objects"] if o["kind"] == "pin"]}
    assert len(draw(state)) == len(draw(pin_only)) > 1
    assert not any(name.endswith("KINDS") for name in dir(guildsync.objects))
    checked(a, b)


def test_two_keys_may_use_the_same_pin_id(guild):
    player, _, _ = guild
    a, b = known(player(A_WHO, A_AT)), known(player(B_WHO, B_AT))
    pin_id = a.det.publish_object("pin", {"label": "IRON HERE"})
    a.sync_once()
    assert b.det.publish_object("pin", {"label": "SAND HERE"})
    b.det.peers.objects_out[0]["id"] = pin_id
    b.sync_once()
    a.sync_once()
    marks = a.det.page_state()["objects"]
    assert sorted(m["id"].split(":")[1] for m in marks) == [pin_id, pin_id]
    assert len({m["id"].split(":")[0] for m in marks}) == 2
    checked(a, b)


def test_a_deleted_pin_leaves_the_other_detector_within_one_poll(guild):
    player, _, _ = guild
    a, b = known(player(A_WHO, A_AT)), player(B_WHO, B_AT)
    pin_id = a.det.publish_object("pin", {"label": "IRON HERE"})
    a.sync_once()
    b.sync_once()
    assert len(b.det.page_state()["objects"]) == 1
    assert a.det.delete_object(pin_id)
    a.sync_once()
    assert a.calls[-1][0]["objects"][0]["ttlS"] == 0
    b.sync_once()
    assert b.det.page_state()["objects"] == [] and b.det.peers.objects == {}
    checked(a, b)


def test_a_revoked_members_pins_leave_the_other_detector_within_one_poll(guild, capsys):
    player, _, db = guild
    a, b = player(A_WHO, A_AT), known(player(B_WHO, B_AT))
    assert b.det.publish_object("pin", {"label": "IRON HERE"})
    b.sync_once()
    a.sync_once()
    assert len(a.det.peers.objects) == 1
    capsys.readouterr()
    assert keysctl.main(["--db", db, "revoke", b.key_id]) == 0
    a.sync_once()
    assert a.det.peers.objects == {} and a.det.page_state()["objects"] == []
    assert isinstance(b.sync_once(), SyncRejected)
    checked(a, b)


def test_a_pin_dropped_in_another_layout_appears_when_the_reader_moves_there(guild):
    player, _, _ = guild
    a, b = known(player(A_WHO, A_AT)), player(B_WHO, B_AT)
    session(b.det, "deepdesert_1")
    b.det.map_files[b.det.store.world] = "deepdesert_1_07"
    assert a.det.publish_object("pin", {"label": "IRON HERE"})
    a.sync_once()
    b.sync_once()
    assert b.calls[-1][0]["partition"] == {"worldId": "deepdesert_1", "layout": "deepdesert_1_07"}
    assert len(b.det.peers.objects) == 1 and b.det.page_state()["objects"] == []
    calls = len(b.calls)
    session(b.det, "survival_1")
    assert [o["body"] for o in b.det.page_state()["objects"]] == [{"label": "IRON HERE"}] and len(b.calls) == calls


class OldApi(Api):
    """The server before objects: it refuses the field and never answers the key."""

    def sync(self, key, body, ip, now):
        if isinstance(body, dict) and "objects" in body:
            return 400, invalid("objects")
        status, resp = super().sync(key, body, ip, now)
        resp.pop("objects", None)
        return status, resp


def test_a_server_without_objects_keeps_presence_and_finds_flowing(guild, tmp_path):
    player, url, db = guild
    storage = Storage.open(db)
    old = make_server(storage, str(CERTS / "test-server.pem"), str(CERTS / "test-server.key"), "127.0.0.1",
                      free_port(), OldApi(storage, time.time))
    threading.Thread(target=old.serve_forever, daemon=True).start()
    try:
        a, b = player(A_WHO, A_AT), player(B_WHO, B_AT)
        a.runner.sync.server = b.runner.sync.server = f"https://127.0.0.1:{old.server_address[1]}"
        b.sync_once()
        assert a.det.publish_object("pin", {"label": "IRON HERE"})
        for _ in range(3):
            resp = a.sync_once()
            assert isinstance(resp, dict) and "objects" not in a.calls[-1][0] and "objects" not in resp
        assert [p["characterName"] for p in resp["presence"]] == ["Beta Test"]
        assert not a.det.peers.objects_seen and len(a.det.peers.objects_out) == 1
    finally:
        old.shutdown()
        old.server_close()
        storage.close()
    a.runner.sync.server = url
    a.sync_once()
    assert "objects" not in a.calls[-1][0] and a.det.peers.objects_seen
    a.sync_once()
    assert [u["kind"] for u in a.calls[-1][0]["objects"]] == ["pin"] and a.det.peers.objects_out == []
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT kind, name FROM objects").fetchall() == [("pin", "Alpha Test")]
    older = {k - {"objects"} for k in ok_response_keys()}  # the previous contract: every key but objects
    for body, answer in b.calls + a.calls:
        assert_contract_request(body)
        assert frozenset(answer) in ok_response_keys() | older
