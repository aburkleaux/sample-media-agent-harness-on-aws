"""Snapshot-friendly init: user-independent work at container start, the rest overlapped.

AgentCore Runtime V2 restores every new session from a snapshot taken after the container
started, so work done at START is paid once per version and work done on the first request is
paid by every session. Measured cold (staging 89f6be71): skills 3.1s, subsystems + model
catalog 2.7s, then four gateways connected one after another 7.5s -- all before the model call.

These tests drive ``main``'s real init functions with every AWS-facing constructor faked (each
with a realistic delay) and pin: what warm-up builds, what it must NOT capture (S3 skills, any
request state), that the lazy path still works without warm-up, and the overlap of the skill
load, the model catalog and the gateway connects on a first request.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402
import mcp_manager as mm  # noqa: E402

SKILLS_DELAY = 0.4
MODELS_DELAY = 0.3
CONNECT_DELAY = 0.3
N_GATEWAYS = 4


class _FakeSkillsManager:
    instances: list = []

    def __init__(self):
        self.preloaded = False
        self.plugin_created = False
        self.agent = None
        _FakeSkillsManager.instances.append(self)

    def preload_disk_skills(self):
        self.preloaded = True
        return 3

    def create_plugin(self):
        time.sleep(SKILLS_DELAY)          # the S3 part of the load
        self.plugin_created = True
        return SimpleNamespace(name="plugin")

    def set_agent(self, agent):
        self.agent = agent

    def list_skills(self):
        return []


class _FakeGateway:
    def __init__(self, gw_id):
        self.id = gw_id

    def awaiting_user_token(self):
        return False

    def connect(self):
        time.sleep(CONNECT_DELAY)
        return {"status": "connected", "tools": []}


class _FakeAgent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.system_prompt = ""


@pytest.fixture
def fresh_main(monkeypatch):
    """main with its init latches reset and every AWS-facing constructor faked."""
    _FakeSkillsManager.instances = []
    calls = {"discover": 0, "user_seen": []}

    def _discover():
        calls["discover"] += 1
        time.sleep(MODELS_DELAY)
        return [{"id": "m1", "name": "Model 1", "supportsEffort": False}]

    def _mcp_manager():
        m = mm.MCPManager()
        for i in range(N_GATEWAYS):
            m._gateways[f"g{i}"] = _FakeGateway(f"g{i}")
        return m

    for name, value in {
        "_STATIC_READY": False, "_AGENT_SUBSYSTEMS_READY": False, "_skills_load_future": None,
        "_models_discovered_at": None, "_models_refreshing": False, "_available_models": [],
        "_gateway_initialized": False, "agent": None, "skills_plugin": None, "skills_manager": None,
        "mcp_manager": None, "model": None, "code_interpreter": None, "s3_client": None,
        "_gateway_registry": None, "_job_registry": None, "harness_hook_provider": None,
        "_all_builtin_tools": {}, "_added_builtins": set(), "_trusted_builtins": set(),
        "GATEWAY_REGISTRY_TABLE": "", "JOB_REGISTRY_TABLE": "", "MEMORY_ID": "",
        "AgentCoreCodeInterpreter": lambda **kw: SimpleNamespace(code_interpreter=object()),
        "SkillsManager": _FakeSkillsManager, "MCPManager": _mcp_manager,
        "build_model": lambda **kw: SimpleNamespace(model_id=kw.get("model_id")),
        "Agent": _FakeAgent, "_build_hook_provider": lambda *a: SimpleNamespace(hooks=a),
        "discover_models": _discover,
        "_build_system_prompt": lambda *a, **k: "SYSTEM",
        "_rebuild_agent_tools": lambda *a, **k: None,
        "_publish_builtin_catalog": lambda: False,
    }.items():
        monkeypatch.setattr(main, name, value)
    monkeypatch.setattr(main.boto3, "client", lambda *a, **k: SimpleNamespace(service=a))
    monkeypatch.delenv("HARNESS_PLATFORM_GATEWAY_ID", raising=False)
    return calls


def test_warm_up_builds_the_static_subsystems_and_the_model_catalog(fresh_main):
    main.warm_up()
    assert main._STATIC_READY
    assert main.model is not None and main.mcp_manager is not None
    assert main.harness_hook_provider is not None
    # Per container, never snapshotted: the code interpreter names a random sandbox session.
    assert main.code_interpreter is None and not main._all_builtin_tools
    assert fresh_main["discover"] == 1 and main._available_models[0]["id"] == "m1"
    sm = _FakeSkillsManager.instances[0]
    assert sm.preloaded                      # bundled skills: parsed into the snapshot
    assert not sm.plugin_created             # S3-authored skills: NOT captured (they change)
    # Nothing request-scoped exists yet: no Agent, no gateway connection, no first request.
    assert main.agent is None and not main._gateway_initialized and not main._AGENT_SUBSYSTEMS_READY


def test_lazy_path_without_warm_up_is_complete(fresh_main):
    main._init_agent_subsystems()            # default: joins the skill load, as before
    assert main._STATIC_READY and main._AGENT_SUBSYSTEMS_READY
    assert isinstance(main.agent, _FakeAgent)
    assert main.agent.kwargs["plugins"] == [main.skills_plugin]
    assert _FakeSkillsManager.instances[0].agent is main.agent
    assert len(_FakeSkillsManager.instances) == 1
    assert main.code_interpreter is not None and "code_interpreter" in main._all_builtin_tools
    # Same insertion order as before: code_interpreter right after the http_request wrap.
    names = list(main._all_builtin_tools)
    assert names.index("code_interpreter") == len(names) - len(main.main_lab_tools.LAB_TOOLS) \
        - len(main.persistence_tool.PERSISTENCE_TOOLS) - 1


def test_first_request_overlaps_skills_models_and_connects(fresh_main):
    """No warm-up at all (worst case): the three slow things now run side by side."""
    sequential = SKILLS_DELAY + MODELS_DELAY + N_GATEWAYS * CONNECT_DELAY   # the old critical path

    async def first_request():
        main._init_agent_subsystems(wait_for_skills=False)
        await main._ensure_gateway_connected(actor_id="actor-1", user_token="")
        await main._await_skills_ready()

    started = time.monotonic()
    asyncio.run(first_request())
    elapsed = time.monotonic() - started
    assert isinstance(main.agent, _FakeAgent) and main.agent.system_prompt == "SYSTEM"
    assert fresh_main["discover"] == 1
    # Floor is the slowest single item (~0.4s); the old sequential sum is 1.9s.
    assert elapsed < 0.6 * sequential, f"first request took {elapsed:.2f}s vs {sequential:.2f}s sequential"


def test_first_request_after_warm_up_skips_static_work(fresh_main):
    main.warm_up()

    async def first_request():
        main._init_agent_subsystems(wait_for_skills=False)
        await main._ensure_gateway_connected(actor_id="actor-1", user_token="")
        await main._await_skills_ready()

    started = time.monotonic()
    asyncio.run(first_request())
    elapsed = time.monotonic() - started
    assert fresh_main["discover"] == 1                  # the snapshot's catalog is reused
    assert len(_FakeSkillsManager.instances) == 1       # one manager, built at warm-up
    assert elapsed < SKILLS_DELAY + CONNECT_DELAY + 0.2  # skills load || connects


def test_stale_catalog_refreshes_in_the_background(fresh_main, monkeypatch):
    main.warm_up()
    monkeypatch.setattr(main, "_models_discovered_at", time.time() - main._MODEL_CATALOG_MAX_AGE_S - 1)
    started = time.monotonic()
    main._ensure_model_catalog(block=False)
    assert time.monotonic() - started < 0.1             # never waits on the network
    deadline = time.monotonic() + 3
    while fresh_main["discover"] < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert fresh_main["discover"] == 2


def test_warm_up_runs_before_the_server_starts():
    src = (os.path.join(os.path.dirname(__file__), "..", "src", "main.py"))
    text = open(src).read()
    tail = text[text.index('if __name__ == "__main__":'):]
    assert tail.index("_run_container_warm_up()") < tail.index("app.run()")


def test_warm_up_never_raises(fresh_main, monkeypatch):
    def boom():
        raise RuntimeError("no network at init")

    monkeypatch.setattr(main, "_construct_static_subsystems", boom)
    main.warm_up()                                       # logged, not raised
    assert main._STATIC_READY                            # same latch semantics as before
