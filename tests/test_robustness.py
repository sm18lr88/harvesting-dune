"""Failure handling: crash-safe files, persisted terrain, expiring corrections, hostile input,
supervised background loops, and the local server's access rules."""

import asyncio
import json
import logging
import shutil
import socket
import subprocess
import threading
import types

import pytest
from helpers import CELL_CM, actors, ore, pose, session

from harvesting_dune import runner as runner_mod
from harvesting_dune.app import Detector
from harvesting_dune.corrections import STALE_TTL_S, ScanIndex
from harvesting_dune.feed import BRIDGE_SCRIPT, CompanionHub, Page
from harvesting_dune.fsutil import read_json, write_json
from harvesting_dune.server import _allowed
from harvesting_dune.terrain import VEHICLES, TerrainGrid


def free_port() -> int:
    """A port nothing listens on now: parallel test runs never collide on a fixed one."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# -- files -------------------------------------------------------------------------------------
def test_atomic_write_leaves_no_temp_files_and_never_writes_nan(tmp_path):
    path = tmp_path / "sub" / "x.json"
    write_json(path, {"a": 1})
    assert json.loads(path.read_text()) == {"a": 1} and [p.name for p in path.parent.iterdir()] == ["x.json"]
    with pytest.raises(ValueError):
        write_json(path, {"a": float("nan")})
    assert json.loads(path.read_text()) == {"a": 1}  # the old file survives a failed write
    assert [p.name for p in path.parent.iterdir()] == ["x.json"]


def test_read_json_rejects_wrong_shapes(tmp_path):
    path = tmp_path / "x.json"
    path.write_text("[1, 2]")
    assert read_json(path, dict, {}) == {}
    path.write_text('{"half": ')
    assert read_json(path, dict, "d") == "d"


def test_malformed_settings_never_stop_the_detector(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({
        "minimapHidden": "iron", "seenResources": 5, "resource": ["x"], "pvpYield": True,
        "lastPveRow": "Z", "bases": {"survival_1|7": ["a", 1]}, "lines": True,
    }))
    det = Detector(None, data_dir=tmp_path)
    assert det.pvp_yield == 2.5 and det.last_pve_row == "D" and det.resource_key == "spice"
    assert det.minimap_hidden == set() and det._bases == {}
    for name in ("ranges.json", "corrections-DD-7.json", "blows-DD-7.json"):
        (tmp_path / name).write_text('["not", "a", "dict"]')
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    session(det, "DD")
    assert det.scan.stale == {} and det.blows == {}


def test_pvp_yield_rejects_nan_and_infinity():
    det = Detector(None)
    for bad in ("nan", "inf", "-inf", "abc", None):
        det.ui("pvp_yield", bad)
    assert det.pvp_yield == 2.5
    det.ui("pvp_yield", "3")
    assert det.pvp_yield == 3.0


# -- terrain and base survive a restart ----------------------------------------------------------
def test_learned_terrain_and_base_survive_a_restart(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    session(det, "survival_1")
    pose(det, 1000, 2000, t=1)
    det.grid.add_driven(10 * CELL_CM, 0, 500, "buggy")
    det.grid.add_height_sample(20 * CELL_CM, 0, 9000)
    det.grid.block(30 * CELL_CM, 0, "sandbike")
    det.ui("base")
    assert det.save_terrain(100.0) == 1 and det.save_terrain(101.0) == 0  # throttled
    assert det.save_terrain(200.0, force=True) == 0  # nothing new: no write

    again = Detector(None, data_dir=tmp_path)
    session(again, "survival_1")
    g = again.grid
    cell = g.cells[(10, 0)]
    assert cell.driven_by == {"buggy"} and cell.z_n == 1 and g.has_proven(VEHICLES["buggy"])
    assert g.cells[(20, 0)].z_max == 9000 and "sandbike" in g.cells[(30, 0)].blocked_for
    assert again.base == (1000, 2000)


def test_terrain_loader_skips_garbage():
    g = TerrainGrid.from_json({"version": 1, "cellCm": CELL_CM, "cells": [[1, 2, 0, 0, 0, 1, ["buggy", "tank"], []], "junk", [1]]})
    assert list(g.cells) == [(1, 2)] and g.cells[(1, 2)].driven_by == {"buggy"}
    assert TerrainGrid.from_json({"version": 99}).cells == {}


# -- corrections expire after the respawn time --------------------------------------------------
def test_stale_corrections_expire_so_respawned_nodes_come_back(tmp_path):
    idx = ScanIndex(tmp_path)
    idx.set_world(("survival_1", 7))
    idx.stale = {
        "ore:1:1": {"x": 1, "y": 1, "z": 0, "confirmedAt": 0.0, "sprite": "magnetiteore"},
        "sp:2:2": {"x": 2, "y": 2, "z": 0, "confirmedAt": 0.0, "sprite": "spicefieldlarge"},
    }
    idx.mark_refreshed()
    assert idx.expire(STALE_TTL_S["node"] - 1) == 0
    assert idx.expire(STALE_TTL_S["node"] + 1) == 1 and set(idx.stale) == {"sp:2:2"}
    assert idx.refresh_pending  # the page must reload its scan layer to show the node again
    idx.mark_refreshed()
    assert not idx.refresh_pending
    assert idx.expire(STALE_TTL_S["spice"] + 1) == 1 and idx.stale == {}
    reloaded = ScanIndex(tmp_path)
    reloaded.set_world(("survival_1", 7))
    assert reloaded.stale == {}


# -- the feed and background loops --------------------------------------------------------------
def test_odd_messages_from_a_page_never_stop_the_feed():
    det = Detector(None, min_rescore_s=0)

    class Hub:
        async def messages(self):
            for m in (["not a dict"], {"type": "ui", "action": None, "value": object()},
                      {"type": "delta", "topic": "dune.player", "payload": {"x": "a"}},
                      {"type": "delta", "topic": "dune.player", "payload": {"x": 5.0, "y": 6.0, "z": 7.0}}):
                yield type("P", (), {"url": "test"})(), m

    r = runner_mod.Runner.__new__(runner_mod.Runner)
    r.det, r.hub = det, Hub()
    asyncio.run(r.consume())
    assert det.store.pose is not None and det.store.pose.x == 5.0


def test_a_crashing_background_loop_is_restarted(monkeypatch):
    monkeypatch.setattr(runner_mod, "RESTART_BACKOFF_S", (0.0,))
    calls = []

    async def main():
        third_run = asyncio.Event()

        async def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("boom")
            third_run.set()
            await asyncio.Event().wait()  # runs on until cancelled

        r = runner_mod.Runner.__new__(runner_mod.Runner)
        t = asyncio.create_task(r._supervised("flaky", flaky))
        await asyncio.wait_for(third_run.wait(), 10)
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)

    asyncio.run(main())
    assert len(calls) == 3


def test_failed_scan_refresh_keeps_the_rows_we_have(monkeypatch):
    det = Detector(None, min_rescore_s=0)
    session(det, "DD")
    det.load_scan([{"type": "MagnetiteOre", "map_marker_id": "magnetiteore", "x": 1000, "y": 0, "z": 0}], 200, 0)

    # the loop polls every 2 s: let its sleeps return at once so the test needs no real waiting
    real_sleep = asyncio.sleep
    monkeypatch.setattr(runner_mod, "asyncio", types.SimpleNamespace(**{**vars(asyncio), "sleep": lambda _s: real_sleep(0)}))

    class Hub:
        primary = object()
        fetched: asyncio.Event

        async def fetch_scan(self, *_):
            self.fetched.set()
            return 503, []

    hub = Hub()
    r = runner_mod.Runner.__new__(runner_mod.Runner)
    r.det, r.hub, r._scan_key, r._scan_failures = det, hub, None, 0
    det.scan.fetched_at = None

    async def main():
        hub.fetched = asyncio.Event()
        t = asyncio.create_task(r.refresh_scan())
        await asyncio.wait_for(hub.fetched.wait(), 10)
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)

    asyncio.run(main())
    assert len(det.scan.rows) == 1 and r._scan_failures == 1


def test_timed_out_devtools_calls_do_not_leak():
    class WS:
        async def send(self, _):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()  # the page never answers

    async def main():
        page = Page({"id": "p", "url": "u"}, WS(), asyncio.Queue())
        with pytest.raises(asyncio.TimeoutError):
            await page.call("Runtime.evaluate", timeout=0.01)
        assert page._pending == {}
        page._reader.cancel()

    asyncio.run(main())


def test_hub_reports_how_long_the_companion_has_been_gone():
    hub = CompanionHub(1, poll_s=0.01)
    assert hub.unreachable_for() is None  # never seen: still waiting, not gone
    hub._seen = True
    assert hub.unreachable_for() is None  # answering, however slow the last poll was
    hub._failing_since = 0.0
    assert hub.unreachable_for() > 0


def test_detector_exits_even_while_the_settings_page_is_open(tmp_path):
    """Regression: `async with server` waited forever for the page's event stream."""
    import sys
    import urllib.request

    port = free_port()
    code = r"""
import asyncio, sys
sys.argv = ["harvesting-dune", "run", "--devtools-port", "1", "--http-port", "PORT",
            "--data-dir", DATA, "--no-journal", "--exit-when-companion-gone", "0.1"]
from harvesting_dune import feed
calls = {"n": 0}
def fake_list(port):
    calls["n"] += 1
    if calls["n"] == 1:
        return []          # seen once...
    raise OSError("refused")  # ...then the companion closes
feed.list_targets = fake_list
feed.CompanionHub.__init__.__defaults__ = (0.2,)
from harvesting_dune import cli
real_serve = cli.serve
async def serve(*a, **k):
    server = await real_serve(*a, **k)
    print("listening", flush=True)  # the parent opens the settings page now
    return server
cli.serve = serve
cli.main()
print("exited")
"""
    # one pipe for both streams, read by one thread: a full stderr pipe would block the child
    proc = subprocess.Popen([sys.executable, "-c", code.replace("DATA", repr(str(tmp_path))).replace("PORT", str(port))],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out: list[str] = []
    listening = threading.Event()

    def read():
        for line in proc.stdout:
            out.append(line)
            if line.strip() == "listening":
                listening.set()

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    stream = None
    try:
        assert listening.wait(20), "the settings server never started"
        stream = urllib.request.urlopen(f"http://127.0.0.1:{port}/events", timeout=5)  # hold it open
        proc.wait(timeout=20)
        reader.join(5)
    finally:
        proc.kill()
        if stream:
            stream.close()
    assert "exited" in "".join(out), "".join(out)[-2000:]


def test_an_error_becomes_a_report_on_the_settings_page_and_a_notice_in_the_companion():
    from harvesting_dune import logsetup

    det = Detector(None, min_rescore_s=0)
    det.settings_url = "http://127.0.0.1:9999/"
    before = logsetup.PROBLEMS.count
    logger = logging.getLogger("harvesting_dune.test.problems")
    logger.addHandler(logsetup.PROBLEMS)
    try:
        try:
            raise ValueError("bad payload")
        except ValueError:
            logger.exception("ranking failed")
        logger.warning("only a warning")  # not a problem
    finally:
        logger.removeHandler(logsetup.PROBLEMS)
    snap = det.snapshot()["problems"]
    assert snap["count"] == before + 1 and snap["items"][-1]["message"] == "ranking failed"
    assert "ValueError: bad payload" in snap["items"][-1]["detail"]
    notice = det.page_state()["problem"]
    assert notice["id"] == snap["count"] and "http://127.0.0.1:9999/" in notice["text"] and "Copy report" in notice["text"]
    assert det.snapshot()["about"]["python"]


# -- local server ------------------------------------------------------------------------------
@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_settings_page_script_parses():
    """One syntax error (a name declared twice) stops the whole settings page."""
    from importlib import resources

    html = resources.files("harvesting_dune").joinpath("viewer.html").read_text("utf8")
    code = html[html.rindex("<script>") + len("<script>"):html.rindex("</script>")]
    out = subprocess.run(["node", "-e", "new Function(require('fs').readFileSync(0, 'utf8'))"], input=code,
                         capture_output=True, text=True, timeout=30, check=False)
    assert out.returncode == 0, out.stderr[-1000:]


def test_server_only_serves_this_machine():
    ok = {"host": "127.0.0.1:8765"}
    assert _allowed(ok, "GET", 8765) and _allowed({**ok, "origin": "http://127.0.0.1:8765"}, "POST", 8765)
    assert not _allowed({"host": "evil.example:8765"}, "GET", 8765)  # DNS rebinding
    assert not _allowed({**ok, "origin": "https://evil.example"}, "POST", 8765)  # cross-site POST
    assert _allowed({**ok, "origin": "https://evil.example"}, "GET", 8765)  # reads are blocked by CORS anyway


# -- bridge dead-man switch ------------------------------------------------------------------------
@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_bridge_dead_man_clears_once_and_reports_empty():
    code = r"""
let now = 1000; Date.now = () => now;
const timers = []; globalThis.setInterval = (fn) => timers.push(fn); globalThis.setTimeout = (fn) => 0;
const wv = { postMessage() {}, addEventListener() {}, dispatchEvent() { return true; } };
globalThis.window = globalThis; globalThis.chrome = { webview: wv };
globalThis.location = { pathname: '/dune/minimap' };
globalThis.document = { readyState: 'complete', getElementById: () => null, body: { appendChild() {} }, documentElement: { appendChild() {} }, createElement: () => ({ style: {}, dataset: {}, append() {}, appendChild() {} }) };
globalThis.__harvestingDuneEmit = () => {};
eval(SCRIPT);
__dd.set({ dest: { hasDestination: true, x: 1, y: 2, z: 3 } });
const before = __dd.ping();
now += 120000; timers.forEach((t) => t());
const after = __dd.ping(), cleared = __dd.cleared, dest = __dd.dest;
now += 120000; timers.forEach((t) => t());
console.log(JSON.stringify({ before, after, cleared, dest }));
"""
    # on stdin, like the other bridge tests: the script outgrew Windows' 32 KB command-line limit
    out = subprocess.run(["node", "-"], input=code.replace("SCRIPT", json.dumps(BRIDGE_SCRIPT)), capture_output=True, text=True, check=True)
    res = json.loads(out.stdout)
    # after the clear the page must report 'empty' so a returning detector pushes state again
    assert res == {"before": "ok", "after": "empty", "cleared": True, "dest": None}


def test_choice_and_terrain_carry_over_to_the_next_detector(tmp_path):
    # end to end: settings saved by one detector, ranking by the next, with terrain on disk
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.ui("minimap_toggle", "iron")
    det.ui("vehicle", "ornithopter")
    session(det, "DD")
    pose(det, 0, 0, t=1)
    actors(det, *[ore(f"a{i}", 30_000 + 300 * i, 0) for i in range(5)], t=2)
    det.compute(3)
    assert det.top(1) and det.save_terrain(10.0, force=True) == 1
    again = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    assert again.resource_key == "iron" and again.lines_active


def test_settings_page_is_versioned_and_changes_are_logged(tmp_path, caplog):
    """An outdated open tab reloads itself; every change and refusal shows in the log."""
    import logging
    import urllib.request

    from harvesting_dune.server import serve

    det = Detector(None, data_dir=tmp_path)
    port = free_port()

    async def main():
        server = await serve(det, port=port)
        try:
            def fetch(path, method="GET", headers=None):
                req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=headers or {})
                try:
                    with urllib.request.urlopen(req, timeout=5) as r:
                        return r.status, r.read()
                except urllib.error.HTTPError as e:
                    return e.code, b""
            page = (await asyncio.to_thread(fetch, "/"))[1].decode()
            state = json.loads((await asyncio.to_thread(fetch, "/state"))[1])
            ok = await asyncio.to_thread(fetch, "/control?action=pvp_only", "POST", {"Origin": f"http://127.0.0.1:{port}"})
            bad = await asyncio.to_thread(fetch, "/control?action=pvp_only", "POST", {"Origin": "https://evil.example"})
            return page, state, ok[0], bad[0]
        finally:
            server.close()

    with caplog.at_level(logging.INFO, logger="harvesting_dune.server"):
        page, state, ok, bad = asyncio.run(main())
    assert "%PAGE_VERSION%" not in page and f'PAGE_VERSION = "{state["pageVersion"]}"' in page
    assert ok == 204 and bad == 403 and det.pvp_only is False  # only the allowed change applied
    assert "settings page: pvp_only" in caplog.text and "refused POST /control" in caplog.text


