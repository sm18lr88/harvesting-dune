"""Guild sync settings: the key travels only through POST /sync/key and never shows up in a log,
the page state or the snapshot; the switches and the per-member show/hide choice persist."""

import asyncio
import json
import logging
import sys
import threading
import urllib.error
import urllib.request

from test_robustness import free_port

from harvesting_dune import logsetup
from harvesting_dune.app import Detector
from harvesting_dune.catalog import RESOURCES
from harvesting_dune.server import serve

KEY = "gs1_ab12cd34_" + "Kq9-_x" * 5 + "Zz"  # 32 chars after the id


def run_requests(det, requests):
    """Send (path, body, headers) POSTs to a real settings server; returns the status codes."""
    port = free_port()
    own = {"Origin": f"http://127.0.0.1:{port}"}

    def post(path, body, headers):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method="POST",
                                     headers={**own, **headers})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    async def main():
        server = await serve(det, port=port)
        try:
            return [await asyncio.to_thread(post, *r) for r in requests]
        finally:
            server.close()

    return asyncio.run(main())


def test_key_is_saved_and_never_shown_or_logged(tmp_path):
    det = Detector(None, data_dir=tmp_path)
    root = logging.getLogger()
    saved, handlers = (sys.excepthook, threading.excepthook, root.level), root.handlers[:]
    try:
        logsetup.setup(tmp_path)  # the live run's log files (they replace pytest's log capture)
        codes = run_requests(det, [("/sync/key", json.dumps({"key": KEY}).encode(), {})])
    finally:
        for h in root.handlers:
            h.flush()
        log_file = (tmp_path / "logs" / "detector.log").read_text("utf8")
        # setup() rewires the whole process, as a live run needs; later tests get pytest's hooks back
        for h in [h for h in root.handlers if h not in handlers and h is not logsetup.PROBLEMS]:
            h.close()
        root.handlers[:] = handlers
        root.setLevel(saved[2])
        logging.captureWarnings(False)  # else a later setup() thinks warnings are still captured
        sys.excepthook, threading.excepthook = saved[:2]
    assert codes == [204]
    assert json.loads((tmp_path / "settings.json").read_text())["syncKey"] == KEY
    peers = det.snapshot()["peers"]
    assert peers["keySet"] is True and peers["keyState"] == "unknown" and peers["positions"] is True
    for text in (json.dumps(det.snapshot()), json.dumps(det.page_state()), log_file):
        assert KEY not in text
    assert "sync key set" in log_file

    again = Detector(None, data_dir=tmp_path)
    assert again.sync_key == KEY and again.peers.key_state == "unknown"
    assert (sys.excepthook, threading.excepthook, root.level) == saved  # later tests keep pytest's hooks


def test_bad_key_requests_change_nothing(tmp_path, caplog):
    det = Detector(None, data_dir=tmp_path)
    cross_site = {"Origin": "https://evil.example"}
    with caplog.at_level(logging.INFO, logger="harvesting_dune"):
        codes = run_requests(det, [
            ("/sync/key", json.dumps({"key": "gs1_short"}).encode(), {}),
            ("/sync/key", json.dumps({"key": KEY + "\n"}).encode(), {}),
            ("/sync/key", b"not json", {}),
            ("/sync/key", json.dumps({"key": KEY}).encode(), cross_site),
            ("/sync/key", json.dumps({"key": KEY, "pad": "x" * 5000}).encode(), {}),
            (f"/control?action=sync_key&value={KEY}", b"", {}),
        ])
    assert codes == [400, 400, 400, 403, 413, 400]  # /control does not know sync_key: refused, never applied
    assert det.sync_key is None and not (tmp_path / "settings.json").exists()
    assert det.snapshot()["peers"]["keySet"] is False
    assert KEY not in caplog.text and "sync key rejected (format)" in caplog.text


