"""Each plan node gets its OWN code-interpreter sandbox.

Session 27c33c57: all 7 trim nodes ran in one microVM, and sandbox sessions are resolved BY NAME
from a module-level map, so nodes that passed no session_name (the instance default) or the same
one ("ffmpeg-trim") shared a sandbox — one filesystem. Against the real Code Interpreter, two
concurrent nodes writing and reading back /tmp/src.mp4 read the OTHER node's file in 2 of 2 runs.
"""
from __future__ import annotations

import asyncio
import contextvars

import sandbox_scope
from tool_input_normalize import NormalizedCodeInterpreterTool


def _ci_use(session_name=None, action_type="executeCode"):
    action = {"type": action_type, "code": "print(1)", "language": "python"}
    if session_name is not None:
        action["session_name"] = session_name
    return {"toolUseId": "t1", "name": "code_interpreter", "input": {"code_interpreter_input": {"action": action}}}


def _session_of(tool_use):
    return tool_use["input"]["code_interpreter_input"]["action"].get("session_name")


def test_no_scope_changes_nothing():
    """The orchestrator's own turns keep the shared, persistent sandbox they rely on."""
    use = _ci_use("analysis")
    assert sandbox_scope.scope_tool_use(use) is use
    use = _ci_use()
    assert sandbox_scope.scope_tool_use(use) is use


def test_concurrent_nodes_never_resolve_to_the_same_sandbox():
    """The bug: two nodes, same (or no) session_name → one shared sandbox. Now: always distinct."""
    seen = {}
    for node in ("trim_shot3", "trim_shot4"):
        token = sandbox_scope.bind(sandbox_scope.node_scope("dcc2a012-f9b3", node))
        try:
            seen[node] = (
                _session_of(sandbox_scope.scope_tool_use(_ci_use("ffmpeg-trim"))),
                _session_of(sandbox_scope.scope_tool_use(_ci_use())),
            )
        finally:
            sandbox_scope.reset(token)
    a, b = seen["trim_shot3"], seen["trim_shot4"]
    assert a[0] != b[0], "the same model-chosen name must not collide across nodes"
    assert a[1] != b[1], "the implicit default session must not be shared across nodes"


def test_a_nodes_own_calls_still_share_one_sandbox():
    """Download once, cut many: successive calls IN one node must land in the same sandbox."""
    token = sandbox_scope.bind(sandbox_scope.node_scope("plan1234", "clip_all"))
    try:
        first = _session_of(sandbox_scope.scope_tool_use(_ci_use("work")))
        again = _session_of(sandbox_scope.scope_tool_use(_ci_use("work")))
        default_1 = _session_of(sandbox_scope.scope_tool_use(_ci_use()))
        default_2 = _session_of(sandbox_scope.scope_tool_use(_ci_use()))
    finally:
        sandbox_scope.reset(token)
    assert first == again
    assert default_1 == default_2


def test_scoping_is_idempotent_and_within_the_name_limit():
    token = sandbox_scope.bind(sandbox_scope.node_scope("p" * 40, "n" * 200))
    try:
        once = sandbox_scope.scope_tool_use(_ci_use("x" * 300))
        twice = sandbox_scope.scope_tool_use(once)
    finally:
        sandbox_scope.reset(token)
    assert _session_of(once) == _session_of(twice)
    assert len(_session_of(once)) <= 100  # StartCodeInterpreterSession.name max


def test_list_local_sessions_is_left_alone():
    token = sandbox_scope.bind("pn-x-y")
    try:
        use = _ci_use(action_type="listLocalSessions")
        assert sandbox_scope.scope_tool_use(use) is use
    finally:
        sandbox_scope.reset(token)


def test_wrapper_applies_the_scope_through_asyncio_to_thread():
    """The real call path: _prepare runs via asyncio.to_thread (private CDN). The contextvar must
    survive that hop, or the scope silently does nothing in staging."""
    captured = {}

    class _Inner:
        tool_name = "code_interpreter"
        tool_spec = {"name": "code_interpreter"}
        tool_type = "python"

        async def stream(self, tool_use, invocation_state, **kwargs):
            captured["session"] = _session_of(tool_use)
            yield {"done": True}

    tool = NormalizedCodeInterpreterTool(_Inner())

    async def _run():
        token = sandbox_scope.bind("pn-abc-trim_shot3")
        try:
            # Force the to_thread branch the private-CDN environments take.
            prepared = await asyncio.to_thread(tool._prepare, _ci_use("ffmpeg-trim"))
            async for _ in tool._inner.stream(prepared, {}):
                pass
        finally:
            sandbox_scope.reset(token)

    asyncio.run(_run())
    assert captured["session"] == "pn-abc-trim_shot3--ffmpeg-trim"


def test_release_stops_only_this_nodes_sandboxes():
    stopped = []

    class _Client:
        def __init__(self, name):
            self.name = name

        def stop(self):
            stopped.append(self.name)

    class _Info:
        def __init__(self, name):
            self.client = _Client(name)

    class _CI:
        _sessions = {
            "pn-a-n1--ffmpeg": _Info("pn-a-n1--ffmpeg"),
            "pn-a-n1--default": _Info("pn-a-n1--default"),
            "pn-a-n2--ffmpeg": _Info("pn-a-n2--ffmpeg"),  # a CONCURRENT node: must survive
            "analysis": _Info("analysis"),  # an unscoped (orchestrator) session: must survive
        }

    ci = _CI()
    n = sandbox_scope.release(ci, "pn-a-n1")
    assert n == 2
    assert sorted(stopped) == ["pn-a-n1--default", "pn-a-n1--ffmpeg"]
    assert set(ci._sessions) == {"pn-a-n2--ffmpeg", "analysis"}


def test_release_never_raises():
    class _Boom:
        def stop(self):
            raise RuntimeError("already stopped")

    class _Info:
        client = _Boom()

    class _CI:
        _sessions = {"pn-z--x": _Info()}

    assert sandbox_scope.release(_CI(), "pn-z") == 0
    assert sandbox_scope.release(None, "pn-z") == 0
    assert sandbox_scope.release(_CI(), "") == 0


def test_scope_is_per_context_not_global():
    """Two nodes run as two tasks in one process; each must see only its own scope."""
    results = {}

    async def node(name):
        token = sandbox_scope.bind(f"pn-x-{name}")
        try:
            await asyncio.sleep(0.01)
            results[name] = _session_of(sandbox_scope.scope_tool_use(_ci_use()))
        finally:
            sandbox_scope.reset(token)

    async def main():
        await asyncio.gather(node("a"), node("b"))

    asyncio.run(main())
    assert results == {"a": "pn-x-a--default", "b": "pn-x-b--default"}
    assert sandbox_scope.current() is None
