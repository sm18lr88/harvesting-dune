"""Guild sync contract over real TLS: every case of fixtures/sync-contract.json, plus keysctl."""

import http.client
import json
import math
import sqlite3
import ssl
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest
from guildsync import __main__ as entry
from guildsync import keysctl
from guildsync.api import Api
from guildsync.httpd import make_server
from guildsync.problems import OTHER_KEY
from guildsync.storage import Storage

HERE = Path(__file__).parent
CERTS = HERE / "certs"
CONTRACT = json.loads((HERE / "fixtures" / "sync-contract.json").read_text(encoding="utf-8"))
CASES = {case["name"]: case for case in CONTRACT["cases"]}
TLS = ssl.create_default_context(cafile=str(CERTS / "test-ca.pem"))

# logical fixture column -> stored column, per table
SIGHTING_COLS = {"battlegroupId": "battlegroup", "worldId": "world", "layout": "layout", "cls": "cls", "type": "type",
                 "x": "x", "y": "y", "z": "z", "fill": "fill", "size": "size", "rowKey": "row_key", "distCm": "dist_cm",
                 "observedAt": "observed_at", "receivedAt": "received_at", "lag": "lag", "observerKey": "observer_key",
                 "observerName": "observer_name", "observers": "observers", "rev": "rev"}
PROFILE_COLS = {"characterId": "character_id", "keyId": "key_id", "battlegroupId": "battlegroup", "characterName": "name",
                "updatedAt": "updated_at", "rev": "rev"}
PRESENCE_COLS = {"keyId": "key_id", "characterId": "character_id", "characterName": "name", "battlegroupId": "battlegroup",
                 "worldId": "world", "layout": "layout", "x": "x", "y": "y", "z": "z", "receivedAt": "received_at", "lag": "lag",
                 "nextS": "next_s", "inGame": "in_game", "leftAt": "left_at"}


def _insert(db: sqlite3.Connection, table: str, row: dict) -> None:
    db.execute(f"INSERT INTO {table}({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))


def _given_sightings(given: dict) -> list[dict]:
    filler = given.get("fillerSightings", {"count": 0, "template": {}})
    extra = [{**filler["template"], "rev": i, "x": i * 2000.0, "cellX": i} for i in range(1, filler["count"] + 1)]
    return given["rows"]["sightings"] + extra


def _sighting_row(s: dict) -> dict:
    cell = [s["battlegroupId"], s["layout"], s["cls"], s["type"], s["cellX"], s["cellY"]]
    return {"key": json.dumps(cell), **{c: s[f] for f, c in SIGHTING_COLS.items()}, "observers": json.dumps(sorted(s["observers"]))}


def _seed(path: Path, case: dict) -> None:
    """The fixture keys and `given` rows, written straight into the server's SQLite file."""
    given = case["given"]
    with sqlite3.connect(path) as db:
        for k in CONTRACT["keys"]:
            _insert(db, "keys", {"id": k["keyId"], "secret_hash": k["sha256"], "label": k["note"], "created_at": k["createdAt"],
                                 "role": k["role"], "revoked_at": 1.0 if k["revoked"] else None})
        for s in _given_sightings(given):
            _insert(db, "sightings", _sighting_row(s))
        for p in given["rows"]["presence"]:
            _insert(db, "presence", {c: p[f] for f, c in PRESENCE_COLS.items() if f in p})
        for p in given["rows"].get("profiles", []):
            _insert(db, "profiles", {**{c: p[f] for f, c in PROFILE_COLS.items()}, "body": json.dumps(p["profile"], separators=(",", ":"), sort_keys=True)})
        db.execute("UPDATE meta SET v = ? WHERE k = 'rev'", (str(given["rev"]),))
        db.execute("UPDATE meta SET v = ? WHERE k = 'profile_rev'", (str(given.get("profileRev", 0)),))
    db.close()


def _stored(path: Path) -> dict[str, list[dict]]:
    """Every row of the data tables, under the fixture's logical column names."""
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        out = {"sightings": [], "presence": []}
        for r in db.execute("SELECT * FROM sightings"):
            cell = json.loads(r["key"])
            out["sightings"].append({**{f: r[c] for f, c in SIGHTING_COLS.items()}, "cellX": cell[4], "cellY": cell[5],
                                     "observers": sorted(json.loads(r["observers"]))})
        out["presence"] = [{f: r[c] for f, c in PRESENCE_COLS.items()} for r in db.execute("SELECT * FROM presence")]
        out["profiles"] = [{**{f: r[c] for f, c in PROFILE_COLS.items()}, "profile": json.loads(r["body"])}
                           for r in db.execute("SELECT * FROM profiles")]
    db.close()
    return out


def _same(got: object, want: object) -> bool:
    if isinstance(want, float) and isinstance(got, int | float):
        return math.isclose(got, want, abs_tol=1e-6)
    if isinstance(want, dict):
        return isinstance(got, dict) and got.keys() == want.keys() and all(_same(got[k], v) for k, v in want.items())
    if isinstance(want, list):
        return isinstance(got, list) and len(got) == len(want) and all(map(_same, got, want, strict=True))
    return type(got) is type(want) and got == want


def _matches(row: dict, want: dict) -> bool:
    return all(_same(sorted(row[k]) if k == "observers" else row[k], v) for k, v in want.items())


@contextmanager
def running(tmp_path: Path, now: float):
    """The real server (TLS, Api, Storage) on a free port, with its clock frozen at `now`."""
    path = tmp_path / "guildsync.sqlite"
    clock = lambda: now  # noqa: E731
    storage = Storage.open(str(path), clock)
    server = make_server(storage, str(CERTS / "test-server.pem"), str(CERTS / "test-server.key"),
                         "127.0.0.1", 0, Api(storage, clock), now=clock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], path
    finally:
        server.shutdown()
        server.server_close()
        storage.close()


def call(port: int, request: dict) -> tuple[int, dict, dict]:
    """(status, JSON body, headers) of one request through urllib over verified TLS."""
    if "rawBody" in request:
        data = request["rawBody"].encode()
    elif "body" in request:
        data = json.dumps(request["body"]).encode()
    else:
        data = None
    req = urllib.request.Request(f"https://127.0.0.1:{port}{request['path']}", data=data,
                                 headers=request.get("headers", {}), method=request["method"])
    try:
        with urllib.request.urlopen(req, context=TLS, timeout=10) as resp:
            return resp.status, json.loads(resp.read()), dict(resp.headers)
    except urllib.error.HTTPError as err:
        with err:
            return err.code, json.loads(err.read()), dict(err.headers)


def call_oversized(port: int, request: dict) -> tuple[int, dict, dict]:
    """Declares the oversized body and sends none: the server answers 413 before reading it,
    and a body sent anyway would end in a TCP reset on Windows instead of a readable reply."""
    conn = http.client.HTTPSConnection("127.0.0.1", port, context=TLS, timeout=10)
    try:
        conn.putrequest(request["method"], request["path"])
        for name, value in {**request["headers"], "Content-Length": str(request["rawBodyBytes"])}.items():
            conn.putheader(name, value)
        conn.endheaders()
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read()), dict(resp.headers)
    finally:
        conn.close()