def test_one_respawn_rule_for_hidden_rows_and_mined_nodes(tmp_path):
    """Hidden scan rows of a node type expire with the detector's respawn time (learned or per
    resource), not a second fixed value."""
    det = Detector(None, data_dir=tmp_path)
    for v in (900, 950, 1000, 1100, 1200):
        det.learning.learn("respawn", "magnetiteore", v)
    session(det, "survival_1")
    det.scan.stale = {"r": {"x": 1, "y": 1, "z": 0, "confirmedAt": 0.0, "sprite": "magnetiteore"}}
    assert det.scan.expire(det.respawn_for("magnetiteore") - 1) == 0
    assert det.scan.expire(det.respawn_for("magnetiteore") + 1) == 1


def test_pvp_yield_only_applies_in_the_deep_desert():
    det = Detector(None)
    session(det, "survival_1")
    assert det.yield_at(0, -900_000) == 1.0  # Hagga Basin has no PvP rows
    session(det, "deepdesert_1", t=1)
    assert det.yield_at(0, -900_000) == det.pvp_yield


def test_hand_edited_corrections_with_odd_types_never_stop_the_upkeep(tmp_path):
    (tmp_path / "corrections-survival_1-7.json").write_text(json.dumps({
        "a": {"x": 1, "y": 1, "z": 0, "confirmedAt": 0, "sprite": 12345},
        "b": {"x": 2, "y": 2, "z": 0, "confirmedAt": 0, "sprite": None, "type": ["x"]},
        "c": {"x": 3, "y": 3, "z": 0, "confirmedAt": 0, "sprite": "lootcontainerrare"},
    }))
    det = Detector(None, data_dir=tmp_path)
    session(det, "survival_1")
    assert det.scan.expire(45 * 60 - 1) == 0  # odd entries and cages keep the fixed 45 min
    assert det.scan.expire(45 * 60 + 1) == 3


