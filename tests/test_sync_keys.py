"""Key roles, the regular-key cap, 30-day idle expiry and renew: storage, keysctl and the 401 over TLS."""

import json
import sqlite3

import pytest
from guildsync import keysctl
from guildsync.keystore import KEY_IDLE_S, MAX_ACTIVE_REGULAR, KeyCapFull
from guildsync.storage import MIGRATIONS, Storage
from test_sync_server import POLL, call, running

T0 = 1794160000.0
DAY = 86400.0


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def store(tmp_path):
    clock = Clock()
    st = Storage.open(str(tmp_path / "guild.db"), clock)
    yield st, clock
    st.close()


def fill(st: Storage, n: int) -> list[tuple[str, str]]:
    return [st.issue_key(f"Fremen {i}") for i in range(n)]


def test_migration_5_adds_roles_and_makes_the_owner_key_admin(tmp_path):
    path = str(tmp_path / "guild.db")
    with sqlite3.connect(path) as db:
        for statements in MIGRATIONS[:4]:
            for statement in statements:
                db.execute(statement)
        db.execute("PRAGMA user_version = 4")
        for key_id in ("7cc09c11", "alpha001"):
            db.execute("INSERT INTO keys(id, secret_hash, label, created_at) VALUES (?, 'x', 'Paul Test', 0)", (key_id,))
    db.close()
    st = Storage.open(path, Clock())
    roles = dict(st._conn.execute("SELECT id, role FROM keys").fetchall())
    assert roles == {"7cc09c11": "admin", "alpha001": "regular"}
    assert st._conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    st.close()


def test_exactly_64_regular_keys_fit_and_the_65th_is_refused(store):
    st, _ = store
    fill(st, MAX_ACTIVE_REGULAR - 1)
    st.issue_key("Number 64")  # the 64th still fits
    assert st.active_regular(T0) == MAX_ACTIVE_REGULAR
    with pytest.raises(KeyCapFull) as full:
        st.issue_key("Number 65")
    assert full.value.active == MAX_ACTIVE_REGULAR
    assert st._conn.execute("SELECT COUNT(*) FROM keys").fetchone()[0] == MAX_ACTIVE_REGULAR  # nothing written


def test_admin_keys_are_outside_the_cap(store):
    st, _ = store
    fill(st, MAX_ACTIVE_REGULAR)
    key_id, _ = st.issue_key("Guild admin", role="admin")
    assert st.active_regular(T0) == MAX_ACTIVE_REGULAR
    assert {k["id"]: k["role"] for k in st.list_keys()}[key_id] == "admin"


def test_revoked_and_expired_keys_free_a_place(store):
    st, clock = store
    keys = fill(st, MAX_ACTIVE_REGULAR)
    st.revoke_key(keys[0][0], T0)
    st.issue_key("After revoke")
    clock.t = T0 + KEY_IDLE_S + 1  # all 64 went unused for over 30 days
    assert st.active_regular(clock.t) == 0
    st.issue_key("After expiry")


def test_a_never_used_key_expires_30_days_after_creation(store):
    st, clock = store
    _, token = st.issue_key("Paul Test")
    for at, state in ((KEY_IDLE_S - 1, "active"), (KEY_IDLE_S, "active"), (KEY_IDLE_S + 1, "expired")):
        clock.t = T0 + at
        assert st.lookup_key(token)["state"] == state, at


def test_each_use_restarts_the_30_days(store):
    st, clock = store
    key_id, token = st.issue_key("Paul Test")
    st.touch_key(key_id, T0 + 20 * DAY, "203.0.113.7")
    clock.t = T0 + 20 * DAY + KEY_IDLE_S - 1
    assert st.lookup_key(token)["state"] == "active"
    clock.t = T0 + 20 * DAY + KEY_IDLE_S + 1
    assert st.lookup_key(token)["state"] == "expired"


def test_admin_keys_never_expire(store):
    st, clock = store
    _, token = st.issue_key("Guild admin", role="admin")
    clock.t = T0 + 365 * DAY
    assert st.lookup_key(token)["state"] == "active"
    assert st.sweep(clock.t)["keys"] == 0


def test_the_sweep_records_expiry_once(store):
    st, clock = store
    old, _ = st.issue_key("Paul Test")
    clock.t = T0 + 10 * DAY
    young, _ = st.issue_key("Chani Test")
    assert st.sweep(T0 + KEY_IDLE_S)["keys"] == 0
    assert st.sweep(T0 + KEY_IDLE_S + 1)["keys"] == 1
    assert st.sweep(T0 + KEY_IDLE_S + 2)["keys"] == 0
    expired = {k["id"]: k["expired_at"] for k in st.list_keys()}
    assert expired == {old: T0 + KEY_IDLE_S + 1, young: None}


