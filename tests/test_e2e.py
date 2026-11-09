"""End to end: two fake companion pages over DevTools -> hub -> detector -> pushes back to both pages."""

import asyncio
import json

import websockets
from helpers import CELL_CM, ore

from harvesting_dune.app import Detector
from harvesting_dune.feed import BINDING, CompanionHub
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
