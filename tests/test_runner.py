"""Live loops on their own: the scan fetch, the minimap filter and the companion link, each without a real companion."""

import asyncio
import json
import logging
import socket
import types
import urllib.request

import websockets
from helpers import M, actors, pose, session, spice

from harvesting_dune import feed, statuspage, sync
from harvesting_dune import runner as runner_mod
from harvesting_dune.app import Detector
from harvesting_dune.catalog import respawn_s
from harvesting_dune.feed import CompanionHub
from harvesting_dune.runner import Runner
from harvesting_dune.zones import GridFit

ROW_A = {"type": "MagnetiteOre", "map_marker_id": "magnetiteore", "x": 1000, "y": 0, "z": 0}


def test_scan_rows_fetched_for_one_world_never_become_the_next_worlds_rows(tmp_path, monkeypatch):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    session(det, "survival_1")
    real_sleep = asyncio.sleep  # the loop polls every 2 s: let its sleeps return at once
    monkeypatch.setattr(runner_mod, "asyncio", types.SimpleNamespace(**{**vars(asyncio), "sleep": lambda _s: real_sleep(0)}))
    runner = Runner(det, CompanionHub(1))
    runner.hub.primary = object()
    asked = []

    async def scenario():
        second = asyncio.Event()

        async def fetch_scan(world, _seed):
            asked.append(world)
            if len(asked) == 1:
                session(det, "DD")  # the player travels while the page downloads survival_1's rows
                return 200, [ROW_A]
            second.set()
            return 503, []  # the new world's own fetch fails: whatever is held stays

        runner.hub.fetch_scan = fetch_scan
        task = asyncio.create_task(runner.refresh_scan())
        try:
            await asyncio.wait_for(second.wait(), 10)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert asked == ["survival_1", "DD"]
    assert det.scan.rows == []


class MinimapPage:
    """The minimap page as the runner sees it, keeping the filter lists written into it."""

    def __init__(self) -> None:
        self.id, self.url, self.closed, self.filters = "m", "https://app.gaming.tools/dune/minimap", asyncio.Event(), []

    @property
    def is_minimap(self) -> bool:
        return True

    async def evaluate(self, expr: str, **_) -> str:
        if "setMinimapHidden(" in expr:
            self.filters.append(json.loads("[" + expr[expr.index("(") + 1:expr.rindex(")")] + "]"))
        return "ok"


