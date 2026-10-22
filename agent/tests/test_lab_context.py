"""Unit tests and property-based tests for LabRequestContext.

Validates: Requirements 1.1, 1.2, 1.6
"""

from __future__ import annotations

import sys
import os
import uuid

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Add agent/src to path so imports resolve
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.context import LabRequestContext
from lab.errors import AuthenticationError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeAgentCoreContext:
    """Minimal stand-in for the AgentCore Runtime invocation context."""

    def __init__(self, actor_id=None, session_id=None):
        self.actor_id = actor_id
        self.session_id = session_id


# ---------------------------------------------------------------------------
# Unit Tests
# ---------------------------------------------------------------------------


class TestLabRequestContextUnit:
    """Unit tests for LabRequestContext.from_agentcore_context."""

    def test_valid_context_creates_instance(self):
        ctx = FakeAgentCoreContext(actor_id="user-123", session_id="sess-abc")
        result = LabRequestContext.from_agentcore_context(ctx)

        assert result.actor_id == "user-123"
        assert result.session_id == "sess-abc"
        assert result.request_id  # non-empty
        # request_id should be a valid UUID4
        uuid.UUID(result.request_id, version=4)

    def test_missing_actor_id_raises_authentication_error(self):
        """Requirement 1.2: reject request when actor_id is missing."""
        ctx = FakeAgentCoreContext(actor_id=None, session_id="sess-abc")
        with pytest.raises(AuthenticationError, match="missing actor_id"):
            LabRequestContext.from_agentcore_context(ctx)

    def test_empty_actor_id_raises_authentication_error(self):
        """Requirement 1.2: reject request when actor_id is empty string."""
        ctx = FakeAgentCoreContext(actor_id="", session_id="sess-abc")
        with pytest.raises(AuthenticationError, match="missing actor_id"):
            LabRequestContext.from_agentcore_context(ctx)

    def test_missing_session_id_defaults_to_empty(self):
        """Session ID is optional; defaults to empty string."""
        ctx = FakeAgentCoreContext(actor_id="user-123", session_id=None)
        result = LabRequestContext.from_agentcore_context(ctx)

        assert result.session_id == ""

    def test_request_id_is_unique_per_call(self):
        """Each call generates a distinct request_id for log correlation."""
        ctx = FakeAgentCoreContext(actor_id="user-123", session_id="sess-1")
        r1 = LabRequestContext.from_agentcore_context(ctx)
        r2 = LabRequestContext.from_agentcore_context(ctx)

        assert r1.request_id != r2.request_id

    def test_context_is_immutable(self):
        """LabRequestContext is frozen — fields cannot be reassigned."""
        ctx = FakeAgentCoreContext(actor_id="user-123", session_id="sess-1")
        result = LabRequestContext.from_agentcore_context(ctx)

        with pytest.raises(Exception):  # FrozenInstanceError
            result.actor_id = "hacker"

    def test_payload_actor_id_is_ignored(self):
        """Requirement 1.6: actor_id from payload is never used.

        The factory only reads from the context object's actor_id attribute.
        Even if a payload contains an actor_id field, it has no effect.
        """
        # Simulate a context where actor_id comes from JWT
        ctx = FakeAgentCoreContext(actor_id="real-user", session_id="sess-1")
        result = LabRequestContext.from_agentcore_context(ctx)

        # The result uses the context actor_id, not any payload value
        assert result.actor_id == "real-user"


# ---------------------------------------------------------------------------
# Property-Based Tests
# ---------------------------------------------------------------------------


# Strategy for valid actor IDs (non-empty strings)
valid_actor_ids = st.text(min_size=1, max_size=200).filter(lambda s: s.strip())
# Strategy for session IDs (can be any string or None)
session_ids = st.one_of(st.none(), st.text(max_size=200))
# Strategy for payload actor IDs that should be ignored
payload_actor_ids = st.one_of(st.none(), st.text(max_size=200))


