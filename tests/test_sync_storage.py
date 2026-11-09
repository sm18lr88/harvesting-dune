import hashlib
import sqlite3
import threading

from guildsync.storage import MIGRATIONS, Storage

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
    assert st.presence_for(BG, NOW) == []
    assert st.sweep(NOW) == {"presence": 1, "sightings": 2, "audit": 0, "profiles": 0, "keys": 0}
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
