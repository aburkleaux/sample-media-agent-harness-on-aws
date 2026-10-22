"""Forwarding the caller's token to sub-agent runtimes.

Live symptom: the main agent connected the user_jwt gateway and the catalog
listed 12 tools, but plan_and_execute reported them unreachable. Dispatch invokes
the sub-agent runtime with a payload and no headers, and the task agent's loader
took no token at all -- so the sub-agent doing the work had no credential for the
gateway holding the user's tools.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from jobs.context import set_user_token, current_user_token


class TestTokenContext:
    def test_bind_and_read(self):
        set_user_token("tok-abc")
        assert current_user_token() == "tok-abc"

    def test_absent_returns_empty_not_raises(self):
        """Unlike current_actor_id, a missing token is normal -- management calls
        carry none -- and callers degrade by skipping the gateway."""
        set_user_token("")
        assert current_user_token() == ""

    def test_none_is_coerced(self):
        set_user_token(None)  # type: ignore[arg-type]
        assert current_user_token() == ""


class TestDispatchPayload:
    def _payload(self, token):
        """Reproduce the dispatch payload contribution."""
        set_user_token(token)
        payload = {}
        tok = current_user_token()
        if tok:
            payload["user_token"] = tok
        return payload

    def test_token_is_forwarded_when_present(self):
        assert self._payload("tok-xyz")["user_token"] == "tok-xyz"

    def test_key_is_absent_not_empty_when_there_is_no_token(self):
        """An empty string would look like a credential to the sub-agent and make
        it attempt a connect that must fail."""
        assert "user_token" not in self._payload("")


class TestTaskAgentAcceptsToken:
    def test_loader_signature_takes_actor_and_token(self):
        import inspect
        import task_agent
        params = inspect.signature(task_agent._ensure_gateways_loaded).parameters
        assert "actor_id" in params and "user_token" in params
        # Defaults must be empty so existing callers keep working.
        assert params["user_token"].default == ""
        assert params["actor_id"].default == ""


class TestTokenIsNotLeaked:
    def test_token_is_not_written_to_logs_in_dispatch(self):
        """The token is a credential. The actor id is safe to log; this is not."""
        src = open(os.path.join(os.path.dirname(__file__), "..", "src", "orchestration",
                                "dispatch_tool.py"), encoding="utf-8").read()
        i = src.index("user_token")
        window = src[max(0, i - 700):i + 700]
        for bad in ("logger.info(payload", "logger.debug(payload",
                    'logger.info("%s", payload', "print(payload"):
            assert bad not in window, bad

    def test_context_module_does_not_log_the_token(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "src",
                                "jobs", "context.py"), encoding="utf-8").read()
        start = src.index("def set_user_token")
        end = src.index("def current_actor_id")
        body = src[start:end]
        assert "logger" not in body and "print(" not in body
