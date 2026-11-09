"""Guild server shared objects: the envelope rules, caps, lifecycle and the shared cursor, in process."""

import random
import re
import sqlite3

import pytest
from guildsync.api import PARTITION_FIELDS, Api
from guildsync.objects import GRACE_S, SCOPE_FIELDS, parse_object, parse_objects
from guildsync.profilestore import player_tag
from guildsync.storage import MIGRATIONS, Storage

NOW = 1794160000.0
BG = "prd-srv-test-alpha-erg"
DD = {"worldId": "deepdesert_1", "layout": "deepdesert_1_07"}
BEAT = {"nextS": 60, "inGame": True, "leaving": False}
ALPHA = {"characterName": "Alpha Test", "characterId": 1001, "battlegroupId": BG}
BETA = {"characterName": "Beta Test", "characterId": 1002, "battlegroupId": BG}
GAMMA = {"characterName": "Gamma Test", "characterId": 1003, "battlegroupId": BG}
POINT = {"type": "point", "x": 1000.0, "y": -2000.0, "z": 0.0}
WIRE_KEYS = {"id", "kind", "scope", "geometry", "body", "ttlS", "ageS", "characterId", "characterName", "playerTag", "rev"}


def pin(oid="p1", label="IRON HERE", ttl=7200, **override):
    return {"id": oid, "kind": "pin", "scope": DD, "geometry": POINT, "body": {"label": label}, "ttlS": ttl, **override}


def request(ident, since=0, objects=None, position=None, records=()):
    body = {"since": since, "sentAt": NOW, "identity": ident, "partition": DD, "position": position,
            "records": list(records), "presence": BEAT}
    return body if objects is None else {**body, "objects": objects}


@pytest.fixture
def world(tmp_path):
    st = Storage.open(str(tmp_path / "guild.db"), lambda: NOW)
    keys = {name: st.issue_key(f"{name} Test")[0] for name in ("Alpha", "Beta", "Gamma")}
    api = Api(st, lambda: NOW)

    def sync(name, ident, now=NOW, **kw):
        return api.sync({"id": keys[name]}, request(ident, **kw), "127.0.0.1", now)

    yield st, sync, keys
    st.close()


def _rev(st):
    return int(st._conn.execute("SELECT v FROM meta WHERE k = 'rev'").fetchone()[0])


def test_an_object_scope_has_exactly_the_partition_fields():
    assert SCOPE_FIELDS == PARTITION_FIELDS


def test_a_published_object_is_stored_and_echoed_with_rev_ttl_age_and_tag(world):
    st, sync, keys = world
    status, mine = sync("Alpha", ALPHA, objects=[pin()])
    assert status == 200 and [(o["id"], o["ttlS"]) for o in mine["objects"]] == [("p1", 7200.0)]
    status, resp = sync("Beta", BETA, now=NOW + 30)
    [o] = resp["objects"]
    assert status == 200 and set(o) == WIRE_KEYS
    assert (o["kind"], o["scope"], o["geometry"], o["body"]) == ("pin", DD, POINT, {"label": "IRON HERE"})
    assert (o["characterId"], o["characterName"], o["ttlS"], o["ageS"]) == (1001, "Alpha Test", 7170.0, 30.0)
    secret_hash = st._conn.execute("SELECT secret_hash FROM keys WHERE id = ?", (keys["Alpha"],)).fetchone()[0]
    assert re.fullmatch(r"[0-9a-f]{8}", o["playerTag"]) and o["playerTag"] == player_tag(keys["Alpha"], secret_hash)
    assert o["playerTag"] != keys["Alpha"] and o["rev"] <= resp["next"]
    assert sync("Beta", BETA, since=resp["next"])[1]["objects"] == []


def test_a_ttl_zero_upload_is_a_tombstone_with_a_new_rev_that_readers_receive(world):
    _, sync, _ = world
    sync("Alpha", ALPHA, objects=[pin()])
    _, first = sync("Beta", BETA)
    assert sync("Alpha", ALPHA, now=NOW + 60, objects=[pin(ttl=0)])[0] == 200
    _, resp = sync("Beta", BETA, since=first["next"], now=NOW + 61)
    [o] = resp["objects"]
    assert o["id"] == "p1" and o["ttlS"] <= 0 and o["rev"] > first["objects"][0]["rev"]


