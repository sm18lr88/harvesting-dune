# Sync loop: when the guild link talks to the server (adaptive cadence, wake-ups, backoff, profiles).
"""Runner starts `poll` as the "guild sync" loop. The loop wakes every TICK_S, asks whether a sync
is due, and only then makes a request (docs/SYNC-PROTOCOL.md "Client"):

    FAST_S   in a world and moved more than 100 m since the last sync, or finds still queued
    CALM_S   in a world, nothing changed
    AWAY_S   no world (menu, loading screen): only the heartbeat, at the last place
    never    no key, sync paused, key rejected, or no identity (the game closed, or not tracked)

Every sync is also the heartbeat (`presence`): it promises the next one within `nextS`, the
slowest cadence the current state allows, and the loop keeps that promise even when the state
slows down, so guildmates never see a playing member turn late. A goodbye (`leaving`) goes out
when the identity ends, sync is paused, position sharing is turned off, the key is removed, and
(`goodbye`, at most GOODBYE_S) when the detector stops.

Entering a world, saving a key, unpausing, a sharing switch and opening the guild lists on the
settings page wake the loop at once (`det.sync_wake`). Being offline is normal for a player, so
every failure is a WARNING plus a pause, never an ERROR (ERROR puts a problem banner on the
companion pages, see logsetup).
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from dataclasses import dataclass

from . import logsetup
from .gate import REFUSED
from .objects import OBJECTS_PAGE
from .peers import MAX_RECORDS
from .sync import BACKOFF_S, SyncClient, SyncError, SyncRejected, SyncServerError, SyncThrottled, SyncTls

log = logging.getLogger(__name__)

FAST_S, CALM_S, AWAY_S = 15.0, 60.0, 180.0
TICK_S = FAST_S
IDLE_S = 5.0  # no key, paused or rejected: look again this often (a wake-up cuts it short)
MOVED_CM = 10_000.0
SIGHTINGS_PAGE = 500  # a reply this long was cut by the server: ask again at once
MAX_RETRY_AFTER_S = 600.0
GOODBYE_S = 3.0


@dataclass
class Pace:
    since_s: float = math.inf  # seconds since the last good sync
    at: tuple[float, float] | None = None  # where we were at the last good sync
    full: bool = False  # the last upload hit the record cap: more is waiting
    forced: bool = False  # a wake-up or a cut reply: sync on the next pass
    promised: float = math.inf  # the nextS the last good sync promised the server


def _nothing() -> None:
    return None


async def nap(seconds: float, wake: asyncio.Event) -> tuple[float, bool]:
    """Wait up to `seconds`; returns (seconds waited, woken early)."""
    start = time.monotonic()
    try:
        await asyncio.wait_for(wake.wait(), seconds)
    except TimeoutError:
        pass
    woken = wake.is_set()
    wake.clear()
    return time.monotonic() - start, woken


def gap(det, client: SyncClient, pace: Pace) -> float:
    """Seconds wanted between two syncs in the current state (E1)."""
    if not _in_world(det):
        return AWAY_S
    pose = det.store.pose
    moved = pose is not None and (pace.at is None or _moved(pose, pace.at))
    queued = pace.full or bool(det.peers.outbox) or bool(client.pending) or bool(det.objects_upload())
    return FAST_S if moved or queued else CALM_S


def _moved(pose, at: tuple[float, float]) -> bool:
    return math.hypot(pose.x - at[0], pose.y - at[1]) > MOVED_CM


def _in_world(det) -> bool:
    return det.in_game is not False and det.partition() is not None


def _promise(det, pace: Pace, sent: list[dict]) -> float:
    """The cadence this sync promises: fast while moving or while finds wait, else the calm one.
    The loop keeps it even if the player stops, so it only has to be honest, not exact."""
    if not _in_world(det):
        return AWAY_S
    pose = det.store.pose
    moving = pose is not None and pace.at is not None and _moved(pose, pace.at)
    return FAST_S if moving or len(sent) >= MAX_RECORDS or det.peers.outbox or det.objects_upload() else CALM_S


def _leaving(det, bye: dict) -> dict:
    return {"since": det.peers.since, "sentAt": time.time(), "identity": bye["identity"], "partition": bye["partition"],
            "position": None, "records": [], "presence": {"nextS": int(CALM_S), "inGame": False, "leaving": True}}


def _usable(det, key: str | None) -> bool:
    return key is not None and det.sync_enabled and det.peers.key_state not in REFUSED


def _failed(det, key: str, exc: SyncError, failures: int) -> tuple[float, int]:
    """Note a failed exchange for the settings page; returns (seconds to wait, new failure count)."""
    peers = det.peers
    peers.connected_as = None
    match exc:
        case SyncRejected():
            if det.sync_key == key:  # not a key the player replaced while we waited
                peers.last_error = exc.reason
                det.key_refused(exc.state)  # the map clears on the next push
            logsetup.limited(log, "sync:rejected", "guild server rejected the key (%s); enter a new one on the settings page",
                             exc.reason, level=logging.WARNING)
            return 0.0, failures
        case SyncThrottled():
            peers.last_error = exc.reason
            return min(exc.retry_after, MAX_RETRY_AFTER_S), failures
        case _:
            peers.last_error = exc.reason
            wait = BACKOFF_S[min(failures, len(BACKOFF_S) - 1)]
            logsetup.limited(log, "sync:tls" if isinstance(exc, SyncTls) else "sync:down",
                             "guild sync failed (%s: %s); retrying in %.0f s", exc.reason, exc, wait, level=logging.WARNING)
            return wait, failures + 1


def _objects_refusal(exc: SyncServerError) -> bool:
    """A 400 whose problem names a field under `objects` (the note is "<type> <field>")."""
    parts = exc.note.split(" ")
    return exc.status == 400 and len(parts) == 2 and parts[1].startswith("objects")


async def _exchange(det, client: SyncClient, key: str, body: dict, pace: Pace) -> dict | None:
    """The sync request; None when the server refused only the staged objects: they are dropped with a
    note and the sync goes again at once without them, so presence and finds never wait on a pin."""
    try:
        return await asyncio.to_thread(client.exchange, key, body)
    except SyncServerError as exc:
        if "objects" not in body or not _objects_refusal(exc):
            raise
        logsetup.limited(log, "sync:objects", "guild server refused the staged objects (%s)", exc.note, level=logging.WARNING)
        det.objects_refused(exc.note)
    pace.forced = True
    return None


async def _sync(det, client: SyncClient, key: str, pace: Pace) -> bool:
    """One sync if there is something we may publish (identity and partition known); True if sent."""
    now = time.time()
    body = det.outbox(now)
    if body is None:
        return False
    sent = client.stage(body["records"], (body["identity"]["characterId"], body["partition"]))
    promise = _promise(det, pace, sent)
    body.update(records=sent, since=det.peers.since, sentAt=now,
                presence={"nextS": int(promise), "inGame": _in_world(det), "leaving": False})
    uploads = det.objects_upload()
    if uploads:
        body["objects"] = uploads
    resp = await _exchange(det, client, key, body, pace)
    if resp is None:
        return False
    det.key_checked(time.time())
    pace.promised = promise
    client.ack(sent)
    det.objects_sent(uploads)
    det.ingest_peer(resp, time.time())
    det.peers.last_error, det.peers.connected_as = None, body["identity"]["characterName"]
    pose = det.store.pose
    rows, objs = resp.get("sightings"), resp.get("objects")
    pace.since_s, pace.at = 0.0, (pose.x, pose.y) if pose is not None else None
    pace.full = len(sent) >= MAX_RECORDS
    pace.forced = (isinstance(rows, list) and len(rows) >= SIGHTINGS_PAGE) or (isinstance(objs, list) and len(objs) >= OBJECTS_PAGE)
    return True


def _send_quietly(client: SyncClient, key: str, body: dict) -> None:
    try:
        client.exchange(key, body)
    except SyncError as exc:  # best effort: the server's deadline rule covers a lost goodbye
        logsetup.limited(log, "sync:goodbye", "guild goodbye not sent (%s)", exc.reason, level=logging.WARNING)


async def _farewell(det, client: SyncClient, pace: Pace) -> None:
    """The queued goodbye, if any, once; then a normal sync at once if one may follow (position sharing off)."""
    bye, det.peers.farewell = det.peers.farewell, None
    if bye is None:
        return
    await asyncio.to_thread(_send_quietly, client, bye["key"], _leaving(det, bye))
    pace.forced = True


def goodbye(det, client: SyncClient) -> None:
    """At shutdown: one goodbye, waited for at most GOODBYE_S. Its thread is a daemon, so a slow
    server never holds the exit."""
    if det.peers.farewell is None and det.sync_enabled:
        det.queue_farewell()
    bye, det.peers.farewell = det.peers.farewell, None
    if bye is None:
        return
    sender = threading.Thread(target=_send_quietly, args=(client, bye["key"], _leaving(det, bye)), name="guild goodbye", daemon=True)
    sender.start()
    sender.join(GOODBYE_S)


async def _profile_call(det, client: SyncClient, key: str, body: dict, path: str) -> dict:
    """A profile request; {} when the server refuses it for good (400: this body, 404: a server without
    profiles), so a refusal never slows the syncs down with backoff."""
    try:
        resp = await asyncio.to_thread(client.exchange, key, body, path)
        det.key_checked(time.time())
        return resp
    except SyncServerError as exc:
        if exc.status not in (400, 404):
            raise
        logsetup.limited(log, f"sync:{path}", "guild server refused %s (%s)", path, exc, level=logging.WARNING)
        return {}


async def _profiles(det, client: SyncClient, key: str) -> bool:
    """Our profile when it changed (at most every 5 min), guildmates' while the Characters view is open."""
    now = time.time()
    upload, ask = det.profile_upload(now), det.profiles_request(now)
    if upload is not None:
        await _profile_call(det, client, key, upload, "/v1/profile")
        det.profile_sent(upload, now)  # a refused profile waits until it changes
    if ask is not None:
        det.ingest_profiles(await _profile_call(det, client, key, ask, "/v1/profiles"), time.time())
    return upload is not None or ask is not None


async def poll(det, client: SyncClient) -> None:
    """Forever: sync when due, merge the guild's answer, wait. No network failure leaves this loop;
    records the server did not acknowledge go out with the next upload."""
    wake, loop = asyncio.Event(), asyncio.get_running_loop()
    det.sync_wake = lambda: loop.call_soon_threadsafe(wake.set)
    pace, failures = Pace(), 0
    try:
        while True:
            await _farewell(det, client, pace)
            key = det.sync_key
            if not _usable(det, key):
                waited, pace.forced = await nap(IDLE_S, wake)
                pace.since_s += waited
                continue
            due, pace.forced = pace.forced or pace.since_s >= min(gap(det, client, pace), pace.promised), False
            try:
                synced = due and await _sync(det, client, key, pace)
                talked = await _profiles(det, client, key) or synced
            except SyncError as exc:
                wait, failures = _failed(det, key, exc, failures)
                if wait:
                    pace.since_s += (await nap(wait, wake))[0]
                continue
            if talked:
                failures = 0
            if not pace.forced:
                waited, pace.forced = await nap(TICK_S, wake)
                pace.since_s += waited
    finally:
        det.sync_wake = _nothing
