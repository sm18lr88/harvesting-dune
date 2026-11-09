# Feed: DevTools transport to the companion's pages; installs bridge.js and relays messages.
"""Two-way bridge to the gaming.tools companion's WebView2 pages over DevTools.

Inbound: the companion's native side (which reads the game through its own driver)
pushes `{type, topic, payload}` messages into its pages via `chrome.webview`. bridge.js
forwards the Dune topics from one primary page to us.

Outbound: bridge.js dispatches synthetic messages into every page through the page's
own listeners, so the companion renders our destination/route natively, and it filters
confirmed-stale rows out of the page's scan-database fetch.

Requires the companion to be started with a local DevTools port, see
scripts/start-companion-feed.ps1. Nothing touches the game process.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import urllib.request
from collections.abc import AsyncIterator
from importlib import resources
from typing import Any

import websockets

from . import logsetup
from .model import BusMessage  # re-exported: callers may import it from here

__all__ = ["BINDING", "BRIDGE_SCRIPT", "TOPICS", "BusMessage", "CompanionHub", "Page", "companion_pages", "to_bus"]

log = logging.getLogger(__name__)

TOPICS = [
    "dune.session",
    "dune.player",
    "dune.map.actors",
    "dune.map.markers",
    "dune.status",
    "dune.trackedDestination",
    "dune.map.route",
    "dune.landsraad.routeMode",
    "dune.rawStats",
    "dune.trackedSession",
    "dune.skillProgress",
    "dune.knownRecipes",
    "dune.achievements",
    "dune.landsraad",
]
FORWARDED = {"dune.session", "dune.player", "dune.map.actors", "dune.status", "dune.rawStats", "dune.trackedSession",
             "dune.trackedDestination",
             "dune.skillProgress", "dune.knownRecipes", "dune.achievements", "dune.landsraad"}
BINDING = "__harvestingDuneEmit"
_BRIDGE_TEMPLATE = (
    resources.files(__package__).joinpath("bridge.js").read_text("utf8")
    .replace("%TOPICS%", json.dumps(TOPICS))
    .replace("%BINDING%", BINDING)
)
# versioned on the final script, so a changed topic list also refreshes live pages
BRIDGE_VERSION = hashlib.blake2b(_BRIDGE_TEMPLATE.encode(), digest_size=6).hexdigest()
BRIDGE_SCRIPT = _BRIDGE_TEMPLATE.replace("%VERSION%", BRIDGE_VERSION)


def list_targets(port: int) -> list[dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as r:
        return json.load(r)


def companion_pages(targets: list[dict]) -> list[dict]:
    pages = [t for t in targets if t.get("type") == "page" and "gaming.tools" in t.get("url", "")]
    # minimap first: it stays open while playing, so it is the most reliable topic source
    pages.sort(key=lambda t: 0 if "minimap" in t.get("url", "").lower() else 1)
    return pages


class Page:
    def __init__(self, target: dict, ws: Any, inbox: asyncio.Queue) -> None:
        self.id = target["id"]
        self.url = target.get("url", "")
        self.ws = ws
        self._inbox = inbox
        self._next = 0
        self._pending: dict[int, asyncio.Future] = {}
        self.closed = asyncio.Event()
        self._reader = asyncio.create_task(self._read())

    @property
    def is_minimap(self) -> bool:
        return "minimap" in self.url.lower()

    async def _read(self) -> None:
        try:
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                    fut = self._pending.pop(msg.get("id", -1), None)
                    params = msg.get("params") or {}
                    if fut is not None:
                        if not fut.done():
                            fut.set_result(msg)
                    elif msg.get("method") == "Runtime.bindingCalled" and params.get("name") == BINDING:
                        await self._inbox.put((self, json.loads(params["payload"])))
                    elif msg.get("method") == "Page.frameNavigated" and not (params.get("frame") or {}).get("parentId"):
                        self.url = params["frame"].get("url", self.url)
                except (ValueError, KeyError, TypeError, AttributeError) as exc:
                    logsetup.limited(log, f"devtools:{self.url}", "ignored a malformed DevTools message from %s: %r", self.url, exc)
        except (websockets.WebSocketException, OSError) as exc:
            log.info("connection to %s ended (%r)", self.url, exc)  # the page closed or reloaded
        finally:
            self.closed.set()
            for fut in self._pending.values():
                if not fut.done():
                    fut.cancel()

    async def call(self, method: str, timeout: float = 15, **params: Any) -> dict:
        if self.closed.is_set():
            raise ConnectionError(f"page {self.url} is closed")
        self._next += 1
        call_id = self._next
        fut = asyncio.get_running_loop().create_future()
        self._pending[call_id] = fut
        try:
            await self.ws.send(json.dumps({"id": call_id, "method": method, "params": params}))
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(call_id, None)  # timed out or failed: never leak the waiter

    async def evaluate(self, expression: str, await_promise: bool = False, timeout: float = 15) -> Any:
        res = await self.call("Runtime.evaluate", timeout=timeout, expression=expression, returnByValue=True, awaitPromise=await_promise)
        result = res.get("result", {})
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            # "Uncaught" alone says nothing: keep the exception's own description (type, message, stack)
            described = (details.get("exception") or {}).get("description") or details.get("text", "page exception")
            raise RuntimeError(f"{described.splitlines()[0][:300]} (in {expression[:60]!r})")
        return result.get("result", {}).get("value")

    async def install(self) -> str:
        await self.call("Runtime.enable")
        await self.call("Page.enable")
        await self.call("Runtime.addBinding", name=BINDING)  # survives reloads
        await self.call("Page.addScriptToEvaluateOnNewDocument", source=BRIDGE_SCRIPT)
        result = await self.evaluate(BRIDGE_SCRIPT)
        if result == "outdated":
            # an older bridge is live in this page and cannot be unhooked in place;
            # a reload brings the page back with the new document-start script
            await self.call("Page.reload")
            return "reloaded (bridge updated)"
        return result

    async def close(self) -> None:
        self._reader.cancel()
        await self.ws.close()


class CompanionHub:
    """Keeps a bridge in every companion page; yields inbound messages."""

    def __init__(self, port: int, poll_s: float = 4.0) -> None:
        self.port = port
        self.poll_s = poll_s
        self.pages: dict[str, Page] = {}
        self.primary: Page | None = None
        self.inbox: asyncio.Queue[tuple[Page, dict]] = asyncio.Queue()
        self.on_attach: list = []  # async callbacks(page) run after a page is (re)installed
        self._warned = False
        self._seen = False  # the DevTools port has answered at least once
        self._failing_since: float | None = None  # first failed poll of the current outage

    def unreachable_for(self) -> float | None:
        """Seconds the companion's DevTools port has been refusing us (None: it answers).

        Measured from the first FAILED poll, never from the last good one: a slow poll (a page
        mid-reload holding an evaluate for 15 s) is not the companion closing.
        """
        if not self._seen or self._failing_since is None:
            return None  # never seen yet (waiting for it to start is not "gone"), or answering
        return time.monotonic() - self._failing_since

    async def _attach(self, target: dict) -> None:
        ws = await websockets.connect(target["webSocketDebuggerUrl"], max_size=128 * 1024 * 1024)
        page = Page(target, ws, self.inbox)
        try:
            log.info("bridge %s: %s", page.url, await page.install())
        except Exception:
            await page.close()
            raise
        self.pages[page.id] = page
        for cb in self.on_attach:
            await cb(page)

    async def _choose_primary(self) -> None:
        live = [p for p in self.pages.values() if not p.closed.is_set()]
        live.sort(key=lambda p: 0 if p.is_minimap else 1)
        best = live[0] if live else None
        if best is not None and best is self.primary:
            # a reload reinstalls the bridge with primary=false; reassert it
            await best.evaluate("window.__dd && (window.__dd.primary = true)")
        if best is not self.primary:
            if self.primary and not self.primary.closed.is_set():
                await self.primary.evaluate("window.__dd && (window.__dd.primary = false)")
            self.primary = best
            if best:
                await best.evaluate("window.__dd && (window.__dd.primary = true)")
                log.info("primary topic source: %s", best.url)
                # re-send current state of every topic from the new source
                await best.evaluate(f"chrome.webview.postMessage({{type:'subscribe',payload:{{topics:{json.dumps(TOPICS)}}}}})")

    async def maintain(self) -> None:
        while True:
            try:
                targets = companion_pages(await asyncio.to_thread(list_targets, self.port))
                self._warned = False
                self._seen, self._failing_since = True, None
            except (OSError, ValueError) as exc:  # refused, timed out, or a half-answer mid-shutdown
                if self._failing_since is None:
                    self._failing_since = time.monotonic()
                if not self._warned:
                    log.warning("DevTools port %s not reachable (%s); start the companion with scripts/start-companion-feed.ps1", self.port, exc)
                    self._warned = True
                targets = []
            for pid in [pid for pid, p in self.pages.items() if p.closed.is_set()]:
                self.pages.pop(pid)
            for t in targets:
                if t["id"] not in self.pages:
                    try:
                        await self._attach(t)
                    except Exception as exc:  # noqa: BLE001 - page may be mid-navigation
                        log.warning("could not attach to %s: %s", t.get("url"), exc)
            try:
                await self._choose_primary()
            except Exception as exc:  # noqa: BLE001 - one failure must not stop the detector
                log.warning("primary selection failed: %s", exc)
                self.primary = None
            await asyncio.sleep(self.poll_s)

    async def messages(self) -> AsyncIterator[tuple[Page, dict]]:
        while True:
            yield await self.inbox.get()

    async def broadcast(self, expression: str) -> None:
        for page in list(self.pages.values()):
            if page.closed.is_set():
                continue
            try:
                await page.evaluate(expression)
            except Exception as exc:  # noqa: BLE001 - one failure must not stop the detector
                logsetup.limited(log, f"broadcast:{page.url}", "sending to %s failed: %r", page.url, exc)

    async def fetch_scan(self, world: str, seed: int) -> tuple[int, list[dict]]:
        page = self.primary
        if page is None:
            return 0, []
        raw = await page.evaluate(f"window.__dd.fetchScan({json.dumps(world)}, {json.dumps(seed)})", await_promise=True)
        data = json.loads(raw or "{}")
        return data.get("status", 0), data.get("rows", [])


def to_bus(message: dict) -> BusMessage | None:
    if message.get("type") in ("helloAck",) or message.get("topic") in FORWARDED:
        return BusMessage(message.get("type", ""), message.get("topic"), message.get("payload"))
    return None