def test_refused_control_actions_answer_400(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    codes = run_requests(det, [
        ("/control?action=base", b"", {}),  # no position yet
        ("/control?action=pvp_yield", b"", {}),  # the page drops a cleared field's value
        ("/control?action=max_km&value=abc", b"", {}),
        ("/control?action=minimap_toggle&value=nonsense", b"", {}),
        ("/control?action=nonsense", b"", {}),
        ("/control?action=max_km&value=5", b"", {}),
        ("/control?action=lines", b"", {}),
    ])
    assert codes == [400, 400, 400, 400, 400, 204, 204]
    assert det.max_km == 5.0 and det._bases == {}


def test_oversized_request_line_is_not_a_problem_report(tmp_path, caplog):
    det = Detector(None, data_dir=tmp_path)
    port = free_port()

    async def main():
        server = await serve(det, port=port)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(f"GET /{'a' * 70000} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
            await writer.drain()
            status = await asyncio.wait_for(reader.readline(), 5)
            writer.close()
            return status
        finally:
            server.close()

    with caplog.at_level(logging.INFO, logger="harvesting_dune"):
        status = asyncio.run(main())
    assert status.startswith(b"HTTP/1.1 431 ")
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]  # no "please report" banner


def test_one_bad_setting_keeps_the_others(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({
        "resource": ["spice"], "maxKm": 3.0, "pvpYield": 4.0,
        "bases": {"DD|\u00b2": [1, 2], "DD|7": [3, 4]},
    }))
    det = Detector(None, data_dir=tmp_path)
    assert (det.max_km, det.pvp_yield) == (3.0, 4.0)
    assert det._bases[("DD", 7)] == (3.0, 4.0)


def test_lines_on_keeps_an_item_or_downed_ship_search(tmp_path):
    for key in ("item:slag", "downed_ships"):
        det = Detector(None, data_dir=tmp_path / key.replace(":", "_"), min_rescore_s=0)
        det.items.ingest({"materials": [{"id": "slag", "name": "Irradiated Slag", "categories": ["items/misc/components"]}]})
        det.ui("lines")  # off: one map resource stays shown
        det.ui("resource", key)  # the page's "Switch target" list
        det.ui("lines")
        assert det.resource_key == key and det.visible_resources() == [] and det.lines_active


def test_forgetting_the_key(tmp_path):
    det = Detector(None, data_dir=tmp_path)
    codes = run_requests(det, [("/sync/key", json.dumps({"key": KEY}).encode(), {}),
                               ("/sync/key", b"", {}),  # an empty body is not a request to forget
                               ("/sync/key", b"{}", {})])
    assert codes == [204, 400, 204]
    assert det.sync_key is None and det.peers.key_state == "none"
    assert json.loads((tmp_path / "settings.json").read_text())["syncKey"] is None


def test_malformed_sync_settings_are_ignored(tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({
        "syncKey": 42, "syncEnabled": "yes", "syncPositions": 1, "syncHiddenMembers": [7, "x", True, 3.5],
    }))
    det = Detector(None, data_dir=tmp_path)
    assert det.sync_key is None and det.peers.key_state == "none"
    assert det.sync_enabled is True and det.sync_positions is True and det.sync_hidden_members == {7}
    (tmp_path / "settings.json").write_text(json.dumps({"syncKey": KEY + "!"}))
    assert Detector(None, data_dir=tmp_path).sync_key is None


def test_switches_and_member_choice_persist(tmp_path):
    det = Detector(None, data_dir=tmp_path)
    det.ui("sync_pause")
    det.ui("sync_positions")
    det.ui("sync_member", "10000001")
    det.ui("sync_member", "-5")  # not a character id: ignored
    det.ui("sync_member", "\u00b2")
    det.ui("share_character")
    det.ui("share_session")
    peers = det.snapshot()["peers"]
    assert peers["enabled"] is False and peers["positions"] is False and peers["hiddenMembers"] == [10000001]

    again = Detector(None, data_dir=tmp_path)
    assert again.sync_enabled is False and again.sync_positions is False
    assert again.sync_hidden_members == {10000001}
    assert (again.share_character, again.share_session) == (False, False)
    again.ui("sync_member", "10000001")  # ticked again: shown
    assert Detector(None, data_dir=tmp_path).sync_hidden_members == set()


def raw_requests(det, heads):
    """Send raw request heads to a real settings server; returns (status lines, ERROR log records)."""
    port = free_port()

    async def one(head):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(head.replace("{port}", str(port)).encode() + b"\r\n")
        await writer.drain()
        status = await asyncio.wait_for(reader.readline(), 5)
        writer.close()
        return status.split(b" ")[1].decode()

    async def main():
        server = await serve(det, port=port)
        try:
            return [await one(h) for h in heads]
        finally:
            server.close()

    return asyncio.run(main())


def test_malformed_requests_are_answered_400_and_never_a_problem_report(tmp_path, caplog):
    det = Detector(None, data_dir=tmp_path)
    host = "Host: 127.0.0.1:{port}\r\n"
    huge = "1" * 5000  # past Python's 4300-digit int() limit
    with caplog.at_level(logging.INFO, logger="harvesting_dune"):
        codes = raw_requests(det, [
            f"GET //[ HTTP/1.1\r\n{host}",
            f"GET /character?id={huge} HTTP/1.1\r\n{host}",
            f"POST /control?action=sync_member&value={huge} HTTP/1.1\r\n{host}Content-Length: 0\r\n",
        ])
    assert codes == ["400", "404", "400"]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]  # no "please report" banner
    assert det.sync_hidden_members == set()


def test_an_oversized_content_length_is_refused_and_never_a_problem_report(tmp_path, caplog):
    det = Detector(None, data_dir=tmp_path)
    huge = "Content-Length: " + "9" * 5000 + "\r\n"
    with caplog.at_level(logging.INFO, logger="harvesting_dune"):
        codes = raw_requests(det, [
            f"POST /sync/key HTTP/1.1\r\nHost: 127.0.0.1:{{port}}\r\n{huge}",
            f"POST /control?action=lines HTTP/1.1\r\nHost: evil.example\r\n{huge}",
        ])
    assert codes == ["411", "403"]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert det.sync_key is None


def test_refused_sync_and_pin_actions_answer_400(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    codes = run_requests(det, [
        ("/control?action=sync_member&value=abc", b"", {}),
        ("/control?action=object_delete&value=no-such-pin", b"", {}),  # already gone: a stale page
        ("/control?action=pin_label&value=%20%20", b"", {}),
        ("/control?action=sync_member&value=1001", b"", {}),
        ("/control?action=pin_label&value=MEET", b"", {}),
    ])
    assert codes == [400, 400, 400, 204, 204]
    assert det.sync_hidden_members == {1001} and det.pin_label == "MEET"


def test_previous_resource_from_a_material_search_is_the_last_resource(tmp_path):
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.items.ingest({"materials": [{"id": "slag", "name": "Irradiated Slag", "categories": ["items/misc/components"]}]})
    det.ui("resource", "item:slag")
    det.ui("prev_resource")
    assert det.resource_key == RESOURCES[-1].key