def test_settings_actions_save_exactly_when_they_change_a_setting(tmp_path):
    det = Detector(None, data_dir=tmp_path)
    path = tmp_path / "settings.json"
    for action, value in (("minimap_extra", "buried_treasure"), ("pve_row", "C"), ("prev_resource", None)):
        path.unlink(missing_ok=True)
        det.ui(action, value)
        assert path.exists(), action
    for action, value in (("cycle_vehicle", None), ("toggle", None), ("pve_row", "Z"), ("pvp_yield", "nan")):
        path.unlink(missing_ok=True)
        det.ui(action, value)
        assert not path.exists(), action


def test_errors_and_warnings_reach_the_log_files(tmp_path):
    import logging
    import threading
    import warnings

    from harvesting_dune import logsetup

    try:
        logsetup.setup(tmp_path)
        logging.getLogger("harvesting_dune.test").warning("a warning")
        warnings.warn("a python warning", stacklevel=1)
        t = threading.Thread(target=lambda: 1 / 0, name="boom")
        t.start()
        t.join()

        async def main():
            logsetup.watch_loop(asyncio.get_running_loop())

            async def fail():
                raise RuntimeError("unawaited")

            task = asyncio.ensure_future(fail())
            await asyncio.wait([task])  # done, its exception never retrieved
            del task  # never awaited: the loop reports it when the task is collected
            import gc
            gc.collect()

        asyncio.run(main())
        for _ in range(3):
            logsetup.limited(logging.getLogger("harvesting_dune.test"), "k", "repeating")
    finally:
        for h in logging.getLogger().handlers:
            h.flush()
    errors = (tmp_path / "logs" / "errors.log").read_text("utf8")
    everything = (tmp_path / "logs" / "detector.log").read_text("utf8")
    assert "a warning" in errors and "a python warning" in errors and "thread boom failed" in errors
    assert "ZeroDivisionError" in errors and "unawaited" in errors
    assert errors.count("repeating") == 1  # rate-limited
    assert logsetup.PROBLEMS in logging.getLogger().handlers  # a live run feeds the settings page's problem list
    assert "a warning" in everything
    logging.basicConfig(force=True, handlers=[logging.StreamHandler()])  # leave other tests as they were
