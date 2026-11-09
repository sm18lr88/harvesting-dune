"""The key gate (gate.py) and the dataset loop (dataloop.py, datacache.py) with a fake server and an
injected clock: no key -> nothing drawn and no request; a valid key -> the datasets come once and feed
the farm routes; a 401 clears the map at once; the 72 h offline grace ends to the second."""

import asyncio
import json
import shutil

import pytest
from helpers import CELL_CM, actors, pose, session, spice
from test_sync_page import run as run_page

from harvesting_dune import syncloop
from harvesting_dune.app import Detector
from harvesting_dune.datacache import DataCache
from harvesting_dune.dataloop import DAY_S, DataLoop
from harvesting_dune.sync import SyncExpired, SyncRejected, SyncUnreachable

KEY = "gs1_alpha001_Qm9vdHN0cmFwVGVzdEtleUFscGhhMDAx"
T0 = 1_800_000_000.0
GRACE_S = 72 * 3600  # the spec's number, not gate.GRACE_S: a test that imports it would follow a wrong value
BLANK = {"dest": None, "route": None, "alternatives": [], "stale": [], "extraActors": [], "problem": None, "objects": []}
ROUTE = {"id": "test-route", "name": "Test route", "items": ["testitem"], "resetS": 2700, "confidence": "high",
         "sites": [{"name": "Test site", "world": "survival_1", "center": {"x": 100000, "y": 0, "z": 0},
                    "stops": [{"x": 100000, "y": 0, "z": 0, "kind": "chest", "name": "Chest", "order": 1}]}]}
DATA = {"routes": ('"r1"', {"version": 1, "routes": [ROUTE]}),
        "regions": ('"g1"', {"version": 1, "regionNames": {"cb_ecolab": "Ecolab"}}),
        "other": ('"o1"', {"version": 1})}


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> float:
        return self.t


