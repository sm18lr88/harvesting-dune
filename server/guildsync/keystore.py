"""Guild keys: issue, look up, touch, revoke, renew, list; the regular-key cap and idle expiry.

A key is `regular` or `admin`. At most MAX_ACTIVE_REGULAR regular keys are active at once (not
revoked, not expired); admin keys are outside the cap and never expire. A regular key expires when
its last use, or its creation or last renewal if later, is more than KEY_IDLE_S ago. Expiry is
computed on every lookup; the sweep only records it in `expired_at`.
"""

import hashlib
import hmac
import secrets

MAX_ACTIVE_REGULAR = 64
KEY_IDLE_S = 30 * 86400
ROLES = ("regular", "admin")
_KEY_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")
_SECRET_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
_KEY_COLS = "id, label, note, created_at, revoked_at, last_seen, last_ip, role, expired_at, renewed_at"
_IDLE_SINCE = "MAX(COALESCE(last_seen, created_at), COALESCE(renewed_at, created_at))"
_ACTIVE_REGULAR = (f"role = 'regular' AND revoked_at IS NULL AND expired_at IS NULL AND ? - {_IDLE_SINCE} <= {KEY_IDLE_S}")
_IDLE_REGULAR = f"role = 'regular' AND revoked_at IS NULL AND expired_at IS NULL AND ? - {_IDLE_SINCE} > {KEY_IDLE_S}"


class KeyRevoked(Exception):
    """The key was revoked after the request's key check, before its write; nothing was written."""


class KeyCapFull(Exception):
    def __init__(self, active: int) -> None:
        super().__init__(active)
        self.active = active


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


_UNKNOWN_KEY_HASH = digest("x" * 32)  # compared against for unknown ids, so timing matches


def key_state(row, now: float) -> str:
    """active, expired or revoked, from a key row at `now`."""
    if row["revoked_at"] is not None:
        return "revoked"
    if row["role"] == "admin":
        return "active"
    used = row["created_at"] if row["last_seen"] is None else row["last_seen"]
    since = max(used, row["created_at"] if row["renewed_at"] is None else row["renewed_at"])
    return "expired" if row["expired_at"] is not None or now - since > KEY_IDLE_S else "active"


def _split(token: str) -> tuple[str, str] | None:
    parts = token.split("_", 2)
    if len(parts) != 3 or parts[0] != "gs1":
        return None
    key_id, secret = parts[1], parts[2]
    if len(key_id) != 8 or not _KEY_ID_CHARS.issuperset(key_id):
        return None
    if len(secret) != 32 or not _SECRET_CHARS.issuperset(secret):
        return None
    return key_id, secret


