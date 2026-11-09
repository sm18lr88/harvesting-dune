"""Key-gated datasets: `GET /v1/data/<name>` serves `<folder>/<name>.json` (docs/SYNC-PROTOCOL.md "Datasets").

A file is served only when its name matches NAME, it is a regular file of at most MAX_BYTES, and it
parses as JSON. Each file is read once per (mtime, size): an unchanged file costs one stat per request.
The name never goes through URL decoding, so `..`, `%2e%2e` and `\\` simply fail NAME.
"""

import gzip
import hashlib
import json
import logging
import re
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl

from .problems import invalid, problem

NAME = re.compile(r"[a-z0-9-]{1,32}")
MAX_BYTES = 4 * 1024 * 1024
MAX_LISTED = 100

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Dataset:
    body: bytes
    etag: str  # strong, quoted: '"<sha256 hex of body>"'
    _gz: bytes | None = field(default=None, repr=False)

    def gzipped(self) -> bytes:
        if self._gz is None:  # two threads may both compress once; the result is the same
            self._gz = gzip.compress(self.body, compresslevel=6, mtime=0)
        return self._gz


def _load(path: Path) -> Dataset | None:
    try:
        with path.open("rb") as f:
            body = f.read(MAX_BYTES + 1)
    except OSError as exc:
        log.warning("dataset %s not served: %s", path.name, type(exc).__name__)
        return None
    if len(body) > MAX_BYTES:
        log.warning("dataset %s not served: over %d bytes", path.name, MAX_BYTES)
        return None
    try:
        json.loads(body)
    except (ValueError, RecursionError):
        log.warning("dataset %s not served: not JSON", path.name)
        return None
    return Dataset(body, f'"{hashlib.sha256(body).hexdigest()}"')


class Datasets:
    """The served files of one folder (None: serve nothing), cached by (mtime_ns, size)."""

    def __init__(self, folder: str | Path | None) -> None:
        self.folder = None if folder is None else Path(folder)
        self._cache: dict[str, tuple[tuple[int, int], Dataset | None]] = {}
        self._lock = threading.Lock()

    def get(self, name: str) -> Dataset | None:
        if self.folder is None or not NAME.fullmatch(name):
            return None
        path = self.folder / f"{name}.json"
        try:
            st = path.stat()
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_BYTES:
            return None
        stamp = (st.st_mtime_ns, st.st_size)
        with self._lock:
            hit = self._cache.get(name)
            if hit is None or hit[0] != stamp:  # a bad file is cached too, so it is not re-read per request
                hit = self._cache[name] = (stamp, _load(path))
        return hit[1]

    def listing(self, after: str = "", limit: int = MAX_LISTED) -> tuple[list[dict], str | None]:
        """Name, ETag and size of the served datasets named after `after`, by name, at most `limit`;
        and the name to pass as `after` for the next page (None on the last page)."""
        if self.folder is None:
            return [], None
        try:
            names = sorted(p.stem for p in self.folder.glob("*.json") if p.stem > after)
        except OSError:
            return [], None
        out = []
        for name in names:
            if (d := self.get(name)) is None:
                continue
            if len(out) == limit:
                return out, out[-1]["name"]
            out.append({"name": name, "etag": d.etag, "bytes": len(d.body)})
        return out, None


def _matches(if_none_match: str | None, etag: str) -> bool:
    """RFC 9110 If-None-Match: `*`, or any listed tag equal by weak comparison."""
    if not if_none_match:
        return False
    tags = [t.strip() for t in if_none_match.split(",")]
    return "*" in tags or any(t.removeprefix("W/") == etag for t in tags)


def _page(query: str) -> tuple[str, int] | None:
    """(after, limit) from the list's query string; None for any other parameter, which gets 404 like
    a key in a URL. A bad value raises ValueError naming the field."""
    try:
        pairs = parse_qsl(query, keep_blank_values=True, max_num_fields=2)
    except ValueError:
        raise ValueError("query") from None
    args = dict(pairs)
    if not set(args) <= {"after", "limit"}:
        return None
    if len(args) != len(pairs):
        raise ValueError("query")
    after, limit = args.get("after", ""), args.get("limit", str(MAX_LISTED))
    if after and not NAME.fullmatch(after):
        raise ValueError("after")
    if not (limit.isascii() and limit.isdigit() and len(limit) <= 3 and 1 <= int(limit) <= MAX_LISTED):
        raise ValueError("limit")
    return after, int(limit)


def _list(datasets: Datasets, query: str | None):
    try:
        page = _page(query or "")
    except ValueError as bad:
        return 400, invalid(str(bad)), {}
    if page is None:
        return 404, problem(404), {}
    items, after = datasets.listing(*page)
    return 200, {"datasets": items, "next": after}, {}


def reply(datasets: Datasets, rest: str, query: str | None, if_none_match: str | None, gzip_over: int | None):
    """(status, body, headers) for the path after `/v1/data` and its query string (None: no `?`): "" lists,
    "/<name>" serves one dataset. The body is bytes, None (304) or a dict; gzip_over is None when the
    client did not accept gzip."""
    if rest == "":
        return _list(datasets, query)
    d = datasets.get(rest[1:]) if rest.startswith("/") and query is None else None
    if d is None:
        return 404, problem(404, detail="No such dataset."), {}
    headers = {"ETag": d.etag, "Cache-Control": "private", "Vary": "Accept-Encoding"}
    if _matches(if_none_match, d.etag):
        return 304, None, headers
    if gzip_over is not None and len(d.body) > gzip_over:
        return 200, d.gzipped(), {**headers, "Content-Encoding": "gzip"}
    return 200, d.body, headers