@pytest.mark.parametrize("name", list(CASES))
def test_contract_case(tmp_path, name):
    case = CASES[name]
    with running(tmp_path, case["serverNow"]) as (port, path):
        _seed(path, case)
        send = call_oversized if "rawBodyBytes" in case["request"] else call
        for _ in range(case["given"]["priorRequests"]):
            send(port, case["request"])
        status, body, headers = send(port, case["request"])
        rows = _stored(path)

    assert status == case["expectedStatus"], body
    assert sorted(body) == sorted(case["expectedResponseKeys"])
    if "expectedSightings" in case:
        want = case["expectedSightings"]
        revs = [s["rev"] for s in body.pop("sightings")]
        assert (len(revs), revs[0], revs[-1]) == (want["count"], want["firstRev"], want["lastRev"])
    assert _same(body, case["expectedResponse"]), json.dumps(body)
    want_type = "application/problem+json" if status >= 400 else "application/json"
    assert headers["Content-Type"] == want_type
    assert all(name in headers for name in case.get("expectedHeaders", [])), headers
    if "Retry-After" in case.get("expectedHeaders", []):
        assert 1 <= int(headers["Retry-After"]) <= 60
    for table, want_rows in case["expectedRows"].items():
        assert len(rows[table]) == len(want_rows), (table, rows[table])
        for want in want_rows:
            assert any(_matches(row, want) for row in rows[table]), (table, want, rows[table])


POLL = {"since": 0, "sentAt": 1794160000.0,
        "identity": {"characterName": "Paul Test", "characterId": 1001, "battlegroupId": "prd-srv-test-alpha-erg"},
        "partition": {"worldId": "survival_1", "layout": "survival_1"},
        "position": None, "records": []}


