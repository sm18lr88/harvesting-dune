"""SQLite store for guild sync: keys, characters, presence, sightings, profiles, audit.

One connection shared by the server threads. Every write runs under one lock in a
`BEGIN IMMEDIATE` transaction, so the global `rev` counter never repeats. Reads
return plain dicts with the wire contract's names (docs/SYNC-PROTOCOL.md). `clock` is the
time source for key creation and key expiry; tests inject a fixed one.
"""

import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager

from .board import BoardMixin
from .keystore import KeyStoreMixin
from .presence import PresenceStoreMixin
from .profilestore import ProfileStoreMixin

SHORT_SIGHTING_S = 3 * 3600  # spice, blow, ore
LONG_SIGHTING_S = 24 * 3600  # ship, mined, stale
AUDIT_S = 30 * 86400
PROFILE_S = 30 * 86400  # an alt not played for weeks stays browsable; longer while it keeps syncing
_SHORT_CLASSES = "('spice', 'blow', 'ore')"

MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        "CREATE TABLE keys(id TEXT PRIMARY KEY, secret_hash TEXT NOT NULL, label TEXT NOT NULL,"
        " note TEXT, created_at REAL NOT NULL, revoked_at REAL, last_seen REAL, last_ip TEXT)",
        "CREATE TABLE characters(key_id TEXT NOT NULL, character_id INTEGER NOT NULL,"
        " name TEXT NOT NULL, battlegroup TEXT NOT NULL, last_seen REAL NOT NULL,"
        " PRIMARY KEY(key_id, character_id))",
        "CREATE TABLE presence(character_id INTEGER PRIMARY KEY, key_id TEXT NOT NULL, name TEXT,"
        " battlegroup TEXT NOT NULL, world TEXT NOT NULL, layout TEXT NOT NULL, x REAL, y REAL,"
        " z REAL, observed_at REAL, received_at REAL NOT NULL, lag REAL NOT NULL)",
        "CREATE TABLE sightings(key TEXT PRIMARY KEY, battlegroup TEXT NOT NULL, world TEXT NOT NULL,"
        " layout TEXT NOT NULL, cls TEXT NOT NULL, type TEXT NOT NULL, x REAL, y REAL, z REAL,"
        " fill REAL, size TEXT, row_key TEXT, dist_cm REAL, observed_at REAL NOT NULL,"
        " received_at REAL NOT NULL, lag REAL NOT NULL, observer_key TEXT NOT NULL,"
        " observer_name TEXT NOT NULL, observers TEXT NOT NULL, rev INTEGER NOT NULL)",
        "CREATE INDEX sightings_part ON sightings(battlegroup, world, layout, rev)",
        "CREATE TABLE bases(id INTEGER PRIMARY KEY AUTOINCREMENT, key_id TEXT NOT NULL,"
        " battlegroup TEXT NOT NULL, world TEXT NOT NULL, layout TEXT NOT NULL, name TEXT NOT NULL,"
        " name_key TEXT NOT NULL, x REAL, y REAL, z REAL, claimed_by TEXT NOT NULL,"
        " created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(key_id, name_key))",
        "CREATE TABLE audit(ts REAL NOT NULL, key_id TEXT, ip TEXT, verb TEXT NOT NULL,"
        " status INTEGER NOT NULL, n_records INTEGER NOT NULL DEFAULT 0, note TEXT)",
        "CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)",
        "INSERT INTO meta(k, v) VALUES ('rev', '0')",
    ),
    ("DROP TABLE IF EXISTS bases",),  # guild bases were removed
    (
        "CREATE TABLE profiles(character_id INTEGER PRIMARY KEY, key_id TEXT NOT NULL, battlegroup TEXT NOT NULL,"
        " name TEXT NOT NULL, body TEXT NOT NULL, updated_at REAL NOT NULL, rev INTEGER NOT NULL)",
        "CREATE INDEX profiles_group ON profiles(battlegroup, rev)",
        "CREATE INDEX presence_group ON presence(battlegroup)",
        "CREATE INDEX characters_id ON characters(character_id, last_seen)",
        "INSERT INTO meta(k, v) VALUES ('profile_rev', '0')",
    ),
    (  # presence lifecycle: the heartbeat's promise, in a world or not, and when it left
        "ALTER TABLE presence ADD COLUMN next_s REAL NOT NULL DEFAULT 60",
        "ALTER TABLE presence ADD COLUMN in_game INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE presence ADD COLUMN left_at REAL",
    ),
    (  # key roles and idle expiry (keystore.py); the owner's existing key becomes the admin key
        "ALTER TABLE keys ADD COLUMN role TEXT NOT NULL DEFAULT 'regular' CHECK (role IN ('regular', 'admin'))",
        "ALTER TABLE keys ADD COLUMN expired_at REAL",
        "ALTER TABLE keys ADD COLUMN renewed_at REAL",
        "UPDATE keys SET role = 'admin' WHERE id = '7cc09c11'",
    ),
)


