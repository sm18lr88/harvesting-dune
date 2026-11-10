# Server: the localhost settings page (viewer, state, live updates, controls).
"""Tiny localhost HTTP server: viewer page, JSON state, SSE updates, control endpoints."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from importlib import resources
from urllib.parse import SplitResult, parse_qs, urlsplit

from . import logsetup
from .app import Detector

log = logging.getLogger(__name__)

MAX_HEADER_LINES = 100
MAX_KEY_BODY = 4096
MAX_DISCARD = 65536


def _response(status: str, ctype: str, body: bytes) -> bytes:
    head = f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n"
    return head.encode() + body


def _allowed(headers: dict[str, str], method: str, port: int) -> bool:
    """Only this machine's own page may use the server.

    * Host must name the loopback address: blocks DNS-rebinding pages that resolve their own
      domain to 127.0.0.1 to read /state.
    * A POST carrying an Origin must come from the settings page itself: any website open in the
      browser could otherwise change settings with a blind cross-site POST.
    """
    host = headers.get("host", "")
    if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
        return False
    origin = headers.get("origin")
    return method != "POST" or origin is None or origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")


async def _discard_body(reader: asyncio.StreamReader, headers: dict[str, str]) -> None:
    """Read a refused body: closing with unread bytes resets the connection (Windows), and the
    client then never sees the answer."""
    length = _content_length(headers)
    if length is not None and length <= MAX_DISCARD:
        await reader.readexactly(length)


def _loggable(value: str | None) -> str:
    """A /control value for the log; a guild key sent to the wrong place stays out of it."""
    if value is None:
        return ""
    return " = (hidden)" if "gs1_" in value else f" = {value}"


def _small_int(text: str) -> int | None:
    """isdigit alone accepts superscripts, and int() raises past 4300 digits: both are bad requests."""
    return int(text) if text.isascii() and text.isdigit() and len(text) <= 18 else None


def _content_length(headers: dict[str, str]) -> int | None:
    return _small_int(headers.get("content-length", ""))


def _parse_key(body: bytes) -> str | None:
    """The key in `{"key": str}`; "" (or `{}`) asks to forget it; None for any other body."""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    key = data.get("key", "") if isinstance(data, dict) else None
    return key if isinstance(key, str) else None


async def _sync_key(det: Detector, reader: asyncio.StreamReader, headers: dict[str, str]) -> str:
    """POST /sync/key; returns the HTTP status. The log never gets the key or the body."""
    length = _content_length(headers)
    if length is None:
        return "411 Length Required"  # a chunked or unsized body is never read as "forget my key"
    if length > MAX_KEY_BODY:
        await _discard_body(reader, headers)
        return "413 Content Too Large"
    key = _parse_key(await reader.readexactly(length))
    if key is None or not det.set_sync_key(key):
        log.info("settings page: sync key rejected (format)")
        return "400 Bad Request"
    log.info("settings page: sync key set" if key else "settings page: sync key cleared")
    det.publish_viewer()
    return "204 No Content"


def _character(det: Detector, cid: str) -> bytes:
    """GET /character?id=: one guild character's profile for the Characters view."""
    number = _small_int(cid)
    detail = None if number is None else det.character_detail(number)
    if detail is None:
        return _response("404 Not Found", "text/plain", b"not found")
    return _response("200 OK", "application/json", json.dumps(detail).encode())


async def _read_head(reader: asyncio.StreamReader) -> tuple[list[str], dict[str, str]]:
    """The request line's words and the lower-cased headers; ValueError for a line over the reader's limit."""
    request_line = (await reader.readline()).decode("latin1").split()
    headers: dict[str, str] = {}
    for _ in range(MAX_HEADER_LINES):
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        name, _, value = line.decode("latin1").partition(":")
        headers[name.strip().lower()] = value.strip()
    return request_line, headers


def _target(word: str) -> SplitResult | None:
    try:
        return urlsplit(word)
    except ValueError:  # e.g. "//[": a bad request, not a detector problem
        return None


def _log_page_open(path: str, headers: dict[str, str]) -> None:
    if path in ("/", "/events"):
        log.info("settings page: %s from %s", "loaded" if path == "/" else "live updates connected",
                 headers.get("user-agent", "?")[:80])


async def serve(det: Detector, host: str = "127.0.0.1", port: int = 8765) -> asyncio.Server:
    raw = resources.files(__package__).joinpath("viewer.html").read_bytes()
    # an open tab compares this with what the server reports and reloads itself after an update
    version = hashlib.blake2b(raw, digest_size=6).hexdigest()
    page = raw.replace(b"%PAGE_VERSION%", version.encode())

    def with_version(snapshot_json: str) -> str:
        return snapshot_json[:-1] + f', "pageVersion": "{version}"}}' if snapshot_json.endswith("}") else snapshot_json

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                request_line, headers = await _read_head(reader)
            except ValueError:  # a line over the reader's 64 KiB limit: a bad request, not a detector problem
                logsetup.limited(log, "settings:oversized", "settings page: request line or header too long")
                writer.write(_response("431 Request Header Fields Too Large", "text/plain", b""))
                await writer.drain()
                return
            if len(request_line) < 2:
                return
            method, target = request_line[0], _target(request_line[1])
            if target is None:
                writer.write(_response("400 Bad Request", "text/plain", b""))
                await writer.drain()
                return
            if not _allowed(headers, method, port):
                log.warning("settings page: refused %s %s (host %r, origin %r)", method, target.path,
                            headers.get("host"), headers.get("origin"))
                await _discard_body(reader, headers)
                writer.write(_response("403 Forbidden", "text/plain", b"forbidden"))
                await writer.drain()
                return
            query = {k: v[0] for k, v in parse_qs(target.query).items()}
            path = target.path

            _log_page_open(path, headers)
            if path == "/events":
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-store\r\n\r\n")
                q: asyncio.Queue[str] = asyncio.Queue()
                det.listeners.add(q)
                try:
                    q.put_nowait(json.dumps(det.snapshot()))
                    while True:
                        writer.write(f"data: {with_version(await q.get())}\n\n".encode())
                        await writer.drain()
                finally:
                    det.listeners.discard(q)
                return

            if method == "POST" and path == "/log":  # a script error on the settings page itself
                logsetup.limited(log, "page:" + query.get("where", "?")[:40], "settings page error: %s",
                                 query.get("msg", "")[:500])
                writer.write(_response("204 No Content", "text/plain", b""))
            elif method == "POST" and path == "/sync/key":  # the key only ever travels in a body
                writer.write(_response(await _sync_key(det, reader, headers), "text/plain", b""))
            elif method == "POST" and path == "/control":
                action, value = query.get("action", ""), query.get("value")
                log.info("settings page: %s%s", action, _loggable(value))
                status = "204 No Content" if det.ui(action, value) else "400 Bad Request"
                det.publish_viewer()
                writer.write(_response(status, "text/plain", b""))
            elif path == "/character":
                writer.write(_character(det, query.get("id", "")))
            elif path == "/state":
                writer.write(_response("200 OK", "application/json", with_version(json.dumps(det.snapshot())).encode()))
            elif path == "/":
                writer.write(_response("200 OK", "text/html; charset=utf-8", page))
            else:
                writer.write(_response("404 Not Found", "text/plain", b"not found"))
            await writer.drain()
        except (ConnectionError, asyncio.CancelledError, asyncio.IncompleteReadError):
            pass
        except Exception:
            log.exception("settings page request failed")
            try:
                writer.write(_response("500 Internal Server Error", "text/plain", b"error; see data-run.log"))
                await writer.drain()
            except (ConnectionError, RuntimeError):
                pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, host, port)
