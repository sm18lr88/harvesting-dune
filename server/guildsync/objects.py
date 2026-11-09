"""Shared overlay objects: the rules of the optional `objects` field of /v1/sync (docs/SYNC-PROTOCOL.md "Objects").

The envelope is strict and the body is opaque: any `kind` matching TYPE is accepted, so a new
kind needs no server change, and the body only has to be plain JSON of at most MAX_OBJECT_BODY
bytes as canonical text. The limits and the cursor page live here; objectstore.py applies them.
"""

import json

from .fields import PLACE, TYPE, BadField, integer, join, number, obj, text
from .profiles import _plain

OBJECT_FIELDS = ("id", "kind", "scope", "geometry", "body", "ttlS")
SCOPE_FIELDS = ("worldId", "layout")  # the same as api.PARTITION_FIELDS; api imports this module
GEOMETRY_FIELDS = ("type", "x", "y", "z")
GEOMETRY_TYPES = frozenset({"point"})
MAX_OBJECTS = 20  # uploads in one request
MAX_OBJECT_BODY = 512  # bytes of canonical body text
MAX_OBJECT_TTL_S = 86400  # a longer ttlS is clamped
MAX_LIVE_PER_KEY = 20
MAX_LIVE_PER_GROUP = 300
MAX_ROWS_PER_KEY = 100  # live and expired; the oldest expired rows beyond it are trimmed
OBJECTS_PAGE = 200
GRACE_S = MAX_OBJECT_TTL_S  # an expired row stays this long, so readers see it end


class ObjectCap(Exception):
    """A live upload over MAX_LIVE_PER_KEY (`which` = "key") or MAX_LIVE_PER_GROUP ("battlegroup")."""

    def __init__(self, field: str, which: str) -> None:
        super().__init__(field, which)
        self.field, self.which = field, which


def _scope(value: object, field: str) -> dict | None:
    if value is None:
        return None
    s = obj(value, field, SCOPE_FIELDS)
    return {key: text(s[key], join(field, key), PLACE, 64) for key in SCOPE_FIELDS}


def _geometry(value: object, field: str) -> dict | None:
    if value is None:
        return None
    g = obj(value, field, GEOMETRY_FIELDS)
    if not isinstance(g["type"], str) or g["type"] not in GEOMETRY_TYPES:
        raise BadField(join(field, "type"))
    return {"type": g["type"], **{axis: number(g[axis], join(field, axis)) for axis in ("x", "y", "z")}}


def _body(value: object, field: str) -> str:
    _plain(value, field)
    stored = json.dumps(value, separators=(",", ":"), sort_keys=True)
    if len(stored.encode()) > MAX_OBJECT_BODY:
        raise BadField(field)
    return stored


def parse_object(value: object, field: str) -> dict:
    """One upload with `body` as canonical text, or BadField for the first field that breaks a rule."""
    o = obj(value, field, OBJECT_FIELDS)
    return {
        "id": text(o["id"], join(field, "id"), PLACE, 64),
        "kind": text(o["kind"], join(field, "kind"), TYPE, 64),
        "scope": _scope(o["scope"], join(field, "scope")),
        "geometry": _geometry(o["geometry"], join(field, "geometry")),
        "body": _body(o["body"], join(field, "body")),
        "ttlS": integer(o["ttlS"], join(field, "ttlS"), 0),
    }


def parse_objects(value: object) -> list[dict]:
    if not isinstance(value, list) or len(value) > MAX_OBJECTS:
        raise BadField("objects")
    return [parse_object(item, f"objects[{n}]") for n, item in enumerate(value)]
