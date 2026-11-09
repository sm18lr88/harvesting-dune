# Sync: the guild server link (one HTTPS request, gzip both ways); the detector itself never touches the network.
"""syncloop.py decides when to talk; this module only does the talking. Both are imported by the
runner only when the "guild sync" loop starts, so replay and the tests that never set a key do not
load them.

The server is a fixed IP behind a private CA (`sync-ca.pem` next to this file), checked with the
default rules: hostname check on, certificate required. The key is a secret: exception and log
text is built from statuses and socket reasons only.
"""

from __future__ import annotations

import functools
import gzip
import http.client
import json
import math
import ssl
import urllib.error
import urllib.request
import zlib
from importlib import resources

from . import statuspage
from .peers import MAX_RECORDS
from .peerview import WIRE_CELL_CM

SERVER = "https://158.69.179.64:8443"
TIMEOUT_S = 10.0
BACKOFF_S = (30.0, 60.0, 120.0, 300.0, 600.0)
MAX_PENDING = 2000
GZIP_OVER = 1024  # request bodies bigger than this travel gzipped
MAX_REPLY = 8 * 1024 * 1024  # an inflated reply stops here


class SyncError(Exception):
    reason = "server"  # what PeerStore.last_error shows


class SyncRejected(SyncError):
    reason = "rejected"
    state = "invalid"  # the key state a 401 leaves (gate.REFUSED)


class SyncExpired(SyncRejected):
    reason = "expired"
    state = "expired"


class SyncThrottled(SyncError):
    reason = "rate-limited"

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"retry after {retry_after:.0f} s")
        self.retry_after = retry_after


class SyncServerError(SyncError):
    def __init__(self, status: int, note: str = "") -> None:
        super().__init__(f"HTTP {status} ({note})" if note else f"HTTP {status}")
        self.status = status


class SyncUnreachable(SyncError):
    reason = "unreachable"

    def __init__(self, cause: object) -> None:
        super().__init__(repr(cause))


class SyncTls(SyncUnreachable):
    reason = "tls"


def _retry_after(value: str | None) -> float:
    try:
        seconds = float(value or "")
    except ValueError:
        return BACKOFF_S[0]
    return seconds if math.isfinite(seconds) and seconds >= 0 else BACKOFF_S[0]


def _problem_note(err: urllib.error.HTTPError) -> str:
    """The problem type's last part and `field` of a refusal, for log text only: the status code
    decides what the loop does. Empty for any other body (a proxy's HTML page, say)."""
    if err.headers.get_content_type() != "application/problem+json":
        return ""
    try:
        p = json.loads(err.read(4096))
    except (ValueError, OSError):
        return ""
    if not isinstance(p, dict):
        return ""
    kind, field = p.get("type"), p.get("field")
    parts = [kind.rsplit("#", 1)[-1] if isinstance(kind, str) and kind != "about:blank" else "",
             field if isinstance(field, str) else ""]
    return " ".join(part[:64] for part in parts if part)


def _refusal(err: urllib.error.HTTPError) -> SyncError:
    match err.code:
        case 401:
            expired = _problem_note(err).split(" ")[0] == "key-expired"
            return SyncExpired("key expired") if expired else SyncRejected("key rejected")
        case 429:
            return SyncThrottled(_retry_after(err.headers.get("Retry-After")))
        case status:
            return SyncServerError(status, _problem_note(err))


def _wire_key(record: dict) -> tuple:
    return (record["cls"], record["type"], record.get("rowKey"),
            math.floor(record["x"] / WIRE_CELL_CM), math.floor(record["y"] / WIRE_CELL_CM))


def _decoded(raw: bytes, encoding: str | None) -> dict:
    if (encoding or "").lower() == "gzip":
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            raw = inflater.decompress(raw, MAX_REPLY)
        except zlib.error:
            raise SyncServerError(200) from None
    try:
        out = json.loads(raw)
    except ValueError:
        raise SyncServerError(200) from None
    if not isinstance(out, dict):
        raise SyncServerError(200)
    return out


class SyncClient:
    def __init__(self, server: str = SERVER, cafile=None) -> None:
        self.server = server
        self.cafile = cafile or resources.files(__package__).joinpath("sync-ca.pem")
        self.version = statuspage.about()["version"]
        self.pending: dict[tuple, dict] = {}  # records the server has not acknowledged yet
        self.pending_scope: tuple | None = None  # (character id, partition): who saw every pending record, and where

    @functools.cached_property
    def ctx(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=str(self.cafile))

    def request(self, key: str, body: dict, path: str) -> urllib.request.Request:
        data = json.dumps(body, separators=(",", ":")).encode()
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                   "X-Client-Version": self.version, "Accept-Encoding": "gzip"}
        if len(data) > GZIP_OVER:
            data, headers["Content-Encoding"] = gzip.compress(data, compresslevel=6, mtime=0), "gzip"
        return urllib.request.Request(f"{self.server}{path}", data=data, headers=headers, method="POST")

    def exchange(self, key: str, body: dict, path: str = "/v1/sync") -> dict:
        """One POST (blocking: run it in a worker thread). Raises a SyncError subclass."""
        raw, headers = self._open(self.request(key, body, path))
        return _decoded(raw, headers.get("Content-Encoding"))

    def fetch(self, key: str, path: str, etag: str | None = None) -> tuple[dict | None, str | None]:
        """One GET of a data route (blocking): (body, ETag), body None for 304 Not Modified."""
        headers = {"Authorization": f"Bearer {key}", "X-Client-Version": self.version, "Accept-Encoding": "gzip"}
        if etag:
            headers["If-None-Match"] = etag
        raw, got = self._open(urllib.request.Request(f"{self.server}{path}", headers=headers, method="GET"))
        return (None if raw is None else _decoded(raw, got.get("Content-Encoding"))), got.get("ETag")

    def _open(self, req: urllib.request.Request) -> tuple[bytes | None, object]:
        """(body, headers); body None for a 304. Raises a SyncError subclass."""
        ctx = self.ctx  # a broken bundled CA file is a bug, not a network failure
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=ctx) as resp:
                return resp.read(), resp.headers
        except urllib.error.HTTPError as err:
            with err:
                if err.code == 304:
                    return None, err.headers
                raise _refusal(err) from None
        except urllib.error.URLError as err:  # urllib wraps a refused certificate in its reason
            failure = SyncTls if isinstance(err.reason, ssl.SSLCertVerificationError) else SyncUnreachable
            raise failure(err.reason) from None
        except ssl.SSLCertVerificationError as exc:
            raise SyncTls(exc) from None
        except (OSError, http.client.HTTPException) as exc:
            raise SyncUnreachable(exc) from None

    def stage(self, records: list[dict], scope: tuple) -> list[dict]:
        """Queue `records` with the unacknowledged ones (the newest per wire key wins) and return
        the next upload: these records first, then the oldest unsent, at most MAX_RECORDS. Records
        staged under another scope (character id, partition) are dropped: another partition's
        coordinates mean nothing here, and another character never sends them as its own."""
        if scope != self.pending_scope:
            self.pending.clear()
            self.pending_scope = scope
        fresh = {_wire_key(r): r for r in records}
        for k in fresh:
            self.pending.pop(k, None)
        batch = (list(fresh.values()) + list(self.pending.values()))[:MAX_RECORDS]
        self.pending.update(fresh)
        while len(self.pending) > MAX_PENDING:
            del self.pending[next(iter(self.pending))]
        return batch

    def ack(self, sent: list[dict]) -> None:
        for r in sent:
            self.pending.pop(_wire_key(r), None)
