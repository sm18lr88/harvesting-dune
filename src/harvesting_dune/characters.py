# Characters: our shared character profile and the guild's, for the settings page's Characters view.
"""A mixin of app.Detector. It keeps the latest payload of each profile topic, decides when our
profile goes up (only when it changed, at most every PROFILE_EVERY_S; a switch change or the first
upload after start at once) and when the guild's profiles are fetched (only while the Characters
view is open). syncloop.py does the requests; nothing here touches the network or a file.

Two switches, saved by settings.py: "Share my character" (off: one delete, then nothing) and
"Share this session" (off: the session part is left out of the next upload).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from . import profile
from .model import BusMessage
from .peerview import STATES

PROFILE_EVERY_S = 300.0
FETCH_EVERY_S = 60.0
OPEN_S = 150.0  # the open Characters view says so every 60 s


@dataclass
class CharacterState:
    topics: dict = field(default_factory=dict)  # profile topic -> latest payload (deltas merged)
    sent_digest: str | None = None  # what the server holds of ours; "deleted" after a delete
    sent_at: float = -math.inf
    forced: bool = True  # upload on the next pass whatever the 5-minute gate says
    open_at: float = -math.inf  # wall time the Characters view last reported itself open
    fetched_at: float = -math.inf
    fetch_forced: bool = False
    since: int = 0  # the server's profile cursor
    scope: str | None = None  # the battlegroup `since` and `guild` are about: one rev for all, rows per battlegroup
    guild: dict = field(default_factory=dict)  # character id -> {name, updatedAt, rev, profile}


def _int(v: object) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _guild_row(row: object, now: float) -> tuple[int, dict] | None:
    if not isinstance(row, dict):
        return None
    cid, name, age = _int(row.get("characterId")), row.get("characterName"), row.get("ageS")
    if cid is None or not isinstance(name, str) or not isinstance(row.get("profile"), dict):
        return None
    age = age if isinstance(age, (int, float)) and not isinstance(age, bool) and math.isfinite(age) else 0.0
    tag = row.get("playerTag")
    return cid, {"name": name[:64], "updatedAt": now - max(age, 0.0), "rev": _int(row.get("rev")),
                 "profile": profile.received(row["profile"]), "tag": tag[:16] if isinstance(tag, str) else None}


def _rank(row: dict) -> int:
    """You first, then the presence states in roster order, then characters with a profile only."""
    return -1 if row["state"] == "you" else STATES.index(row["state"]) if row["state"] in STATES else len(STATES)


def _by_player(rows: list[dict]) -> list[dict]:
    """Each player's characters together (the server's opaque tag; no names of people anywhere),
    yours first, then players with someone online; inside a player, the order of `_rank`. A row
    whose tag is not known yet stands alone, last. `player` heads a player with alts: their names."""
    group = {r["id"]: r.pop("tag") or ("alone", r["id"]) for r in rows}
    mine = next((group[r["id"]] for r in rows if r["self"]), None)
    best: dict = {}
    for r in rows:
        best[group[r["id"]]] = min(best.get(group[r["id"]], len(STATES)), _rank(r))
    out = sorted(rows, key=lambda r: (group[r["id"]] != mine, isinstance(group[r["id"]], tuple), best[group[r["id"]]],
                                      str(group[r["id"]]), _rank(r), str(r["name"]).lower()))
    names: dict = {}
    for r in out:
        names.setdefault(group[r["id"]], []).append(str(r["name"]))
    for r in out:
        alts = names[group[r["id"]]]
        r["player"] = " · ".join(alts) if len(alts) > 1 else ""
    return out


class CharacterMixin:
    def on_character_topic(self, msg: BusMessage) -> None:
        topics = self.chars.topics
        if msg.type == "topicReset":
            topics.pop(msg.topic, None)
        elif isinstance(msg.payload, dict):
            topics[msg.topic] = {**topics.get(msg.topic, {}), **msg.payload}

    def own_profile(self) -> dict:
        world = self.store.world
        status = {"worldId": world[0] if world else None, "worldName": self.world_name,
                  "activity": self.activity, "inGame": self.in_game}
        return profile.build(self.chars.topics, status, self.share_session)

    def _profile_ident(self) -> dict | None:
        ident = self.identity
        ready = self.sync_key and self.sync_enabled and self.identity_ready and "characterId" in ident
        return {f: ident[f] for f in ("characterName", "characterId", "battlegroupId")} if ready else None

    def profile_upload(self, now: float) -> dict | None:
        """The next POST /v1/profile body, or None when nothing needs to go up."""
        ident, c = self._profile_ident(), self.chars
        if ident is None:
            return None
        if not self.share_character:
            return None if c.sent_digest == "deleted" else {"identity": ident, "profile": None}
        if not c.forced and now - c.sent_at < PROFILE_EVERY_S:
            return None
        mine = self.own_profile()
        if profile.digest(mine) == c.sent_digest:
            c.forced = False
            return None
        return {"identity": ident, "profile": mine}

    def profile_sent(self, body: dict, now: float) -> None:
        c = self.chars
        c.sent_digest, c.sent_at, c.forced = profile.digest(body["profile"]), now, False

    def sharing_changed(self) -> None:
        self.chars.forced = True
        self.sync_wake()

    def characters_opened(self) -> None:
        """The settings page shows the Characters view: fetch now, and keep fetching while it stays open."""
        c, now = self.chars, time.time()
        reopened = now - c.open_at > OPEN_S  # else the view's 60 s "still open": the loop's next tick fetches, no sync
        c.open_at, c.fetch_forced = now, True
        if reopened:
            self.sync_wake()

    def profiles_request(self, now: float) -> dict | None:
        ident, c = self._profile_ident(), self.chars
        if ident is None or time.time() - c.open_at > OPEN_S:
            return None
        if c.scope != ident["battlegroupId"]:  # another character's battlegroup: all of its profiles, now
            c.guild.clear()
            c.since, c.scope, c.fetch_forced = 0, ident["battlegroupId"], True
        if not c.fetch_forced and now - c.fetched_at < FETCH_EVERY_S:
            return None
        return {"identity": ident, "since": c.since}

    def ingest_profiles(self, response: dict, now: float) -> None:
        """Merge a POST /v1/profiles answer: changed profiles replace ours, ids no longer listed go."""
        c = self.chars
        c.fetched_at, c.fetch_forced = now, False
        ids, cursor = response.get("ids"), _int(response.get("next"))
        if not isinstance(ids, list) or cursor is None:
            return
        for row in response.get("profiles") if isinstance(response.get("profiles"), list) else ():
            parsed = _guild_row(row, now)
            if parsed is not None:
                c.guild[parsed[0]] = parsed[1]
        listed = {i for i in ids if _int(i) is not None}
        c.guild = {cid: g for cid, g in c.guild.items() if cid in listed}
        c.since = cursor

    def character_ended(self) -> None:
        """The character we profiled is gone (the game closed, or another one is playing): its topics
        must never be uploaded under the next identity, and the next one uploads at once."""
        c = self.chars
        c.topics.clear()
        c.sent_digest, c.sent_at, c.forced = None, -math.inf, True

    def forget_guild_profiles(self) -> None:
        """A new key may belong to another guild: what we hold and what we sent no longer apply."""
        self.chars.guild.clear()
        self.chars.since, self.chars.sent_digest, self.chars.forced = 0, None, True

    # -- what the settings page shows ---------------------------------------------------------
    def _guild_row(self, cid: int, now: float) -> dict:
        """A guildmate known from presence, from a profile, or both."""
        m, g = self.peers.members.get(cid), self.chars.guild.get(cid)
        status = g["profile"].get("status") if g else None
        status = status if isinstance(status, dict) else {}
        return {"id": cid, "name": g["name"] if g else m["characterName"], "self": False, "state": m["state"] if m else None,
                "tag": g.get("tag") if g else None,
                "world": status.get("worldName") or (m["worldId"] if m else None), "activity": status.get("activity"),
                "seenS": None if m is None else round(self.clock - m["seen_at"]),
                "profileAgeS": None if g is None else round(now - g["updatedAt"]), "rev": g["rev"] if g else None}

    def characters_snapshot(self) -> dict:
        c, now, me = self.chars, time.time(), self.identity.get("characterId")
        rows = [self._guild_row(cid, now) for cid in (set(self.peers.members) | set(c.guild)) - {me}]
        if me is not None and self.identity.get("characterName"):
            sent = c.sent_digest not in (None, "deleted")
            rows.append({"id": me, "name": self.identity["characterName"], "self": True, "state": "you",
                         "tag": c.guild.get(me, {}).get("tag"), "world": self.world_name,
                         "activity": self.activity, "seenS": 0, "profileAgeS": round(now - c.sent_at) if sent else None,
                         "rev": c.sent_digest})
        return {"share": self.share_character, "shareSession": self.share_session, "rows": _by_player(rows)}

    def character_detail(self, cid: int) -> dict | None:
        """One character's full profile for the details pane; ours is what the guild sees of us."""
        if cid == self.identity.get("characterId"):
            mine = self.own_profile() if self.share_character else None
            body = {"name": self.identity.get("characterName"), "self": True, "ageS": None, "profile": mine}
        elif cid in self.chars.guild:
            g = self.chars.guild[cid]
            body = {"name": g["name"], "self": False, "ageS": round(time.time() - g["updatedAt"]), "profile": g["profile"]}
        elif cid in self.peers.members:  # listed from presence alone: the pane says so instead of asking for a pick
            body = {"name": self.peers.members[cid]["characterName"], "self": False, "ageS": None, "profile": None}
        else:
            return None
        recipes = (body["profile"] or {}).get("recipes")
        names = {r[0]: self.items.items[r[0]]["name"] for r in recipes if isinstance(r, list) and r
                 and isinstance(r[0], str) and r[0] in self.items.items} if isinstance(recipes, list) else {}
        return {"id": cid, **body, "recipeNames": names}
