"""A keyed render converges on ONE tab across processes.

The UI keys a render tab by render_request_id. The manifest S3 key is already
content/job-addressed, so two dispatches of the same job write one manifest — but each slot
used to mint a random uuid rid, so a sub-agent's forwarded render AND the main agent's own
hook render of the same job showed as TWO tabs (session f0598806's trim). Deriving the slot
rid from the slot key (render_key + media class) makes the tab identity match the render
identity, so those converge on one tab; two views of one node stay two tabs; keyless slots
stay random.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hooks.actions import _slot_rid


def test_same_render_key_and_media_class_gives_the_same_rid():
    # Two processes (sub-agent + main agent) render the same trim job's video output.
    rk = "arn_aws_states_execution_media-processing_352875c6"
    a = _slot_rid((rk, "video"), rk)
    b = _slot_rid((rk, "video"), rk)
    assert a == b  # -> UI dedups the composing markers to ONE tab


def test_two_views_of_one_node_get_distinct_rids():
    rk = "d9e45ae9#stem_separation"
    assert _slot_rid((rk, "audio"), rk) != _slot_rid((rk, "other"), rk)


def test_different_jobs_get_distinct_rids():
    assert _slot_rid(("jobA", "video"), "jobA") != _slot_rid(("jobB", "video"), "jobB")


def test_keyless_slot_is_random_each_call():
    # No render_key (image gallery / label-only): identity is not derivable here, so a
    # fresh id each time — the manifest still converges via its content hash.
    assert _slot_rid(("", "image"), "") != _slot_rid(("", "image"), "")


def test_label_only_slot_without_render_key_is_random():
    # key[0] is a human label, not a stable cross-process id -> not deterministic.
    assert _slot_rid(("Segment Shots", "video"), "") != _slot_rid(("Segment Shots", "video"), "")


def test_rid_is_a_plausible_id_string():
    rid = _slot_rid(("job#node", "video"), "job#node")
    assert isinstance(rid, str) and rid.isalnum() and len(rid) == 32
