"""Two detectors through the real guild server over TLS: what A sees reaches B (rings, roster,
live ore with its age), a revoked key stops on its next call, another battlegroup sees nothing, every body on the
wire has the shape of tests/fixtures/sync-contract.json, and no key reaches a log, snapshot or page."""

import asyncio
import json
import logging
import threading
import time
from pathlib import Path

import pytest
from guildsync import keysctl
from guildsync.api import Api
from guildsync.httpd import make_server
from guildsync.storage import Storage
from helpers import actors, ore, pose, session, spice
from test_robustness import free_port

from harvesting_dune import logsetup, syncloop
from harvesting_dune.app import Detector
from harvesting_dune.feed import BusMessage, CompanionHub
from harvesting_dune.runner import Runner
from harvesting_dune.sync import SyncClient, SyncRejected

HERE = Path(__file__).parent
CA = HERE / "certs" / "test-ca.pem"
CONTRACT = json.loads((HERE / "fixtures" / "sync-contract.json").read_text(encoding="utf-8"))
SYNC_CASES = [c for c in CONTRACT["cases"] if c["request"]["path"] == "/v1/sync" and "body" in c["request"]]
HAGGA = "survival_1"
ALPHA, OTHER = "prd-srv-test-alpha-erg", "prd-srv-test-other-erg"
BLOW_AT, SHIP_AT, ORE_AT = (40_000.0, 0.0), (0.0, 60_000.0), (20_000.0, 5_000.0)


class Stop(Exception):
    pass


def _key_sets(part: str) -> set[frozenset]:
    """Every key set the contract's sync requests use for one list part (records)."""
    return {frozenset(item) for c in SYNC_CASES for item in c["request"]["body"][part] if isinstance(item, dict)}


def assert_contract_request(body: dict) -> None:
    [first] = [c["request"]["body"] for c in SYNC_CASES if c["name"] == "first-poll"]
    [beat] = [c["request"]["body"] for c in SYNC_CASES if c["name"] == "presence-leaving"]
    assert set(body) == set(beat) and set(body["presence"]) == set(beat["presence"])  # this client always sends presence
    for part in ("identity", "partition"):
        assert set(body[part]) == set(first[part])
    assert body["position"] is None or set(body["position"]) == set(first["position"])
    assert all(frozenset(r) in _key_sets("records") for r in body["records"])


def ok_response_keys() -> set[frozenset]:
    return {frozenset(c["expectedResponseKeys"]) for c in SYNC_CASES if c["expectedStatus"] == 200}


@pytest.fixture
def guild(tmp_path):
    """The real server (Storage, Api, TLS front with the test certificates) on a free port."""
    db = tmp_path / "guildsync.sqlite"
    storage = Storage.open(str(db))
    server = make_server(storage, str(HERE / "certs" / "test-server.pem"), str(HERE / "certs" / "test-server.key"),
                         "127.0.0.1", free_port(), Api(storage, time.time))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"https://127.0.0.1:{server.server_address[1]}", str(db)
    server.shutdown()
    server.server_close()
    storage.close()


@pytest.fixture
def logs(caplog, monkeypatch):
    """Every record from DEBUG up, a fresh rate limiter, and PROBLEMS counting as in a live run."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(logsetup, "_last", {})
    root = logging.getLogger("harvesting_dune")
    root.addHandler(logsetup.PROBLEMS)
    before = logsetup.PROBLEMS.count
    yield caplog
    root.removeHandler(logsetup.PROBLEMS)
    assert logsetup.PROBLEMS.count == before
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def issue(capsys, db: str, label: str) -> dict:
    capsys.readouterr()  # the server's audit lines go to stdout too
    assert keysctl.main(["--db", db, "issue", "--label", label, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


class Player:
    """One detector with its own data folder, wired to the guild like the live Runner."""

    def __init__(self, tmp: Path, url: str, token: str, who: dict, t0: float) -> None:
        self.det = Detector(None, data_dir=tmp, min_rescore_s=0)
        self.det.set_sync_key(token)
        session(self.det, HAGGA, t=t0)
        self.det.handle(BusMessage("delta", "dune.trackedSession", who), t0)
        self.runner = Runner(self.det, CompanionHub(1))
        self.runner.sync = SyncClient(server=url, cafile=CA)
        self.calls: list[tuple[dict, object]] = []  # sync only: (request body, response or the SyncError raised)
        self.profile_calls: list[tuple[str, dict, dict]] = []  # (path, request body, response)
        sent = self.runner.sync.exchange

        def recorded(key: str, body: dict, path: str = "/v1/sync") -> dict:
            copy = json.loads(json.dumps(body))
            try:
                resp = sent(key, body, path)
            except SyncRejected as exc:
                if path == "/v1/sync":
                    self.calls.append((copy, exc))
                raise
            if path == "/v1/sync":
                self.calls.append((copy, resp))
            else:
                self.profile_calls.append((path, copy, resp))
            return resp

        self.runner.sync.exchange = recorded

    def sync_once(self) -> object:
        """One pass of Runner.sync_loop: it ends at the loop's first wait, which is never slept."""
        before = len(self.calls)
        with pytest.raises(Stop):
            asyncio.run(self.runner.sync_loop())
        assert len(self.calls) == before + 1
        return self.calls[-1][1]

    def kinds(self) -> list[tuple[str, float, float]]:
        return sorted((m["kind"], m["x"], m["y"]) for m in self.det.page_state()["alternatives"]
                      if m["kind"] in ("peer", "member"))