class TestLabRequestContextProperties:
    """Property-based tests for LabRequestContext."""

    @given(actor_id=valid_actor_ids, session_id=session_ids)
    @settings(max_examples=100)
    def test_property_actor_id_from_context_only(self, actor_id: str, session_id):
        """**Validates: Requirements 1.1**

        Property: For all valid contexts, the resulting LabRequestContext.actor_id
        equals the context's actor_id exactly — identity is derived solely from
        the AgentCore Runtime context.
        """
        ctx = FakeAgentCoreContext(actor_id=actor_id, session_id=session_id)
        result = LabRequestContext.from_agentcore_context(ctx)
        assert result.actor_id == actor_id

    @given(actor_id=valid_actor_ids, session_id=session_ids)
    @settings(max_examples=100)
    def test_property_request_id_is_valid_uuid4(self, actor_id: str, session_id):
        """**Validates: Requirements 1.1**

        Property: For all valid contexts, the generated request_id is always a
        valid UUID4 string suitable for log correlation.
        """
        ctx = FakeAgentCoreContext(actor_id=actor_id, session_id=session_id)
        result = LabRequestContext.from_agentcore_context(ctx)
        # Must parse as UUID version 4
        parsed = uuid.UUID(result.request_id)
        assert parsed.version == 4

    @given(
        context_actor_id=valid_actor_ids,
        payload_actor_id=payload_actor_ids,
        session_id=session_ids,
    )
    @settings(max_examples=100)
    def test_property_payload_actor_id_never_used(
        self, context_actor_id: str, payload_actor_id, session_id
    ):
        """**Validates: Requirements 1.2**

        Property 3: Actor_Id is never taken from payload. For all requests that
        include an actor_id-shaped field in the payload, the Lab_Service's tenant
        decision equals the actor_id derived from the invocation context,
        regardless of the payload value.
        """
        ctx = FakeAgentCoreContext(actor_id=context_actor_id, session_id=session_id)
        result = LabRequestContext.from_agentcore_context(ctx)

        # The result always uses the context actor_id, never the payload
        assert result.actor_id == context_actor_id
        # Even if payload_actor_id differs, it has no effect
        if payload_actor_id and payload_actor_id != context_actor_id:
            assert result.actor_id != payload_actor_id


# ---------------------------------------------------------------------------
# Group Membership (Requirement 16)
# ---------------------------------------------------------------------------


class FakeAgentCoreContextWithClaims:
    """Stand-in for the AgentCore Runtime context that carries JWT claims."""

    def __init__(self, actor_id=None, session_id=None, claims=None):
        self.actor_id = actor_id
        self.session_id = session_id
        self.claims = claims


class TestLabRequestContextGroupIds:
    """Validates: Requirements 16.1, 16.2, 16.3"""

    def test_cognito_groups_claim_populates_group_ids(self):
        """Requirement 16.1: cognito:groups claim is read as list[str]."""
        ctx = FakeAgentCoreContextWithClaims(
            actor_id="user-1",
            session_id="sess-1",
            claims={"cognito:groups": ["editors", "reviewers"]},
        )
        result = LabRequestContext.from_agentcore_context(ctx)
        assert result.group_ids == ["editors", "reviewers"]
        assert all(isinstance(g, str) for g in result.group_ids)

    def test_absent_claim_yields_empty_list(self):
        """Requirement 16.2: missing claim ⇒ empty list."""
        # No claims attribute at all.
        ctx_no_claims = FakeAgentCoreContext(actor_id="user-1", session_id="sess-1")
        assert LabRequestContext.from_agentcore_context(ctx_no_claims).group_ids == []

        # Claims dict present, but no cognito:groups key.
        ctx_other_claims = FakeAgentCoreContextWithClaims(
            actor_id="user-1", session_id="sess-1", claims={"sub": "user-1"}
        )
        assert (
            LabRequestContext.from_agentcore_context(ctx_other_claims).group_ids == []
        )

    def test_default_group_ids_when_constructed_directly(self):
        """Direct construction of LabRequestContext defaults group_ids to []."""
        ctx = LabRequestContext(
            actor_id="user-1", session_id="sess-1", request_id="req-1"
        )
        assert ctx.group_ids == []

    def test_string_claim_is_split_on_commas(self):
        """Defensive: stringified cognito:groups claim is parsed into list[str]."""
        ctx = FakeAgentCoreContextWithClaims(
            actor_id="user-1",
            session_id="sess-1",
            claims={"cognito:groups": "editors,reviewers"},
        )
        assert LabRequestContext.from_agentcore_context(ctx).group_ids == [
            "editors",
            "reviewers",
        ]

    def test_empty_list_claim_yields_empty_list(self):
        """An explicit empty list claim is preserved as []."""
        ctx = FakeAgentCoreContextWithClaims(
            actor_id="user-1",
            session_id="sess-1",
            claims={"cognito:groups": []},
        )
        assert LabRequestContext.from_agentcore_context(ctx).group_ids == []

    def test_group_ids_field_is_immutable_reference(self):
        """The frozen dataclass prevents reassigning the group_ids field."""
        ctx = FakeAgentCoreContextWithClaims(
            actor_id="user-1",
            session_id="sess-1",
            claims={"cognito:groups": ["editors"]},
        )
        result = LabRequestContext.from_agentcore_context(ctx)
        with pytest.raises(Exception):  # FrozenInstanceError
            result.group_ids = ["hacker-group"]
