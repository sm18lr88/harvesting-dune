"""Shared overlay objects in the guild store: one row per (key, object id), revs from the shared `meta.rev`.

A mixin of Storage (storage.py), which owns the connection and the write lock. A key writes only
its own rows: the primary key starts with the key id, so ownership holds by construction. Rows
are delivered battlegroup-wide (no partition filter); the client decides what to draw.
"""

import json

from .objects import (GRACE_S, MAX_LIVE_PER_GROUP, MAX_LIVE_PER_KEY, MAX_OBJECT_TTL_S, MAX_ROWS_PER_KEY, OBJECTS_PAGE,
                      ObjectCap)
from .profilestore import player_tag

_OBJECT_COLS = ("key_id, id, battlegroup, character_id, name, kind, scope_world, scope_layout, geometry, body,"
                " expires_at, received_at, rev")


def _rev(c) -> int:
    return int(c.execute("SELECT v FROM meta WHERE k = 'rev'").fetchone()[0])


def _wire(r, now: float) -> dict:
    scope = None if r["scope_world"] is None else {"worldId": r["scope_world"], "layout": r["scope_layout"]}
    return {"id": r["id"], "kind": r["kind"], "scope": scope,
            "geometry": None if r["geometry"] is None else json.loads(r["geometry"]), "body": json.loads(r["body"]),
            "ttlS": r["expires_at"] - now, "ageS": now - r["received_at"], "characterId": r["character_id"],
            "characterName": r["name"], "playerTag": player_tag(r["key_id"], r["secret_hash"]), "rev": r["rev"]}


class ObjectStoreMixin:
    def upsert_objects(self, key_id: str, identity: dict, uploads: list[dict], now: float) -> int:
        """Store parsed uploads (objects.parse_object), each with the next rev; `ttlS` 0 leaves a tombstone.
        ObjectCap (nothing written) when a live upload would pass a cap. Returns the number stored."""
        group = identity["battlegroupId"]
        with self._tx() as c:
            rev = _rev(c)
            for n, u in enumerate(uploads):
                if u["ttlS"] > 0:
                    self._check_caps(c, key_id, group, u["id"], now, f"objects[{n}]")
                rev += 1
                scope, geometry = u["scope"] or {}, u["geometry"]
                c.execute(
                    f"INSERT OR REPLACE INTO objects({_OBJECT_COLS}) VALUES ({', '.join('?' * 13)})",
                    (key_id, u["id"], group, identity["characterId"], identity["characterName"], u["kind"],
                     scope.get("worldId"), scope.get("layout"), None if geometry is None else json.dumps(geometry),
                     u["body"], now + min(u["ttlS"], MAX_OBJECT_TTL_S), now, rev),
                )
            self._trim_objects(c, key_id, now)
            c.execute("UPDATE meta SET v = ? WHERE k = 'rev'", (str(rev),))
        return len(uploads)

    def _check_caps(self, c, key_id: str, group: str, object_id: str, now: float, field: str) -> None:
        """Counted in the writing transaction, so uploads earlier in the request count too; the row
        being replaced never counts against itself."""
        mine = c.execute("SELECT COUNT(*) FROM objects WHERE key_id = ? AND id != ? AND expires_at > ?",
                         (key_id, object_id, now)).fetchone()[0]
        if mine >= MAX_LIVE_PER_KEY:
            raise ObjectCap(field, "key")
        group_live = c.execute(
            "SELECT COUNT(*) FROM objects WHERE battlegroup = ? AND expires_at > ? AND NOT (key_id = ? AND id = ?)",
            (group, now, key_id, object_id)).fetchone()[0]
        if group_live >= MAX_LIVE_PER_GROUP:
            raise ObjectCap(field, "battlegroup")

    def _trim_objects(self, c, key_id: str, now: float) -> None:
        """Bounds what a restart refetches: a spammer's tombstones beyond MAX_ROWS_PER_KEY go, oldest
        first; live rows are never trimmed."""
        excess = c.execute("SELECT COUNT(*) FROM objects WHERE key_id = ?", (key_id,)).fetchone()[0] - MAX_ROWS_PER_KEY
        if excess > 0:
            c.execute("DELETE FROM objects WHERE key_id = ? AND id IN (SELECT id FROM objects WHERE key_id = ?"
                      " AND expires_at <= ? ORDER BY rev LIMIT ?)", (key_id, key_id, now, excess))

    def objects_since(self, battlegroup: str, since: int, now: float, limit: int = OBJECTS_PAGE) -> tuple[list[dict], int | None]:
        """Battlegroup rows with rev > since, lowest rev first, in the wire shape; and the page's cut
        (its last rev) when the page is full, else None."""
        rows = self._conn.execute(
            "SELECT o.*, k.secret_hash FROM objects o JOIN keys k ON k.id = o.key_id"
            " WHERE o.battlegroup = ? AND o.rev > ? ORDER BY o.rev LIMIT ?", (battlegroup, since, limit),
        ).fetchall()
        out = [_wire(r, now) for r in rows]
        return out, (out[-1]["rev"] if len(out) == limit else None)

    def _objects_left(self, c, key_id: str, now: float) -> None:
        """A revoked or expired key's live objects end now, each with a new rev so readers learn it."""
        ids = [r[0] for r in c.execute("SELECT id FROM objects WHERE key_id = ? AND expires_at > ? ORDER BY rev",
                                       (key_id, now))]
        rev = _rev(c)
        for object_id in ids:
            rev += 1
            c.execute("UPDATE objects SET expires_at = ?, rev = ? WHERE key_id = ? AND id = ?", (now, rev, key_id, object_id))
        c.execute("UPDATE meta SET v = ? WHERE k = 'rev'", (str(rev),))

    def _sweep_objects(self, c, now: float) -> int:
        return c.execute("DELETE FROM objects WHERE expires_at + ? < ?", (GRACE_S, now)).rowcount