def test_renew_brings_an_expired_key_back_for_30_days(store):
    st, clock = store
    key_id, token = st.issue_key("Paul Test")
    clock.t = T0 + 40 * DAY
    st.sweep(clock.t)
    assert st.renew_key(key_id, clock.t) == "renewed"
    assert st.lookup_key(token)["state"] == "active"
    assert st.renew_key(key_id, clock.t) == "active"
    clock.t += KEY_IDLE_S + 1
    assert st.lookup_key(token)["state"] == "expired"


def test_renew_counts_against_the_cap_and_refuses_revoked_and_unknown_keys(store):
    st, clock = store
    old, _ = st.issue_key("Paul Test")
    clock.t = T0 + KEY_IDLE_S + 1
    fill(st, MAX_ACTIVE_REGULAR)
    with pytest.raises(KeyCapFull):
        st.renew_key(old, clock.t)
    gone, _ = st.issue_key("Feyd Test", role="admin")
    st.revoke_key(gone, clock.t)
    assert st.renew_key(gone, clock.t) == "revoked"
    assert st.renew_key("zzzz9999", clock.t) == "unknown"


def ctl(capsys, clock, *argv: str) -> tuple[int, str, str]:
    capsys.readouterr()
    code = keysctl.main(list(argv), clock=clock)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_keysctl_cap_admin_list_and_renew(tmp_path, capsys):
    db, clock = str(tmp_path / "guild.db"), Clock()
    Storage.open(db).close()
    st = Storage.open(db, clock)
    old, _ = st.issue_key("Paul Test")
    clock.t = T0 + KEY_IDLE_S + 1
    fill(st, MAX_ACTIVE_REGULAR - 1)
    st.close()
    code, out, _ = ctl(capsys, clock, "--db", db, "issue", "--label", "Number 64", "--json")
    assert code == 0 and json.loads(out)["role"] == "regular"
    code, out, err = ctl(capsys, clock, "--db", db, "issue", "--label", "Number 65", "--json")
    assert code == keysctl.CAP_FULL == 4 and json.loads(out) == {"error": "key cap reached", "active": 64, "cap": 64}
    assert "64/64" in err
    code, out, _ = ctl(capsys, clock, "--db", db, "issue", "--label", "Guild admin", "--admin", "--json")
    assert code == 0 and json.loads(out)["role"] == "admin"
    code, out, _ = ctl(capsys, clock, "--db", db, "list")
    assert code == 0 and out.rstrip().endswith("active regular keys: 64/64")
    assert " expired " in out and " admin " in out
    code, out, err = ctl(capsys, clock, "--db", db, "list", "--json")
    states = {k["keyId"]: (k["role"], k["state"]) for k in json.loads(out)}
    assert states[old] == ("regular", "expired") and "64/64" in err
    assert ctl(capsys, clock, "--db", db, "renew", old)[0] == 4
    assert ctl(capsys, clock, "--db", db, "revoke", next(k for k in states if k != old and states[k][0] == "regular"))[0] == 0
    code, out, _ = ctl(capsys, clock, "--db", db, "renew", old)
    assert code == 0 and out == f"renewed {old}\n"
    assert ctl(capsys, clock, "--db", db, "renew", old)[0] == 0  # already active
    assert ctl(capsys, clock, "--db", db, "renew", "zzzz9999")[0] == 2
    assert ctl(capsys, clock, "--db", str(tmp_path / "missing.db"), "renew", old)[0] == 3


def _sync(port: int, token: str, character_id: int = 1001) -> tuple[int, dict, dict]:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {**POLL, "identity": {**POLL["identity"], "characterId": character_id}}
    return call(port, {"method": "POST", "path": "/v1/sync", "headers": headers, "body": body})


def test_over_tls_an_idle_key_gets_key_expired_and_renew_restores_it(tmp_path):
    now = T0 + KEY_IDLE_S + 1
    with running(tmp_path, now) as (port, path):
        st = Storage.open(str(path), Clock(T0))
        key_id, token = st.issue_key("Paul Test")
        _, admin = st.issue_key("Guild admin", role="admin")
        status, body, headers = _sync(port, token)
        assert (status, body["type"].rsplit("#", 1)[1]) == (401, "key-expired")
        assert headers["WWW-Authenticate"] == 'Bearer realm="guildsync"'
        assert _sync(port, admin, 1002)[0] == 200  # as idle, but an admin key
        assert st.renew_key(key_id, now) == "renewed"
        assert _sync(port, token)[0] == 200
        st.close()
