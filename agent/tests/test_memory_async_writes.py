"""History writes leave the event loop, keep their order, and are all stored by turn end.

With the SDK default (``batch_size=1``) every message and agent-state sync was a synchronous
``CreateEvent`` (0.1-0.6s each) on the turn's critical path. ``FramedMemorySessionManager``
with ``async_writes=True`` serializes each message when added, sends from one background writer,
and drains at turn end -- and fixes two SDK batching defects on that path (a flush cleared the
whole buffer after sending a snapshot of it; a batched agent-state event put the OLDEST state at
``payload[0]``, which is what ``read_agent`` restores).

Also: the long-term-memory retrieval prefetch serves the turn's hook when the query matches.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import memory_context as mc  # noqa: E402
from security import identity  # noqa: E402
from strands.types.session import SessionAgent, SessionMessage  # noqa: E402


class _Gmdp:
    def __init__(self, delay=0.0, gate=None):
        self.events = []
        self.delay = delay
        self.gate = gate
        self.lock = threading.Lock()

    def create_event(self, **kwargs):
        if self.gate is not None:
            self.gate.wait(5)
        time.sleep(self.delay)
        with self.lock:
            self.events.append(copy.deepcopy(kwargs))
        return {"eventId": f"e{len(self.events)}"}


class _NullClient:
    def create_event(self, **kwargs):
        return {"eventId": "init"}


class _FakeMemoryClient:
    def __init__(self, region_name=None):
        self.gmdp_client = _NullClient()
        self.gmcp_client = _NullClient()

    def list_events(self, **kwargs):
        return []


class _FakeBotoSession:
    region_name = "us-west-2"

    def client(self, *args, **kwargs):
        return _NullClient()



def _manager(*, batch_size=50, async_writes=True, gmdp=None):
    """A real FramedMemorySessionManager whose AWS clients are fakes (no network)."""
    import bedrock_agentcore.memory.integrations.strands.session_manager as sdk

    cfg = identity.build_memory_config(memory_id="m", session_id="s", actor_id="a",
                                       enable_retrieval=False, batch_size=batch_size)
    real = sdk.MemoryClient
    sdk.MemoryClient = _FakeMemoryClient
    try:
        mgr = mc.FramedMemorySessionManager(agentcore_memory_config=cfg, region_name="us-west-2",
                                            boto_session=_FakeBotoSession(), async_writes=async_writes)
    finally:
        sdk.MemoryClient = real
    if mgr.async_writes:
        mgr.finish_writes()  # drain anything the init itself queued
    mgr.memory_client.gmdp_client = gmdp or _Gmdp()
    return mgr


def _msg(i):
    return SessionMessage.from_message({"role": "user", "content": [{"text": f"m{i}"}]}, 0)


def _texts(gmdp):
    out = []
    for ev in gmdp.events:
        for item in ev["payload"]:
            if "conversational" in item:
                out.append(json.loads(item["conversational"]["content"]["text"])["message"]["content"][0]["text"])
    return out


def test_config_carries_the_batch_size():
    assert identity.build_memory_config(memory_id="m", session_id="s", actor_id="a").batch_size == 1
    assert identity.build_memory_config(memory_id="m", session_id="s", actor_id="a",
                                        batch_size=50).batch_size == 50
    assert identity.build_memory_config(memory_id="m", session_id="s", actor_id="a",
                                        batch_size=500).batch_size == 100


def test_create_message_does_not_wait_for_the_network():
    gmdp = _Gmdp(delay=0.3)
    mgr = _manager(gmdp=gmdp)
    started = time.monotonic()
    for i in range(3):
        mgr.create_message("s", "agent", _msg(i))
    assert time.monotonic() - started < 0.2      # inline writes would take >= 0.9s
    mgr.finish_writes()
    assert _texts(gmdp) == ["m0", "m1", "m2"]     # all stored, in order


def test_a_message_added_mid_flush_is_not_dropped():
    gate = threading.Event()
    gmdp = _Gmdp(gate=gate)
    mgr = _manager(gmdp=gmdp)
    mgr.create_message("s", "agent", _msg(0))     # background flush starts, blocks on the gate
    time.sleep(0.1)
    mgr.create_message("s", "agent", _msg(1))     # appended while that flush is in flight
    gate.set()
    mgr.finish_writes()
    assert _texts(gmdp) == ["m0", "m1"]


def test_the_message_is_serialized_when_added():
    """A later in-memory edit of the same message must not leak into what is stored."""
    gate = threading.Event()
    gmdp = _Gmdp(gate=gate)
    mgr = _manager(gmdp=gmdp)
    raw = {"role": "user", "content": [{"text": "original"}]}
    mgr.create_message("s", "agent", SessionMessage.from_message(raw, 0))
    raw["content"].insert(0, {"text": "<user_context>injected</user_context>"})
    gate.set()
    mgr.finish_writes()
    stored = json.loads(gmdp.events[0]["payload"][0]["conversational"]["content"]["text"])
    assert stored["message"]["content"] == [{"text": "original"}]


def test_only_the_latest_agent_state_is_sent():
    gate = threading.Event()
    gmdp = _Gmdp(gate=gate)
    mgr = _manager(gmdp=gmdp)
    now = datetime.now(timezone.utc).isoformat()
    for v in range(3):
        agent = SessionAgent(agent_id="agent", state={"v": v}, conversation_manager_state={},
                             created_at=now, updated_at=now)
        mgr.create_agent("s", agent)
    gate.set()
    mgr.finish_writes()
    blobs = [json.loads(item["blob"]) for ev in gmdp.events for item in ev["payload"] if "blob" in item]
    # One state per event, and the last one stored is the newest (read_agent reads payload[0]).
    assert all(len(ev["payload"]) == 1 for ev in gmdp.events)
    assert blobs[-1]["state"] == {"v": 2}


def test_sdk_default_path_is_untouched():
    gmdp = _Gmdp()
    mgr = _manager(batch_size=1, async_writes=True, gmdp=gmdp)
    assert mgr.async_writes is False              # needs batch_size > 1
    mgr.memory_client.create_event = lambda **kw: gmdp.create_event(**kw)
    mgr.create_message("s", "agent", _msg(0))     # inline, as before
    assert len(gmdp.events) == 1


def test_main_turns_async_writes_on_and_drains_at_turn_end():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src" / "main.py").read_text()
    assert '"async_writes": MEMORY_ASYNC_WRITES' in src
    assert "batch_size=MEMORY_WRITE_BATCH_SIZE if MEMORY_ASYNC_WRITES else 1" in src
    assert "await asyncio.to_thread(session_manager.finish_writes)" in src
    assert src.index("session_manager.finish_writes") < src.index("yield json.dumps(_summary)")


# --- retrieval prefetch -------------------------------------------------------------------------

class _Retriever:
    def __init__(self, delay=0.0):
        self.calls = 0
        self.delay = delay

    def retrieve_memories(self, **kwargs):
        self.calls += 1
        time.sleep(self.delay)
        return [{"content": {"text": "prefers mp4"}, "score": 0.9}]


def _retrieving_manager(client):
    mgr = mc.FramedMemorySessionManager.__new__(mc.FramedMemorySessionManager)
    mgr.config = identity.build_memory_config(memory_id="m", session_id="s", actor_id="a")
    mgr.memory_client = client
    return mgr


def test_prefetch_serves_the_matching_turn():
    client = _Retriever(delay=0.2)
    mgr = _retrieving_manager(client)
    mgr.prefetch("Trim the clip")
    msgs = [{"role": "user", "content": [{"text": "Trim the clip"}]}]
    mgr.retrieve_customer_context(SimpleNamespace(agent=SimpleNamespace(messages=msgs)))
    assert client.calls == 1                      # no second, live retrieval
    assert msgs[0]["content"][0]["text"].startswith(mc.REMEMBERED_CONTEXT_OPEN)
    assert msgs[0]["content"][1] == {"text": "Trim the clip"}


def test_prefetch_for_another_query_falls_back_to_live_retrieval():
    client = _Retriever()
    mgr = _retrieving_manager(client)
    mgr.prefetch("something else")
    time.sleep(0.05)
    msgs = [{"role": "user", "content": [{"text": "Trim the clip"}]}]
    mgr.retrieve_customer_context(SimpleNamespace(agent=SimpleNamespace(messages=msgs)))
    assert client.calls == 2
    assert msgs[0]["content"][0]["text"].startswith(mc.REMEMBERED_CONTEXT_OPEN)


def test_prefetch_is_a_noop_without_retrieval():
    mgr = _retrieving_manager(_Retriever())
    mgr.config = identity.build_memory_config(memory_id="m", session_id="s", actor_id="a",
                                              enable_retrieval=False)
    mgr.prefetch("x")
    assert mgr._prefetch is None
