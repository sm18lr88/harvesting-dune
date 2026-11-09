# Data loop: when the detector asks the guild server for its datasets (on start, then once a day).
"""Runner starts `DataLoop.run` as the "datasets" loop (docs/SYNC-PROTOCOL.md "Datasets").

The list (`GET /v1/data`) is also the key check behind the gate (gate.py): it runs on start and then
at most once a day after an accepted check, sooner only to retry a failed one (RETRY_S). A dataset is
fetched only when its ETag in the list differs from the cached copy, with `If-None-Match`, and only
after the list call accepted the key. No key or a refused key: no request at all. Being offline is
normal, so a failure is a WARNING, never an ERROR (that would put a problem notice on the companion).
"""

from __future__ import annotations

import asyncio
import logging
import time

from . import logsetup
from .datacache import NAMES, DataCache
from .gate import REFUSED
from .sync import SyncClient, SyncError, SyncRejected

log = logging.getLogger(__name__)

DAY_S = 24 * 3600.0
RETRY_S = (60.0, 300.0, 900.0, 1800.0, 3600.0)
TICK_S = 5.0  # how soon a new key is checked
MAX_PAGES = 10  # 100 datasets a page: far more than this detector uses


def _page(body: object) -> tuple[dict[str, str], str | None]:
    """(name -> ETag, the `next` cursor) of one list page; bad rows are left out."""
    if not isinstance(body, dict):
        return {}, None
    rows = body.get("datasets") if isinstance(body.get("datasets"), list) else []
    tags = {r["name"]: r["etag"] for r in rows
            if isinstance(r, dict) and isinstance(r.get("name"), str) and isinstance(r.get("etag"), str)}
    after = body.get("next")
    return tags, after if isinstance(after, str) and after else None


class DataLoop:
    def __init__(self, det, client: SyncClient, cache: DataCache, clock=time.time) -> None:
        self.det, self.client, self.cache, self.clock = det, client, cache, clock
        self.due = 0.0  # wall time of the next check; 0: at once
        self.failures = 0
        self.key: str | None = None

    def start(self) -> None:
        """Hand the cached datasets and the saved key check to the detector."""
        for name, data in self.cache.load().items():
            if not self.det.on_dataset(name, data):
                log.warning("cached dataset %s is not usable; it is fetched again", name)
        self.key = self.det.sync_key
        if self.det.key_checked_at is None:
            self.det.key_checked_at = self.cache.checked_at(self.key)
        self.det.gate_poll(self.clock())

    async def run(self, sleep=asyncio.sleep) -> None:
        self.start()
        while True:
            await self.step()
            await sleep(TICK_S)

    async def step(self) -> None:
        det, now, key = self.det, self.clock(), self.det.sync_key
        if key != self.key:  # a new key (or none): check it at once
            self.key, self.due, self.failures = key, 0.0, 0
        if key is not None:
            self.cache.note_check(key, det.key_checked_at)
        det.gate_poll(now)
        if key is None or det.peers.key_state in REFUSED or now < self.due:
            return
        await self.check(key, now)

    async def check(self, key: str, now: float) -> None:
        """The list (the key check), then each changed dataset."""
        try:
            listed = await self._listing(key)
            self.det.key_checked(now)
            self.cache.note_check(key, now)
            self.failures, self.due = 0, now + DAY_S
            for name in NAMES:
                if name in listed and listed[name] != self.cache.etag(name):
                    await self._fetch(key, name)
        except SyncRejected as exc:
            if self.det.sync_key == key:
                self.det.key_refused(exc.state)
                self.cache.note_check(key, None)
            logsetup.limited(log, "data:rejected", "guild server refused the key (%s)", exc.reason, level=logging.WARNING)
        except SyncError as exc:
            wait = RETRY_S[min(self.failures, len(RETRY_S) - 1)]
            self.failures, self.due = self.failures + 1, now + wait
            self.det.key_unchecked(exc.reason)
            logsetup.limited(log, "data:down", "dataset check failed (%s); retrying in %.0f s", exc.reason, wait,
                             level=logging.WARNING)

    async def _listing(self, key: str) -> dict[str, str]:
        """Dataset name -> ETag, every page."""
        out, after = {}, None
        for _ in range(MAX_PAGES):
            body, _ = await asyncio.to_thread(self.client.fetch, key, "/v1/data" + (f"?after={after}" if after else ""))
            tags, after = _page(body)
            out |= tags
            if after is None:
                break
        return out

    async def _fetch(self, key: str, name: str) -> None:
        body, etag = await asyncio.to_thread(self.client.fetch, key, f"/v1/data/{name}", self.cache.etag(name))
        if body is None:
            return  # 304: the cached copy is current
        if self.det.on_dataset(name, body):
            self.cache.store(name, etag, body)
            log.info("dataset %s updated from the guild server", name)
        else:
            log.warning("dataset %s from the guild server is not usable; keeping the cached copy", name)
