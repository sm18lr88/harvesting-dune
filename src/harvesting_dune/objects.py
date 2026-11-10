# Objects: shared overlay objects (pins today, anything with a kind and a body tomorrow): received, aged, filtered, published.
"""A base of peers.PeerMixin (so of app.Detector); the state lives in `PeerStore`, in memory only.

The server keeps the envelope strict and the body opaque (docs/SYNC-PROTOCOL.md "Objects"), so a
kind this client does not know is kept and listed but never drawn, without a warning. Rows are
delivered for the whole battlegroup and filtered here at draw time, so a pin dropped on another map
appears when the player gets there. Nothing here writes `dest`, the main line or overlay markers,
and no body text reaches a log line. Uploads are staged and leave only after a successful exchange,
and only once the server has shown it knows objects: an older server refuses the whole sync.
"""

from __future__ import annotations

import secrets
import time

from .gate import REFUSED
from .peerview import _coord, _num, _skip, _text
from .zones import DEFAULT_FIT, cell_name, is_deep_desert

PIN_TTL_S = 2 * 3600
MAX_TTL_S = 86400
MAX_LIVE_MINE = 20
MAX_LIVE_GROUP = 300
MAX_PER_SYNC = 20
OBJECTS_PAGE = 200
LABEL_MAX = 16
PUBLISH_GAP_S = 1.0
LABEL_KEYS = ("label", "name", "text")


def _scope_ok(scope: object) -> bool:
    return scope is None or (isinstance(scope, dict) and isinstance(scope.get("worldId"), str)
                             and isinstance(scope.get("layout"), str))


def _geometry_ok(geometry: object) -> bool:
    return geometry is None or (isinstance(geometry, dict) and isinstance(geometry.get("type"), str))


def _object(row: object) -> dict | None:
    """One wire object as stored (still with its ttlS and ageS), or None if unusable."""
    if not isinstance(row, dict):
        return None
    cid, nums = row.get("characterId"), [_num(row.get(k)) for k in ("ttlS", "ageS", "rev")]
    texts = [_text(row, k) for k in ("id", "kind", "playerTag")]
    if None in nums or None in texts or not isinstance(cid, int) or isinstance(cid, bool):
        return None
    scope, geometry = row.get("scope"), row.get("geometry")
    if not (_scope_ok(scope) and _geometry_ok(geometry)):
        return None
    return {"id": texts[0], "kind": texts[1], "playerTag": texts[2], "characterId": cid,
            "characterName": _text(row, "characterName") or "", "ttlS": nums[0], "ageS": nums[1], "rev": nums[2],
            "scope": None if scope is None else {"worldId": scope["worldId"], "layout": scope["layout"]},
            "geometry": geometry, "body": row.get("body")}


def _point(geometry: object) -> tuple[float, float, float] | None:
    """Where a point object sits; None for no geometry or a shape this client does not draw."""
    if not isinstance(geometry, dict) or geometry.get("type") != "point":
        return None
    x, y = _coord(geometry.get("x")), _coord(geometry.get("y"))
    return None if x is None or y is None else (x, y, _num(geometry.get("z")) or 0.0)


def _label(o: dict) -> str:
    body = o["body"] if isinstance(o["body"], dict) else {}
    return next((body[k] for k in LABEL_KEYS if isinstance(body.get(k), str)), o["kind"])