def test_two_detectors_share_through_the_real_server(tmp_path, guild, logs, capsys, monkeypatch):
    async def no_wait(seconds, wake):
        raise Stop

    monkeypatch.setattr(syncloop, "nap", no_wait)
    url, db = guild
    keys = {name: issue(capsys, db, name) for name in ("Paul", "Chani", "Feyd")}
    t0 = time.time()
    paul = {"characterName": "Paul Test", "characterId": 1001, "battlegroupId": ALPHA}
    chani = {"characterName": "Chani Test", "characterId": 1002, "battlegroupId": ALPHA}
    a = Player(tmp_path / "a", url, keys["Paul"]["token"], paul, t0)
    b = Player(tmp_path / "b", url, keys["Chani"]["token"], chani, t0)
    c = Player(tmp_path / "c", url, keys["Feyd"]["token"],
               {"characterName": "Feyd Test", "characterId": 2001, "battlegroupId": OTHER}, t0)
    pose(a.det, 0, 0, t=t0 + 1)
    pose(b.det, 100_000, 0, t=t0 + 1)
    pose(c.det, 0, 0, t=t0 + 1)
    assert a.det.sync_positions  # on by default
    downed = {"id": "s1", "actorType": "BP_SmallShipwreck_01_C", "markerType": "SalvageSteel",
              "spriteKey": "salvagesteel", "x": SHIP_AT[0], "y": SHIP_AT[1], "z": 0.0, "reportable": True}
    actors(a.det, spice("site", *BLOW_AT, spawned=False), downed, ore("iron", *ORE_AT), world=HAGGA, t=t0 + 2)
    actors(a.det, spice("site", *BLOW_AT), downed, ore("iron", *ORE_AT), world=HAGGA, t=t0 + 3)

    a.sync_once()
    b.sync_once()
    assert ("peer", *BLOW_AT) in b.kinds() and ("peer", *SHIP_AT) in b.kinds() and ("peer", *ORE_AT) in b.kinds()
    assert ("member", 0.0, 0.0) in b.kinds()
    [seen_a] = [m for m in b.det.peers_snapshot()["members"] if m["name"] == "Paul Test"]
    assert (seen_a["worldId"], seen_a["distanceM"], seen_a["shown"]) == (HAGGA, 1000, True)
    iron = {f["cls"]: f for f in b.det.peers_snapshot()["sightings"]}["ore"]
    assert (iron["name"], iron["observer"], iron["distanceM"], iron["lifetimeS"]) == ("Iron Ore", "Paul Test", 802, 1800)
    assert 0 <= iron["ageS"] < 60

    # the user's roster tick box: untick a guildmate and their ring leaves this minimap only
    b.det.ui("sync_member", "1001")
    assert ("member", 0.0, 0.0) not in b.kinds() and ("peer", *SHIP_AT) in b.kinds()
    assert Detector(None, data_dir=tmp_path / "b", min_rescore_s=0).sync_hidden_members == {1001}

    resp = c.sync_once()  # another battlegroup on the same map: the partition holds end to end
    assert (resp["sightings"], resp["presence"]) == ([], [])
    assert c.kinds() == [] and [m["name"] for m in c.det.peers_snapshot()["members"]] == ["Feyd Test"]  # only itself

    a.sync_once()
    assert [m["name"] for m in a.det.peers_snapshot()["members"]] == ["Paul Test", "Chani Test"]  # you, then the guild
    assert ("member", 100_000.0, 0.0) in a.kinds()

    capsys.readouterr()
    assert keysctl.main(["--db", db, "revoke", keys["Paul"]["keyId"]]) == 0
    assert isinstance(a.sync_once(), SyncRejected)
    assert a.det.peers.key_state == "invalid" and a.det.peers_snapshot()["keyState"] == "invalid"
    assert isinstance(b.sync_once(), dict)

    for player in (a, b, c):
        for body, answer in player.calls:
            assert_contract_request(body)
            assert isinstance(answer, SyncRejected) or frozenset(answer) in ok_response_keys()
    tokens = [k["token"] for k in keys.values()]
    for player in (a, b, c):
        shown = json.dumps(player.det.snapshot(), default=str) + json.dumps(player.det.page_state(), default=str)
        assert not [t for t in tokens if t in shown]
    assert not [t for t in tokens if t in logs.text]