class Storage(BoardMixin, KeyStoreMixin, PresenceStoreMixin, ProfileStoreMixin):
    def __init__(self, conn: sqlite3.Connection, clock: Callable[[], float] = time.time) -> None:
        self._conn = conn
        self._lock = threading.Lock()
        self.clock = clock

    @classmethod
    def open(cls, path: str, clock: Callable[[], float] = time.time) -> "Storage":
        conn = sqlite3.connect(path, timeout=5, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        for pragma in ("journal_mode=WAL", "busy_timeout=5000", "synchronous=NORMAL", "foreign_keys=ON"):
            conn.execute(f"PRAGMA {pragma}")
        store = cls(conn, clock)
        store.migrate()
        return store

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def migrate(self) -> int:
        """Apply the migrations above `PRAGMA user_version`; return the new version."""
        with self._tx() as c:
            version = c.execute("PRAGMA user_version").fetchone()[0]
            for number, statements in enumerate(MIGRATIONS[version:], start=version + 1):
                for statement in statements:
                    c.execute(statement)
                c.execute(f"PRAGMA user_version = {number}")
        return len(MIGRATIONS)

    # characters and presence

    def owned_elsewhere(self, key_id: str, character_id: int) -> bool:
        """True when another unrevoked key already wrote this character (a sync or a profile): it
        belongs to that key. The writers below ask inside their own transaction, so two keys racing
        for a new character cannot both win."""
        return self._conn.execute(
            "SELECT 1 FROM keys WHERE id != ? AND revoked_at IS NULL AND id IN (SELECT key_id FROM characters"
            " WHERE character_id = ? UNION SELECT key_id FROM profiles WHERE character_id = ?) LIMIT 1",
            (key_id, character_id, character_id),
        ).fetchone() is not None

    def upsert_character(self, key_id: str, identity: dict, now: float) -> bool:
        """Record that this key plays this character; False (nothing written) when another key owns it."""
        with self._tx() as c:
            if self.owned_elsewhere(key_id, identity["characterId"]):
                return False
            c.execute(
                "INSERT INTO characters(key_id, character_id, name, battlegroup, last_seen) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(key_id, character_id) DO UPDATE SET name = excluded.name,"
                " battlegroup = excluded.battlegroup, last_seen = excluded.last_seen",
                (key_id, identity["characterId"], identity["characterName"], identity["battlegroupId"], now),
            )
        return True

    # audit and retention

    def audit(self, now: float, key_id: str | None, ip: str | None, verb: str, status: int, n: int, note: str | None = None) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO audit(ts, key_id, ip, verb, status, n_records, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (now, key_id, ip, verb, status, n, note),
            )

    def sweep(self, now: float) -> dict[str, int]:
        """Drop rows past the wire contract's retention; return deleted counts per table."""
        with self._tx() as c:
            def delete(sql: str, *args: object) -> int:
                return c.execute(sql, args).rowcount

            presence = self._sweep_presence(c, now)
            short = delete(
                f"DELETE FROM sightings WHERE cls IN {_SHORT_CLASSES} AND ? - received_at + lag > ?", now, SHORT_SIGHTING_S
            )
            long = delete(
                f"DELETE FROM sightings WHERE cls NOT IN {_SHORT_CLASSES} AND ? - received_at + lag > ?", now, LONG_SIGHTING_S
            )
            keys = self.expire_idle_keys(c, now)
            audit = delete("DELETE FROM audit WHERE ? - ts > ?", now, AUDIT_S)
            profiles = delete(
                "DELETE FROM profiles WHERE ? - updated_at > ? AND NOT EXISTS (SELECT 1 FROM characters c"
                " WHERE c.character_id = profiles.character_id AND ? - c.last_seen <= ?)", now, PROFILE_S, now, PROFILE_S
            )
        return {"presence": presence, "sightings": short + long, "audit": audit, "profiles": profiles, "keys": keys}
