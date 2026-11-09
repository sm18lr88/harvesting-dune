"""Guild sync client and loop against the real HTTPS front over TLS: a failure is a warning and a
pause, never a problem report, and the key never reaches a log line."""

import asyncio
import json
import logging
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from guildsync import httpd
from guildsync.httpd import Rejected, make_server
from guildsync.problems import OTHER_KEY as OWNED, invalid
from helpers import pose, session

from harvesting_dune import logsetup, syncloop
from harvesting_dune.app import Detector
from harvesting_dune.feed import BusMessage, CompanionHub
from harvesting_dune.runner import Runner
from harvesting_dune.sync import SyncClient, SyncServerError

CERTS = Path(__file__).parent / "certs"
CONTRACT = json.loads((Path(__file__).parent / "fixtures" / "sync-contract.json").read_text(encoding="utf8"))
FIRST_POLL = next(c for c in CONTRACT["cases"] if c["name"] == "first-poll")["expectedResponse"]
KEY = "gs1_alpha001_Qm9vdHN0cmFwVGVzdEtleUFscGhhMDAx"
OTHER_KEY = "gs1_bravo002_VGVzdE9ubHlCcmF2b0tleVNlY3JldDAy"
WHO = {"characterName": "Paul Test", "characterId": 1001, "battlegroupId": "prd-srv-test-alpha-erg"}


class FakeStorage:
    def __init__(self) -> None:
        self.lookups: list[str] = []

    def lookup_key(self, token: str) -> dict | None:
        self.lookups.append(token)
        return {"id": "alpha001"} if token == KEY else None

    def audit(self, *row) -> None:
        pass


class FakeApi:
    """Answers with the scripted replies in order (a Rejected is raised), then with first-poll."""

    def __init__(self) -> None:
        self.bodies: list[dict] = []
        self.script: list = []

    def sync(self, key: dict, body: dict, ip: str, now: float) -> tuple[int, dict]:
        self.bodies.append(body)
        reply = self.script.pop(0) if self.script else (200, FIRST_POLL)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def profile(self, key: dict, body: dict, ip: str, now: float) -> tuple[int, dict]:
        return 200, {"ok": True}


class Stop(Exception):
    pass


@pytest.fixture
def guild():
    """Start a guild front with the given server certificate; returns (url, api, storage)."""
    servers = []

    def start(cert: str = "test-server"):
        api, storage = FakeApi(), FakeStorage()
        server = make_server(storage, str(CERTS / f"{cert}.pem"), str(CERTS / f"{cert}.key"), "127.0.0.1", 0, api)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"https://127.0.0.1:{server.server_address[1]}", api, storage

    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


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
    assert KEY not in caplog.text and OTHER_KEY not in caplog.text


def runner_for(tmp_path, url: str, key: str = KEY) -> Runner:
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.set_sync_key(key)
    session(det, "deepdesert_1")
    det.map_files[det.store.world] = "deepdesert_1_07"
    det.handle(BusMessage("delta", "dune.trackedSession", WHO), 0)
    pose(det, 0, 0, t=1)
    runner = Runner(det, CompanionHub(1))
    runner.sync = SyncClient(server=url, cafile=CERTS / "test-ca.pem")
    return runner


def drive(runner: Runner, monkeypatch, on_sleep) -> list[float]:
    """Run the guild sync loop with each wait recorded, not waited; `on_sleep(delays)` True ends it."""
    delays: list[float] = []

    async def nap(seconds, wake):
        delays.append(seconds)
        if on_sleep(delays):
            raise Stop
        return seconds, False

    monkeypatch.setattr(syncloop, "nap", nap)
    with pytest.raises(Stop):
        asyncio.run(runner.sync_loop())
    return delays


