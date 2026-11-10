import contextlib
import hashlib
import sqlite3
import threading

from guildsync import profilestore
from guildsync.api import Api, parse
from guildsync.objects import parse_objects
from guildsync.profiles import PROFILE_FIELDS
from guildsync.storage import MIGRATIONS, Storage
from test_sync_objects_server import ALPHA, BETA, pin, request

NOW = 1794160000.0
BG = "prd-srv-test-alpha-erg"
DD = {"worldId": "deepdesert_1", "layout": "deepdesert_1_07"}
DD_OTHER = {"worldId": "deepdesert_1", "layout": "deepdesert_1_03"}


def record(cls="spice", type_="spicefieldlarge", x=123456.0, y=-654321.0, observed_at=NOW - 30):
    return {"cls": cls, "type": type_, "x": x, "y": y, "z": 1500.0, "observedAt": observed_at,
            "fill": None, "size": None, "rowKey": None, "distCm": None}


def opened(tmp_path):
    return Storage.open(str(tmp_path / "guild.db"))


def test_migration_sets_version_and_reopen_keeps_data(tmp_path):
    st = opened(tmp_path)
    key_id, token = st.issue_key("Paul")
    st.close()
    st = opened(tmp_path)
    assert st._conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    assert st._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert st.lookup_key(token)["id"] == key_id
    st.close()


def test_keys_lookup_revoke_and_hash_only(tmp_path):
    st = opened(tmp_path)
    key_id, token = st.issue_key("Paul", "Atreides")
    secret = token.split("_", 2)[2]
    assert len(key_id) == 8 and len(secret) == 32
    stored = st._conn.execute("SELECT secret_hash FROM keys").fetchone()[0]
    assert stored == hashlib.sha256(secret.encode()).hexdigest()
    assert st.lookup_key(token)["label"] == "Paul"
    wrong = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert st.lookup_key(wrong) is None
    assert st.lookup_key("gs1_bad") is None
    assert st.lookup_key("gs1_zzzzzzzz_" + "a" * 32) is None
    assert st.revoke_key(key_id, NOW) == "revoked"
    assert st.lookup_key(token) is None
    assert st.revoke_key(key_id, NOW) == "already"
    assert st.revoke_key("nope0000", NOW) == "unknown"
    st.close()


def test_list_keys_has_characters_and_world(tmp_path):
    st = opened(tmp_path)
    key_id, _ = st.issue_key("Paul")
    ident = {"characterName": "Paul Test", "characterId": 1001, "battlegroupId": BG}
    st.upsert_character(key_id, ident, NOW)
    st.upsert_presence(key_id, ident, DD, None, NOW, 0.0)
    [row] = st.list_keys()
    assert row["characters"] == [{"name": "Paul Test", "character_id": 1001, "battlegroup": BG, "last_seen": NOW}]
    assert row["world"] == "deepdesert_1"
    st.close()


def test_sighting_upsert_merges_observers_and_bumps_rev(tmp_path):
    st = opened(tmp_path)
    st.upsert_sightings("alpha001", "Paul Test", BG, DD, [record()], NOW, NOW)
    st.upsert_sightings("bravo002", "Chani Test", BG, DD, [record(x=123999.0, observed_at=NOW + 5)], NOW + 10, NOW + 10)
    rows, nxt = st.sightings_since(BG, DD["worldId"], DD["layout"], 0)
    assert len(rows) == 1 and nxt == 2
    assert rows[0]["rev"] == 2 and rows[0]["observers"] == ["alpha001", "bravo002"]
    assert rows[0]["x"] == 123999.0 and rows[0]["observerKey"] == "bravo002" and rows[0]["lag"] == 5.0
    st.upsert_sightings("charl003", "Feyd", BG, DD, [record(x=123000.0, observed_at=NOW - 100)], NOW + 20, NOW + 20)
    [row], _ = st.sightings_since(BG, DD["worldId"], DD["layout"], 2)
    assert row["x"] == 123999.0 and row["observers"] == ["alpha001", "bravo002", "charl003"]
    st.close()


