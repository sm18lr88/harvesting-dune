# Journal: writes the companion's bus traffic to data\journals for calibration and replay.
"""Journal of bus messages (JSON lines), for calibration and offline replay.

Only changes are written. The big actor list is written at most every 20 s and never in lean
mode; raw stats are cut down to their group names and vehicle-related entries; the tracked session
is cut down to who plays (name, id, battlegroup, session). The character-only topics (skills,
schematics, achievements, Landsraad) are not journaled: replay does not use them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

from .model import BusMessage, parse_stat_names

log = logging.getLogger(__name__)

ACTOR_JOURNAL_EVERY_S = 20.0
JOURNAL_PART_BYTES = 50 * 1024 * 1024  # the governor never deletes the live file, so a long run splits it
_SESSION_FIELDS = ("characterName", "characterId", "battlegroupId", "sessionId", "isActive")
_NOT_JOURNALED = frozenset({"dune.skillProgress", "dune.knownRecipes", "dune.achievements", "dune.landsraad",
                            "dune.trackedDestination"})  # the player's destination stays in memory only
_VEHICLE_WORDS = ("vehicle", "buggy", "sandbike", "ornithopter", "crawler", "harvester", "thopter", "treadwheel")


def _who_plays(payload: object) -> object:
    """Earnings (currencies, loot, dailies) stay private, and dropping the per-second timers lets
    the change-only hash skip the ticks."""
    if not isinstance(payload, dict):
        return payload
    return {k: payload[k] for k in _SESSION_FIELDS if k in payload}


class Journal:
    def __init__(self, directory: Path | None) -> None:
        self.directory = directory  # None: journaling off
        self.path: str | None = None
        self._file = None
        self._last_hash: dict[str, str] = {}
        self._actors_at = -1e18
        self._part = 0
        self._head: dict[str, str] = {}  # last line per topic: a new part starts with them so it replays alone

    def write(self, now: float, msg: BusMessage, lean: bool = False) -> None:
        if self.directory is None or msg.topic in _NOT_JOURNALED:
            return
        if msg.topic == "dune.map.actors" and msg.type != "topicReset":
            if lean:
                return  # memory is short: skip the big payloads entirely
            # the full list is ~150 KB in Hagga and changes twice a second; a snapshot every
            # 20 s is plenty for calibration (detection ranges are learned live)
            if now - self._actors_at < ACTOR_JOURNAL_EVERY_S:
                return
            self._actors_at = now
        body = json.dumps(_kept(msg), separators=(",", ":"), sort_keys=True)
        digest = hashlib.blake2b(body.encode(), digest_size=12).hexdigest()
        if self._last_hash.get(msg.topic or "") == digest:
            return
        self._last_hash[msg.topic or ""] = digest
        line = f'{{"t":{now:.3f},"type":{json.dumps(msg.type)},"topic":{json.dumps(msg.topic)},"payload":{body}}}\n'
        try:
            self._append(line, msg)
        except OSError:
            # disk full or folder unusable: raising here would drop every bus message before dispatch
            log.exception("journal could not be written; journaling is off for this run")
            self.directory = None
            try:
                self.close()
            except OSError:
                pass

    def close(self) -> None:
        """Ends the current file; journaling stays on, and the next write opens a new one."""
        if self._file is not None:
            file, self._file = self._file, None
            file.close()

    def _append(self, line: str, msg: BusMessage) -> None:
        if self._file is None:
            self.directory.mkdir(parents=True, exist_ok=True)
            # the stamp has one-second resolution and the file is opened for append
            suffix = f"-{self._part}" if self._part else ""
            path = self.directory / time.strftime(f"journal-%Y%m%d-%H%M%S{suffix}.jsonl")
            self.path = str(path)
            self._file = path.open("a", encoding="utf8")
            log.info("journaling to %s", path)
            self._file.writelines(v for k, v in self._head.items() if k != msg.topic)
        self._file.write(line)
        self._file.flush()
        if msg.type != "event":  # an event repeated in the next part would count twice in replay
            self._head[msg.topic or ""] = line
        if self._file.tell() > JOURNAL_PART_BYTES:
            self.close()
            self._part += 1


def _kept(msg: BusMessage) -> object:
    """The part of a payload worth journaling."""
    if msg.topic == "dune.rawStats":
        return _stat_summary(msg.payload)
    if msg.topic == "dune.trackedSession":
        return _who_plays(msg.payload)
    return msg.payload


def _stat_summary(payload: object) -> dict:
    """Raw stats are large and tick constantly: keep only which groups exist and anything vehicle-related."""
    names = parse_stat_names(payload)
    groups = payload.get("groups") if isinstance(payload, dict) else None
    return {
        "groups": sorted(n if isinstance(n := g.get("name") or g.get("title"), str) else "?"
                         for g in (groups if isinstance(groups, list) else []) if isinstance(g, dict)),
        "vehicleEntries": sorted(n for n in names if any(k in n.lower() for k in _VEHICLE_WORDS)),
    }
