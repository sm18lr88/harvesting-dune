"""Pin producers: the settings-page actions and hotkey 5 reach one publish path; the preset label is
saved; nothing here touches the ranking or the minimap filter."""

import re

from helpers import pose, session

from harvesting_dune import hotkeys
from harvesting_dune.app import Detector
from harvesting_dune.feed import BusMessage

KEY = "gs1_alpha001_Qm9vdHN0cmFwVGVzdEtleUFscGhhMDAx"
WHO = {"characterName": "Alpha Test", "characterId": 1001, "battlegroupId": "prd-srv-test-alpha-erg"}


def player(tmp_path) -> Detector:
    det = Detector(None, data_dir=tmp_path, min_rescore_s=0)
    det.set_sync_key(KEY)
    session(det, "survival_1")
    det.handle(BusMessage("delta", "dune.trackedSession", WHO), 0)
    pose(det, 0, 0, t=1)
    return det


def test_the_pin_action_stages_a_pin_with_the_typed_label_or_the_preset(tmp_path):
    det = player(tmp_path)
    det.ui("object_pin", "iron here")
    assert [u["body"] for u in det.peers.objects_out] == [{"label": "iron here"}]
    assert [u["kind"] for u in det.peers.objects_out] == ["pin"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{12}", det.peers.objects_out[0]["id"])
    det.peers.published_at = 0.0
    det.ui("object_pin")
    assert det.peers.objects_out[-1]["body"] == {"label": "PIN"}
    det.peers.published_at = 0.0
    det.ui("object_pin", "x" * 40)
    assert det.peers.objects_out[-1]["body"] == {"label": "x" * 16}
    det.peers.published_at = 0.0
    before = len(det.peers.objects_out)
    det.ui("object_pin", "   ")  # blank typed text falls back to the preset, which is never blank
    assert len(det.peers.objects_out) == before + 1
    det.pin_label = ""
    det.peers.published_at = 0.0
    det.ui("object_pin", "   ")
    assert len(det.peers.objects_out) == before + 1  # nothing to say: nothing staged


def test_the_preset_label_is_saved_and_reloaded(tmp_path):
    det = player(tmp_path)
    det.ui("pin_label", "  MEET ")
    assert det.pin_label == "MEET"
    assert Detector(None, data_dir=tmp_path).pin_label == "MEET"
    det.ui("pin_label", "   ")
    assert det.pin_label == "MEET"
    det.ui("pin_label", "y" * 40)
    assert det.pin_label == "y" * 16


def test_the_delete_action_delegates_to_delete_object(tmp_path):
    det = player(tmp_path)
    seen = []
    det.delete_object = lambda oid: seen.append(oid) or True
    det.ui("object_delete", "abc123")
    det.ui("object_delete")
    assert seen == ["abc123", ""]


def test_hotkey_5_is_ctrl_alt_p_for_object_pin():
    assert hotkeys.BINDINGS[5] == (0x50, "object_pin")
    assert len(hotkeys.BINDINGS) == len({vk for vk, _ in hotkeys.BINDINGS.values()})
    assert len({name for _, name in hotkeys.BINDINGS.values()}) == len(hotkeys.BINDINGS)


def test_pin_actions_never_touch_the_ranking_or_the_minimap_filter(tmp_path):
    det = player(tmp_path)
    keep = (det.areas, set(det.minimap_hidden), det.resource_key, det.minimap_rev, det.lines_enabled)
    rev = det.publish_rev
    det.ui("object_pin", "iron here")
    det.ui("pin_label", "MEET")
    det.ui("object_delete", "nope")
    assert (det.areas, det.minimap_hidden, det.resource_key, det.minimap_rev, det.lines_enabled) == keep
    assert det.publish_rev == rev