def test_two_keys_may_use_the_same_object_id(world):
    _, sync, _ = world
    assert sync("Alpha", ALPHA, objects=[pin("same", "ALPHA")])[0] == 200
    assert sync("Beta", BETA, objects=[pin("same", "BETA")])[0] == 200
    _, resp = sync("Gamma", GAMMA)
    assert sorted(o["body"]["label"] for o in resp["objects"]) == ["ALPHA", "BETA"]
    assert len({o["playerTag"] for o in resp["objects"]}) == 2


def test_caps_refuse_the_21st_live_object_and_the_301st_in_a_battlegroup_but_not_a_replace_or_a_delete(world):
    st, sync, _ = world
    at = {"x": 0.0, "y": 0.0, "z": 0.0, "observedAt": NOW}
    assert sync("Alpha", ALPHA, objects=[pin(f"p{n}") for n in range(20)], position=at)[0] == 200
    ore = {"cls": "ore", "type": "magnetiteore", "x": 100.0, "y": 0.0, "z": 0.0, "observedAt": NOW + 10, "fill": None,
           "size": None, "rowKey": None, "distCm": None}
    status, body = sync("Alpha", ALPHA, now=NOW + 10, objects=[pin("p20")], position={**at, "observedAt": NOW + 10},
                        records=[ore])
    assert status == 400 and body["type"].endswith("#object-cap") and body["field"] == "objects[0]"
    assert "key already has 20" in body["detail"]
    count = lambda sql: st._conn.execute(sql).fetchone()[0]  # noqa: E731
    assert count("SELECT received_at FROM presence") == NOW  # the refused request wrote no presence
    assert (count("SELECT COUNT(*) FROM sightings"), count("SELECT COUNT(*) FROM objects")) == (0, 20)
    assert sync("Alpha", ALPHA, objects=[pin("p0", "NEW LABEL")])[0] == 200  # a replace
    assert sync("Alpha", ALPHA, objects=[pin("p1", ttl=0), pin("p20")])[0] == 200  # a delete frees a slot
    for n in range(14):  # 14 more keys x 20 + Alpha's 20 = 300 live in the battlegroup
        filler = {"characterName": f"Filler {n} Test", "characterId": 2000 + n, "battlegroupId": BG}
        st.upsert_objects(f"fill{n:04d}", filler, parse_objects([pin(f"f{i}") for i in range(20)]), NOW)
    status, body = sync("Beta", BETA, objects=[pin("b1")])
    assert status == 400 and body["field"] == "objects[0]" and "battlegroup already has 300" in body["detail"]
    assert sync("Beta", BETA, objects=[pin("b1", ttl=0)])[0] == 200
    assert sync("Alpha", ALPHA, objects=[pin("p0", "AGAIN")])[0] == 200


def _body_of(size):
    body = ["x" * 200, "x" * 200, "x" * (size - 410)]  # canonical text: 410 bytes of punctuation and full strings
    return body


@pytest.mark.parametrize(("objects", "field"), [
    ([pin(body=_body_of(513))], "objects[0].body"),
    ([pin(body={"v": float("nan")})], "objects[0].body.v"),
    ([pin(body=[[[[[[["deep"]]]]]]])], "objects[0].body[0][0][0][0][0][0][0]"),
    ([{**pin(), "extra": 1}], "objects[0].extra"),
    ([pin(kind="Pin")], "objects[0].kind"),
    ([pin(oid="")], "objects[0].id"),
    ([pin(geometry={**POINT, "type": "blob"})], "objects[0].geometry.type"),
    ([pin(geometry={**POINT, "type": ["point"]})], "objects[0].geometry.type"),
    ([pin(geometry={**POINT, "x": float("inf")})], "objects[0].geometry.x"),
    ([pin(scope={"worldId": "deep desert", "layout": "x"})], "objects[0].scope.worldId"),
    ([pin(ttl=-1)], "objects[0].ttlS"),
    ([pin(ttl=True)], "objects[0].ttlS"),
    ([pin(), pin("p2", ttl=1.5)], "objects[1].ttlS"),
    ([pin(f"p{n}") for n in range(21)], "objects"),
    ({"id": "p1"}, "objects"),
])
def test_object_body_limits_answer_400(world, objects, field):
    st, sync, _ = world
    status, body = sync("Alpha", ALPHA, objects=objects)
    assert (status, body["type"].rsplit("#", 1)[-1], body["field"]) == (400, "invalid-field", field)
    assert st._conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0] == 0