def test_sightings_since_returns_only_newer_rows_of_the_partition(tmp_path):
    st = opened(tmp_path)
    st.upsert_sightings("alpha001", "Paul", BG, DD, [record(x=0.0), record(x=5000.0)], NOW, NOW)
    st.upsert_sightings("alpha001", "Paul", BG, DD_OTHER, [record(x=9000.0)], NOW, NOW)
    st.upsert_sightings("alpha001", "Paul", BG, DD, [record(x=20000.0)], NOW, NOW)
    rows, nxt = st.sightings_since(BG, DD["worldId"], DD["layout"], 1)
    assert [r["rev"] for r in rows] == [2, 4] and nxt == 4
    rows, nxt = st.sightings_since(BG, DD["worldId"], DD["layout"], 0, limit=2)
    assert [r["rev"] for r in rows] == [1, 2] and nxt == 2
    assert st.sightings_since("other-bg", DD["worldId"], DD["layout"], 0) == ([], 4)
    st.close()


def test_a_version_1_database_loses_its_bases_table_and_keeps_its_rows(tmp_path):
    path = str(tmp_path / "guild.db")
    with sqlite3.connect(path) as db:
        for statement in MIGRATIONS[0]:
            db.execute(statement)
        db.execute("PRAGMA user_version = 1")
        db.execute("INSERT INTO keys(id, secret_hash, label, created_at) VALUES ('alpha001', 'x', 'Paul', 0)")
    db.close()
    st = Storage.open(path)
    tables = {r[0] for r in st._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "bases" not in tables and "profiles" in tables and [k["id"] for k in st.list_keys()] == ["alpha001"]
    assert st._conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    st.close()


def test_sweep_applies_retention(tmp_path):
    st = opened(tmp_path)
    ident = {"characterName": "Paul", "characterId": 1, "battlegroupId": BG}
    st.upsert_presence("alpha001", ident, DD, None, NOW - 19 * 60, 0.0)  # offline for over 15 min: gone
    st.upsert_sightings("alpha001", "Paul", BG, DD, [record()], NOW - 4 * 3600, NOW - 4 * 3600)
    st.upsert_sightings("alpha001", "Paul", BG, DD, [record("ore", "magnetiteore", x=0.0)], NOW - 4 * 3600, NOW - 4 * 3600)
    st.upsert_sightings("alpha001", "Paul", BG, DD, [record("ship", "salvagesteel")], NOW - 20 * 3600, NOW - 20 * 3600)
    st.audit(NOW, "alpha001", "127.0.0.1", "sync", 200, 1)
    gone = {"id": "pin1", "kind": "pin", "scope": None, "geometry": None, "body": "{}", "ttlS": 60}
    st.upsert_objects("alpha001", ident, [gone], NOW - 25 * 3600)  # expired over 24 hours ago
    assert st.presence_for(BG, NOW) == []
    assert st.sweep(NOW) == {"presence": 1, "sightings": 2, "audit": 0, "profiles": 0, "keys": 0, "objects": 1}
    rows, _ = st.sightings_since(BG, DD["worldId"], DD["layout"], 0)
    assert [r["cls"] for r in rows] == ["ship"]
    st.close()


def test_two_threads_writing_sightings_keep_every_row(tmp_path):
    st = opened(tmp_path)
    errors: list[BaseException] = []

    def writer(offset):
        try:
            for i in range(200):
                st.upsert_sightings("alpha001", "Paul", BG, DD, [record(x=(offset + i) * 2000.0)], NOW, NOW)
        except (sqlite3.Error, ValueError) as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(o,)) for o in (0, 1000)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    rows, nxt = st.sightings_since(BG, DD["worldId"], DD["layout"], 0, limit=1000)
    assert len(rows) == 400 and nxt == 400
    assert sorted(r["rev"] for r in rows) == list(range(1, 401))
    st.close()



def test_a_profile_stays_30_days_after_its_last_upload(tmp_path):
    st = opened(tmp_path)
    blank = '{"status":null}'
    for cid, days in ((1, 29), (2, 31)):
        st.put_profile("alpha001", {"characterName": f"Alt {cid}", "characterId": cid, "battlegroupId": BG}, blank,
                       NOW - days * 86400)
    assert st.sweep(NOW)["profiles"] == 1
    assert st.profiles_since(BG, 0)[1] == [1]
    st.close()


class _Abort(Exception):
    pass


