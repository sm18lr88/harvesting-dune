"""HTTPS front of the guild sync server: TLS, bearer keys, rate limits, size limits, gzip, audit lines.

Wire contract: docs/SYNC-PROTOCOL.md. The handler owns transport rules only; `api` owns the
body's meaning and `storage` owns keys and the queryable audit table.
"""

import contextlib
import gzip
import http.server
import json
import logging
import math
import secrets
import ssl
import sys
import time
import zlib
from http import HTTPStatus

from . import datasets
from .buckets import Buckets
from .problems import BAD_KEY, CHALLENGE, EXPIRED_KEY, MEDIA_TYPE, invalid, problem

MAX_BODY = 262144  # bytes, after any gzip is undone
GZIP_OVER = 1024  # a reply body bigger than this goes out gzipped when the client asks for it
ROUTES = {"/v1/sync": "sync", "/v1/profile": "profile", "/v1/profiles": "profiles"}  # path -> Api method
HEALTH, HEALTH_METHODS = "/health", "GET, HEAD"
DATA, DATA_METHODS = "/v1/data", "GET, HEAD"  # DATA alone lists; DATA + "/<name>" serves one dataset
KEY_RATE = (30, 30 / 60)  # (bucket size, tokens per second): 30 requests a minute per key
IP_RATE = (10, 10 / 60)  # per IP without a valid key, and /health
HANDSHAKE_TIMEOUT_S = 10.0

log = logging.getLogger(__name__)


class GuildServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], storage, api, now, data: datasets.Datasets | None = None) -> None:
        super().__init__(address, Handler)
        self.storage, self.api, self.now = storage, api, now
        self.datasets = data or datasets.Datasets(None)
        self.key_buckets, self.ip_buckets = Buckets(*KEY_RATE), Buckets(*IP_RATE)


class Rejected(Exception):
    """A transport-level refusal: the status, the problem body and any extra headers."""

    def __init__(self, status: int, body: dict, headers: dict[str, str] | None = None) -> None:
        super().__init__(status)
        self.status, self.body, self.headers = status, body, headers or {}


def _limited(retry_after: float) -> Rejected:
    wait = max(1, math.ceil(retry_after))
    return Rejected(429, problem(429, "rate-limited", f"Try again in {wait} s."), {"Retry-After": str(wait)})


def _too_large() -> Rejected:
    return Rejected(413, problem(413, detail=f"The body is over {MAX_BODY} bytes, as sent or after inflating."))


def _bad_gzip() -> Rejected:
    return Rejected(400, invalid("body", "The gzip body does not inflate."))


def _gunzip(raw: bytes) -> bytes:
    """At most MAX_BODY bytes out of a gzip body: inflating stops there, so a small bomb stays small."""
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = inflater.decompress(raw, MAX_BODY + 1)
    except zlib.error:
        raise _bad_gzip() from None
    if len(out) > MAX_BODY:
        raise _too_large()
    if not inflater.eof:
        raise _bad_gzip()
    return out


def _wants_gzip(accept: str | None) -> bool:
    """RFC 9110: `gzip;q=0` means gzip is not acceptable."""
    for part in (accept or "").split(","):
        coding, *params = part.split(";")
        if coding.strip().lower() != "gzip":
            continue
        q = next((p.split("=", 1)[1] for p in params if p.strip().lower().startswith("q=")), "1")
        try:
            return float(q) > 0
        except ValueError:
            return False
    return False


def _is_data(target: str) -> bool:
    path = target.split("?", 1)[0]
    return path == DATA or path.startswith(DATA + "/")


