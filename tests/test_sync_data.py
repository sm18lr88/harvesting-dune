"""Key-gated datasets over real TLS: GET/HEAD /v1/data[/<name>], ETag and 304, gzip, keys, names, limits, audit."""

import gzip
import hashlib
import http.client
import json
import os
import ssl
import threading
from pathlib import Path

import pytest
from guildsync import __main__ as entry
from guildsync import datasets
from guildsync.api import Api
from guildsync.datasets import Datasets
from guildsync.httpd import make_server
from guildsync.keystore import KEY_IDLE_S
from guildsync.storage import Storage

CERTS = Path(__file__).parent / "certs"
TLS = ssl.create_default_context(cafile=str(CERTS / "test-ca.pem"))
T0 = 1794160000.0
ROUTES = {"chests": [{"site": "Paul Test's cave", "x": float(i), "y": -float(i)} for i in range(60)]}  # > 1 KiB
SMALL = {"tables": ["ore"]}


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def box(tmp_path):
    """(port, storage, clock, folder, token) of a running TLS server over a real database and dataset folder."""
    clock, folder = Clock(), tmp_path / "datasets"
    folder.mkdir()
    (folder / "routes.json").write_text(json.dumps(ROUTES), encoding="utf-8")
    (folder / "small.json").write_text(json.dumps(SMALL), encoding="utf-8")
    storage = Storage.open(str(tmp_path / "guild.db"), clock)
    _, token = storage.issue_key("Paul Test")
    server = make_server(storage, str(CERTS / "test-server.pem"), str(CERTS / "test-server.key"), "127.0.0.1", 0,
                         Api(storage, clock), now=clock, data=Datasets(folder))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], storage, clock, folder, token
    server.shutdown()
    server.server_close()
    storage.close()


def get(port: int, path: str, token: str | None = None, method: str = "GET", **headers: str):
    """(status, headers, raw body bytes); the path goes out byte for byte, never normalised."""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    conn = http.client.HTTPSConnection("127.0.0.1", port, context=TLS, timeout=10)
    try:
        conn.request(method, path, headers={k.replace("_", "-"): v for k, v in headers.items()})
        resp = conn.getresponse()
        return resp.status, resp.headers, resp.read()
    finally:
        conn.close()


def etag_of(value: object) -> str:
    return f'"{hashlib.sha256(json.dumps(value).encode()).hexdigest()}"'


def test_a_dataset_is_served_with_a_strong_etag_and_private_caching(box):
    port, _, _, _, token = box
    status, headers, raw = get(port, "/v1/data/small", token)
    assert (status, json.loads(raw)) == (200, SMALL)
    assert headers["ETag"] == etag_of(SMALL) and not headers["ETag"].startswith("W/")
    assert (headers["Content-Type"], headers["Cache-Control"], headers["Vary"]) == ("application/json", "private", "Accept-Encoding")
    assert "Content-Encoding" not in headers  # under 1 KiB, even though the client may not ask; see the gzip test


def test_if_none_match_gets_304_with_no_body_and_a_changed_file_gets_200(box):
    port, _, _, folder, token = box
    tag = get(port, "/v1/data/routes", token)[1]["ETag"]
    for sent in (tag, f'"other", {tag}', f"W/{tag}", "*"):
        status, headers, raw = get(port, "/v1/data/routes", token, If_None_Match=sent)
        assert (status, raw, headers["ETag"]) == (304, b"", tag), sent
        assert "Content-Type" not in headers and "Content-Length" not in headers
    assert get(port, "/v1/data/routes", token, If_None_Match='"stale"')[0] == 200
    (folder / "routes.json").write_text(json.dumps({**ROUTES, "v": 2}), encoding="utf-8")
    status, headers, raw = get(port, "/v1/data/routes", token, If_None_Match=tag)
    assert (status, json.loads(raw)["v"], headers["ETag"]) == (200, 2, etag_of({**ROUTES, "v": 2}))


