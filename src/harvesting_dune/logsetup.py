# Log setup: log files that keep every warning and error, across restarts, from every source.
"""Logging for a live run.

    data\\logs\\detector.log   everything from INFO up (2 MB each, the current file plus 5 old ones)
    data\\logs\\errors.log     only warnings and errors (1 MB each, the current file plus 5 old ones)
    stderr                    as before (the autorun watcher writes it to data-run.log)

Also routed into these logs: uncaught exceptions, exceptions in threads, exceptions in
asyncio tasks nobody awaited, and Python warnings. `limited()` logs a repeating warning at most
once a minute per key, with the number of repeats it held back.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)s %(message)s"
_last: dict[str, tuple[float, int, float]] = {}  # key -> (last logged, repeats held back since, last repeat)
log = logging.getLogger("harvesting_dune")


class Problems(logging.Handler):
    """The last errors of this run: the settings page shows them with a report to copy, and the
    companion's map window shows a small notice. The companion itself is never affected."""

    def __init__(self, keep: int = 20) -> None:
        super().__init__(logging.ERROR)
        self.items: collections.deque[dict] = collections.deque(maxlen=keep)
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.count += 1
            self.items.append({"id": self.count, "t": record.created, "level": record.levelname, "where": record.name,
                               "message": record.getMessage()[:300], "detail": self.format(record)[-3000:]})
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)

    def last(self) -> dict | None:
        return self.items[-1] if self.items else None

    def snapshot(self, limit: int = 5) -> dict:
        return {"count": self.count, "items": list(self.items)[-limit:]}


PROBLEMS = Problems()


def setup(data_dir: Path | None, verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if data_dir is not None:
        logs = data_dir / "logs"
        try:
            logs.mkdir(parents=True, exist_ok=True)
            everything = RotatingFileHandler(logs / "detector.log", maxBytes=2_000_000, backupCount=5, encoding="utf8")
            errors = RotatingFileHandler(logs / "errors.log", maxBytes=1_000_000, backupCount=5, encoding="utf8")
            errors.setLevel(logging.WARNING)
            handlers += [everything, errors]
        except OSError as exc:
            print(f"cannot write log files in {logs}: {exc}", file=sys.stderr)
    handlers.append(PROBLEMS)
    logging.basicConfig(level=level, format=FORMAT, handlers=handlers, force=True)
    logging.captureWarnings(True)  # warnings.warn(...) -> the "py.warnings" logger

    def uncaught(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            return
        log.critical("uncaught exception, the detector stops", exc_info=(exc_type, exc, tb))

    def thread_crash(args: threading.ExceptHookArgs) -> None:
        name = args.thread.name if args.thread else "?"
        log.error("thread %s failed", name, exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = uncaught
    threading.excepthook = thread_crash


def watch_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Log exceptions of asyncio tasks and callbacks that nobody awaited."""

    def handler(_loop: asyncio.AbstractEventLoop, context: dict) -> None:
        exc = context.get("exception")
        log.error("event loop: %s", context.get("message", "unhandled error"),
                  exc_info=(type(exc), exc, exc.__traceback__) if exc else None)

    loop.set_exception_handler(handler)


def limited(logger: logging.Logger, key: str, msg: str, *args: object, level: int = logging.WARNING,
            every_s: float = 60.0) -> None:
    """Log at most once per `every_s` for `key`; the next message says how many were held back."""
    now = time.monotonic()
    last, held, held_at = _last.get(key, (-1e18, 0, 0.0))
    if now - last < every_s:
        _last[key] = (last, held + 1, now)
        return
    _last[key] = (now, 0, 0.0)
    if held:  # the held repeats may be hours old: a burst long ago, then quiet
        msg += f" (and {held} more like it, the last one {now - held_at:.0f} s ago)"
    logger.log(level, msg, *args)