class _Announcing:
    """Storage._lock that sets `asked` as soon as a thread starts to take it."""

    def __init__(self, lock, asked: threading.Event) -> None:
        self.lock, self.asked = lock, asked

    def __enter__(self):
        self.asked.set()
        return self.lock.__enter__()

    def __exit__(self, *exc):
        return self.lock.__exit__(*exc)


def read_during_a_rolled_back_write(st, write, read):
    """What `read()` returns on another thread while `write(c)` is uncommitted; the write then rolls back."""
    held, release, progress, seen = threading.Event(), threading.Event(), threading.Event(), []

    def writer():
        with contextlib.suppress(_Abort), st._tx() as c:
            write(c)
            held.set()
            release.wait(10)
            raise _Abort

    def reader():
        seen.append(read())
        progress.set()

    w = threading.Thread(target=writer)
    w.start()
    assert held.wait(10)
    lock, st._lock = st._lock, _Announcing(st._lock, progress)
    r = threading.Thread(target=reader)
    r.start()
    assert progress.wait(10)  # the reader is done, or it waits for the writer's lock
    release.set()
    w.join()
    r.join()
    st._lock = lock
    return seen[0]


def test_a_sync_reply_never_shows_a_write_that_rolls_back(tmp_path):
    st = opened(tmp_path)
    key_id, _ = st.issue_key("Beta")
    api = Api(st, lambda: NOW)

    def write(c):  # rows a sync has written when its next object hits the cap
        c.execute("INSERT INTO objects(key_id, id, battlegroup, character_id, name, kind, body, expires_at, received_at,"
                  " rev) VALUES (?, 'ghost', ?, 1002, 'Beta Test', 'pin', '{}', ?, ?, 1)", (key_id, BG, NOW + 60, NOW))
        c.execute("INSERT INTO sightings(key, battlegroup, world, layout, cls, type, observed_at, received_at, lag,"
                  " observer_key, observer_name, observers, rev) VALUES ('ghost', ?, ?, ?, 'spice', 'spicefieldlarge',"
                  " ?, ?, 0, ?, 'Beta Test', '[]', 2)", (BG, DD["worldId"], DD["layout"], NOW, NOW, key_id))
        c.execute("UPDATE meta SET v = '2' WHERE k = 'rev'")

    view = read_during_a_rolled_back_write(st, write, lambda: api._view(parse(request(ALPHA)), NOW))
    assert (view["objects"], view["sightings"], view["next"]) == ([], [], 0)
    st.close()


def test_a_profiles_reply_never_shows_a_write_that_rolls_back(tmp_path):
    st = opened(tmp_path)
    key_id, _ = st.issue_key("Beta")
    api = Api(st, lambda: NOW)

    def write(c):
        c.execute("INSERT INTO profiles(character_id, key_id, battlegroup, name, body, updated_at, rev)"
                  " VALUES (1002, ?, ?, 'Beta Test', '{}', ?, 1)", (key_id, BG, NOW))
        c.execute("UPDATE meta SET v = '1' WHERE k = 'profile_rev'")

    status, reply = read_during_a_rolled_back_write(
        st, write, lambda: api.profiles({"id": key_id}, {"identity": ALPHA, "since": 0}, "127.0.0.1", NOW))
    assert (status, reply["profiles"], reply["ids"], reply["next"]) == (200, [], [], 0)
    st.close()


def test_a_sync_refused_for_the_object_cap_does_not_claim_the_character(tmp_path):
    st = opened(tmp_path)
    alpha, beta = st.issue_key("Alpha")[0], st.issue_key("Beta")[0]
    api = Api(st, lambda: NOW)
    st.upsert_objects(alpha, ALPHA, parse_objects([pin(f"p{n}") for n in range(20)]), NOW)
    status, body = api.sync({"id": alpha}, request(BETA, objects=[pin("one-too-many")]), "127.0.0.1", NOW)
    assert (status, body["type"].rsplit("#", 1)[1]) == (400, "object-cap")
    assert st._conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0] == 0
    assert api.sync({"id": beta}, request(BETA), "127.0.0.1", NOW)[0] == 200
    st.close()


