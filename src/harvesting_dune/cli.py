# CLI: the harvesting-dune command (run, replay, stats) and the composition of a live run.
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import time
from pathlib import Path

from . import logsetup
from .app import VEHICLE_CHOICES, Detector
from .catalog import RESOURCES
from .feed import BusMessage, CompanionHub
from .model import Pose, parse_actors, parse_pose
from .runner import Runner
from .server import serve

log = logging.getLogger(__name__)

DEFAULT_DATA = Path("data")
RESOURCE_KEYS = [r.key for r in RESOURCES]


async def _run(args: argparse.Namespace) -> None:
    logsetup.watch_loop(asyncio.get_running_loop())
    data = Path(args.data_dir)
    det = Detector(None if args.no_journal else data / "journals", data_dir=data)
    det.gate_required = True  # gate.py: nothing on the map without a checked guild key
    det.settings_url = f"http://127.0.0.1:{args.http_port}/"
    if args.resource:
        det.ui("resource", args.resource)
    hub = CompanionHub(args.devtools_port)
    server = await serve(det, port=args.http_port)
    log.info("debug viewer: http://127.0.0.1:%s/", args.http_port)
    try:
        await Runner(det, hub, exit_when_gone_s=args.exit_when_companion_gone).run()
    finally:
        # not `async with server`: its exit waits for every client, and an open settings page
        # holds its event stream forever, so the detector would hang instead of exiting
        server.close()
        if hasattr(server, "close_clients"):  # Python 3.13+
            server.close_clients()


def _journal_messages(path: Path) -> list[tuple[float, BusMessage]]:
    out = []
    with path.open(encoding="utf8") as fh:
        for line in fh:
            rec = json.loads(line)
            out.append((rec["t"], BusMessage(rec["type"], rec["topic"], rec["payload"])))
    return out


def _replay(args: argparse.Namespace) -> None:
    det = Detector(None, min_rescore_s=0)
    det.ui("resource", args.resource)
    if args.vehicle != "auto":
        det.ui("vehicle", args.vehicle)
    for t, msg in _journal_messages(Path(args.journal)):
        det.handle(msg, t)
        det.maybe_rescore(t)
    det.compute(time.time())
    snap = det.snapshot()
    print(json.dumps({k: v for k, v in snap.items() if k != "areas"}, indent=2))
    for i, a in enumerate(a for a in snap["areas"] if a["feasible"]):
        print(f"{i + 1:>2}. {a['distance_m']:>6} m  {a['detail']}")
    for a in (a for a in snap["areas"] if not a["feasible"]):
        print(f"  x. {a['distance_m']:>6} m  {'; '.join(a['reasons'])}")


def _stats(args: argparse.Namespace) -> None:
    """Detection-range and topic statistics from journals: answers "how far away does
    the companion see a spice field, regardless of where I am looking"."""
    topics: dict[str, int] = {}
    first_seen: dict[str, float] = {}
    pose: Pose | None = None
    loaded = False
    fields: list[dict] = []
    for path in sorted(Path(args.journal_dir).glob("*.jsonl")):
        for t, msg in _journal_messages(path):
            topics[msg.topic or "?"] = topics.get(msg.topic or "?", 0) + 1
            if msg.topic == "dune.player":
                pose = parse_pose(msg.payload) or pose
            elif msg.topic == "dune.map.actors":
                for a in parse_actors(msg.payload):
                    if a.key in first_seen or not a.is_spice_field:
                        first_seen.setdefault(a.key, t)
                        continue
                    first_seen[a.key] = t
                    if loaded and pose is not None:
                        fields.append({"size": a.spice_size, "dist_m": pose.dist2d(a.x, a.y) / 100, "fill": a.fill_rate})
                loaded = True
    print("messages per topic:", json.dumps(topics, indent=2))
    dists = [f["dist_m"] for f in fields]
    if dists:
        q = statistics.quantiles(dists, n=10) if len(dists) >= 2 else dists
        print(f"spice fields first observed after load-in: {len(dists)}")
        print(f"first-seen distance m: min {min(dists):.0f}  median {statistics.median(dists):.0f}  p90 {q[-1]:.0f}  max {max(dists):.0f}")
    else:
        print("no spice fields appeared after load-in yet; drive around the Deep Desert with the feed running")


def main() -> None:
    p = argparse.ArgumentParser(prog="harvesting-dune")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="attach to the companion and drive its map")
    r.add_argument("--devtools-port", type=int, default=9231)
    r.add_argument("--http-port", type=int, default=8765)
    r.add_argument("--resource", choices=RESOURCE_KEYS, default=None, help="default: the last one you chose")
    r.add_argument("--data-dir", default=str(DEFAULT_DATA))
    r.add_argument("--no-journal", action="store_true")
    r.add_argument("--exit-when-companion-gone", type=float, default=0.0, metavar="SECONDS",
                   help="stop cleanly once the companion has been closed this long (0 = wait for it)")

    rp = sub.add_parser("replay", help="recommend offline from a recorded journal")
    rp.add_argument("journal")
    rp.add_argument("--resource", choices=RESOURCE_KEYS, default="spice")
    rp.add_argument("--vehicle", choices=VEHICLE_CHOICES, default="auto")

    st = sub.add_parser("stats", help="detection-range statistics from journals")
    st.add_argument("--journal-dir", default=str(DEFAULT_DATA / "journals"))

    args = p.parse_args()
    if args.cmd == "run":
        logsetup.setup(Path(args.data_dir), args.verbose)  # log files in data\\logs, all error sources
    else:
        logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.cmd == "run":
        try:
            asyncio.run(_run(args))
        except KeyboardInterrupt:
            pass
    elif args.cmd == "replay":
        _replay(args)
    else:
        _stats(args)


if __name__ == "__main__":
    main()
