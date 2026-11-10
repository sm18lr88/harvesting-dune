"""Guild sync HTTPS front over real TLS: trust anchor, bearer keys, limits, errors, audit lines."""

import email.message
import gzip
import json
import socket
import ssl
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from guildsync.httpd import Handler, make_server

CERTS = Path(__file__).parent / "certs"
TOKEN = "gs1_alpha001_Qm9vdHN0cmFwVGVzdEtleUFscGhhMDAx"
BAD_TOKEN = "gs1_zzzz9999_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
IDLE_TOKEN = "gs1_idle0005_SWRsZVRlc3RLZXlTZWNyZXQwMDA1MDA1"
NOW = 1794160000.0
POLL = {"since": 0, "records": [{"cls": "spice"}]}


class FakeStorage:
    def __init__(self) -> None:
        self.audits: list[tuple] = []
        self.touched: list[tuple] = []

    def lookup_key(self, token: str) -> dict | None:
        if token == IDLE_TOKEN:
            return {"id": "idle0005", "state": "expired"}
        return {"id": "alpha001"} if token == TOKEN else None

    def audit(self, *row) -> None:
        self.audits.append(row)

    def touch_key(self, *use) -> None:
        self.touched.append(use)


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.bodies: list[dict] = []
        self.sightings: list[dict] = []
        self.fail = False

    def health(self) -> tuple[int, dict]:
        self.calls.append("health")
        return 200, {"ok": True, "now": NOW, "version": "1"}

    def sync(self, key: dict, body: dict, ip: str, now: float) -> tuple[int, dict]:
        self.calls.append("sync")
        self.bodies.append(body)
        if self.fail:
            self.fail = False
            raise RuntimeError("boom")
        return 200, {"now": now, "next": 0, "keyState": "valid", "presence": [], "sightings": self.sightings}


@pytest.fixture
def front():
    api, storage = FakeApi(), FakeStorage()
    server = make_server(storage, str(CERTS / "test-server.pem"), str(CERTS / "test-server.key"),
                         "127.0.0.1", 0, api, now=lambda: NOW)  # a frozen clock: buckets never refill
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1], api, storage
    server.shutdown()
    server.server_close()


