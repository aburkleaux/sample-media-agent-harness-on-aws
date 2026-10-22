"""Tool-ERROR results rejected by strict mcp 2.x validation are recovered, not masked.

Staging 2026-10-01: on protocol revision 2026-07-28 a ``tools/call`` result must carry
``resultType``; the gateway's error results did not, so ~60 fal calls surfaced only as
"2 validation errors for union[CallToolResult,InputRequiredResult]" and the model never saw the
server's actual message. These tests use mcp's own validator to raise the real exception.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pydantic  # noqa: E402
import pytest  # noqa: E402

import mcp_result_compat as mrc  # noqa: E402
from mcp.client import session as _mcp_session  # noqa: E402

ERROR_PAYLOAD = {"content": [{"type": "text", "text": "Model fal-ai/x not found on server"}], "isError": True}


def _real_validation_error(raw):
    try:
        _mcp_session._methods.validate_server_result("tools/call", "2026-07-28", dict(raw))
    except pydantic.ValidationError as e:
        return e
    raise AssertionError("expected mcp to reject the payload on 2026-07-28")


def test_production_exception_reproduces_on_2026_07_28():
    e = _real_validation_error(ERROR_PAYLOAD)
    assert "union[CallToolResult,InputRequiredResult]" in str(e)


def test_recovers_tool_error_with_full_text():
    r = mrc.recover_error_result(_real_validation_error(ERROR_PAYLOAD))
    assert r is not None
    assert getattr(r, "is_error", getattr(r, "isError", None)) is True
    assert r.content[0].text == "Model fal-ai/x not found on server"   # untruncated


def test_non_error_payload_is_not_recovered():
    bad_success = {"content": [{"type": "text", "text": "ok"}]}  # malformed SUCCESS: must not be masked
    try:
        _mcp_session._methods.validate_server_result("tools/call", "2026-07-28", dict(bad_success))
        pytest.skip("mcp accepts a success result without resultType on this version")
    except pydantic.ValidationError as e:
        assert mrc.recover_error_result(e) is None


def test_unrelated_exception_is_not_recovered():
    assert mrc.recover_error_result(RuntimeError("boom")) is None


def test_patched_compat_call_tool_returns_recovered_result(monkeypatch):
    from strands.tools.mcp import mcp_client as mc

    async def _raises(*a, **k):
        raise _real_validation_error(ERROR_PAYLOAD)

    # Install the shim over a fresh "original" that raises the production error.
    monkeypatch.setattr(mc, "compat_call_tool", _raises)
    assert mrc.install() is True
    result = asyncio.run(mc.compat_call_tool(None, "run_model", {}, None, None, None))
    assert result.content[0].text == "Model fal-ai/x not found on server"


def test_patched_compat_call_tool_reraises_other_validation_errors(monkeypatch):
    from strands.tools.mcp import mcp_client as mc

    class _M(pydantic.BaseModel):
        x: int

    async def _raises(*a, **k):
        _M.model_validate({"x": "nope"})

    monkeypatch.setattr(mc, "compat_call_tool", _raises)
    mrc.install()
    with pytest.raises(pydantic.ValidationError):
        asyncio.run(mc.compat_call_tool(None, "t", {}, None, None, None))


def test_install_is_idempotent_and_active_in_mcp_manager():
    import mcp_manager  # noqa: F401 — importing installs the shim
    from strands.tools.mcp import mcp_client as mc

    assert getattr(mc.compat_call_tool, mrc._PATCH_MARK, False) is True
    first = mc.compat_call_tool
    mrc.install()
    assert mc.compat_call_tool is first  # no double wrap
