"""The dataset loop against the real guild server, run as `python -m guildsync` over TLS with the
test certificates and a --datasets folder: no key asks nothing, a valid key gets each dataset once
(then the list only, and a 304 for the cached ETag), and a revoked key clears the map."""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from guildsync import keysctl
from guildsync.storage import Storage
from test_gate import BLANK, ROUTE, Clock, scene
from test_robustness import free_port

from harvesting_dune.app import Detector
from harvesting_dune.datacache import DataCache
from harvesting_dune.dataloop import DAY_S, DataLoop
from harvesting_dune.sync import SyncClient

HERE = Path(__file__).parent
CERTS = HERE / "certs"
START_S = 30.0
STOP_S = 10.0


@pytest.fixture
def server(tmp_path, capsys):
    """(url, db, datasets folder) of a real guildsync process; it is stopped (and waited for) afterwards."""
    folder, db = tmp_path / "served", tmp_path / "guildsync.sqlite"
    folder.mkdir()
    (folder / "routes.json").write_text(json.dumps({"version": 1, "routes": [ROUTE]}), "utf8")
    (folder / "regions.json").write_text(json.dumps({"version": 1, "regionNames": {"cb_ecolab": "Ecolab"}}), "utf8")
    Storage.open(str(db)).close()
    port = free_port()
    env = {**os.environ, "PYTHONPATH": str(HERE.parent / "server")}
    proc = subprocess.Popen([sys.executable, "-m", "guildsync", "--db", str(db), "--datasets", str(folder),
                             "--cert", str(CERTS / "test-server.pem"), "--key", str(CERTS / "test-server.key"),
                             "--host", "127.0.0.1", "--port", str(port)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    ready = threading.Event()

    def drain():  # keep reading: a full pipe would block the server
        for line in proc.stderr:
            if "listening on" in line:
                ready.set()

    threading.Thread(target=drain, daemon=True).start()
    try:
        assert ready.wait(START_S), "guildsync did not start"
        yield f"https://127.0.0.1:{port}", str(db), folder
    finally:  # bounded either way: a server that ignores terminate is killed, never left running
        proc.terminate()
        try:
            proc.wait(STOP_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(STOP_S)


class Counted(SyncClient):
    def __init__(self, url: str) -> None:
        super().__init__(server=url, cafile=CERTS / "test-ca.pem")
        self.calls: list[tuple[str, str | None]] = []

    def fetch(self, key, path, etag=None):
        self.calls.append((path, etag))
        return super().fetch(key, path, etag)


def issue(capsys, db: str) -> dict:
    capsys.readouterr()
    assert keysctl.main(["--db", db, "issue", "--label", "gate test", "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def setup(tmp_path, url, key, clock):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.gate_required, det.gate_clock = True, clock
    if key:
        det.set_sync_key(key)
    loop = DataLoop(det, Counted(url), DataCache(tmp_path), clock)
    loop.start()
    return det, loop


def test_datasets_through_the_real_tls_server(tmp_path, server, capsys):
    url, db, folder = server
    clock = Clock()

    det, loop = setup(tmp_path / "nokey", url, None, clock)
    asyncio.run(loop.step())
    assert loop.client.calls == [] and scene(det) == BLANK  # no key: nothing drawn, nothing asked

    key = issue(capsys, db)
    det, loop = setup(tmp_path / "player", url, key["token"], clock)
    asyncio.run(loop.step())
    assert loop.client.calls == [("/v1/data", None), ("/v1/data/routes", None), ("/v1/data/regions", None)]
    tag = f'"{hashlib.sha256((folder / "routes.json").read_bytes()).hexdigest()}"'
    assert loop.cache.etag("routes") == tag and [r.id for r in det.routes] == ["test-route"]
    assert det.gate_open() and scene(det)["dest"] is not None

    loop.client.calls.clear()
    clock.t += DAY_S
    asyncio.run(loop.step())
    assert loop.client.calls == [("/v1/data", None)]  # the next day: unchanged, so the list only
    assert loop.client.fetch(key["token"], "/v1/data/routes", tag) == (None, tag)  # the server's 304 for our copy

    assert keysctl.main(["--db", db, "revoke", key["keyId"]]) == 0
    clock.t += DAY_S
    asyncio.run(loop.step())
    assert det.peers.key_state == "invalid" and det.page_state() == BLANK and not det.gate_open()
