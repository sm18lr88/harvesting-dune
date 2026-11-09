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
_SESSION_FIELDS = ("characterName", "characterId", "battlegroupId", "sessionId", "isActive")
_NOT_JOURNALED = frozenset({"dune.skillProgress", "dune.knownRecipes", "dune.achievements", "dune.landsraad"})
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

    def write(self, now: float, msg: BusMessage, lean: bool = False) -> None:
        if self.directory is None or msg.topic in _NOT_JOURNALED:
            return
        payload = msg.payload
        if msg.topic == "dune.map.actors" and msg.type != "topicReset":
            if lean:
                return  # memory is short: skip the big payloads entirely
            # the full list is ~150 KB in Hagga and changes twice a second; a snapshot every
            # 20 s is plenty for calibration (detection ranges are learned live)
            if now - self._actors_at < ACTOR_JOURNAL_EVERY_S:
                return
            self._actors_at = now
        if msg.topic == "dune.rawStats":
            # large and ticking constantly: keep only which groups exist and anything vehicle-related
            names = parse_stat_names(payload)
            payload = {
                "groups": sorted(g.get("name") or g.get("title") or "?" for g in (payload or {}).get("groups", []) if isinstance(g, dict)),
                "vehicleEntries": sorted(n for n in names if any(k in n.lower() for k in _VEHICLE_WORDS)),
            }
        if msg.topic == "dune.trackedSession":
            payload = _who_plays(payload)
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        digest = hashlib.blake2b(body.encode(), digest_size=12).hexdigest()
        if self._last_hash.get(msg.topic or "") == digest:
            return
        self._last_hash[msg.topic or ""] = digest
        if self._file is None:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / time.strftime("journal-%Y%m%d-%H%M%S.jsonl")
            self.path = str(path)
            self._file = path.open("a", encoding="utf8")
            log.info("journaling to %s", path)
        self._file.write(f'{{"t":{now:.3f},"type":"{msg.type}","topic":{json.dumps(msg.topic)},"payload":{body}}}\n')
        self._file.flush()
