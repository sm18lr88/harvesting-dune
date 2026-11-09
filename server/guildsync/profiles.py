"""Character profile routes: POST /v1/profile stores or deletes one, POST /v1/profiles returns the battlegroup's.

A profile is plain JSON with exactly the documented top-level keys (docs/SYNC-PROTOCOL.md
"Characters"); unknown keys are refused, so a field the client should never send (a currency, a
base id) cannot ride along. Stored as canonical text, at most MAX_PROFILE bytes.
"""

import json
import math

from .fields import MAX_INT, BadField, identity, integer, join, obj
from .problems import OTHER_KEY, invalid

MAX_PROFILE = 32768
PROFILE_FIELDS = ("status", "skills", "stats", "recipes", "landsraad", "achievements", "session")
SESSION_FIELDS = ("characterXp", "specializationXp", "dailies", "dungeonRuns", "loot")
PROFILE_BODY, PROFILES_BODY = ("identity", "profile"), ("identity", "since")
MAX_DEPTH, MAX_TEXT, MAX_ITEMS, MAX_KEYS = 6, 200, 1000, 100


def _scalar_ok(value: object) -> bool:
    match value:
        case None | bool():
            return True
        case int():
            return abs(value) <= MAX_INT
        case float():
            return math.isfinite(value)
        case str():
            return len(value) <= MAX_TEXT
        case _:
            return False


def _children(value: list | dict, field: str) -> list[tuple[object, str]]:
    if isinstance(value, list):
        if len(value) > MAX_ITEMS:
            raise BadField(field)
        return [(item, f"{field}[{n}]") for n, item in enumerate(value)]
    if len(value) > MAX_KEYS or not all(isinstance(k, str) and len(k) <= 64 for k in value):
        raise BadField(field)
    return [(item, join(field, k)) for k, item in value.items()]


def _plain(value: object, field: str, depth: int = 0) -> None:
    """Refuses what a JSON reader elsewhere could choke on: NaN, huge ints, deep or wide nesting."""
    if depth > MAX_DEPTH:
        raise BadField(field)
    if isinstance(value, (list, dict)):
        for item, name in _children(value, field):
            _plain(item, name, depth + 1)
    elif not _scalar_ok(value):
        raise BadField(field)


def parse_profile(value: object) -> str:
    """The profile as canonical JSON text, or BadField."""
    p = obj(value, "profile", PROFILE_FIELDS)
    if p["session"] is not None:
        obj(p["session"], "profile.session", SESSION_FIELDS)
    _plain(p, "profile")
    stored = json.dumps(p, separators=(",", ":"), sort_keys=True)
    if len(stored.encode()) > MAX_PROFILE:
        raise BadField("profile")
    return stored


class ProfileRoutes:
    """Mixed into api.Api, which provides `storage`."""

    def profile(self, key: dict, body: object, ip: str, now: float) -> tuple[int, dict]:
        try:
            b = obj(body, "", PROFILE_BODY)
            who = identity(b["identity"])
            stored = None if b["profile"] is None else parse_profile(b["profile"])
        except BadField as bad:
            return 400, invalid(bad.field)
        if stored is None:
            written = self.storage.delete_profile(key["id"], who["characterId"])
        else:
            written = self.storage.put_profile(key["id"], who, stored, now)
        if not written:  # the ownership check ran inside the write
            return 403, OTHER_KEY
        self.storage.touch_key(key["id"], now, ip)
        return 200, {"ok": True}

    def profiles(self, key: dict, body: object, ip: str, now: float) -> tuple[int, dict]:
        try:
            b = obj(body, "", PROFILES_BODY)
            who, since = identity(b["identity"]), integer(b["since"], "since", 0)
        except BadField as bad:
            return 400, invalid(bad.field)
        rows, ids, cursor = self.storage.profiles_since(who["battlegroupId"], since)
        profiles = [{"characterId": r["characterId"], "characterName": r["characterName"], "rev": r["rev"],
                     "ageS": now - r["updatedAt"], "profile": json.loads(r["body"]), "playerTag": r["playerTag"]} for r in rows]
        return 200, {"now": now, "next": cursor, "ids": ids, "profiles": profiles}
