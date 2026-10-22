"""Evidence gate: is the MCP session-reboot wrapper still needed on mcp 2.x?

Context
-------
The gateway session-reboot machinery — ``GatewayConnection.reconnect`` /
``_session_alive`` and the ``jobs.strands_tool_wrap`` resilient wrapper — was added
to survive a gateway idle-closing strands' long-lived streamable-HTTP session
server-side (the ``fal`` video-gen infinite-loop root cause). Two earlier commits
labelled it an interim workaround "until Strands unpins mcp<2.0.0 and a stateless
client removes the long-lived session."

We are now ON mcp 2.1.1 (the 2026-07-28 protocol is stateless), so the natural
question is "can we delete the reboot code?" The answer hinges on ONE empirical fact,
not on the version bump: **does strands transparently recover a dead session on the
next call, or does it still raise?** These tests pin that fact so the decision is made
on evidence — and act as a CANARY: if a future strands starts self-recovering, the
assertions here flip and tell us the wrapper can be reconsidered.

Removal is safe ONLY when ``test_strands_still_raises_on_a_dead_session`` starts
failing (i.e. strands no longer raises — it recovered on its own).
"""
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from jobs.strands_tool_wrap import _is_session_down_error  # noqa: E402


def _dead_session_client():
    """A real strands ``MCPClient`` whose session is NOT running.

    A never-started client is the same OBSERVABLE state as a session the gateway
    idle-closed server-side before reconnect: ``_is_session_active()`` is False and
    the next call must go through strands' own "session is not running" guard. We use
    a bogus url so no network is touched even if the guard ever regressed.
    """
    from strands.tools.mcp.mcp_client import MCPClient
    return MCPClient(url="http://127.0.0.1:9")


class TestStrandsRecoveryBehavior:
    """The empirical basis for keeping (or one day removing) the reboot wrapper."""

    def test_strands_reports_a_dead_session_as_not_active(self):
        c = _dead_session_client()
        assert c._is_session_active() is False, (
            "strands' liveness hook changed; the reboot code keys off _is_session_active"
        )

    def test_strands_still_raises_on_a_dead_session(self):
        """CANARY. On mcp 2.1.1 strands does NOT transparently recover — it raises.

        This is the whole reason ``GatewayConnection.reconnect`` + ``wrap_resilient``
        still exist despite the "stateless MCP" upgrade: strands' MCPClient is still a
        long-lived session and a call against a dead one raises rather than reopening.

        If this test ever fails because NO error is raised, strands has begun
        self-recovering dead sessions — at THAT point re-evaluate deleting the reboot
        code (see GatewayConnection.reconnect docstring). Do not delete it while this
        test still passes.
        """
        c = _dead_session_client()
        with pytest.raises(BaseException) as exc_info:
            c.call_tool_sync("tid", "any_tool", {})
        # It must be a *pre-request* session-down error (nothing reached the upstream),
        # which is exactly what our wrapper is allowed to reconnect+retry once.
        assert _is_session_down_error(exc_info.value), (
            f"strands raised {type(exc_info.value).__name__} that our resilience "
            f"classifier does not treat as session-down: {exc_info.value!r}. Either "
            "strands changed its error, or recovery would now wrongly retry a request "
            "that reached the upstream — revisit _is_session_down_error."
        )

    def test_our_classifier_is_narrow_and_does_not_match_upstream_errors(self):
        """Recovery must NOT fire for errors that reached the upstream (double-submit risk)."""
        assert not _is_session_down_error(RuntimeError("ThrottlingException: rate exceeded"))
        assert not _is_session_down_error(ValueError("tool returned an error"))
        assert not _is_session_down_error(RuntimeError("HTTP 500 from server"))
