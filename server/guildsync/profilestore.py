"""Character profiles in the guild store: one row per character, with its own `profile_rev` counter.

A mixin of Storage (storage.py), which owns the connection and the write lock. An unchanged
profile keeps its rev and its age, so a reader's cursor never brings it back.
"""

import hashlib
import hmac

TAG_HEX = 8
MAX_PROFILES_PER_KEY = 20  # a player's characters; bounds what one key adds to every guildmate's reply


class ProfileCap(Exception):
    """The key already stores MAX_PROFILES_PER_KEY other profiles; nothing was written."""


def player_tag(key_id: str, secret_hash: str) -> str:
    """The same short tag for every character of one key, so guildmates can group a player's alts
    without learning who it is: never the key id, never the admin's label, and not computable from
    the key id without the stored secret hash."""
    return hmac.new(bytes.fromhex(secret_hash), key_id.encode("ascii"), hashlib.sha256).hexdigest()[:TAG_HEX]


class ProfileStoreMixin:
    def put_profile(self, key_id: str, identity: dict, body: str, now: float) -> bool:
        """Store the profile; False (nothing written) when another key owns the character, ProfileCap
        when this key already stores the most profiles."""
        cid = identity["characterId"]
        with self._tx() as c:
            if self.owned_elsewhere(key_id, cid):
                return False
            others = c.execute("SELECT COUNT(*) FROM profiles WHERE key_id = ? AND character_id != ?",
                               (key_id, cid)).fetchone()[0]
            if others >= MAX_PROFILES_PER_KEY:
                raise ProfileCap
            old = c.execute("SELECT body, battlegroup, name, key_id FROM profiles WHERE character_id = ?", (cid,)).fetchone()
            # a new key changes the row's playerTag, so it needs a new rev like any other change
            if old is not None and tuple(old) == (body, identity["battlegroupId"], identity["characterName"], key_id):
                c.execute("UPDATE profiles SET updated_at = ? WHERE character_id = ?", (now, cid))
                return True
            rev = int(c.execute("SELECT v FROM meta WHERE k = 'profile_rev'").fetchone()[0]) + 1
            c.execute(
                "INSERT OR REPLACE INTO profiles(character_id, key_id, battlegroup, name, body, updated_at, changed_at, rev)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (cid, key_id, identity["battlegroupId"], identity["characterName"], body, now, now, rev),
            )
            c.execute("UPDATE meta SET v = ? WHERE k = 'profile_rev'", (str(rev),))
        return True

    def delete_profile(self, key_id: str, character_id: int) -> bool:
        """Delete the profile; False (nothing deleted) when another key owns the character."""
        with self._tx() as c:
            if self.owned_elsewhere(key_id, character_id):
                return False
            c.execute("DELETE FROM profiles WHERE character_id = ?", (character_id,))
        return True

    def profiles_since(self, battlegroup: str, since: int) -> tuple[list[dict], list[int], int]:
        """(rows with rev > since, every character id of the battlegroup that has a profile, cursor).
        Each row carries its player's opaque tag (`player_tag`): rows of one key share it."""
        # a row written without changed_at (seeded straight into the file) ages from its last upload
        rows = self._conn.execute(
            "SELECT p.character_id, p.name, p.body, p.updated_at, COALESCE(p.changed_at, p.updated_at) AS changed_at,"
            " p.rev, p.key_id, k.secret_hash FROM profiles p"
            " JOIN keys k ON k.id = p.key_id"
            " WHERE p.battlegroup = ? AND p.rev > ? ORDER BY p.rev",
            (battlegroup, since),
        ).fetchall()
        ids = [r[0] for r in self._conn.execute(
            "SELECT character_id FROM profiles WHERE battlegroup = ? ORDER BY character_id", (battlegroup,))]
        cursor = int(self._conn.execute("SELECT v FROM meta WHERE k = 'profile_rev'").fetchone()[0])
        out = [{"characterId": r["character_id"], "characterName": r["name"], "body": r["body"],
                "updatedAt": r["updated_at"], "changedAt": r["changed_at"], "rev": r["rev"],
                "playerTag": player_tag(r["key_id"], r["secret_hash"])}
               for r in rows]
        return out, ids, cursor