def test_gzip_only_when_accepted_and_over_1_kib(box):
    port, _, _, _, token = box
    status, headers, raw = get(port, "/v1/data/routes", token, Accept_Encoding="gzip")
    assert (status, headers["Content-Encoding"], json.loads(gzip.decompress(raw))) == (200, "gzip", ROUTES)
    assert headers["ETag"] == etag_of(ROUTES) and int(headers["Content-Length"]) == len(raw) < len(json.dumps(ROUTES))
    status, headers, raw = get(port, "/v1/data/routes", token)
    assert (status, json.loads(raw)) == (200, ROUTES) and "Content-Encoding" not in headers
    assert "Content-Encoding" not in get(port, "/v1/data/small", token, Accept_Encoding="gzip")[1]


def test_head_sends_the_headers_without_the_body(box):
    port, _, _, _, token = box
    status, headers, raw = get(port, "/v1/data/routes", token, method="HEAD")
    assert (status, raw, headers["ETag"]) == (200, b"", etag_of(ROUTES))
    assert int(headers["Content-Length"]) == len(json.dumps(ROUTES))


def test_the_list_names_every_served_dataset_for_the_daily_check(box):
    port, _, _, folder, token = box
    (folder / "broken.json").write_text("{not json", encoding="utf-8")
    (folder / "Upper.json").write_text("{}", encoding="utf-8")
    status, headers, raw = get(port, "/v1/data", token)
    assert (status, headers["Content-Type"]) == (200, "application/json")
    assert json.loads(raw) == {"datasets": [
        {"name": "routes", "etag": etag_of(ROUTES), "bytes": len(json.dumps(ROUTES))},
        {"name": "small", "etag": etag_of(SMALL), "bytes": len(json.dumps(SMALL))}], "next": None}


def test_the_list_pages_by_name(box):
    port, _, _, folder, token = box
    (folder / "ore.json").write_text("[]", encoding="utf-8")
    (folder / "broken.json").write_text("{not json", encoding="utf-8")  # skipped, never a page's last name

    def page(query: str) -> tuple[list[str], str | None]:
        status, _, raw = get(port, "/v1/data?" + query, token)
        body = json.loads(raw)
        assert status == 200, body
        return [d["name"] for d in body["datasets"]], body["next"]

    assert page("limit=2") == (["ore", "routes"], "routes")
    assert page("limit=2&after=routes") == (["small"], None)
    assert page("limit=3") == (["ore", "routes", "small"], None)
    assert page("after=") == (["ore", "routes", "small"], None)
    for query, field in (("limit=0", "limit"), ("limit=101", "limit"), ("limit=x", "limit"), ("after=..", "after"),
                         ("limit=1&limit=2", "query"), ("limit=1&after=a&after=b", "query")):
        status, _, raw = get(port, "/v1/data?" + query, token)
        assert (status, json.loads(raw)["field"]) == (400, field), query
    assert get(port, "/v1/data?access_token=" + token, token)[0] == 404  # a key in a URL stays a 404


@pytest.mark.parametrize("path", ["/v1/data/nope", "/v1/data/", "/v1/data/../guild", "/v1/data/%2e%2e/guild",
                                  "/v1/data/%2e%2e%2fguild", "/v1/data/..\\guild", "/v1/data/small.json",
                                  "/v1/data/SMALL", "/v1/data/small?x=1", "/v1/data/" + "a" * 33, "/v1/data/broken"])
def test_unknown_or_bad_names_are_404_problems(box, path):
    port, _, _, folder, token = box
    (folder.parent / "guild.json").write_text("{}", encoding="utf-8")  # what a traversal would reach
    (folder / "broken.json").write_text("{not json", encoding="utf-8")
    status, headers, raw = get(port, path, token)
    assert (status, headers["Content-Type"], json.loads(raw)["status"]) == (404, "application/problem+json", 404)


