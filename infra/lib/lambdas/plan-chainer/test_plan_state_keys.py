"""Both plan-state key formats must be readable, and new writes must be qualified.

The sort key was the literal "PLAN" for a plan row and a bare node id for a node row. "PLAN"
being FIXED meant a second plan in a session overwrote the first's row, nodes_json included,
so the first plan could never be completed. Node ids are positional (t1, t2, t3), so they
also collided across plans -- a stale terminal row satisfying the wrong plan's completion.

Readers accept both formats deliberately: a deploy must not strand plans mid-flight.
"""
import importlib.util
import os
import sys
from pathlib import Path

CH = Path(__file__).resolve().parent
for k in ("JOB_REGISTRY_TABLE", "PLAN_STATE_TABLE", "GATEWAY_REGISTRY_TABLE", "SKILLS_S3_BUCKET"):
    os.environ.setdefault(k, "t")
os.environ.setdefault("TASK_AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/task")
# Loaded by path under a unique name: the chainer and the poller both define `handler` and
# `gateway_invoker`, and whichever imports first would otherwise shadow the other.
spec = importlib.util.spec_from_file_location("_ch_keys", CH / "handler.py")
pc = importlib.util.module_from_spec(spec)
sys.modules["_ch_keys"] = pc
spec.loader.exec_module(pc)


def test_plan_sort_key_is_qualified_by_plan():
    assert pc._plan_sk("p1") == "PLAN#p1"
    assert pc._node_sk("p1", "t3_merge") == "p1#t3_merge"


def test_both_formats_parse():
    assert pc._split_sk("PLAN") == ("", "PLAN")
    assert pc._split_sk("PLAN#p1") == ("p1", "PLAN")
    assert pc._split_sk("p1#t3") == ("p1", "t3")
    assert pc._split_sk("t3") == ("", "t3")


def test_a_node_id_containing_a_hash_keeps_its_remainder():
    assert pc._split_sk("p1#a#b") == ("p1", "a#b")


def test_legacy_keys_still_work_when_no_plan_id_is_known():
    assert pc._plan_sk("") == "PLAN"
    assert pc._node_sk("", "t3") == "t3"


def test_every_node_write_goes_through_the_plan_key_format():
    """No writer hardcodes a bare node key any more: all address rows via _node_key/_plan_key.

    Writers must not switch piecemeal. They did, briefly, and it reached staging: the chainer
    wrote a node's "running" row under "<plan_id>#<node_id>" while state_store.put_node_status
    wrote its terminal status under the bare "<node_id>" -- two rows for one node. Now every
    writer (chainer, StateStore, plan_and_execute) writes in the format the plan's PLAN row was
    written with, so a legacy plan stays bare and a new plan stays qualified.
    """
    src = (CH / "handler.py").read_text()
    bare_key = 'Key={"session_id": session_id, "node_id": node_id}'
    # The one bare read left is the legacy-producer fallback in _external_producer_row.
    assert src.count(bare_key) == 1
    assert src.index(bare_key) > src.index("def _external_producer_row")
    assert src.index(bare_key) < src.index("def _external_producer_value")
    for bare in ('"node_id": nid}', '"node_id": "PLAN"}', '"node_id": c["id"]', '"node_id": mid,',
                 '"node_id": gid,', '"node_id": producer}'):
        assert bare not in src, bare


def test_node_key_follows_the_plan_row_format():
    pc._remember_plan_key("pq", "PLAN#pq")
    pc._remember_plan_key("pl", "PLAN")
    assert pc._node_key("s", "pq", "sg1") == {"session_id": "s", "node_id": "pq#sg1"}
    assert pc._node_key("s", "pl", "sg1") == {"session_id": "s", "node_id": "sg1"}
    assert pc._node_key("s", "unknown", "sg1") == {"session_id": "s", "node_id": "sg1"}
    assert pc._plan_key("s", "pq") == {"session_id": "s", "node_id": "PLAN#pq"}
    assert pc._plan_key("s", "pl") == {"session_id": "s", "node_id": "PLAN"}


def test_the_chainer_advances_every_plan_in_a_session():
    src = (CH / "handler.py").read_text()
    assert "for plan_record in plan_records:" in src
    assert "def _advance_plan(" in src