def test_one_key_stores_a_bounded_number_of_profiles(tmp_path):
    st = opened(tmp_path)
    key_id, _ = st.issue_key("Paul")
    api = Api(st, lambda: NOW)
    blank = dict.fromkeys(PROFILE_FIELDS)

    def upload(cid):
        body = {"identity": {**ALPHA, "characterId": cid}, "profile": blank}
        return api.profile({"id": key_id}, body, "127.0.0.1", NOW)

    cap = profilestore.MAX_PROFILES_PER_KEY
    assert [upload(cid)[0] for cid in range(1, cap + 1)] == [200] * cap
    status, body = upload(cap + 1)
    assert (status, body["field"]) == (400, "identity.characterId")
    assert len(st.profiles_since(BG, 0)[1]) == cap
    assert upload(1)[0] == 200  # a stored character still updates
    st.close()


def test_the_newest_sighting_is_judged_on_the_server_clock_not_the_senders(tmp_path):
    st = opened(tmp_path)
    fast = 900.0  # Paul's PC clock runs 15 min ahead
    paul = {**record(observed_at=NOW - 30 + fast), "fill": 0.9}  # seen 30 s before the server's NOW
    st.upsert_sightings("alpha001", "Paul Test", BG, DD, [paul], NOW, NOW + fast)
    chani = {**record(x=123999.0, observed_at=NOW + 30), "fill": 0.1}  # seen 60 s later, by a correct clock
    st.upsert_sightings("bravo002", "Chani Test", BG, DD, [chani], NOW + 60, NOW + 60)
    [row], _ = st.sightings_since(BG, DD["worldId"], DD["layout"], 0)
    assert (row["observerKey"], row["fill"], row["x"]) == ("bravo002", 0.1, 123999.0)
    assert row["observers"] == ["alpha001", "bravo002"]
    st.close()


def test_a_profile_taken_over_by_a_new_key_reaches_readers_with_a_cursor(tmp_path):
    st = opened(tmp_path)
    old, new = st.issue_key("Paul")[0], st.issue_key("Paul again")[0]
    ident = {"characterName": "Paul Test", "characterId": 1001, "battlegroupId": BG}
    blank = '{"status":null}'
    st.put_profile(old, ident, blank, NOW)
    [first], _, cursor = st.profiles_since(BG, 0)
    st.revoke_key(old, NOW + 1)
    assert st.put_profile(new, ident, blank, NOW + 2)  # the same body, uploaded through the player's new key
    rows, _, _ = st.profiles_since(BG, cursor)
    fresh, _, _ = st.profiles_since(BG, 0)
    assert rows == fresh and len(rows) == 1 and rows[0]["playerTag"] != first["playerTag"]
    st.close()

def test_an_unchanged_profile_uploaded_again_keeps_its_age_from_the_last_change(tmp_path):
    st = opened(tmp_path)
    key_id, _ = st.issue_key("Paul")
    api = Api(st, lambda: NOW)
    upload = {"identity": ALPHA, "profile": dict.fromkeys(PROFILE_FIELDS)}
    assert api.profile({"id": key_id}, upload, "127.0.0.1", NOW)[0] == 200
    assert api.profile({"id": key_id}, upload, "127.0.0.1", NOW + 600)[0] == 200  # the same profile, 10 min later
    _, reply = api.profiles({"id": key_id}, {"identity": ALPHA, "since": 0}, "127.0.0.1", NOW + 600)
    assert [p["ageS"] for p in reply["profiles"]] == [600.0]
    st.close()


def test_a_version_7_database_keeps_its_profiles_and_dates_their_last_change_from_their_last_upload(tmp_path):
    path = str(tmp_path / "guild.db")
    with sqlite3.connect(path) as db:
        for statements in MIGRATIONS[:7]:
            for statement in statements:
                db.execute(statement)
        db.execute("PRAGMA user_version = 7")
        db.execute("INSERT INTO keys(id, secret_hash, label, created_at) VALUES ('alpha001', 'x', 'Paul', 0)")
        db.execute("INSERT INTO profiles(character_id, key_id, battlegroup, name, body, updated_at, rev)"
                   " VALUES (1001, 'alpha001', ?, 'Paul Test', '{}', ?, 1)", (BG, NOW - 3600))
    db.close()
    st = Storage.open(path)
    assert st._conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS) > 7
    row = st._conn.execute("SELECT character_id, name, body, updated_at, changed_at, rev FROM profiles").fetchall()
    assert [tuple(r) for r in row] == [(1001, "Paul Test", "{}", NOW - 3600, NOW - 3600, 1)]
    st.close()
