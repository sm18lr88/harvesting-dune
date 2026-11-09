"""Guild sync settings: the key travels only through POST /sync/key and never shows up in a log,
the page state or the snapshot; the switches and the per-member show/hide choice persist."""

import asyncio
import json
import logging
import urllib.error
import urllib.request

from test_robustness import free_port

from harvesting_dune import logsetup
from harvesting_dune.app import Detector
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
    try:
        logsetup.setup(tmp_path)  # the live run's log files (they replace pytest's log capture)
        codes = run_requests(det, [("/sync/key", json.dumps({"key": KEY}).encode(), {})])
    finally:
        for h in logging.getLogger().handlers:
            h.flush()
        log_file = (tmp_path / "logs" / "detector.log").read_text("utf8")
        logging.basicConfig(force=True, handlers=[logging.StreamHandler()])  # leave other tests as they were
        logging.captureWarnings(False)  # else a later setup() thinks warnings are still captured
    assert codes == [204]
    assert json.loads((tmp_path / "settings.json").read_text())["syncKey"] == KEY
    peers = det.snapshot()["peers"]
    assert peers["keySet"] is True and peers["keyState"] == "unknown" and peers["positions"] is True
    for text in (json.dumps(det.snapshot()), json.dumps(det.page_state()), log_file):
        assert KEY not in text
    assert "sync key set" in log_file

    again = Detector(None, data_dir=tmp_path)
    assert again.sync_key == KEY and again.peers.key_state == "unknown"


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
    assert codes == [400, 400, 400, 403, 413, 204]
    assert det.sync_key is None and not (tmp_path / "settings.json").exists()
    assert det.snapshot()["peers"]["keySet"] is False
    assert KEY not in caplog.text and "sync key rejected (format)" in caplog.text


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