class Handler(http.server.BaseHTTPRequestHandler):
    server: GuildServer
    server_version = "guildsync/1"
    sys_version = ""
    protocol_version = "HTTP/1.0"
    timeout = HANDSHAKE_TIMEOUT_S

    def setup(self) -> None:
        super().setup()
        self.shaken = False
        try:  # here, not in accept(): a silent client must not stall the accept loop
            self.request.do_handshake()
            self.shaken = True
        except (ssl.SSLError, OSError) as exc:
            self.log_message("handshake failed: %s", type(exc).__name__)

    def handle(self) -> None:
        if self.shaken:
            super().handle()

    def do_GET(self) -> None:
        self._serve(self._get)

    def do_POST(self) -> None:
        self._serve(self._post)

    def _other_method(self) -> None:
        self._serve(self._refuse_method)

    do_HEAD = do_GET  # `_reply` sends no body for HEAD
    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_QUERY = _other_method

    def _refuse_method(self) -> tuple[int, dict]:
        """405 with `Allow` on a known path, 404 on any other."""
        allow = (HEALTH_METHODS if self.path == HEALTH else DATA_METHODS if _is_data(self.path)
                 else "POST" if self.path in ROUTES else None)
        if allow is None:
            raise Rejected(404, problem(404))
        raise Rejected(405, problem(405), {"Allow": allow})

    def _get(self) -> tuple:
        if _is_data(self.path):
            return self._data()
        if self.path != HEALTH:
            return self._refuse_method()
        retry = self.server.ip_buckets.take(self.client_address[0], self.server.now())
        if retry:
            raise _limited(retry)
        return self.server.api.health()

    def _post(self) -> tuple[int, dict]:
        route = ROUTES.get(self.path)
        if route is None:
            return self._refuse_method()
        srv, ip, now = self.server, self.client_address[0], self.server.now()
        try:  # before the body: a caller without a valid key spends the IP bucket, whatever it sent
            key = self._caller(ip, now)
        except Rejected:
            self._discard_body()
            raise
        raw = self._read_body()
        if self.headers.get_content_type() != "application/json":
            raise Rejected(415, problem(415, detail="Send the body as application/json."))
        try:
            body = json.loads(raw)
        except (ValueError, RecursionError):  # RecursionError: nesting deeper than the parser's stack
            raise Rejected(400, invalid("body", "The body is not JSON, or nests too deep.")) from None
        if not isinstance(body, dict):  # every route takes an object; also stops deep arrays where the stack is big enough to parse them
            raise Rejected(400, invalid("body", "The body must be a JSON object."))
        if isinstance(body.get("records"), list):
            self.n = len(body["records"])
        return getattr(srv.api, route)(key, body, ip, now)

    def _data(self) -> tuple:
        ip, now = self.client_address[0], self.server.now()
        key = self._caller(ip, now)
        gzip_over = GZIP_OVER if _wants_gzip(self.headers.get("Accept-Encoding")) else None
        path, mark, query = self.path.partition("?")
        reply = datasets.reply(self.server.datasets, path[len(DATA):], query if mark else None,
                               self.headers.get("If-None-Match"), gzip_over)
        if reply[0] < 400:  # an accepted request (a 304 too) is key use
            self.server.storage.touch_key(key["id"], now, ip)
        return reply

    def _caller(self, ip: str, now: float) -> dict:
        """The caller's active key, recorded for the audit, after its rate bucket allowed the request."""
        key = self._key(ip, now)
        self.key_id = key["id"]
        retry = self.server.key_buckets.take(key["id"], now)
        if retry:
            raise _limited(retry)
        return key

    def _key(self, ip: str, now: float) -> dict:
        """The caller's active key; a missing, bad, revoked or expired one spends the IP bucket and gets 401."""
        auth = self.headers.get("Authorization", "")
        key = self.server.storage.lookup_key(auth[7:]) if auth.startswith("Bearer ") else None
        if key is not None and key.get("state") != "expired":
            return key
        retry = self.server.ip_buckets.take(ip, now)
        raise _limited(retry) if retry else Rejected(401, BAD_KEY if key is None else EXPIRED_KEY, CHALLENGE)

    def _read_raw(self) -> bytes:
        """The body as sent, refused by its declared size before a byte of it is read. All of it must
        arrive within `timeout` seconds: the socket timeout alone bounds each read, not the body."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0:
            raise Rejected(400, invalid("body", "Content-Length is not a size."))
        if length > MAX_BODY:
            raise _too_large()
        deadline, parts, got = time.monotonic() + self.timeout, [], 0
        try:
            while got < length:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError
                self.connection.settimeout(left)
                part = self.rfile.read1(length - got)
                if not part:
                    raise ConnectionError
                parts.append(part)
                got += len(part)
        except OSError:  # the client stalled, trickled or dropped mid-body: its fault, not a server error
            self.close_connection = True
            raise Rejected(408, problem(408)) from None
        finally:
            self.connection.settimeout(self.timeout)  # the reply gets the whole timeout, not what the body left
        return b"".join(parts)

    def _discard_body(self) -> None:
        """Read a refused caller's body without inflating or parsing it: closing on unread bytes
        would reset the connection before the client reads its 401."""
        self.close_connection = True
        with contextlib.suppress(Rejected):
            self._read_raw()

    def _read_body(self) -> bytes:
        """The whole body (`_read_raw`); a gzip body is inflated (Content-Encoding: gzip), any other
        encoding is refused."""
        raw = self._read_raw()
        match (self.headers.get("Content-Encoding") or "identity").strip().lower():
            case "identity":
                return raw
            case "gzip":
                return _gunzip(raw)
            case _:
                raise Rejected(415, problem(415, detail="Only gzip is accepted as Content-Encoding."),
                               {"Accept-Encoding": "gzip"})

    def _serve(self, route) -> None:
        start = time.perf_counter()
        self.key_id, self.n, headers = None, 0, {}
        try:
            status, body, *more = route()  # a route may add headers as a third item
            headers = more[0] if more else {}
        except Rejected as r:
            status, body, headers = r.status, r.body, r.headers
        except Exception:
            request_id = secrets.token_hex(8)  # the log line and the reply share it; the reply holds nothing else
            log.exception("request %s failed: %s %s", request_id, self.command, self._logged_path())
            status, body = 500, problem(500, requestId=request_id)
        self._audit(status, (time.perf_counter() - start) * 1000.0)
        self._reply(status, body, headers)

    def _logged_path(self) -> str:
        return self.path.split("?", 1)[0][:200]  # never a query string: a careless client may put a key there

    def _audit(self, status: int, ms: float) -> None:
        """Both audit records go out before the reply, so a client that has its answer sees them.
        `client` (X-Client-Version) shows which detector versions still call a route."""
        ip, path, now = self.client_address[0], self._logged_path(), self.server.now()
        client = (self.headers.get("X-Client-Version") or "")[:32] or None
        line = {"ts": now, "ip": ip, "key": self.key_id, "path": path, "status": status, "n": self.n,
                "ms": round(ms, 1), "client": client}
        print(json.dumps(line), file=sys.stdout, flush=True)
        try:
            self.server.storage.audit(now, self.key_id, ip, f"{self.command} {path}", status, self.n, client)
        except Exception:
            log.exception("audit write failed")

    def _encode(self, body: dict | bytes, headers: dict[str, str]) -> tuple[bytes, dict[str, str]]:
        """Bytes go out as they are (a route encoded them); a dict goes out as JSON, gzipped when it pays."""
        if isinstance(body, bytes):
            return body, headers
        data = json.dumps(body).encode()
        if len(data) > GZIP_OVER and _wants_gzip(getattr(self, "headers", None) and self.headers.get("Accept-Encoding")):
            return gzip.compress(data, compresslevel=6, mtime=0), {**headers, "Content-Encoding": "gzip"}
        return data, headers

    def _reply(self, status: int, body: dict | bytes | None, headers: dict[str, str]) -> None:
        """body None: a 304, which has no body, no Content-Type and no Content-Length."""
        data, headers = (b"", headers) if body is None else self._encode(body, headers)
        fixed = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Connection": "close"}
        if body is not None:
            fixed |= {"Content-Type": MEDIA_TYPE if status >= 400 else "application/json", "Content-Length": str(len(data))}
        try:
            self.send_response(status)
            for name, value in {**fixed, **headers}.items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except OSError as exc:
            self.log_message("reply failed: %s", type(exc).__name__)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """Parse failures in the base class (bad request line, header limits, unknown method) answer
        with problem details too. An unknown method (501 in the base class) gets the 405 or 404 of
        every other method: its request line and headers are parsed by then."""
        if code == HTTPStatus.NOT_IMPLEMENTED:
            self._serve(self._refuse_method)
            return
        self.log_message("refused %d: %s", code, HTTPStatus(code).phrase)  # the base class's text may quote the request line, key and all
        self.close_connection = True
        self._reply(code, problem(code), {})

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        """Silent: `_audit` writes the one line per request."""

    def log_message(self, format: str, *args) -> None:
        line = {"ts": self.server.now(), "ip": self.client_address[0], "event": format % args}
        print(json.dumps(line), file=sys.stdout, flush=True)


def make_server(storage, cert_path: str, key_path: str, host: str, port: int, api, now=time.time,
                data: datasets.Datasets | None = None) -> http.server.ThreadingHTTPServer:
    """A bound HTTPS server (TLS 1.2+); run it with `serve_forever()`, stop it with `shutdown()`."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert_path, key_path)
    server = GuildServer((host, port), storage, api, now, data)
    server.socket = ctx.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    return server