class KeyStoreMixin:
    """Mixed into storage.Storage, which provides `_conn`, `_tx`, `clock`, `_presence_left` and `_objects_left`."""

    def active_regular(self, now: float, conn=None) -> int:
        return (conn or self._conn).execute(f"SELECT COUNT(*) FROM keys WHERE {_ACTIVE_REGULAR}", (now,)).fetchone()[0]

    def issue_key(self, label: str, note: str | None = None, role: str = "regular") -> tuple[str, str]:
        """Create a key; return (key_id, token). Only the secret's sha256 is stored. KeyCapFull when a
        regular key would be one over the cap."""
        if role not in ROLES:
            raise ValueError(role)
        key_id, secret, now = secrets.token_hex(4), secrets.token_urlsafe(24), self.clock()
        with self._tx() as c:
            if role == "regular" and (active := self.active_regular(now, c)) >= MAX_ACTIVE_REGULAR:
                raise KeyCapFull(active)
            c.execute(
                "INSERT INTO keys(id, secret_hash, label, note, created_at, role) VALUES (?, ?, ?, ?, ?, ?)",
                (key_id, digest(secret), label, note, now, role),
            )
        return key_id, f"gs1_{key_id}_{secret}"

    def lookup_key(self, token: str) -> dict | None:
        """The key row of a known, unrevoked token, with `state` active or expired; None for any bad,
        unknown or revoked token."""
        split = _split(token)
        if split is None:
            return None
        key_id, secret = split
        with self._lock:  # every request thread shares the connection; reads take the write lock too
            row = self._conn.execute(f"SELECT {_KEY_COLS}, secret_hash FROM keys WHERE id = ?", (key_id,)).fetchone()
        stored = row["secret_hash"] if row is not None else _UNKNOWN_KEY_HASH
        if not hmac.compare_digest(digest(secret), stored) or row is None or row["revoked_at"] is not None:
            return None
        return {**{k: row[k] for k in _KEY_COLS.split(", ")}, "state": key_state(row, self.clock())}

    def revoked_since_check(self, c, key_id: str) -> bool:
        """Asked inside a write: keysctl may have revoked the key after the request's key check."""
        row = c.execute("SELECT revoked_at FROM keys WHERE id = ?", (key_id,)).fetchone()
        return row is not None and row["revoked_at"] is not None

    def touch_key(self, key_id: str, now: float, ip: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE keys SET last_seen = ?, last_ip = ? WHERE id = ?", (now, ip, key_id))

    def revoke_key(self, key_id: str, now: float) -> str:
        """One of: revoked, already (revoked before), unknown."""
        with self._tx() as c:
            row = c.execute("SELECT revoked_at FROM keys WHERE id = ?", (key_id,)).fetchone()
            if row is None:
                return "unknown"
            if row["revoked_at"] is not None:
                return "already"
            c.execute("UPDATE keys SET revoked_at = ? WHERE id = ?", (now, key_id))
            self._presence_left(c, key_id, now)
            self._objects_left(c, key_id, now)
            return "revoked"

    def renew_key(self, key_id: str, now: float) -> str:
        """One of: renewed, active (nothing to do), revoked, unknown. KeyCapFull when the cap is full."""
        with self._tx() as c:
            row = c.execute(f"SELECT {_KEY_COLS} FROM keys WHERE id = ?", (key_id,)).fetchone()
            if row is None:
                return "unknown"
            state = key_state(row, now)
            if state != "expired":
                return state
            if (active := self.active_regular(now, c)) >= MAX_ACTIVE_REGULAR:
                raise KeyCapFull(active)
            c.execute("UPDATE keys SET expired_at = NULL, renewed_at = ? WHERE id = ?", (now, key_id))
            return "renewed"

    def expire_idle_keys(self, c, now: float) -> int:
        """The sweep's part: record `expired_at` on regular keys idle past KEY_IDLE_S and end their objects."""
        ids = [r[0] for r in c.execute(f"SELECT id FROM keys WHERE {_IDLE_REGULAR}", (now,))]
        c.executemany("UPDATE keys SET expired_at = ? WHERE id = ?", [(now, key_id) for key_id in ids])
        for key_id in ids:
            self._objects_left(c, key_id, now)
        return len(ids)

    def list_keys(self) -> list[dict]:
        """Every key with its state, its characters (newest first) and its last presence world."""
        now = self.clock()
        chars: dict[str, list[dict]] = {}
        for r in self._conn.execute(
            "SELECT key_id, name, character_id, battlegroup, last_seen FROM characters ORDER BY last_seen DESC"
        ):
            chars.setdefault(r["key_id"], []).append({k: r[k] for k in ("name", "character_id", "battlegroup", "last_seen")})
        worlds = {r["key_id"]: r["world"] for r in self._conn.execute("SELECT key_id, world FROM presence ORDER BY received_at")}
        rows = self._conn.execute(f"SELECT {_KEY_COLS} FROM keys ORDER BY created_at, id").fetchall()
        return [{**dict(r), "state": key_state(r, now), "characters": chars.get(r["id"], []), "world": worlds.get(r["id"])}
                for r in rows]