def _keysctl(capsys, *argv: str) -> tuple[int, str, str]:
    capsys.readouterr()  # drop the server's audit lines: it prints them before each reply, in this process
    code = keysctl.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_keys_issued_by_keysctl_sync_list_and_stop_on_revoke(tmp_path, capsys):
    with running(tmp_path, 1794160000.0) as (port, path):
        db = ("--db", str(path))
        code, out, _ = _keysctl(capsys, *db, "issue", "--label", "Paul", "--json")
        paul = json.loads(out)
        assert code == 0 and paul["label"] == "Paul" and paul["token"].startswith(f"gs1_{paul['keyId']}_")
        code, chani_token, _ = _keysctl(capsys, *db, "issue", "--label", "Chani")
        chani_id = chani_token.split("_")[1]

        chani_poll = {**POLL, "identity": {**POLL["identity"], "characterName": "Chani Test", "characterId": 2002}}

        def sync(token: str) -> int:  # each key plays its own character: a character belongs to one key
            body = chani_poll if token.strip() != paul["token"] else POLL
            headers = {"Authorization": f"Bearer {token.strip()}", "Content-Type": "application/json"}
            return call(port, {"method": "POST", "path": "/v1/sync", "headers": headers, "body": body})[0]

        assert sync(paul["token"]) == 200 and sync(chani_token) == 200
        code, out, _ = _keysctl(capsys, *db, "list", "--json")
        listed = {k["keyId"]: k for k in json.loads(out)}
        assert code == 0 and listed[paul["keyId"]]["characters"] == ["Paul Test@prd-srv-test-alpha-erg"]
        assert listed[paul["keyId"]]["lastSeen"] == 1794160000.0

        assert _keysctl(capsys, *db, "revoke", chani_id)[0] == 0
        assert sync(chani_token) == 401  # the very next request
        assert sync(paul["token"]) == 200
        code, _, err = _keysctl(capsys, *db, "revoke", chani_id)
        assert code == 0 and "already" in err
        assert _keysctl(capsys, *db, "revoke", "zzzz9999")[0] == 2
        code, out, _ = _keysctl(capsys, *db, "list")
        assert code == 0 and "Paul Test@prd-srv-test-alpha-erg" in out and paul["token"] not in out
    assert _keysctl(capsys, "--db", str(tmp_path / "missing.sqlite"), "list")[0] == 3


def test_plain_http_is_refused_off_loopback(capsys):
    with pytest.raises(SystemExit) as stop:
        entry.parse_args(["--no-tls", "--host", "0.0.0.0"])
    assert stop.value.code == 2 and "--no-tls" in capsys.readouterr().err
    assert entry.parse_args(["--no-tls", "--host", "127.0.0.1"]).no_tls


def test_a_character_belongs_to_the_first_key_that_synced_it(tmp_path):
    import copy
    import json
    from pathlib import Path

    from guildsync.api import Api
    from guildsync.storage import Storage

    cases = json.loads((Path(__file__).parent / "fixtures" / "sync-contract.json").read_text("utf8"))["cases"]
    sync_body = next(c for c in cases if c["request"]["path"] == "/v1/sync" and c["expectedStatus"] == 200)["request"]["body"]
    profile_body = next(c for c in cases if c["request"]["path"] == "/v1/profile" and c["expectedStatus"] == 200
                        and c["request"]["body"]["profile"])["request"]["body"]
    st = Storage.open(str(tmp_path / "g.db"))
    owner, other = {"id": st.issue_key("Paul")[0]}, {"id": st.issue_key("Feyd")[0]}
    api, now = Api(st, lambda: 0.0), sync_body["sentAt"]
    me = copy.deepcopy(profile_body)
    me["identity"] = sync_body["identity"]
    assert api.sync(owner, sync_body, "203.0.113.7", now)[0] == 200
    assert api.profile(owner, me, "203.0.113.7", now)[0] == 200
    refused = (403, OTHER_KEY)
    assert api.sync(other, sync_body, "203.0.113.8", now) == refused  # no stealing the roster row
    assert api.profile(other, {**me, "profile": None}, "203.0.113.8", now) == refused  # nor deleting the profile
    assert st.profiles_since(sync_body["identity"]["battlegroupId"], 0)[1] == [sync_body["identity"]["characterId"]]
    st.revoke_key(owner["id"], now)  # a revoked key lets go of its characters
    assert api.sync(other, sync_body, "203.0.113.8", now)[0] == 200
    st.close()


def test_a_profile_uploaded_before_any_sync_already_claims_its_character(tmp_path):
    profile_case = next(c for c in CONTRACT["cases"] if c["name"] == "profile-upload")["request"]["body"]
    sync_body = {**POLL, "identity": profile_case["identity"]}
    st = Storage.open(str(tmp_path / "g.db"))
    owner, other = {"id": st.issue_key("Paul")[0]}, {"id": st.issue_key("Feyd")[0]}
    api, now = Api(st, lambda: 0.0), POLL["sentAt"]
    assert api.profile(owner, profile_case, "203.0.113.7", now)[0] == 200  # no sync yet: no roster row
    assert api.profile(other, {**profile_case, "profile": None}, "203.0.113.8", now) == (403, OTHER_KEY)
    assert api.profile(other, profile_case, "203.0.113.8", now) == (403, OTHER_KEY)
    assert api.sync(other, sync_body, "203.0.113.8", now) == (403, OTHER_KEY)
    assert st.upsert_character(other["id"], sync_body["identity"], now) is False  # the check sits in the write
    assert api.sync(owner, sync_body, "203.0.113.7", now)[0] == 200
    st.close()