def test_object_body_limits_accept_the_edges_and_clamp_the_lifetime(world):
    _, sync, _ = world
    status, resp = sync("Alpha", ALPHA, objects=[pin(body=_body_of(512)), pin("p2", ttl=10**6), pin("p3", scope=None, geometry=None)])
    assert status == 200 and [o["ttlS"] for o in resp["objects"]] == [7200.0, 86400.0, 7200.0]
    assert resp["objects"][0]["body"] == _body_of(512) and resp["objects"][2]["scope"] is None


@pytest.mark.parametrize(("n_sightings", "n_objects"), [(600, 450), (600, 10), (10, 450)])
def test_both_pages_cut_deliver_every_sighting_and_object_once_per_rev(world, n_sightings, n_objects):
    st, sync, keys = world
    kinds = ["s"] * n_sightings + ["o"] * n_objects
    random.Random(7).shuffle(kinds)
    owners = list(keys.values())
    with st._tx() as c:
        for rev, kind in enumerate(kinds, start=1):
            if kind == "s":
                c.execute("INSERT INTO sightings(key, battlegroup, world, layout, cls, type, x, y, z, observed_at,"
                          " received_at, lag, observer_key, observer_name, observers, rev)"
                          " VALUES (?, ?, ?, ?, 'ore', 'magnetiteore', ?, 0, 0, ?, ?, 0, 'k', 'Alpha Test', '[]', ?)",
                          (f"s{rev}", BG, DD["worldId"], DD["layout"], rev * 2000.0, NOW, NOW, rev))
            else:
                c.execute("INSERT INTO objects(key_id, id, battlegroup, character_id, name, kind, body, expires_at,"
                          " received_at, rev) VALUES (?, ?, ?, 1001, 'Alpha Test', 'pin', '{}', ?, ?, ?)",
                          (owners[rev % 3], f"o{rev}", BG, NOW + 3600, NOW, rev))
        c.execute("UPDATE meta SET v = ? WHERE k = 'rev'", (str(len(kinds)),))
    revs, objects, since = set(), set(), 0
    for _ in range(20):
        status, resp = sync("Gamma", GAMMA, since=since)
        assert status == 200
        revs |= {r["rev"] for r in resp["sightings"]} | {o["rev"] for o in resp["objects"]}
        objects |= {(o["playerTag"], o["id"]) for o in resp["objects"]}
        since = resp["next"]
        if len(resp["sightings"]) < 500 and len(resp["objects"]) < 200:
            break
    else:
        pytest.fail("the cursor never caught up")
    assert revs == set(range(1, len(kinds) + 1)) and len(objects) == n_objects and since == len(kinds)