def test_an_oversized_file_is_not_served_or_listed(box, monkeypatch):
    port, _, _, folder, token = box
    monkeypatch.setattr(datasets, "MAX_BYTES", 64)
    (folder / "big.json").write_text(json.dumps({"x": "y" * 60}), encoding="utf-8")  # 69 bytes
    (folder / "edge.json").write_text(json.dumps({"x": "y" * 55}), encoding="utf-8")  # exactly 64 bytes
    assert get(port, "/v1/data/big", token)[0] == 404
    assert get(port, "/v1/data/edge", token)[0] == 200
    assert [d["name"] for d in json.loads(get(port, "/v1/data", token)[2])["datasets"]] == ["edge", "small"]


def test_the_real_limit_is_4_mib():
    assert datasets.MAX_BYTES == 4 * 1024 * 1024


def test_missing_bad_revoked_and_expired_keys_get_401_before_any_name_check(box):
    port, storage, clock, _, token = box
    key_id = token.split("_")[1]
    revoked_id, revoked = storage.issue_key("Chani Test")
    storage.revoke_key(revoked_id, T0)
    for sent, kind in ((None, "#invalid-key"), ("gs1_zzzz9999_" + "x" * 32, "#invalid-key"), (revoked, "#invalid-key")):
        status, headers, raw = get(port, "/v1/data/nope", sent)
        assert (status, json.loads(raw)["type"].endswith(kind)) == (401, True)
        assert headers["WWW-Authenticate"] == 'Bearer realm="guildsync"'
    clock.t = T0 + KEY_IDLE_S + 1
    status, _, raw = get(port, "/v1/data", token)
    assert (status, json.loads(raw)["type"].endswith("#key-expired")) == (401, True)
    assert storage.renew_key(key_id, clock.t) == "renewed" and get(port, "/v1/data", token)[0] == 200


def test_the_key_bucket_is_shared_with_sync_and_every_call_is_audited(box):
    port, storage, _, _, token = box
    statuses = [get(port, "/v1/data/small", token, method="HEAD")[0] for _ in range(30)]
    status, headers, raw = get(port, "/v1/data/small", token)
    assert statuses == [200] * 30 and (status, json.loads(raw)["type"].endswith("#rate-limited")) == (429, True)
    rows = storage._conn.execute("SELECT key_id, verb, status FROM audit ORDER BY rowid").fetchall()
    key_id = token.split("_")[1]
    assert [tuple(r) for r in rows] == [(key_id, "HEAD /v1/data/small", 200)] * 30 + [(key_id, "GET /v1/data/small", 429)]


def test_other_methods_are_405_with_allow(box):
    port, _, _, _, token = box
    for path in ("/v1/data", "/v1/data/small"):
        status, headers, raw = get(port, path, token, method="POST")
        assert (status, headers["Allow"], json.loads(raw)["status"]) == (405, "GET, HEAD", 405)


def test_an_unchanged_file_is_read_once(tmp_path, monkeypatch):
    (tmp_path / "small.json").write_text(json.dumps(SMALL), encoding="utf-8")
    reads = []
    real = datasets._load
    monkeypatch.setattr(datasets, "_load", lambda path: reads.append(path.name) or real(path))
    data = Datasets(tmp_path)
    assert [data.get("small").etag for _ in range(3)] == [etag_of(SMALL)] * 3 and reads == ["small.json"]
    st = (tmp_path / "small.json").stat()
    os.utime(tmp_path / "small.json", ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert data.get("small") is not None and reads == ["small.json"] * 2
    assert Datasets(None).get("small") is None and Datasets(None).listing() == ([], None)


def test_the_folder_defaults_to_datasets_next_to_the_database():
    args = entry.parse_args(["--db", "/data/guild.sqlite", "--no-tls", "--host", "127.0.0.1"])
    assert entry.data_folder(args) == Path("/data/datasets")
    args = entry.parse_args(["--db", "/data/guild.sqlite", "--datasets", "/srv/sets", "--no-tls", "--host", "127.0.0.1"])
    assert entry.data_folder(args) == Path("/srv/sets")
