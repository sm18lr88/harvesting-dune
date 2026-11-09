"""The meaning of a guild sync request: body rules, the writes, and the response (docs/SYNC-PROTOCOL.md).

`sync` parses the whole body before it writes anything, so a rejected request stores nothing.
The HTTPS front (httpd.py) has already checked the key, the rate limit and the body size.
Character profiles have their own two routes (profiles.py).
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

from .board import MAX_LAG_S
from .fields import ANY, PLACE, BadField, flag, identity, integer, join, number, obj, text
from .presence import NEXT_S_RANGE, OLD_CLIENT, Beat
from .problems import OTHER_KEY, invalid
from .profiles import ProfileRoutes

MAX_RECORDS = 200
CLASSES = frozenset({"spice", "blow", "ship", "mined", "stale", "ore"})
SIZES = frozenset({"small", "medium", "large"})
_TYPE = re.compile(r"[a-z0-9_]{1,64}")
BODY_FIELDS = ("since", "sentAt", "identity", "partition", "position", "records")
OPTIONAL_BODY_FIELDS = ("presence",)  # added in /v1: a body without it is an older client
PRESENCE_FIELDS = ("nextS", "inGame", "leaving")
RECORD_FIELDS = ("cls", "type", "x", "y", "z", "observedAt", "fill", "size", "rowKey", "distCm")
PARTITION_FIELDS = ("worldId", "layout")
POINT_FIELDS = ("x", "y", "z", "observedAt")

__all__ = ["Api", "BadField", "SyncRequest", "parse"]


@dataclass(frozen=True, slots=True)
class SyncRequest:
    since: int
    sent_at: float
    identity: dict
    partition: dict
    position: dict | None
    records: list[dict]
    beat: Beat = OLD_CLIENT


def _choice(value: object, field: str, allowed: frozenset[str]) -> str:
    if value not in allowed:
        raise BadField(field)
    return value


def _list(value: object, field: str, max_len: int) -> list:
    if not isinstance(value, list) or len(value) > max_len:
        raise BadField(field)
    return value


def _point(raw: dict, field: str) -> dict:
    return {axis: number(raw[axis], join(field, axis)) for axis in POINT_FIELDS}


def _record(value: object, field: str) -> dict:
    r = obj(value, field, RECORD_FIELDS)
    return {
        "cls": _choice(r["cls"], f"{field}.cls", CLASSES),
        "type": text(r["type"], f"{field}.type", _TYPE, 64),
        **_point(r, field),
        "fill": None if r["fill"] is None else number(r["fill"], f"{field}.fill", 0.0, 1.0),
        "size": None if r["size"] is None else _choice(r["size"], f"{field}.size", SIZES),
        "rowKey": None if r["rowKey"] is None else text(r["rowKey"], f"{field}.rowKey", ANY, 128),
        "distCm": None if r["distCm"] is None else number(r["distCm"], f"{field}.distCm", 0.0),
    }


def _beat(value: object) -> Beat:
    p = obj(value, "presence", PRESENCE_FIELDS)
    next_s = integer(p["nextS"], "presence.nextS", -(2**63))
    low, high = NEXT_S_RANGE
    return Beat(float(min(max(next_s, low), high)), flag(p["inGame"], "presence.inGame"),
                flag(p["leaving"], "presence.leaving"))


def parse(body: object) -> SyncRequest:
    """The typed request, or BadField for the first field (in body order) that breaks a rule;
    `presence` is checked last."""
    given = isinstance(body, dict) and "presence" in body
    b = obj({k: v for k, v in body.items() if k != "presence"} if given else body, "", BODY_FIELDS)
    since = integer(b["since"], "since", 0)
    sent_at = number(b["sentAt"], "sentAt")
    who = identity(b["identity"])
    p = obj(b["partition"], "partition", PARTITION_FIELDS)
    partition = {key: text(p[key], f"partition.{key}", PLACE, 64) for key in PARTITION_FIELDS}
    position = None if b["position"] is None else _point(obj(b["position"], "position", POINT_FIELDS), "position")
    records = [_record(r, f"records[{n}]") for n, r in enumerate(_list(b["records"], "records", MAX_RECORDS))]
    beat = _beat(body["presence"]) if given else OLD_CLIENT
    return SyncRequest(since, sent_at, who, partition, position, records, beat)


def _lag(sent_at: float, observed_at: float) -> float:
    return min(max(sent_at - observed_at, 0.0), MAX_LAG_S)


class Api(ProfileRoutes):
    def __init__(self, storage, now: Callable[[], float]) -> None:
        self.storage, self.now = storage, now

    def health(self) -> tuple[int, dict]:
        return 200, {"ok": True, "now": self.now(), "version": "1"}

    def sync(self, key: dict, body: object, ip: str, now: float) -> tuple[int, dict]:
        try:
            req = parse(body)
        except BadField as bad:
            return 400, invalid(bad.field)
        if not self.storage.upsert_character(key["id"], req.identity, now):  # the ownership check, in the write
            return 403, OTHER_KEY
        self._write(key["id"], req, ip, now)
        return 200, self._view(req, now)

    def _write(self, key_id: str, req: SyncRequest, ip: str, now: float) -> None:
        s, name, group = self.storage, req.identity["characterName"], req.identity["battlegroupId"]
        lag = 0.0 if req.position is None else _lag(req.sent_at, req.position["observedAt"])
        s.upsert_presence(key_id, req.identity, req.partition, req.position, now, lag, req.beat)
        if req.records:
            s.upsert_sightings(key_id, name, group, req.partition, req.records, now, req.sent_at)
        s.touch_key(key_id, now, ip)

    def _view(self, req: SyncRequest, now: float) -> dict:
        s, group, part = self.storage, req.identity["battlegroupId"], req.partition
        me = req.identity["characterId"]
        presence = [p for p in s.presence_for(group, now) if p["characterId"] != me]
        rows, cursor = s.sightings_since(group, part["worldId"], part["layout"], req.since)
        sightings = [
            {**{k: v for k, v in r.items() if k not in ("receivedAt", "lag")}, "ageS": now - r["receivedAt"] + r["lag"]}
            for r in rows
        ]
        return {"now": now, "next": cursor, "keyState": "valid", "presence": presence, "sightings": sightings}
