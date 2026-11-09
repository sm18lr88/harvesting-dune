"""Run the guild sync server: `python -m guildsync --db <file> --cert <pem> --key <pem>`.

A daemon thread sweeps expired rows and idle rate buckets every 60 s. SIGTERM stops the
server cleanly. `--no-tls` (plain HTTP) exists for local debugging and needs `--host 127.0.0.1`.
"""

import argparse
import http.server
import logging
import signal
import sys
import threading
import time
from pathlib import Path

from .api import Api
from .datasets import Datasets
from .httpd import GuildServer, Handler, make_server
from .storage import Storage

SWEEP_S = 60.0
LOOPBACK = "127.0.0.1"

log = logging.getLogger("guildsync")


class PlainHandler(Handler):
    """`--no-tls` only: the socket is plain, so there is no handshake to run."""

    def setup(self) -> None:
        http.server.BaseHTTPRequestHandler.setup(self)
        self.shaken = True


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="guildsync", description="Guild sync HTTPS server (docs/SYNC-PROTOCOL.md).")
    p.add_argument("--db", default="/data/guildsync.sqlite", help="SQLite file (default: %(default)s)")
    p.add_argument("--datasets", help="folder of <name>.json files for GET /v1/data (default: datasets next to --db)")
    p.add_argument("--cert", help="server certificate chain, PEM")
    p.add_argument("--key", help="server private key, PEM")
    p.add_argument("--host", default="0.0.0.0", help="bind address (default: %(default)s)")
    p.add_argument("--port", type=int, default=8443, help="(default: %(default)s)")
    p.add_argument("--no-tls", action="store_true", help=f"plain HTTP for local debugging; only with --host {LOOPBACK}")
    args = p.parse_args(argv)
    if args.no_tls and args.host != LOOPBACK:
        p.error(f"--no-tls is allowed only with --host {LOOPBACK}")
    if not args.no_tls and not (args.cert and args.key):
        p.error("--cert and --key are required")
    return args


def data_folder(args: argparse.Namespace) -> Path:
    return Path(args.datasets) if args.datasets else Path(args.db).parent / "datasets"


def _sweep_forever(server: GuildServer, stop: threading.Event) -> None:
    while not stop.wait(SWEEP_S):
        try:
            now = server.now()
            counts = server.storage.sweep(now)
            server.key_buckets.prune(now)
            server.ip_buckets.prune(now)
            log.debug("sweep %s", counts)
        except Exception:  # the next sweep retries; the server keeps serving
            log.exception("sweep failed")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stderr)
    storage = Storage.open(args.db)
    api = Api(storage, time.time)
    data = Datasets(data_folder(args))
    if args.no_tls:
        server = GuildServer((args.host, args.port), storage, api, time.time, data)
        server.RequestHandlerClass = PlainHandler
    else:
        server = make_server(storage, args.cert, args.key, args.host, args.port, api, data=data)
    stop = threading.Event()
    threading.Thread(target=_sweep_forever, args=(server, stop), name="sweep", daemon=True).start()
    # shutdown() waits for serve_forever, which runs on this thread: call it from another one
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    log.info("listening on %s:%d (%s)", args.host, server.server_address[1], "plain HTTP" if args.no_tls else "TLS")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        storage.close()
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
