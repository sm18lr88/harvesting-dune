"""Shared board of the guild store: the sightings table.

A mixin of Storage (storage.py), which owns the connection and the write lock; every write here
runs in its `_tx()` transaction, so the global `rev` counter never repeats.
"""

import json

CELL_CM = 2000.0
MAX_LAG_S = 3600.0
_SIGHTING_COLS = (
    "key, battlegroup, world, layout, cls, type, x, y, z, fill, size, row_key, dist_cm,"
    " observed_at, received_at, lag, observer_key, observer_name, observers, rev"
)


def _lag(sent_at: float, observed_at: float) -> float:
    return min(max(sent_at - observed_at, 0.0), MAX_LAG_S)


class BoardMixin:
    # sightings

    def upsert_sightings(self, key_id: str, observer_name: str, battlegroup: str, partition: dict,
                         records: list[dict], now: float, sent_at: float) -> int:
        """Upsert records by the wire key; each gets the next rev. Returns the number upserted.

        The newest observation keeps the row's fields; an older one only adds its observer. Newest
        on the server's clock (receipt time less lag): two players' PC clocks are never compared.
        """
        world, layout = partition["worldId"], partition["layout"]
        with self._tx() as c:
            rev = int(c.execute("SELECT v FROM meta WHERE k = 'rev'").fetchone()[0])
            for r in records:
                rev += 1
                cell = [battlegroup, layout, r["cls"], r["type"], int(r["x"] // CELL_CM), int(r["y"] // CELL_CM)]
                key = json.dumps(cell)
                old = c.execute("SELECT received_at, lag, observers FROM sightings WHERE key = ?", (key,)).fetchone()
                observers = json.dumps(sorted({key_id, *json.loads(old["observers"])}) if old else [key_id])
                lag = _lag(sent_at, r["observedAt"])
                if old is not None and now - lag < old["received_at"] - old["lag"]:
                    c.execute("UPDATE sightings SET observers = ?, rev = ? WHERE key = ?", (observers, rev, key))
                    continue
                c.execute(
                    f"INSERT OR REPLACE INTO sightings({_SIGHTING_COLS}) VALUES ({', '.join('?' * 20)})",
                    (key, battlegroup, world, layout, r["cls"], r["type"], r["x"], r["y"], r["z"], r["fill"],
                     r["size"], r["rowKey"], r["distCm"], r["observedAt"], now, lag,
                     key_id, observer_name, observers, rev),
                )
            c.execute("UPDATE meta SET v = ? WHERE k = 'rev'", (str(rev),))
        return len(records)

    def sightings_since(self, battlegroup: str, world: str, layout: str, since: int, limit: int = 500) -> tuple[list[dict], int]:
        """Partition rows with rev > since, lowest rev first, and the cursor to send next.

        Rows carry `receivedAt` and `lag` instead of `ageS`: the caller computes
        `ageS = now - receivedAt + lag` with its own clock.
        """
        rows = self._conn.execute(
            "SELECT * FROM sightings WHERE battlegroup = ? AND world = ? AND layout = ? AND rev > ? ORDER BY rev LIMIT ?",
            (battlegroup, world, layout, since, limit),
        ).fetchall()
        out = [
            {"cls": r["cls"], "type": r["type"], "x": r["x"], "y": r["y"], "z": r["z"], "fill": r["fill"],
             "size": r["size"], "rowKey": r["row_key"], "distCm": r["dist_cm"], "observerName": r["observer_name"],
             "observerKey": r["observer_key"], "observers": json.loads(r["observers"]), "rev": r["rev"],
             "worldId": r["world"], "layout": r["layout"], "receivedAt": r["received_at"], "lag": r["lag"]}
            for r in rows
        ]
        if len(out) == limit:
            return out, out[-1]["rev"]
        return out, int(self._conn.execute("SELECT v FROM meta WHERE k = 'rev'").fetchone()[0])