class ObjectsMixin:
    def objects_reset(self) -> None:
        """Forget received and staged objects (a new key or identity); the capability memory stays."""
        p = self.peers
        p.objects, p.objects_out, p.objects_note, p.my_tag, p.published_at = {}, [], None, None, 0.0
        p.objects_asked = None

    # -- receiving -----------------------------------------------------------------------------
    def _merge_objects(self, rows: object) -> bool:
        """Merge the response's `objects`; True when a drawable row came, went or changed."""
        peers, me = self.peers, self.identity.get("characterId")
        peers.objects_seen = True
        if not isinstance(rows, list):
            _skip("object", "objects is not a list")
            return False
        before, tag, parsed = self._drawable_revs(), peers.my_tag, [_object(row) for row in rows]
        # learned before any row is flagged: a same-key alt's row may come ahead of our own in rev order
        peers.my_tag = next((o["playerTag"] for o in parsed if o is not None and me is not None and o["characterId"] == me),
                            peers.my_tag)
        for o in parsed:
            if o is None:
                _skip("object", "missing or bad id, kind, characterId, playerTag, ttlS, ageS, rev, scope or geometry")
                continue
            key, ttl, age = (o["playerTag"], o["id"]), o.pop("ttlS"), o.pop("ageS")
            if ttl <= 0:
                peers.objects.pop(key, None)
                continue
            o.update(received_feed=self.clock - age, expires_feed=self.clock + ttl,
                     mine=o["characterId"] == me or o["playerTag"] == peers.my_tag)
            peers.objects[key] = o
        claimed = peers.my_tag != tag and self._claim_tagged()
        return claimed or before != self._drawable_revs()

    def _claim_tagged(self) -> bool:
        """Our tag learned now also owns rows stored earlier: a same-key alt's pin that came in an
        earlier reply. True when one became ours."""
        tagged = [o for o in self.peers.objects.values() if not o["mine"] and o["playerTag"] == self.peers.my_tag]
        for o in tagged:
            o["mine"] = True
        return bool(tagged)

    def _drawable_revs(self) -> dict:
        return {k: o["rev"] for k, o in self.peers.objects.items() if _point(o["geometry"])}

    def objects_expire(self, now: float) -> None:
        """Drop rows whose lifetime ended; `now` is the feed clock."""
        objects = self.peers.objects
        for k in [k for k, o in objects.items() if o["expires_feed"] <= now]:
            del objects[k]

    # -- what is shown -------------------------------------------------------------------------
    def _object_here(self, o: dict) -> bool:
        scope = o["scope"]
        return scope is None or (scope["worldId"], scope["layout"]) == self._peer_here()

    def _drawable_here(self) -> list[dict]:
        return [o for o in self.peers.objects.values() if _point(o["geometry"]) and self._object_here(o)]

    def object_marks(self) -> list[dict]:
        """page_state `objects`: every point object for this map, newest first, except hidden members'."""
        if not self.enabled:
            return []
        hidden = self.sync_hidden_members
        rows = sorted((o for o in self._drawable_here() if o["characterId"] not in hidden), key=lambda o: -o["received_feed"])
        out = []
        for o in rows:
            x, y, z = _point(o["geometry"])
            out.append({"id": f"{o['playerTag']}:{o['id']}", "kind": o["kind"], "x": x, "y": y, "z": z, "body": o["body"],
                        "mine": o["mine"], "by": o["characterName"]})
        return out

    def objects_snapshot(self) -> dict:
        """The settings page's Pins block: your live objects, counts, where a pin would land, why one was refused."""
        p = self.peers
        mine = sorted((o for o in p.objects.values() if o["mine"]), key=lambda o: -o["received_feed"])
        return {"mine": [self._mine_row(o) for o in mine], "pending": sum(1 for u in p.objects_out if u["ttlS"] > 0),
                "here": len(self._drawable_here()), "all": len(p.objects), "hotkeyLabel": self.pin_label,
                "where": self.pin_where(), "note": p.objects_note}

    def _mine_row(self, o: dict) -> dict:
        pt, world, pose = _point(o["geometry"]), (o["scope"] or {}).get("worldId"), self.store.pose
        deep = pt is not None and world is not None and is_deep_desert(world)
        near = pt is not None and pose is not None and self._object_here(o)
        return {"id": o["id"], "kind": o["kind"], "label": _label(o), "worldId": world,
                "sector": cell_name(pt[0], pt[1], self.learning.grid_fit(world) or DEFAULT_FIT) if deep else None,
                "distanceM": round(pose.dist2d(pt[0], pt[1]) / 100) if near else None,
                "ttlS": max(0, round(o["expires_feed"] - self.clock)), "ageS": round(self.clock - o["received_feed"])}

    # -- publishing ----------------------------------------------------------------------------
    def pin_position(self) -> tuple[float, float, float] | None:
        """The companion destination the player set, else where they stand."""
        d, pose = self.native_dest, self.store.pose
        if d is not None:
            return d["x"], d["y"], d["z"]
        return (pose.x, pose.y, pose.z) if pose is not None else None

    def pin_where(self) -> str | None:
        return "destination" if self.native_dest is not None else "position" if self.store.pose is not None else None

    def _publish_refusal(self, geometry: str | None, scope: str | None) -> str | None:
        p = self.peers
        self.objects_expire(self.clock)
        if self._publisher() is None:
            return "needs a guild key and a tracked character"
        if p.key_state in REFUSED:
            return "the guild key was refused: enter a new one"
        if time.time() - p.published_at < PUBLISH_GAP_S:
            return "wait a moment"
        if geometry == "here" and self.pin_position() is None:
            return "no position yet"
        if scope == "here" and self.partition() is None:
            return "no map yet"
        staged = sum(1 for u in p.objects_out if u["ttlS"] > 0)
        if sum(1 for o in p.objects.values() if o["mine"]) + staged >= MAX_LIVE_MINE:
            return f"you already have {MAX_LIVE_MINE} live pins: delete one first"
        if len(p.objects) + staged >= MAX_LIVE_GROUP:  # the server counts every map and every shape
            return f"the guild already has {MAX_LIVE_GROUP} live objects"
        return None

    def publish_object(self, kind: str, body: object, *, geometry: str | None = "here", scope: str | None = "here",
                       ttl_s: float = PIN_TTL_S) -> str | None:
        """Stage one object for the next sync and wake the loop; returns its id, or None with
        `peers.objects_note` saying why not. "here" = the pin position / the current partition;
        None = positionless / battlegroup-wide. No local row: the server's echo brings it."""
        p = self.peers
        p.objects_note = self._publish_refusal(geometry, scope)
        if p.objects_note is not None:
            return None
        pt = self.pin_position() if geometry == "here" else None
        upload = {"id": secrets.token_urlsafe(9), "kind": kind, "scope": self.partition() if scope == "here" else None,
                  "geometry": None if pt is None else {"type": "point", "x": pt[0], "y": pt[1], "z": pt[2]},
                  "body": body, "ttlS": int(min(ttl_s, MAX_TTL_S))}
        p.objects_out.append(upload)
        p.published_at = time.time()
        self.sync_wake()
        return upload["id"]

    def delete_object(self, object_id: str) -> bool:
        """Unstage an upload, or hide an own row at once and stage its delete (ttlS 0); True if either."""
        p = self.peers
        staged = [u for u in p.objects_out if u["id"] == object_id and u["ttlS"] > 0]
        if staged:
            p.objects_out = [u for u in p.objects_out if u not in staged]
        else:
            key = next((k for k, o in p.objects.items() if o["mine"] and o["id"] == object_id), None)
            if key is None:
                return False
            o = p.objects.pop(key)
            p.objects_out.append({"id": o["id"], "kind": o["kind"], "scope": o["scope"], "geometry": o["geometry"],
                                  "body": o["body"], "ttlS": 0})
            self._changed()
        self.sync_wake()
        return True

    # -- the sync loop's side ------------------------------------------------------------------
    def objects_upload(self) -> list[dict]:
        """What the next sync carries: nothing until a response of this process showed `objects`."""
        p = self.peers
        uploads = list(p.objects_out[:MAX_PER_SYNC]) if p.objects_seen else []
        p.objects_asked = {u["id"] for u in uploads}  # the sync loop asks right before it sends
        return uploads

    def objects_sent(self, uploads: list[dict]) -> None:
        sent = {u["id"] for u in uploads}
        self.peers.objects_out = [u for u in self.peers.objects_out if u["id"] not in sent]

    def objects_refused(self, note: str) -> None:
        """The server refused the objects of the last request (a 400 naming `objects[...]`): drop
        them, say why. One staged after that request left was never judged, so it stays; with no
        request on record every staged one counts. A cap refusal keeps the deletes: they always pass
        the cap, and the row is already hidden here. Any other refusal may name a delete itself, so
        keeping it would resend it in a loop."""
        cap, asked = note.split(" ")[0] == "object-cap", self.peers.objects_asked
        self.peers.objects_out = [u for u in self.peers.objects_out
                                  if (asked is not None and u["id"] not in asked) or (cap and u["ttlS"] == 0)]
        self.peers.objects_note = "the server refused the pin" + (": too many live objects" if cap else "")