def test_first_poll_draws_the_guilds_ring_without_a_problem(tmp_path, guild, logs, monkeypatch):
    url, api, _ = guild()
    runner = runner_for(tmp_path, url)
    det = runner.det
    notice = det.page_state()["problem"]  # None alone; earlier tests in the run may have left one
    assert drive(runner, monkeypatch, lambda d: True) == [15.0]
    [body] = api.bodies
    assert body["since"] == 0 and isinstance(body["sentAt"], float) and body["identity"] == WHO
    assert det.peers.since == 1 and det.peers.key_state == "valid" and det.peers.last_error is None
    assert [(r["x"], r["kind"]) for r in det.page_state()["alternatives"]] == [(123456.0, "peer")]
    assert det.page_state()["problem"] == notice
    snap = det.peers_snapshot()
    assert snap["lastSyncAgeS"] is not None and (snap["syncError"], snap["connectedAs"]) == (None, "Paul Test")


def test_server_down_backs_off_with_warnings_only(tmp_path, logs, monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # closed again before the loop connects
    runner = runner_for(tmp_path, f"https://127.0.0.1:{port}")
    assert drive(runner, monkeypatch, lambda d: len(d) == 2) == [30.0, 60.0]
    snap = runner.det.peers_snapshot()
    assert (snap["syncError"], snap["connectedAs"], snap["keyState"]) == ("unreachable", None, "unknown")
    assert [r.levelno for r in logs.records if r.name == "harvesting_dune.syncloop"] == [logging.WARNING]


def test_a_server_from_another_ca_is_refused_before_any_request(tmp_path, guild, logs, monkeypatch):
    url, api, storage = guild("other-server")
    runner = runner_for(tmp_path, url)
    assert drive(runner, monkeypatch, lambda d: True) == [30.0]
    assert runner.det.peers_snapshot()["syncError"] == "tls" and api.bodies == [] and storage.lookups == []
    assert any(r.levelno == logging.WARNING and "tls" in r.getMessage() for r in logs.records)


def test_a_rejected_key_stops_polling_until_a_new_key_is_saved(tmp_path, guild, logs, monkeypatch):
    url, api, storage = guild()
    runner = runner_for(tmp_path, url, key=OTHER_KEY)
    det = runner.det
    states = []

    def on_sleep(delays):
        snap = det.peers_snapshot()
        states.append((snap["keyState"], snap["syncError"], snap["connectedAs"]))
        if len(delays) == 2:
            det.set_sync_key(KEY)
        return len(delays) == 3

    assert drive(runner, monkeypatch, on_sleep) == [5.0, 5.0, 15.0]
    assert states == [("invalid", None, None)] * 2 + [("valid", None, "Paul Test")]
    assert storage.lookups == [OTHER_KEY, KEY, KEY]  # the sync, then our first profile upload
    assert len(api.bodies) == 1 and det.peers.last_error is None
    assert any("rejected the key" in r.getMessage() for r in logs.records)


def test_rate_limited_waits_for_retry_after(tmp_path, guild, logs, monkeypatch):
    url, api, _ = guild()
    api.script = [Rejected(429, {"error": "rate limited"}, {"Retry-After": "7"})]
    runner = runner_for(tmp_path, url)
    assert drive(runner, monkeypatch, lambda d: True) == [7.0]
    assert runner.det.peers_snapshot()["syncError"] == "rate_limited"


def test_a_failed_upload_is_sent_with_the_next_poll(tmp_path, guild, logs, monkeypatch):
    url, api, _ = guild()
    api.script = [Rejected(503, {"error": "server"})]
    runner = runner_for(tmp_path, url)
    runner.det.peer_note("blow", "spicefieldlarge", 5000.0, 0.0, 0.0, 1.0, size="large")
    pending = []

    def on_sleep(delays):
        pending.append([r["x"] for r in runner.sync.pending.values()])
        return len(delays) == 2

    assert drive(runner, monkeypatch, on_sleep) == [30.0, 15.0]
    assert [[r["cls"] for r in b["records"]] for b in api.bodies] == [["blow"], ["blow"]]
    assert pending == [[5000.0], []] and runner.det.peers.last_error is None


def test_a_refusal_names_its_problem_type_and_field_in_the_error_text(tmp_path, guild, logs):
    url, api, _ = guild()
    api.script = [Rejected(400, invalid("records[0].cls")), Rejected(403, OWNED), Rejected(502, {"oops": 1})]
    client = SyncClient(server=url, cafile=CERTS / "test-ca.pem")
    texts = []
    for _ in range(3):
        with pytest.raises(SyncServerError) as caught:
            client.exchange(KEY, {"since": 0})
        texts.append((caught.value.status, str(caught.value)))
    assert texts == [(400, "HTTP 400 (invalid-field records[0].cls)"), (403, "HTTP 403 (character-owned)"),
                     (502, "HTTP 502")]


def test_replay_never_loads_the_network_client(tmp_path):
    journal = tmp_path / "j.jsonl"
    rows = [{"t": 0, "type": "delta", "topic": "dune.session", "payload": {"worldId": "DD", "coriolisWorldSeed": 7}},
            {"t": 1, "type": "delta", "topic": "dune.player", "payload": {"x": 0, "y": 0, "z": 0}}]
    journal.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf8")
    code = ("import argparse, sys\nfrom harvesting_dune import cli\n"
            f"cli._replay(argparse.Namespace(journal={str(journal)!r}, resource='spice', vehicle='auto'))\n"
            "assert 'harvesting_dune.sync' not in sys.modules\n")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr


def test_a_failed_upload_never_carries_its_records_into_another_world(tmp_path, guild, logs, monkeypatch):
    url, api, _ = guild()
    api.script = [Rejected(503, {"error": "server"})]
    runner = runner_for(tmp_path, url)
    runner.det.peer_note("blow", "spicefieldlarge", 5000.0, 0.0, 0.0, 1.0, size="large")

    def on_sleep(delays):
        if len(delays) == 1:  # the upload failed; the player moves to Hagga Basin before the retry
            session(runner.det, "survival_1")
        return len(delays) == 2

    assert drive(runner, monkeypatch, on_sleep) == [30.0, 15.0]
    first, retry = api.bodies
    assert first["partition"]["layout"] == "deepdesert_1_07" and [r["cls"] for r in first["records"]] == ["blow"]
    assert retry["partition"] == {"worldId": "survival_1", "layout": "survival_1"} and retry["records"] == []


def test_the_client_gzips_big_bodies_and_reads_gzipped_answers(tmp_path, guild, logs):
    url, api, _ = guild()
    big = [{"cls": "ore", "x": float(i), "observer": "Leto Test"} for i in range(80)]
    api.script = [(200, {**FIRST_POLL, "sightings": big})]
    client = SyncClient(server=url, cafile=CERTS / "test-ca.pem")
    body = {"since": 0, "records": big}
    req = client.request(KEY, body, "/v1/sync")
    assert req.get_header("Content-encoding") == "gzip" and len(req.data) < len(json.dumps(body)) / 4
    assert client.exchange(KEY, body)["sightings"] == big and api.bodies == [body]
    small = client.request(KEY, {"since": 0}, "/v1/sync")
    assert small.get_header("Content-encoding") is None and small.get_header("Accept-encoding") == "gzip"


def test_a_server_without_profile_routes_never_slows_the_syncs(tmp_path, guild, logs, monkeypatch):
    monkeypatch.setattr(httpd, "ROUTES", {"/v1/sync": "sync"})  # the server as deployed before profiles
    url, api, _ = guild()
    runner = runner_for(tmp_path, url)
    runner.det.characters_opened()
    assert drive(runner, monkeypatch, lambda d: len(d) == 2) == [15.0, 15.0]
    assert len(api.bodies) == 1 and runner.det.peers.last_error is None
    assert runner.det.profile_upload(1e12) is None  # the refused profile waits until it changes