def test_revoke_and_idle_expiry_expire_the_keys_objects_with_new_revs(world):
    st, sync, keys = world
    sync("Alpha", ALPHA, objects=[pin("a1"), pin("a2")])
    prev = _rev(st)
    assert st.revoke_key(keys["Alpha"], NOW + 5) == "revoked"
    rows = st._conn.execute("SELECT expires_at, rev FROM objects WHERE key_id = ?", (keys["Alpha"],)).fetchall()
    assert [r[0] for r in rows] == [NOW + 5] * 2 and all(r[1] > prev for r in rows)
    got, _ = st.objects_since(BG, prev, NOW + 5)
    assert sorted(o["id"] for o in got) == ["a1", "a2"] and all(o["ttlS"] <= 0 for o in got)
    sync("Beta", BETA)
    later = NOW + 31 * 86400  # Beta idle for 31 days; its object from an hour ago is still live
    st.upsert_objects(keys["Beta"], BETA, parse_objects([pin("b1")]), later - 3600)
    prev = _rev(st)
    assert st.sweep(later)["keys"] == 2  # Beta, and Gamma that never synced
    [(expires, rev)] = st._conn.execute("SELECT expires_at, rev FROM objects WHERE key_id = ?", (keys["Beta"],)).fetchall()
    assert expires == later and rev > prev
    got, _ = st.objects_since(BG, prev, later)
    assert [(o["id"], o["ttlS"]) for o in got] == [("b1", 0.0)]


def test_sweep_keeps_expired_rows_24_hours_then_deletes_them_and_trims_a_key_to_100_rows(world):
    st, sync, keys = world
    sync("Alpha", ALPHA, objects=[pin("gone", ttl=0)])
    assert st.sweep(NOW + GRACE_S)["objects"] == 0
    assert st.sweep(NOW + GRACE_S + 1)["objects"] == 1
    st.upsert_objects(keys["Beta"], BETA, parse_objects([pin(f"live{n}") for n in range(20)]), NOW)
    for batch in range(6):  # 120 tombstones: only the newest 80 fit beside the 20 live rows
        st.upsert_objects(keys["Beta"], BETA, parse_objects([pin(f"t{batch}x{n}", ttl=0) for n in range(20)]), NOW + 1)
    ids = {r[0] for r in st._conn.execute("SELECT id FROM objects WHERE key_id = ?", (keys["Beta"],))}
    assert len(ids) == 100 and {f"live{n}" for n in range(20)} <= ids
    assert not any(i.startswith(("t0x", "t1x")) for i in ids)


def test_a_version_5_database_gains_the_objects_table_and_keeps_its_rows(tmp_path):
    path = str(tmp_path / "guild.db")
    with sqlite3.connect(path) as db:
        for statements in MIGRATIONS[:5]:
            for statement in statements:
                db.execute(statement)
        db.execute("PRAGMA user_version = 5")
        db.execute("INSERT INTO keys(id, secret_hash, label, created_at) VALUES ('alpha001', 'x', 'Alpha Test', 0)")
        db.execute("INSERT INTO sightings(key, battlegroup, world, layout, cls, type, observed_at, received_at, lag,"
                   " observer_key, observer_name, observers, rev) VALUES ('k', ?, 'w', 'l', 'ore', 'iron', 0, 0, 0,"
                   " 'alpha001', 'Alpha Test', '[]', 1)", (BG,))
    db.close()
    st = Storage.open(path)
    assert st._conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS) == 6
    assert [k["id"] for k in st.list_keys()] == ["alpha001"]
    assert st._conn.execute("SELECT COUNT(*) FROM sightings").fetchone()[0] == 1
    assert st.objects_since(BG, 0, NOW) == ([], None)
    st.close()


def test_every_objects_query_of_a_sync_uses_an_index(world):
    st, sync, keys = world
    tombstones = [parse_object(pin(f"t{n}", ttl=0), "objects[0]") for n in range(100)]
    st.upsert_objects(keys["Alpha"], ALPHA, tombstones, NOW)  # the traced upload below also trims
    statements: list[str] = []
    st._conn.set_trace_callback(statements.append)
    assert sync("Alpha", ALPHA, objects=[pin()])[0] == 200
    assert sync("Beta", BETA)[0] == 200
    st._conn.set_trace_callback(None)
    queries = [s for s in statements if "objects" in s and (s.split()[0].upper() in ("SELECT", "UPDATE", "DELETE") or " WHERE " in s)]
    assert any(s.startswith("DELETE FROM objects") for s in queries)
    for sql in queries:
        plan = [row[3] for row in st._conn.execute("EXPLAIN QUERY PLAN " + sql)]
        assert not [p for p in plan if p.startswith("SCAN")], (sql, plan)