def call(port: int, path: str, *, body: dict | None = None, token: str | None = None, ca: str = "test-ca.pem",
         method: str | None = None):
    """(status, headers, json) for one request; urllib raises HTTPError for non-2xx, which carries the same."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"https://127.0.0.1:{port}{path}", data=data, headers=headers, method=method)
    ctx = ssl.create_default_context(cafile=str(CERTS / ca))
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            return resp.status, resp.headers, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        with err:
            return err.code, err.headers, json.loads(err.read())


def test_health_over_verified_tls(front):
    port, api, storage = front
    status, headers, body = call(port, "/health")
    assert (status, body) == (200, {"ok": True, "now": NOW, "version": "1"})
    assert headers["Content-Type"] == "application/json" and headers["Cache-Control"] == "no-store"
    assert api.calls == ["health"] and storage.audits[0][3:] == ("GET /health", 200, 0, None)


def test_other_ca_fails_the_handshake_before_any_request(front):
    port, api, _ = front
    with pytest.raises(urllib.error.URLError) as caught:
        call(port, "/health", ca="other-ca.pem")
    assert isinstance(caught.value.reason, ssl.SSLCertVerificationError)
    assert api.calls == []
    assert call(port, "/health")[0] == 200  # the refused handshake did not wedge the server


def test_a_silent_client_does_not_stall_the_accept_loop(front):
    port, _, _ = front
    with socket.create_connection(("127.0.0.1", port), timeout=10):  # connects, never says hello
        assert call(port, "/health")[0] == 200


def test_missing_and_bad_keys_get_401_then_the_ip_bucket_runs_dry(front):
    port, api, _ = front
    status, headers, body = call(port, "/v1/sync", body=POLL)
    assert (status, body["status"], body["error"]) == (401, 401, "invalid key") and body["type"].endswith("#invalid-key")
    assert headers["WWW-Authenticate"] == 'Bearer realm="guildsync"'
    assert headers["Content-Type"] == "application/problem+json"
    statuses = [call(port, "/v1/sync", body=POLL, token=BAD_TOKEN) for _ in range(10)]
    assert [s for s, _, _ in statuses[:9]] == [401] * 9
    status, headers, body = statuses[9]  # the 11th unauthenticated request from this IP
    assert (status, body["status"], body["error"]) == (429, 429, "rate limited") and int(headers["Retry-After"]) >= 1
    assert body["type"].endswith("#rate-limited")
    assert api.calls == []


def test_an_expired_key_gets_key_expired_and_spends_the_ip_bucket(front):
    port, api, storage = front
    status, headers, body = call(port, "/v1/sync", body=POLL, token=IDLE_TOKEN)
    assert (status, body["status"], body["error"]) == (401, 401, "invalid key") and body["type"].endswith("#key-expired")
    assert headers["WWW-Authenticate"] == 'Bearer realm="guildsync"'
    assert storage.audits[-1][1] is None  # an expired key is not a caller: no key id in the audit
    statuses = [call(port, "/v1/sync", body=POLL, token=IDLE_TOKEN)[0] for _ in range(10)]
    assert statuses == [401] * 9 + [429] and api.calls == []


def test_key_bucket_allows_30_polls_a_minute(front):
    port, api, _ = front
    statuses = [call(port, "/v1/sync", body=POLL, token=TOKEN)[0] for _ in range(30)]
    status, headers, body = call(port, "/v1/sync", body=POLL, token=TOKEN)
    assert statuses == [200] * 30 and (status, body["status"], body["error"]) == (429, 429, "rate limited")
    assert 1 <= int(headers["Retry-After"]) <= 60 and api.calls == ["sync"] * 30


def test_oversized_body_is_refused_unread_and_the_socket_closes_cleanly(front):
    port, api, _ = front
    ctx = ssl.create_default_context(cafile=str(CERTS / "test-ca.pem"))
    head = (f"POST /v1/sync HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {TOKEN}\r\n"
            f"Content-Length: {300 * 1024}\r\n\r\n")
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw, \
            ctx.wrap_socket(raw, server_hostname="127.0.0.1") as tls:
        tls.sendall(head.encode())  # no body: the answer must come from the declared size alone
        chunks = []
        while chunk := tls.recv(4096):
            chunks.append(chunk)
    reply = b"".join(chunks)
    head, _, body = reply.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.0 413 ") and b"Content-Type: application/problem+json" in head
    assert json.loads(body)["status"] == 413
    assert api.calls == []


def test_a_body_that_stalls_is_408_not_500(front, monkeypatch):
    port, api, storage = front
    monkeypatch.setattr(Handler, "timeout", 0.5)  # the stall is the behaviour: wait less than the real 10 s
    ctx = ssl.create_default_context(cafile=str(CERTS / "test-ca.pem"))
    head = (f"POST /v1/sync HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {TOKEN}\r\n"
            "Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{\"")
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw, \
            ctx.wrap_socket(raw, server_hostname="127.0.0.1") as tls:
        tls.sendall(head.encode())  # 2 of the 100 bytes, then silence
        chunks = []
        while chunk := tls.recv(4096):
            chunks.append(chunk)
    head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.0 408 ") and json.loads(body)["status"] == 408
    assert api.calls == [] and storage.audits[-1][4] == 408


def test_bad_json_is_400_and_an_api_crash_is_500_then_service_goes_on(front):
    port, api, _ = front
    status, _, raw = raw_post(port, b"{not json", {})
    assert (status, json.loads(raw)["field"], json.loads(raw)["status"]) == (400, "body", 400)
    api.fail = True
    status, headers, body = call(port, "/v1/sync", body=POLL, token=TOKEN)
    assert (status, body["status"], body["type"], body["error"]) == (500, 500, "about:blank", "server")
    assert set(body) == {"type", "title", "status", "error", "requestId"} and "boom" not in json.dumps(body)
    assert call(port, "/v1/sync", body=POLL, token=TOKEN)[0] == 200


def test_json_nested_past_the_parser_stack_is_400_not_500(front):
    port, api, _ = front
    status, headers, raw = raw_post(port, b"[" * 100_000 + b"]" * 100_000, {})
    assert (status, json.loads(raw)["field"], headers["Content-Type"]) == (400, "body", "application/problem+json")
    assert api.calls == []


def test_a_body_that_is_not_json_by_its_content_type_is_415(front):
    port, api, _ = front
    status, _, raw = raw_post(port, json.dumps(POLL).encode(), {"Content-Type": "text/plain"})
    assert (status, json.loads(raw)["status"]) == (415, 415) and api.calls == []
    assert raw_post(port, json.dumps(POLL).encode(), {"Content-Type": "application/json; charset=utf-8"})[0] == 200


def test_other_methods_on_a_route_are_405_with_allow(front):
    port, api, _ = front
    for method, path, allow in (("GET", "/v1/sync", "POST"), ("PUT", "/v1/profile", "POST"),
                                ("DELETE", "/health", "GET, HEAD"), ("POST", "/health", "GET, HEAD"),
                                ("QUERY", "/v1/profiles", "POST")):
        status, headers, body = call(port, path, method=method, token=TOKEN)
        assert (status, headers["Allow"], body["status"]) == (405, allow, 405), (method, path)
    assert call(port, "/nope", method="PUT")[0] == 404
    assert api.calls == []


def test_other_paths_are_404(front):
    port, api, _ = front
    status, headers, body = call(port, "/nope")
    assert (status, body["status"], body["error"], headers["Content-Type"]) == (404, 404, "not found", "application/problem+json")
    assert call(port, "/v1/other", body=POLL, token=TOKEN)[0] == 404
    assert api.calls == []


def test_stdout_audit_line_never_carries_the_token(front, capsys):
    port, _, storage = front
    call(port, "/v1/sync", body=POLL, token=TOKEN)
    call(port, "/v1/sync", body=POLL, token=BAD_TOKEN)
    call(port, f"/v1/sync?access_token={TOKEN}", body=POLL)  # a key in the query is refused and never logged
    out = capsys.readouterr().out
    assert TOKEN not in out and BAD_TOKEN not in out and "Qm9vdHN0" not in out
    lines = [r for r in map(json.loads, out.splitlines()) if "status" in r]  # skip late handshake events
    assert [(r["key"], r["path"], r["status"], r["n"]) for r in lines] == [
        ("alpha001", "/v1/sync", 200, 1), (None, "/v1/sync", 401, 0), (None, "/v1/sync", 404, 0)]
    assert all(set(r) == {"ts", "ip", "key", "path", "status", "n", "ms", "client"} for r in lines)
    assert [a[1:5] for a in storage.audits] == [
        ("alpha001", "127.0.0.1", "POST /v1/sync", 200), (None, "127.0.0.1", "POST /v1/sync", 401),
        (None, "127.0.0.1", "POST /v1/sync", 404)]


def test_the_client_version_reaches_both_audit_records(front, capsys):
    port, _, storage = front
    raw_post(port, json.dumps(POLL).encode(), {"X-Client-Version": "0.9.0"})
    [line] = [r for r in map(json.loads, capsys.readouterr().out.splitlines()) if "status" in r]
    assert line["client"] == "0.9.0" and storage.audits[-1][-1] == "0.9.0"


def raw_post(port: int, data: bytes, headers: dict[str, str]):
    """(status, headers, body bytes) of one POST /v1/sync with these exact body bytes."""
    req = urllib.request.Request(f"https://127.0.0.1:{port}/v1/sync", data=data,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json", **headers})
    ctx = ssl.create_default_context(cafile=str(CERTS / "test-ca.pem"))
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as err:
        with err:
            return err.code, err.headers, err.read()


def test_gzip_both_ways_only_when_it_pays(front):
    port, api, _ = front
    api.sightings = [{"cls": "ore", "type": "magnetiteore", "x": float(i)} for i in range(40)]
    body = {"since": 0, "records": [{"cls": "ore", "x": float(i)} for i in range(60)]}
    status, headers, raw = raw_post(port, gzip.compress(json.dumps(body).encode()),
                                    {"Content-Encoding": "gzip", "Accept-Encoding": "gzip"})
    assert status == 200 and api.bodies == [body]
    assert headers["Content-Encoding"] == "gzip" and len(json.loads(gzip.decompress(raw))["sightings"]) == 40
    api.sightings = []
    status, headers, raw = raw_post(port, json.dumps(POLL).encode(), {"Accept-Encoding": "gzip"})
    assert status == 200 and "Content-Encoding" not in headers and json.loads(raw)["sightings"] == []  # under 1 KiB


def test_gzip_with_q_0_is_not_acceptable(front):
    port, api, _ = front
    api.sightings = [{"cls": "ore", "type": "magnetiteore", "x": float(i)} for i in range(40)]
    status, headers, raw = raw_post(port, json.dumps(POLL).encode(), {"Accept-Encoding": "gzip;q=0, identity"})
    assert status == 200 and "Content-Encoding" not in headers and len(json.loads(raw)["sightings"]) == 40
    assert raw_post(port, json.dumps(POLL).encode(), {"Accept-Encoding": "gzip; q=0.5"})[1]["Content-Encoding"] == "gzip"


def test_an_accepted_data_request_counts_as_key_use(front):
    port, _, storage = front
    assert call(port, "/v1/data", token=TOKEN)[0] == 200
    assert storage.touched == [("alpha001", NOW, "127.0.0.1")]
    assert call(port, "/v1/data/nope", token=TOKEN)[0] == 404 and len(storage.touched) == 1


def test_a_gzip_body_is_measured_after_inflating_so_a_bomb_stays_small(front):
    port, api, _ = front
    bomb = gzip.compress(b"[" + b" " * (300 * 1024) + b"]")
    assert len(bomb) < 2000
    status, _, raw = raw_post(port, bomb, {"Content-Encoding": "gzip"})
    assert (status, json.loads(raw)["status"]) == (413, 413)
    broken = raw_post(port, b"not gzip at all", {"Content-Encoding": "gzip"})
    other = raw_post(port, json.dumps(POLL).encode(), {"Content-Encoding": "br"})
    assert (broken[0], other[0], other[1]["Accept-Encoding"]) == (400, 415, "gzip") and api.calls == []


def _tls(port: int):
    ctx = ssl.create_default_context(cafile=str(CERTS / "test-ca.pem"))
    raw = socket.create_connection(("127.0.0.1", port), timeout=10)
    return ctx.wrap_socket(raw, server_hostname="127.0.0.1")


def _read_all(tls) -> bytes:
    chunks = []
    while chunk := tls.recv(4096):
        chunks.append(chunk)
    return b"".join(chunks)


def test_a_body_trickled_in_under_the_read_timeout_is_still_408_once_the_whole_body_is_late(front, monkeypatch):
    port, api, _ = front
    monkeypatch.setattr(Handler, "timeout", 0.5)  # the deadline is the behaviour: wait less than the real 10 s
    body = b'{"a": 1}  '
    head = (f"POST /v1/sync HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {TOKEN}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n")
    with _tls(port) as tls:
        tls.sendall(head.encode())
        tls.settimeout(0.2)
        reply = b""
        for byte in body:  # one byte per 0.2 s: each read is quick, the whole body takes 2 s
            tls.sendall(bytes([byte]))
            try:  # wait up to 0.2 s for the answer; once it came, stop sending into a closing socket
                reply = tls.recv(4096)
                break
            except TimeoutError:
                continue
        tls.settimeout(10)
        reply += _read_all(tls)
    assert reply.startswith(b"HTTP/1.0 408 "), reply[:40]
    assert api.calls == []


def test_trace_connect_and_unknown_methods_get_405_on_a_route_and_404_elsewhere(front):
    port, api, storage = front
    for method, path, allow in (("TRACE", "/health", "GET, HEAD"), ("CONNECT", "/v1/sync", "POST"),
                                ("FOO", "/v1/data", "GET, HEAD")):
        status, headers, body = call(port, path, method=method)
        assert (status, headers["Allow"], body["status"]) == (405, allow, 405), (method, path)
    status, _, body = call(port, "/nope", method="FOO")
    assert (status, body["status"]) == (404, 404)
    assert [a[3:5] for a in storage.audits] == [("TRACE /health", 405), ("CONNECT /v1/sync", 405),
                                                ("FOO /v1/data", 405), ("FOO /nope", 404)]
    assert api.calls == []


def test_a_malformed_request_line_is_refused_without_logging_its_query(front, capsys):
    port, _, _ = front
    with _tls(port) as tls:
        tls.sendall(f"GET /v1/data?access_token={TOKEN} extra HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
        reply = _read_all(tls)
    assert reply.startswith(b"HTTP/1.0 400 ")
    out = capsys.readouterr().out
    assert "refused 400" in out and "Qm9vdHN0" not in out and "access_token" not in out


def test_keyless_requests_spend_the_ip_bucket_even_when_their_body_would_be_refused(front):
    port, api, _ = front
    bomb = gzip.compress(b"[" + b" " * (300 * 1024) + b"]")  # 413 for a valid key, once inflated

    def keyless_bomb() -> int:
        req = urllib.request.Request(f"https://127.0.0.1:{port}/v1/sync", data=bomb,
                                     headers={"Content-Type": "application/json", "Content-Encoding": "gzip"})
        ctx = ssl.create_default_context(cafile=str(CERTS / "test-ca.pem"))
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
                return resp.status
        except urllib.error.HTTPError as err:
            with err:
                return err.code

    assert [keyless_bomb() for _ in range(11)] == [401] * 10 + [429]  # IP_RATE: 10 a minute without a valid key
    assert api.calls == []


def test_a_header_too_long_to_parse_gets_431_as_problem_details(front):
    port, api, _ = front
    with _tls(port) as tls:
        tls.sendall(f"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Long: {'a' * 70_000}\r\n\r\n".encode())
        reply = _read_all(tls)
    head, _, body = reply.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.0 431 ") and b"Content-Type: application/problem+json" in head, head
    assert json.loads(body)["status"] == 431 and api.calls == []


def test_a_negative_content_length_gets_400_at_once(front):
    port, api, _ = front
    with _tls(port) as tls:
        tls.sendall(f"POST /v1/sync HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {TOKEN}\r\n"
                    "Content-Type: application/json\r\nContent-Length: -1\r\n\r\n".encode())
        reply = _read_all(tls)  # the socket timeout (10 s) fails the test if the server waits for a body
    head, _, body = reply.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.0 400 ") and b"Content-Type: application/problem+json" in head, head
    assert json.loads(body)["field"] == "body" and api.calls == []


def test_the_reply_after_a_body_gets_the_whole_socket_timeout_again():
    a, b = socket.socketpair()
    try:
        a.settimeout(Handler.timeout)
        h = Handler.__new__(Handler)
        h.connection, h.rfile, h.timeout, h.close_connection = a, a.makefile("rb"), Handler.timeout, False
        h.headers = email.message.Message()
        h.headers["Content-Length"] = "5"
        b.sendall(b"hello")
        assert h._read_raw() == b"hello"
        assert a.gettimeout() == Handler.timeout  # the reply write must not inherit what was left of the body budget
    finally:
        a.close()
        b.close()
