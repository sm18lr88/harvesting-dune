"""Guild presence in the store: one row per character, and its lifecycle state from server time only.

A mixin of Storage (storage.py), which owns the connection and the write lock. A sync is the
heartbeat: it promises the next one within `nextS` seconds. The state of a row is derived from
its stored `received_at`, `next_s`, `in_game` and `left_at` at read time, so it survives a restart
and never compares client clocks (docs/SYNC-PROTOCOL.md "Presence").
"""

from dataclasses import dataclass

GRACE_S = 20.0  # network jitter and one retry
OFFLINE_KEEP_S = 15 * 60  # an offline row is listed this long, then it is gone
NEXT_S_RANGE = (5, 600)  # a promise outside this is clamped
STATES = ("online", "in_menu", "late", "offline")
# when the row stops being listed: its leaving time, else the end of `late`, plus the offline window
_GONE_AT = f"COALESCE(left_at, received_at + 3 * next_s + {GRACE_S}) + {OFFLINE_KEEP_S}"


@dataclass(frozen=True, slots=True)
class Beat:
    """What one sync says about its sender: the next sync within `next_s`, in a world, or leaving."""

    next_s: float = 60.0
    in_game: bool = True
    leaving: bool = False


OLD_CLIENT = Beat()  # a request without `presence`


def state_of(row: dict, now: float) -> str:
    """`online`, `in_menu`, `late` or `offline` for a row that is not gone (`_GONE_AT` decides that).
    A row only turns `online` again on a new heartbeat: time alone never moves a state back."""
    next_s, deadline = row["next_s"], row["received_at"] + row["next_s"] + GRACE_S
    if row["left_at"] is not None:
        return "offline"
    if now <= deadline:
        return "online" if row["in_game"] else "in_menu"
    return "late" if now <= deadline + 2 * next_s else "offline"


class PresenceStoreMixin:
    def upsert_presence(self, key_id: str, identity: dict, partition: dict, position: dict | None, now: float,
                        lag: float, beat: Beat = OLD_CLIENT) -> None:
        """`position` None means the member does not share it: x, y, z and observed_at stay NULL."""
        pos = position or {}
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO presence(character_id, key_id, name, battlegroup, world, layout,"
                " x, y, z, observed_at, received_at, lag, next_s, in_game, left_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (identity["characterId"], key_id, identity["characterName"], identity["battlegroupId"],
                 partition["worldId"], partition["layout"], pos.get("x"), pos.get("y"), pos.get("z"),
                 pos.get("observedAt"), now, lag, beat.next_s, int(beat.in_game), now if beat.leaving else None),
            )

    def presence_for(self, battlegroup: str, now: float) -> list[dict]:
        """Members of the battlegroup that are not gone, by name, with their state; the caller filters itself out."""
        rows = self._conn.execute(
            f"SELECT * FROM presence WHERE battlegroup = ? AND {_GONE_AT} >= ? ORDER BY name", (battlegroup, now),
        )
        return [{"characterName": r["name"], "characterId": r["character_id"], "battlegroupId": r["battlegroup"],
                 "worldId": r["world"], "layout": r["layout"], "x": r["x"], "y": r["y"], "z": r["z"],
                 "ageS": now - r["received_at"] + r["lag"], "keyId": r["key_id"], "state": state_of(r, now),
                 "lastNextS": r["next_s"]} for r in rows]

    def _sweep_presence(self, c, now: float) -> int:
        return c.execute(f"DELETE FROM presence WHERE {_GONE_AT} < ?", (now,)).rowcount

    def _presence_left(self, c, key_id: str, now: float) -> None:
        """A revoked key's characters leave at once."""
        c.execute("UPDATE presence SET left_at = ? WHERE key_id = ? AND left_at IS NULL", (now, key_id))