def test_a_closed_gate_gives_the_minimap_back_every_type_it_hid(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    runner = Runner(det, CompanionHub(1))
    page = MinimapPage()
    runner.hub.pages = {"m": page}
    asyncio.run(runner.apply_minimap_filter())
    hidden, managed = page.filters[-1]
    assert hidden and set(hidden) <= set(managed)  # gate open: the detector hides what you do not search
    det.gate_required = True  # no key: gate.py closes the gate
    asyncio.run(runner.apply_minimap_filter())
    assert page.filters[1:] == [[[], managed]]


class SilentPage:
    """A companion page that is slow to answer anything (a busy WebView2)."""

    def __init__(self) -> None:
        self.id, self.url, self.closed = "m", "https://app.gaming.tools/dune/minimap", asyncio.Event()

    @property
    def is_minimap(self) -> bool:
        return True

    async def evaluate(self, *_a, **_k) -> str:
        await asyncio.Event().wait()
        return "ok"


def test_one_failed_poll_is_checked_again_without_waiting_for_a_slow_page(monkeypatch):
    async def scenario():
        loop, reprobed, calls = asyncio.get_running_loop(), asyncio.Event(), []

        def listing(_port):
            calls.append(1)
            if len(calls) == 1:
                raise ConnectionRefusedError("refused")
            loop.call_soon_threadsafe(reprobed.set)
            return []

        monkeypatch.setattr(feed, "list_targets", listing)
        hub = CompanionHub(1, poll_s=30)
        hub.pages = {"m": SilentPage()}
        task = asyncio.create_task(hub.maintain())
        try:
            await asyncio.wait_for(reprobed.wait(), 10)  # the run is stopped 8 s into an outage
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return len(calls)

    assert asyncio.run(scenario()) == 2


def _markers_of(grid: GridFit) -> list[list]:
    """Labelled markers as a companion map draws them: three per cell of a 9x9 grid."""
    return [[f"{'ABCDEFGHI'[r]}{c + 1}", grid.col_1 + c * grid.col_step + (k - 1) * 70_000,
             grid.row_a + r * grid.row_step + (1 - k) * 60_000] for r in range(9) for c in range(9) for k in range(3)]


class GridPage:
    """A companion page holding one map's markers; `labelled` is False for a bridge from before the map id."""

    def __init__(self, map_id: str, samples: list, labelled: bool = True) -> None:
        self.id, self.url, self.closed = "g", "https://app.gaming.tools/dune", asyncio.Event()
        self.map_id, self.samples, self.labelled = map_id, samples, labelled

    async def evaluate(self, expr: str, **_):
        if "gridSamples" not in expr:
            return None
        if self.labelled and ", true)" in expr:
            return {"map": self.map_id, "samples": self.samples}
        return self.samples


def _fit_grid_from(det: Detector, page: GridPage) -> None:
    runner = Runner(det, CompanionHub(1))
    runner.hub.pages = {page.id: page}
    asyncio.run(runner.fit_grid())


OTHER_GRID = GridFit(900_000.0, -250_000.0, -1_000_000.0, 250_000.0)


def test_another_maps_markers_never_become_the_grid_of_the_world_you_are_in(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    session(det, "deepdesert_1")
    _fit_grid_from(det, GridPage("survival_1", _markers_of(OTHER_GRID)))  # the map window shows Hagga Basin
    assert not det.learning.grid_fitted("deepdesert_1")
    assert det.grid_fit_wanted() == "deepdesert_1"  # still to be fitted from this world's own map


def test_markers_of_the_map_you_are_on_fit_its_grid(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    session(det, "deepdesert_1")
    _fit_grid_from(det, GridPage("deepdesert_1_07", _markers_of(OTHER_GRID)))  # a weekly layout of this map
    assert det.learning.grid_fitted("deepdesert_1") and det.grid_fit_wanted() is None


def test_markers_from_a_bridge_that_names_no_map_are_not_fitted(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    session(det, "deepdesert_1")
    _fit_grid_from(det, GridPage("survival_1", _markers_of(OTHER_GRID), labelled=False))
    assert not det.learning.grid_fitted("deepdesert_1")


def test_the_app_reports_the_version_it_was_shipped_as(tmp_path, monkeypatch):
    shipped = tmp_path / "version.txt"
    shipped.write_text("0.2.0-dev.42\r\n", encoding="ascii")  # as build-zip.ps1 writes it beside pyproject.toml
    monkeypatch.setattr(statuspage, "VERSION_FILE", shipped, raising=False)
    assert statuspage.about()["version"] == "0.2.0-dev.42"
    assert sync.SyncClient().version == "0.2.0-dev.42"  # the X-Client-Version header


def test_a_damaged_version_file_never_reaches_the_guild_server_header(tmp_path, monkeypatch):
    shipped = tmp_path / "version.txt"
    shipped.write_text("0.2.0\r\nX-Injected: 1\r\n", encoding="ascii")
    monkeypatch.setattr(statuspage, "VERSION_FILE", shipped, raising=False)
    assert "\n" not in sync.SyncClient().version and "X-Injected" not in sync.SyncClient().version


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _proxy_set_on_this_pc(monkeypatch) -> None:
    dead = f"http://127.0.0.1:{_free_port()}"  # a proxy that refuses everything
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, dead)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    # urlopen keeps the opener of its first call: drop it, or an earlier test's call hides the proxy
    monkeypatch.setattr(urllib.request, "_opener", None)


def test_the_devtools_page_list_is_read_without_the_pcs_proxy(monkeypatch):
    _proxy_set_on_this_pc(monkeypatch)
    listing = [{"id": "p", "type": "page", "url": "https://app.gaming.tools/dune"}]
    body = json.dumps(listing).encode()

    async def scenario():
        async def http(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
                         b"Connection: close\r\n\r\n" % len(body) + body)
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(http, "127.0.0.1", 0)
        async with server:
            return await asyncio.to_thread(feed.list_targets, server.sockets[0].getsockname()[1])

    assert asyncio.run(scenario()) == listing


def test_a_companion_page_is_attached_without_the_pcs_proxy(monkeypatch):
    _proxy_set_on_this_pc(monkeypatch)

    async def scenario():
        reached = asyncio.Event()

        async def handler(ws):
            reached.set()
            await ws.close()

        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            hub = CompanionHub(1)
            target = {"id": "p", "type": "page", "url": "https://app.gaming.tools/dune",
                      "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/"}
            try:
                await hub._attach(target)
            except Exception:  # noqa: BLE001 - this page closes at once; only the connection is under test
                pass
            return reached.is_set()

    assert asyncio.run(scenario())


class TextPage:
    """A companion page keeping each expression the runner sends it."""

    def __init__(self, id_: str, path: str) -> None:
        self.id, self.url, self.closed, self.sent = id_, f"https://app.gaming.tools{path}", asyncio.Event(), []

    @property
    def is_minimap(self) -> bool:
        return "minimap" in self.url

    async def evaluate(self, expr: str, **_) -> str:
        self.sent.append(expr)
        return "ok"


def test_the_minimap_badge_and_the_picker_follow_you_as_you_move(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.ui("vehicle", "buggy")
    session(det, "DD", activity="Driving")
    actors(det, spice("s1", 3000 * M, 0, size="Large"), t=1)
    mini, window = TextPage("m", "/dune/minimap"), TextPage("w", "/dune")
    runner = Runner(det, CompanionHub(1))
    runner.hub.pages = {"m": mini, "w": window}

    async def scenario():
        pose(det, 100 * M, 0, t=2)
        det.compute(2)
        await runner.push()
        rev = det.publish_rev
        pose(det, 200 * M, 0, t=3)  # 2800 m from the field now
        det.compute(3)
        assert det.publish_rev == rev  # nothing on the map moved: only the texts changed
        await runner.push()

    asyncio.run(scenario())
    assert "best 2.8 km" in [e for e in mini.sent if "renderBadge" in e][-1]
    assert "2800 m away" in [e for e in window.sent if "renderPicker" in e][-1]


def test_a_respawn_sample_beyond_the_two_hour_bound_is_dropped_when_learned_json_loads(tmp_path, caplog):
    (tmp_path / "learned.json").write_text(json.dumps({"respawn": {"stravidiumore": [86400.0] * 5 + [1800.0]}}))
    caplog.set_level(logging.WARNING)
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    assert det.learning.samples("respawn", {"stravidiumore"}) == 1
    assert det.respawn_for("stravidiumore") == respawn_s("stravidiumore")  # too few real samples: the default
    assert caplog.records == []
