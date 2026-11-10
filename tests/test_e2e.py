"""End to end: two fake companion pages over DevTools -> hub -> detector -> pushes back to both pages."""

import asyncio
import ctypes
import gc
import http.client
import json
import logging
import time

import pytest
import websockets
from helpers import CELL_CM, ore, session
from test_lifecycle import priority_class

from harvesting_dune.app import Detector
from harvesting_dune import feed, hotkeys
from harvesting_dune.feed import BINDING, CompanionHub, Page
from harvesting_dune.governor import Governor
from harvesting_dune.runner import Runner
from harvesting_dune.sync import SyncClient

BUS = [
    {"type": "delta", "topic": "dune.session", "payload": {"worldId": "DD", "coriolisWorldSeed": 7, "mapScanSeed": 7, "worldName": "Deep Desert", "activity": "Exploration"}},
    {"type": "delta", "topic": "dune.player", "payload": {"x": 100, "y": 0, "z": 50}},
    {"type": "delta", "topic": "dune.map.actors", "payload": {"gated": False, "worldId": "DD", "coriolisWorldSeed": 7,
                                                               "actors": [ore(1, 20 * CELL_CM, 0), ore(2, 20.3 * CELL_CM, 0)]}},
]
SCAN_ROWS = [{"type": "ResourceNode", "map_marker_id": "magnetiteore", "x": -30 * CELL_CM, "y": 0, "z": 0}]


class FakePage:
    def __init__(self, pid: str, url: str, activity: asyncio.Event) -> None:
        self.id, self.url = pid, url
        self.evaluated: list[str] = []
        self.ws = None
        self.activity = activity  # set on every call the detector makes into a page

    async def handler(self, ws):
        self.ws = ws
        async for raw in ws:
            req = json.loads(raw)
            value = None
            if req["method"] == "Runtime.evaluate":
                expr = req["params"]["expression"]
                self.evaluated.append(expr)
                self.activity.set()
                if "chrome.webview" in expr and "__dd" in expr and "TOPICS" in expr:
                    value = "installed"
                elif "fetchScan" in expr:
                    value = json.dumps({"status": 200, "rows": SCAN_ROWS})
                elif "ping()" in expr:
                    value = "ok"
                elif "__dd.primary = true" in expr and "minimap" in self.url:
                    await ws.send(json.dumps({"id": req["id"], "result": {"result": {"type": "boolean", "value": True}}}))
                    await self.emit({"type": "scanWorld", "world": "deepdesert_1", "seed": "7"})
                    for m in BUS:
                        await self.emit(m)
                    continue
                else:
                    value = "ok"
            await ws.send(json.dumps({"id": req["id"], "result": {"result": {"type": "string", "value": value}}}))

    async def emit(self, m: dict) -> None:
        await self.ws.send(json.dumps({"method": "Runtime.bindingCalled", "params": {"name": BINDING, "payload": json.dumps(m)}}))

    def calls(self, fn: str) -> list[str]:
        return [e for e in self.evaluated if f"__dd.{fn}(" in e]


class StubPage:
    """A page as the runner sees it, recording every expression sent into it."""

    def __init__(self, pid: str, url: str) -> None:
        self.id, self.url, self.closed, self.evaluated = pid, url, asyncio.Event(), []

    @property
    def is_minimap(self) -> bool:
        return "minimap" in self.url

    async def evaluate(self, expr: str, **_) -> str:
        fn = expr.split("(", 1)[0].rsplit(".", 1)[1]  # the bridge function called
        if fn != "setMinimapHidden":  # the minimap filter has its own rules and tests
            self.evaluated.append(fn)
        return "ok"


def test_pages_get_only_what_changed_and_a_reloaded_page_gets_everything(tmp_path):
    async def scenario():
        det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
        hub = CompanionHub(1)
        runner = Runner(det, hub)
        big, mini = StubPage("b", "https://app.gaming.tools/dune"), StubPage("m", "https://app.gaming.tools/dune/minimap")
        hub.pages = {"b": big, "m": mini}
        await runner.push()
        assert big.evaluated == ["set", "renderPicker"] and mini.evaluated == ["set", "renderBadge"]
        det.publish_rev += 1  # a bump with nothing new to show
        await runner.push()
        assert len(big.evaluated) == 2 and len(mini.evaluated) == 2
        det.ui("resource", "iron")  # no position yet: same map state, new picker choice and badge text
        await runner.push()
        assert big.evaluated[2:] == ["renderPicker"] and mini.evaluated[2:] == ["renderBadge"]
        await runner._push_page(mini)  # reloaded: its ping answered 'empty'
        assert mini.evaluated[3:] == ["set", "renderBadge"] and len(big.evaluated) == 3

    asyncio.run(scenario())


