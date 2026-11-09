# Runner: the live loops that connect the detector, the companion pages and the settings page.
"""Live wiring: companion pages <-> detector, scan refresh, hotkeys, debug viewer."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from urllib.parse import urlsplit

from . import hotkeys, logsetup
from .app import Detector
from .feed import CompanionHub, Page, to_bus
from .governor import Governor
from .zones import fit_grid

log = logging.getLogger(__name__)

SCAN_REFRESH_S = 600.0
RESTART_BACKOFF_S = (1.0, 5.0, 30.0)  # a crashing background task is restarted, slower each time
MAP_WINDOW = re.compile(r"^/dune/?$")  # the only page bridge.js shows the picker in


def _js_call(fn: str, *args: object) -> str:
    return f"window.__dd && window.__dd.{fn}({', '.join(json.dumps(a) for a in args)})"


class Runner:
    def __init__(self, det: Detector, hub: CompanionHub, exit_when_gone_s: float = 0.0) -> None:
        self.det = det
        self.hub = hub
        self.exit_when_gone_s = exit_when_gone_s  # 0: keep waiting for the companion forever
        self._pushed_rev = -1
        self._sent: dict[str, dict[str, str]] = {}  # page id -> slot -> the expression that page last got
        self.pushed: dict[str, list[int]] = {}  # page url -> [calls, bytes] sent by push, for diagnosis
        self._minimap_rev = -1
        self._scan_key: tuple | None = None
        self._scan_failures = 0
        self._problems = 0
        self.governor = Governor(det, det.journal.directory)
        self.sync = None  # sync.SyncClient, built by sync_loop on first use
        hub.on_attach.append(self._push_page)

    def _expressions(self) -> dict[str, str]:
        if not self.det.gate_open():  # gate.py: nothing of ours on the companion pages
            return {"set": _js_call("set", self.det.page_state()), "picker": _js_call("hidePicker"),
                    "badge": _js_call("renderBadge", None)}
        p = self.det.picker()
        return {
            "set": _js_call("set", self.det.page_state()),
            "picker": _js_call("renderPicker", p["resources"], p["vehicles"], p["selected"], p["vehicle"], p["status"], p["minimap"]),
            "badge": _js_call("renderBadge", self.det.badge()),
        }

    async def _send(self, page: Page, exprs: dict[str, str]) -> None:
        """Only what this page shows and has not got yet: an unchanged push costs the page nothing."""
        path = urlsplit(page.url).path
        sent = self._sent.setdefault(page.id, {})
        for slot, expr in exprs.items():
            if (slot == "picker" and not MAP_WINDOW.match(path)) or (slot == "badge" and "minimap" not in path):
                continue
            if sent.get(slot) == expr:
                continue
            try:
                await page.evaluate(expr)
                sent[slot] = expr
            except Exception as exc:  # noqa: BLE001 - one failure must not stop the detector
                sent.pop(slot, None)
                logsetup.limited(log, f"push:{page.url}", "sending state to %s failed: %r", page.url, exc)
            stat = self.pushed.setdefault(page.url, [0, 0])
            stat[0] += 1
            stat[1] += len(expr)

    async def _push_page(self, page: Page) -> None:
        """A (re)attached or reloaded page holds none of our state: send it everything."""
        if page.is_minimap:
            self._minimap_rev = -1  # (re)attached minimap: make sure its filter matches
        self._sent.pop(page.id, None)
        await self._send(page, self._expressions())

    async def _push_all(self) -> None:
        exprs = self._expressions()
        for pid in [pid for pid in self._sent if pid not in self.hub.pages]:
            del self._sent[pid]
        for page in list(self.hub.pages.values()):
            if not page.closed.is_set():
                await self._send(page, exprs)

    async def apply_minimap_filter(self) -> None:
        """Write the minimap's own filter list (the minimap page reloads to apply it)."""
        if self.det.minimap_rev == self._minimap_rev or not self.det.gate_open():
            return
        self._minimap_rev = self.det.minimap_rev
        hidden, managed = self.det.minimap_filter()
        for page in list(self.hub.pages.values()):
            if page.is_minimap and not page.closed.is_set():
                try:
                    result = await page.evaluate(_js_call("setMinimapHidden", hidden, managed))
                    log.info("minimap filter: %s hidden (%s)", len(hidden), result)
                except Exception as exc:  # noqa: BLE001 - one failure must not stop the detector
                    log.warning("minimap filter not applied: %s", exc)

    async def push(self) -> None:
        await self.apply_minimap_filter()
        if self.det.publish_rev == self._pushed_rev:
            return
        self._pushed_rev = self.det.publish_rev
        await self._push_all()
        self.det.publish_viewer()

    async def consume(self) -> None:
        async for page, m in self.hub.messages():
            if not isinstance(m, dict):
                continue
            try:
                kind = m.get("type")
                if kind == "ui":
                    self.det.ui(str(m.get("action", "")), m.get("value"))
                elif kind == "scanWorld":
                    self.det.on_scan_world(m.get("world"))
                elif kind == "log":  # an error inside bridge.js, in a companion page
                    where = str(m.get("where", "?"))[:40]
                    logsetup.limited(log, f"bridge:{page.url}:{where}", "bridge.js in %s, %s: %s",
                                     page.url, where, str(m.get("message", ""))[:500], level=logging.ERROR)
                else:
                    msg = to_bus(m)
                    if msg is not None:
                        self.det.handle(msg, time.time())
            except Exception:
                log.exception("ignored a message from %s", page.url)  # one odd message must not stop the feed

    async def tick(self) -> None:
        last_ping = 0.0
        last_view = 0.0
        last_grid = 0.0
        while True:
            # idle (menus, overland map, nothing tracked): check far less often
            busy = self.det.store.pose is not None and bool(self.det.store.items)
            await asyncio.sleep(0.5 if busy else 2.0)
            try:
                now = time.time()
                self.det.gate_poll(now)  # the 72 h grace can run out between two checks
                self.governor.tick(now)
                if logsetup.PROBLEMS.count != self._problems:
                    self._problems = logsetup.PROBLEMS.count
                    self.det.publish_rev += 1  # the pages show a notice: push now
                await self.det.maybe_rescore_async(now)
                await self.push()
                if self.det.listeners and now - last_view > 2:
                    last_view = now  # control page: distances to cages etc. change as you move
                    self.det.publish_viewer()
                if self.det.scan_refresh_due(now):
                    await self._push_all()  # stale list first
                    await self.hub.broadcast(_js_call("refreshScan"))
                    self.det.scan_refreshed(now)
                    log.info("companion scan layer reloaded without %s confirmed-stale entries", len(self.det.scan.stale))
                if now - last_grid > 60:
                    last_grid = now
                    await self.fit_grid()
                    await self.load_shipwrecks()
                if time.time() - last_ping > 5:
                    last_ping = time.time()
                    for page in list(self.hub.pages.values()):
                        if page.closed.is_set():
                            continue
                        # a page that lost its state (reload / dead-man clear) gets it again
                        if await page.evaluate("window.__dd ? window.__dd.ping() : 'none'") != "ok":
                            await self._push_page(page)
            except Exception:
                log.exception("tick failed")

    async def fit_grid(self) -> None:
        """Fit this map's grid from the companion's own markers (once per run and map)."""
        world_id = self.det.grid_fit_wanted()
        if world_id is None:
            return
        for page in list(self.hub.pages.values()):
            if page.closed.is_set():
                continue
            try:
                samples = await page.evaluate("window.__dd && __dd.gridSamples ? __dd.gridSamples(5000) : []")
                if not samples:  # no labelled markers drawn here: read the map data file instead
                    samples = await page.evaluate(
                        "window.__dd && __dd.gridSamplesFromData ? __dd.gridSamplesFromData(5000) : []", await_promise=True)
            except Exception as exc:  # noqa: BLE001 - one failure must not stop the detector
                logsetup.limited(log, f"grid:{page.url}", "grid samples from %s failed: %r", page.url, exc)
                continue
            if samples:
                fit = await asyncio.to_thread(fit_grid, samples)  # about a second for a full map
                if self.det.on_grid_samples(world_id, samples, fit):
                    return

    async def load_shipwrecks(self) -> None:
        """The named shipwrecks of this map, from the companion's own map data (once per map and cycle)."""
        if not self.det.shipwrecks_wanted():
            return
        for page in list(self.hub.pages.values()):
            if page.closed.is_set():
                continue
            try:
                rows = await page.evaluate(
                    "window.__dd && __dd.shipwrecksFromData ? __dd.shipwrecksFromData() : null", await_promise=True)
            except Exception as exc:  # noqa: BLE001 - one failure must not stop the detector
                logsetup.limited(log, f"wrecks:{page.url}", "shipwrecks from %s failed: %r", page.url, exc)
                continue
            if self.det.on_shipwrecks(rows):
                return

    async def refresh_scan(self) -> None:
        while True:
            await asyncio.sleep(2)
            req = self.det.scan_request()
            if req is None or self.hub.primary is None:
                continue
            fetched = self.det.scan.fetched_at
            if req == self._scan_key and fetched is not None and time.time() - fetched < SCAN_REFRESH_S:
                continue
            self._scan_key = req
            try:
                status, rows = await self.hub.fetch_scan(*req)
                if status != 200:
                    raise RuntimeError(f"HTTP {status}")
            except Exception as exc:  # noqa: BLE001 - one failure must not stop the detector
                # keep the rows we have; retry soon at first, then every 10 min
                self._scan_failures += 1
                wait = min(SCAN_REFRESH_S, 30.0 * 2 ** (self._scan_failures - 1))
                log.warning("scan database fetch for %s/%s failed: %r; retrying in %.0f s", *req, exc, wait)
                self.det.scan.fetched_at = time.time() - SCAN_REFRESH_S + wait
                continue
            self._scan_failures = 0
            self.det.load_scan(rows, status, time.time())

    async def refresh_loot_index(self) -> None:
        """Build (or refresh weekly) the rare-cage loot index through a companion page."""
        from .loot import CRAWL_SCRIPT

        while True:
            await asyncio.sleep(5)
            if (not self.det.loot.stale and self.det.items.items) or self.hub.primary is None:
                await asyncio.sleep(55)
                continue
            try:
                log.info("building rare-cage loot index from gaming.tools item data (about 30 s)")
                raw = await self.hub.primary.evaluate(CRAWL_SCRIPT, await_promise=True, timeout=240)
                n = self.det.loot.ingest_crawl(raw)
                m = self.det.items.ingest(json.loads(raw))
                log.info("loot index ready: %s rare/ultra-rare cage locations, %s lootable materials", n, m)
                self.det.publish_viewer()
            except Exception as exc:  # noqa: BLE001 - one failure must not stop the detector
                log.warning("loot index build failed (%r); retrying in 10 min", exc)
                await asyncio.sleep(600)

    def on_hotkey(self, action: str) -> None:  # runs as a loop callback: never raise
        try:
            log.info("hotkey: %s", action)
            if action == "minimap":  # the companion's own widget setting: only a page can change it
                asyncio.get_running_loop().create_task(self.toggle_minimap())
            else:
                self.det.ui(action)
        except Exception:
            log.exception("hotkey %s failed", action)

    async def toggle_minimap(self) -> None:
        page = self.hub.primary
        if page is None:
            log.info("minimap hotkey: no companion page yet")
            return
        try:
            log.info("minimap: %s", await page.evaluate(_js_call("toggleMinimap")))
        except Exception as exc:  # noqa: BLE001 - a hotkey must never stop the detector
            log.warning("minimap hotkey failed: %r", exc)

    def _client(self):
        from . import sync  # here, not at import: replay never loads the network client

        if self.sync is None:
            self.sync = sync.SyncClient()
        return self.sync

    async def sync_loop(self) -> None:
        from . import syncloop

        await syncloop.poll(self.det, self._client())

    async def data_loop(self) -> None:
        from .datacache import DataCache
        from .dataloop import DataLoop

        await DataLoop(self.det, self._client(), DataCache(self.det.data_dir)).run()

    async def _supervised(self, name: str, factory) -> None:
        """Run a background loop forever: a crash is logged and the loop restarted, so one bug in
        one part (scan refresh, loot index...) never takes the lines off the minimap."""
        failures = 0
        while True:
            started = time.monotonic()
            try:
                await factory()
                log.warning("%s ended unexpectedly; restarting", name)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s crashed; restarting", name)
            failures = 0 if time.monotonic() - started > 300 else failures + 1
            await asyncio.sleep(RESTART_BACKOFF_S[min(failures, len(RESTART_BACKOFF_S) - 1)])

    async def _watch_companion(self) -> None:
        """Exit cleanly (saving state and clearing the pages) once the companion has been gone
        for `exit_when_gone_s`; the autorun watcher only has to force-stop a stuck detector."""
        while True:
            await asyncio.sleep(2)
            gone = self.hub.unreachable_for()
            if self.exit_when_gone_s and gone is not None and gone >= self.exit_when_gone_s:
                log.info("companion closed %.0f s ago: stopping", gone)
                return

    async def run(self) -> None:
        self.governor.start()
        stop_hotkeys = hotkeys.start(asyncio.get_running_loop(), self.on_hotkey)
        loops = {"companion link": self.hub.maintain, "feed": self.consume, "tick": self.tick,
                 "scan refresh": self.refresh_scan, "loot index": self.refresh_loot_index,
                 "guild sync": self.sync_loop, "datasets": self.data_loop}
        tasks = [asyncio.create_task(self._supervised(n, f)) for n, f in loops.items()]
        watch = asyncio.create_task(self._watch_companion())
        try:
            await watch
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.sync is not None:  # the guild sees us leave now, not after the deadline
                from . import syncloop

                await asyncio.to_thread(syncloop.goodbye, self.det, self.sync)
            if stop_hotkeys:
                stop_hotkeys()
            self.det.save_terrain(time.time(), force=True)
            # leave the companion as we found it
            self.det.enabled = False
            self.det.scan.stale.clear()
            await self.hub.broadcast(_js_call("set", {}))
            await self.hub.broadcast(_js_call("renderBadge", None))