class FakeServer:
    """SyncClient.fetch as the guild server answers it; `fail` raises instead (a 401, the server down)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.fail: Exception | None = None

    def fetch(self, key, path, etag=None):
        self.calls.append((path, etag))
        if self.fail is not None:
            raise self.fail
        if path == "/v1/data":
            return {"datasets": [{"name": n, "etag": e, "bytes": 1} for n, (e, _) in DATA.items()], "next": None}, None
        tag, body = DATA[path.rsplit("/", 1)[1]]
        return (None, tag) if etag == tag else (json.loads(json.dumps(body)), tag)


def gated(tmp_path, clock, key=KEY):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.gate_required, det.gate_clock = True, clock
    if key:
        det.set_sync_key(key)
    return det


def loop_for(det, clock, server=None):
    loop = DataLoop(det, server or FakeServer(), DataCache(det.data_dir), clock)
    loop.start()
    return loop


def step(loop, n=1):
    for _ in range(n):
        asyncio.run(loop.step())


def scene(det, t=1.0):
    """A medium spice field 2 km away: the detector draws a line to it whenever it may."""
    session(det, "DD")
    pose(det, 10, 0, t=t)
    actors(det, spice("M", 40 * CELL_CM, 0, "Medium", fill=0.7), t=t + 1)
    det.ui("resource", "spice")
    det.maybe_rescore(t + 2)
    return det.page_state()


def test_no_key_draws_nothing_and_asks_the_server_nothing(tmp_path):
    clock = Clock()
    det = gated(tmp_path / "a", clock, key=None)
    loop = loop_for(det, clock)
    step(loop, 3)
    clock.t += 2 * DAY_S
    step(loop)
    assert loop.client.calls == []  # no list, no dataset
    assert scene(det) == BLANK and det.badge() is None and det.areas == []
    assert det.snapshot()["gate"] == {"open": False, "reason": "no-key", "text": "Enter your guild key to use the detector."}
    assert det.routes == [] and not (tmp_path / "a" / "datasets").exists()
    open_det = Detector(None, data_dir=tmp_path / "b", min_rescore_s=0)  # the same scene, no gate: it draws
    assert scene(open_det)["dest"] is not None and open_det.badge() is not None


def test_valid_key_fetches_each_dataset_once_and_feeds_the_farm_routes(tmp_path):
    clock, server = Clock(), FakeServer()
    det = gated(tmp_path, clock)
    assert det.gate_reason() == "checking" and scene(det) == BLANK  # nothing until the server accepts the key
    loop = loop_for(det, clock, server)
    step(loop)
    assert server.calls == [("/v1/data", None), ("/v1/data/routes", None), ("/v1/data/regions", None)]  # never "other"
    assert det.gate_open() and det.peers.key_state == "valid"
    assert [r.id for r in det.routes] == ["test-route"] and det.items.region("cb_ecolab_3") == "Ecolab 3"
    assert scene(det)["dest"] is not None
    session(det, "survival_1", seed=1)
    pose(det, 0, 0, t=10)
    det.ui("route_start", "test-route")
    assert det.farm is not None and det.page_state()["dest"]["title"] == "Farm route · Test site"
    server.calls.clear()
    clock.t += DAY_S - 1
    step(loop, 3)
    assert server.calls == []  # at most once a day
    clock.t += 1
    step(loop)
    assert server.calls == [("/v1/data", None)]  # unchanged ETags: the list only, no dataset body
    index = json.loads((tmp_path / "datasets" / "index.json").read_text())
    assert index["etags"] == {"routes": '"r1"', "regions": '"g1"'} and index["keyId"] == "alpha001"
    assert KEY.split("_")[2] not in (tmp_path / "datasets" / "index.json").read_text()  # never the secret
    assert server.fetch(KEY, "/v1/data/routes", index["etags"]["routes"]) == (None, '"r1"')  # a 304 for the cached tag

    restarted = gated(tmp_path, clock)  # a restart: cached data, no request needed to start working
    server.calls.clear()
    server.fail = SyncUnreachable(OSError("down"))
    loop = loop_for(restarted, clock, server)
    assert restarted.gate_open() and [r.id for r in restarted.routes] == ["test-route"]
    step(loop)
    assert server.calls == [("/v1/data", None)] and restarted.gate_open()  # the start check failed: still in grace


def test_a_changed_dataset_is_fetched_with_if_none_match(tmp_path, monkeypatch):
    clock, server = Clock(), FakeServer()
    loop = loop_for(gated(tmp_path, clock), clock, server)
    step(loop)
    monkeypatch.setitem(DATA, "routes", ('"r2"', {"version": 1, "routes": [{**ROUTE, "id": "new-route"}]}))
    server.calls.clear()
    clock.t += DAY_S
    step(loop)
    assert server.calls == [("/v1/data", None), ("/v1/data/routes", '"r1"')]
    assert [r.id for r in loop.det.routes] == ["new-route"] and loop.cache.etag("routes") == '"r2"'


@pytest.mark.parametrize(("refusal", "state", "reason"), [(SyncRejected("key rejected"), "invalid", "rejected"),
                                                         (SyncExpired("key expired"), "expired", "expired")])
def test_a_401_clears_the_map_at_once_and_stops_every_request(tmp_path, refusal, state, reason):
    clock, server = Clock(), FakeServer()
    det = gated(tmp_path, clock)
    loop = loop_for(det, clock, server)
    step(loop)
    assert scene(det)["dest"] is not None and det.badge() is not None  # working before the refusal
    rev = det.publish_rev
    server.fail = refusal
    clock.t += DAY_S
    step(loop)
    assert det.page_state() == BLANK and det.badge() is None and det.areas == []
    assert det.publish_rev > rev  # the runner pushes the empty state on its next tick
    assert det.peers.key_state == state and det.snapshot()["gate"]["reason"] == reason
    assert json.loads((tmp_path / "datasets" / "index.json").read_text())["checkedAt"] is None  # no grace after a restart
    server.calls.clear()
    clock.t += 3 * DAY_S
    step(loop, 3)
    assert server.calls == []  # a refused key asks nothing until a new key is saved
    det.set_sync_key(KEY.replace("alpha001", "alpha002"))
    server.fail = None
    step(loop)
    assert det.gate_open() and server.calls[0] == ("/v1/data", None)


def test_a_401_from_a_sync_clears_the_map_too(tmp_path):
    clock = Clock()
    det = gated(tmp_path, clock)
    det.key_checked(clock())
    assert scene(det)["dest"] is not None
    syncloop._failed(det, KEY, SyncExpired("key expired"), 0)
    assert det.page_state() == BLANK and det.snapshot()["gate"]["text"] == "Key expired: ask the admin to renew it."


def test_offline_grace_works_72_hours_and_stops_one_second_later(tmp_path):
    clock, server = Clock(), FakeServer()
    det = gated(tmp_path, clock)
    loop = loop_for(det, clock, server)
    step(loop)
    checked = clock.t
    server.fail = SyncUnreachable(OSError("down"))
    clock.t = checked + GRACE_S - 1
    step(loop)
    assert det.gate_open() and scene(det)["dest"] is not None
    clock.t = checked + GRACE_S + 1
    step(loop)
    assert not det.gate_open() and det.page_state() == BLANK and det.areas == []
    assert det.snapshot()["gate"]["text"] == "Server unreachable for 72 h: ask the admin."
    datasets_seen = [c for c in server.calls if c[0] != "/v1/data"]
    assert datasets_seen == [("/v1/data/routes", None), ("/v1/data/regions", None)]  # only from the first, accepted check

    late = gated(tmp_path / "late", clock)  # a restart after 72 h offline: the saved check is too old
    DataLoop(late, FakeServer(), DataCache(tmp_path), clock).start()
    assert late.key_checked_at == checked and not late.gate_open()

    server.fail = None
    clock.t += 3600
    step(loop)
    assert det.gate_open()  # the next accepted check opens it again


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_settings_page_shows_only_the_key_field_and_the_reason(tmp_path):
    det = gated(tmp_path, Clock(), key=None)
    steps = """
      page.render(SNAP);
      log.gate = el("gate").hidden; log.board = el("board").hidden; log.why = el("gateWhy").textContent;
      log.problems = el("problems").hidden; log.chars = el("sec-chars").hidden;
    """
    log = run_page(det.snapshot(), steps)
    assert log == {"gate": False, "board": True, "why": "Enter your guild key to use the detector.", "problems": True,
                   "chars": True}
    det.gate_required = False
    log = run_page(det.snapshot(), steps)
    assert (log["gate"], log["board"], log["chars"]) == (True, False, False)