async def until(pred, activity: asyncio.Event, timeout: float = 15.0) -> None:
    """Re-check `pred` whenever the detector calls into a page; fail after `timeout` seconds."""
    async with asyncio.timeout(timeout):
        while True:
            activity.clear()
            if pred():
                return
            await activity.wait()


async def fake_devtools(pages: list[FakePage]):
    servers = []
    listing = []
    for p in pages:
        s = await websockets.serve(p.handler, "127.0.0.1", 0)
        servers.append(s)
        listing.append({"id": p.id, "type": "page", "url": p.url, "webSocketDebuggerUrl": f"ws://127.0.0.1:{s.sockets[0].getsockname()[1]}/"})
    body = json.dumps(listing).encode()

    async def http(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
        await writer.drain()
        writer.close()

    http_server = await asyncio.start_server(http, "127.0.0.1", 0)
    return servers + [http_server], http_server.sockets[0].getsockname()[1]


def test_live_pipeline_drives_both_companion_pages(tmp_path, monkeypatch):
    def no_network(*_):
        raise AssertionError("no guild key: the sync loop must not call the server")

    monkeypatch.setattr(SyncClient, "exchange", no_network)
    registered, priority = [], priority_class()
    monkeypatch.setattr(ctypes.windll.user32, "RegisterHotKey", lambda *a: registered.append(a[1]) or 0)
    monkeypatch.setattr(hotkeys, "start", lambda *_: None)  # Ctrl+Alt hotkeys are global: they belong to this desktop
    monkeypatch.setattr(Governor, "start", lambda self: None)  # its process policy would stay on pytest and every child

    async def scenario():
        activity = asyncio.Event()
        mini = FakePage("m", "https://app.gaming.tools/dune/minimap", activity)
        big = FakePage("b", "https://app.gaming.tools/dune", activity)
        servers, port = await fake_devtools([big, mini])
        det = Detector(tmp_path / "journals", data_dir=tmp_path, min_rescore_s=0)
        det.ui("resource", "iron")
        runner = Runner(det, CompanionHub(port, poll_s=0.2))
        task = asyncio.create_task(runner.run())
        try:
            def pushed():  # the latest push has reached both pages
                badge = mini.calls("renderBadge")[-1] if mini.calls("renderBadge") else ""
                return bool(det.scan.rows) and "best" in badge and all(
                    p.calls("set") and '"route": {' in p.calls("set")[-1] for p in (mini, big))

            await until(pushed, activity)
            snap = det.snapshot()
            assert snap["connected"] and snap["worldName"] == "Deep Desert"
            assert det.scan.map_id == "deepdesert_1" and snap["scanRows"] == 1
            assert snap["vehicle"] == "foot" and snap["planVehicle"] == "buggy"
            # both pages receive the same recommendation; only the map window renders a picker, only the minimap a badge
            for page in (mini, big):
                last = page.calls("set")[-1]
                assert json.dumps("Iron Ore · best for buggy") in last
                assert '"mapId": "deepdesert_1"' in last  # the page's own map id, learned from its scan fetch
            assert big.calls("renderPicker") and mini.calls("renderBadge")
            assert json.dumps("Iron Ore · on foot, plan buggy · best")[1:-1] in mini.calls("renderBadge")[-1]
            # a UI pick from the map window flows back and is pushed out
            await big.emit({"type": "ui", "action": "resource", "value": "spice"})
            await until(lambda: json.dumps("Spice · ")[1:-1] in mini.calls("renderBadge")[-1], activity)
            assert json.dumps("Spice · ")[1:-1] in mini.calls("renderBadge")[-1]
            assert '"dest": null' in mini.calls("set")[-1]  # no spice known: nothing stale left on the map
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            for s in servers:
                s.close()

    asyncio.run(scenario())
    assert registered == [] and priority_class() == priority  # this desktop's hotkeys and the test process stay as they were


async def _call_page(answer):
    """One evaluate on a real Page whose DevTools socket replies to the call through `answer(ws, request)`."""
    async def handler(ws):
        await answer(ws, json.loads(await ws.recv()))

    server = await websockets.serve(handler, "127.0.0.1", 0)
    ws = await websockets.connect(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/")
    page = Page({"id": "p", "url": "https://app.gaming.tools/dune"}, ws, asyncio.Queue())
    try:
        return await page.evaluate("1 + 1")
    finally:
        await page.close()
        server.close()


def test_a_page_closing_mid_call_fails_the_call_instead_of_cancelling_the_callers_loop():
    async def close(ws, _req):
        await ws.close()

    with pytest.raises(ConnectionError):  # a CancelledError here would end a supervised loop for good
        asyncio.run(_call_page(close))


def test_a_devtools_error_answer_raises_instead_of_reading_as_none():
    async def error(ws, req):
        await ws.send(json.dumps({"id": req["id"], "error": {"code": -32000, "message": "Execution context was destroyed."}}))

    with pytest.raises(RuntimeError, match="Execution context was destroyed"):
        asyncio.run(_call_page(error))


def test_a_half_answer_from_the_devtools_port_counts_as_unreachable(monkeypatch):
    async def scenario():
        loop, polled, calls = asyncio.get_running_loop(), asyncio.Event(), []

        def half_answer(_port):
            calls.append(1)
            if len(calls) == 2:
                loop.call_soon_threadsafe(polled.set)
            raise http.client.IncompleteRead(b"[", 10)

        monkeypatch.setattr(feed, "list_targets", half_answer)
        hub = CompanionHub(1, poll_s=0)
        task = asyncio.create_task(hub.maintain())
        waiter = asyncio.create_task(polled.wait())
        try:
            await asyncio.wait({task, waiter}, timeout=15, return_when=asyncio.FIRST_COMPLETED)
            assert waiter.done(), task.exception() if task.done() else "no second poll"
            assert hub._failing_since is not None
        finally:
            for t in (task, waiter):
                t.cancel()
            await asyncio.gather(task, waiter, return_exceptions=True)

    asyncio.run(scenario())


class PingPage(StubPage):
    """A StubPage whose ping is answered by `answer()`; `pinged` is set on every ping, `refilled` on a
    state push after a ping."""

    def __init__(self, pid: str, url: str, answer) -> None:
        super().__init__(pid, url)
        self.answer, self.pinged, self.refilled = answer, asyncio.Event(), asyncio.Event()

    async def evaluate(self, expr: str, **kw) -> str:
        if "ping()" in expr:
            self.evaluated.append("ping")
            self.pinged.set()
            return self.answer()
        value = await super().evaluate(expr, **kw)
        if "ping" in self.evaluated and self.evaluated[-1] == "set":
            self.refilled.set()
        return value


async def _tick_until(runner: Runner, event: asyncio.Event) -> None:
    task = asyncio.create_task(runner.tick())
    try:
        await asyncio.wait_for(event.wait(), 15)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_one_page_failing_its_ping_still_pings_and_refills_the_others(tmp_path, caplog):
    def broken():
        raise TimeoutError

    async def scenario():
        runner = Runner(Detector(None, data_dir=tmp_path, min_rescore_s=0), CompanionHub(1))
        bad = PingPage("b", "https://app.gaming.tools/dune", broken)
        lost = PingPage("m", "https://app.gaming.tools/dune/minimap", lambda: "none")  # reloaded: lost our state
        runner.hub.pages = {"b": bad, "m": lost}
        await _tick_until(runner, lost.refilled)  # the refill comes after the ping, not with it
        return lost.evaluated

    with caplog.at_level(logging.WARNING, logger="harvesting_dune"):
        evaluated = asyncio.run(scenario())
    assert "set" in evaluated[evaluated.index("ping"):]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]  # a routine reload is no problem notice


def test_a_closed_gate_keeps_the_scan_layer_reload_pending(tmp_path):
    async def scenario():
        det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
        det.gate_required = True  # no key: the gate stays closed
        det.scan._restored = True  # confirmed-stale rows from a past run still wait for a reload
        assert det.scan_refresh_due(time.time()) and not det.gate_open()
        runner = Runner(det, CompanionHub(1))
        page = PingPage("m", "https://app.gaming.tools/dune/minimap", lambda: "ok")
        runner.hub.pages = {"m": page}
        await _tick_until(runner, page.pinged)  # the ping comes after the scan refresh in a tick
        return page.evaluated, det.scan_refresh_due(time.time())

    evaluated, due = asyncio.run(scenario())
    assert "refreshScan" not in evaluated and due  # reloaded once the gate opens, not used up now


class ScanPage(PingPage):
    """A PingPage whose scan-layer reload is answered by `scan()`."""

    def __init__(self, pid: str, url: str, scan) -> None:
        super().__init__(pid, url, lambda: "ok")
        self.scan = scan

    async def evaluate(self, expr: str, **kw) -> str:
        if "refreshScan()" in expr:
            self.evaluated.append("refreshScan")
            return self.scan()
        return await super().evaluate(expr, **kw)


def test_a_scan_layer_reload_no_page_carried_out_stays_pending(tmp_path):
    async def scenario():
        det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
        det.scan._restored = True  # confirmed-stale rows from a past run still wait for a reload
        runner = Runner(det, CompanionHub(1))
        page = ScanPage("m", "https://app.gaming.tools/dune/minimap", lambda: "no-session")
        runner.hub.pages = {"m": page}
        await _tick_until(runner, page.pinged)  # the ping comes after the scan refresh in a tick
        return page.evaluated, det.scan.refresh_pending

    evaluated, pending = asyncio.run(scenario())
    assert "refreshScan" in evaluated and pending


def test_a_reloaded_page_reloads_its_scan_layer_so_removed_rows_stay_gone(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    session(det, "DD")
    det.load_scan([{"type": "MagnetiteOre", "map_marker_id": "magnetiteore", "x": 1000, "y": 0, "z": 0}], 200, 0)
    assert det.scan.mark_mined("magnetiteore", 1000, 0, 0, 3600)
    det.scan_refreshed(0)  # every page's scan layer was reloaded without the row
    runner = Runner(det, CompanionHub(1))
    page = StubPage("b", "https://app.gaming.tools/dune")
    asyncio.run(runner._push_page(page))  # the page reloaded (its ping answered 'empty') and fetched every row
    assert "refreshScan" in page.evaluated[page.evaluated.index("set") + 1:]


def test_a_clean_exit_takes_the_picker_out_of_the_map_window(tmp_path, monkeypatch):
    monkeypatch.setattr(hotkeys, "start", lambda *_: None)

    async def idle():
        await asyncio.Event().wait()

    async def scenario():
        det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
        hub = CompanionHub(1)
        runner = Runner(det, hub, exit_when_gone_s=1)
        runner.governor.start = lambda: None
        for name in ("consume", "tick", "refresh_scan", "refresh_loot_index", "sync_loop", "data_loop"):
            setattr(runner, name, idle)
        hub.maintain = idle
        big = StubPage("b", "https://app.gaming.tools/dune")
        hub.pages = {"b": big}
        await runner.push()
        assert "renderPicker" in big.evaluated
        hub._seen, hub._failing_since = True, time.monotonic() - 60  # the companion's port has stopped answering
        await asyncio.wait_for(runner.run(), 15)
        return big.evaluated

    evaluated = asyncio.run(scenario())
    assert "hidePicker" in evaluated[evaluated.index("renderPicker"):]


class ClosingWS:
    """A DevTools socket that closes while a call is being sent: the reader fails the waiter first."""

    def __init__(self) -> None:
        self.gone = asyncio.Event()
        self.page = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self.gone.wait()
        raise OSError("connection lost")

    async def send(self, _):
        self.gone.set()
        await self.page.closed.wait()
        raise OSError("connection lost")

    async def close(self) -> None:
        pass


def test_a_page_that_closes_behind_a_call_files_no_problem_report():
    async def scenario():
        reports = []
        asyncio.get_running_loop().set_exception_handler(lambda _loop, ctx: reports.append(ctx.get("message")))
        ws = ClosingWS()
        ws.page = Page({"id": "p", "url": "https://app.gaming.tools/dune"}, ws, asyncio.Queue())
        try:
            await ws.page.evaluate("1 + 1")
        except OSError:
            pass  # the caller learns of the close; nothing else may report it
        gc.collect()
        return reports

    assert asyncio.run(scenario()) == []
