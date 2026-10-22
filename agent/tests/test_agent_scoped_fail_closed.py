"""An agent-scoped invocation must never fall back to a generic all-tools run.

Parameterized over all three markers (``principal`` / ``agent_test`` /
``delegated``) AND over the two ways config resolution can fail, because they
take different branches and only one of them was originally closed:

  fetch-returns-none
      ``_fetch_external_custom_agent_config`` yields no config. The
      ``_agent_scoped`` guard catches this and emits ``agent_config_unresolved``.

  resolver-raises
      A config IS fetched, so the guard is satisfied — then
      ``_resolve_custom_agent_config`` raises. That exception is caught and
      logged "continuing with raw payload fields", so ``apply_to_payload``
      never runs and ``payload["tools"]`` is never written. Execution proceeds.

The second mode is the one that stayed open: ``requested_tools`` was
``payload.get("tools", None)`` and ``None`` means *load every built-in and every
connected gateway tool*. The reviewer measured it on the owner test console —
``agent_test`` bound 5/5 fake tools while ``delegated`` bound 0, and delegated
was safe only because ``invoke_custom_agent`` puts ``tools: []`` on the wire.

So these tests assert the BOUND TOOL COUNT, not just the emitted error code. An
error-code assertion passes while the hole is open, which is exactly how it
survived a per-task review.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

MARKERS = [
    pytest.param({"principal": {"issuer": "https://i", "sub": "s"}}, id="external"),
    pytest.param({"agent_test": True}, id="owner-test"),
    pytest.param({"delegated": True}, id="delegated"),
]

# A resolvable-looking config, so the `_agent_scoped` guard is satisfied and we
# reach the resolver (which we then make raise).
A_VALID_CONFIG = {
    "persona": "You are a transcript cleaner.",
    "model": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "skills": [],
    "tool_collections": ["tc-1"],
}


class _FakeTool:
    """Minimal stand-in: `_alias_oversized_tool_names` only reads `tool_name`."""

    def __init__(self, name: str) -> None:
        self.tool_name = name

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"<_FakeTool {self.tool_name}>"


def _install_tool_universe(monkeypatch, task_agent) -> int:
    """Give the runtime 3 built-ins + 2 gateway tools. Returns the total.

    Without a non-empty universe the "load all" branch would bind 0 tools
    anyway and the assertions below could not tell fail-closed from fail-open.
    """
    builtins = {f"builtin_{i}": _FakeTool(f"builtin_{i}") for i in range(3)}
    gateway = {f"gw_{i}": _FakeTool(f"gw_{i}") for i in range(2)}
    monkeypatch.setattr(task_agent, "_BUILTINS", builtins)
    monkeypatch.setattr(task_agent, "_all_gateway_tools_by_name", lambda: gateway)
    return len(builtins) + len(gateway)


def _install_capturing_agent(monkeypatch, task_agent) -> list[list]:
    """Replace ``Agent`` with a stub recording the tools bound to each instance.

    Returns the accumulator: one entry per ``Agent(...)`` construction. An
    invocation that returns before building an Agent leaves it empty.
    """
    captured: list[list] = []

    class _CapturingAgent:
        def __init__(self, **kwargs):
            captured.append(list(kwargs.get("tools") or []))

        def stream_async(self, _prompt):
            async def _empty():
                return
                yield  # pragma: no cover — makes this an async generator

            return _empty()

    monkeypatch.setattr(task_agent, "Agent", _CapturingAgent)
    monkeypatch.setattr(task_agent, "build_model", lambda **_kw: object())
    return captured


def _bound_tool_names(captured: list[list]) -> list[str]:
    return [t.tool_name for tools in captured for t in tools]


async def _drain(gen) -> list:
    return [e async for e in gen]


def _error_codes(events: list) -> list:
    codes = []
    for e in events:
        try:
            got = json.loads(e)
        except (TypeError, ValueError):
            continue
        if isinstance(got, dict):
            codes.append(got.get("error"))
    return codes


@pytest.fixture
def runtime(monkeypatch):
    """task_agent with gateways stubbed out and a capturing Agent installed."""
    import task_agent

    async def _no_gateways(*_args, **_kwargs):
        pass

    monkeypatch.setattr(task_agent, "_ensure_gateways_loaded", _no_gateways)
    total = _install_tool_universe(monkeypatch, task_agent)
    captured = _install_capturing_agent(monkeypatch, task_agent)
    return task_agent, captured, total


def _apply_failure_mode(monkeypatch, task_agent, mode: str) -> None:
    if mode == "fetch-returns-none":
        async def _no_config(_payload):
            return None

        monkeypatch.setattr(
            task_agent, "_fetch_external_custom_agent_config", _no_config
        )
    elif mode == "resolver-raises":
        async def _a_config(_payload):
            return dict(A_VALID_CONFIG)

        def _boom(**_kwargs):
            raise RuntimeError("tool collection lookup exploded")

        monkeypatch.setattr(
            task_agent, "_fetch_external_custom_agent_config", _a_config
        )
        monkeypatch.setattr(task_agent, "_resolve_custom_agent_config", _boom)
    else:  # pragma: no cover — guards a typo in the parametrize list
        raise AssertionError(f"unknown failure mode {mode!r}")


@pytest.mark.parametrize("mode", ["fetch-returns-none", "resolver-raises"])
@pytest.mark.parametrize("marker", MARKERS)
@pytest.mark.asyncio
async def test_agent_scoped_binds_no_tools_when_config_fails(
    marker, mode, runtime, monkeypatch
):
    """The load-bearing assertion: ZERO tools bound, however resolution failed.

    ``payload`` deliberately carries no ``tools`` key. The guarantee must come
    from the runtime, not from a well-behaved caller — ``buildAgentTestPayload``
    sends no ``tools`` at all, and relying on ``invoke_custom_agent`` sending
    ``tools: []`` is what masked this on the delegated path.
    """
    task_agent, captured, total = runtime
    _apply_failure_mode(monkeypatch, task_agent, mode)
    assert total == 5, "fixture sanity: an all-tools run would bind 5"

    payload = {"prompt": "do the thing", "agent_id": "ag-1", **marker}
    await _drain(task_agent.invoke(payload, None))

    assert _bound_tool_names(captured) == [], (
        f"agent-scoped invoke ({mode}) bound tools it was never given: "
        f"{_bound_tool_names(captured)}"
    )


@pytest.mark.parametrize("marker", MARKERS)
@pytest.mark.asyncio
async def test_unresolved_config_errors_instead_of_running_bare(
    marker, runtime, monkeypatch
):
    """The ``None``-config guard still emits its terminal error frame."""
    task_agent, captured, _total = runtime
    _apply_failure_mode(monkeypatch, task_agent, "fetch-returns-none")

    payload = {"prompt": "do the thing", "agent_id": "ag-1", **marker}
    events = await _drain(task_agent.invoke(payload, None))

    assert "agent_config_unresolved" in _error_codes(events), events
    # It returns BEFORE building an agent, so nothing runs generically.
    assert captured == []


@pytest.mark.asyncio
async def test_the_all_tools_branch_is_genuinely_reachable(runtime):
    """Control: the same fixture binds all 5 when the invoke is NOT agent-scoped.

    Without this, a fail-closed assertion of "0 tools" could be passing because
    the stubs never bind anything at all.
    """
    task_agent, captured, total = runtime

    payload = {"prompt": "hi"}  # no agent_id, no marker, no tools key
    await _drain(task_agent.invoke(payload, None))

    assert len(_bound_tool_names(captured)) == total, _bound_tool_names(captured)


@pytest.mark.asyncio
async def test_a_bare_dispatch_without_agent_id_is_untouched(runtime, monkeypatch):
    """No agent_id ⇒ not agent-scoped ⇒ existing dispatch behavior stands."""
    task_agent, _captured, _total = runtime

    payload = {"prompt": "hi", "tools": []}
    events = await _drain(task_agent.invoke(payload, None))
    assert "agent_config_unresolved" not in _error_codes(events)


@pytest.mark.asyncio
async def test_an_explicit_tool_list_still_binds_that_list(runtime, monkeypatch):
    """The coercion must not clobber a tool list the resolver DID produce."""
    task_agent, captured, _total = runtime
    _apply_failure_mode(monkeypatch, task_agent, "resolver-raises")

    payload = {
        "prompt": "do the thing",
        "agent_id": "ag-1",
        "agent_test": True,
        "tools": ["gw_0"],
    }
    await _drain(task_agent.invoke(payload, None))

    assert _bound_tool_names(captured) == ["gw_0"]
