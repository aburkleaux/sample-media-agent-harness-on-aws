"""Task 1.3 — SpecialtyAgent (Custom Agent) extended serialization tests.

Validates that a ``SpecialtyAgent`` carrying the Custom Agent extension fields
(``model``, ``tool_collections``, ``integration_config``) serializes to the
AssetIndex DDB item shape and reconstructs losslessly across any combination of
those fields being present or absent.

**Validates: Requirements 1.6, 2.1, 3.1, 8.1**
"""

from __future__ import annotations

import os
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

# Add agent/src to path so imports resolve
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.models import (  # noqa: E402
    IntegrationConfig,
    SpecialtyAgent,
    ToolCollectionSnapshot,
    ToolRef,
    from_index_item,
    to_index_item,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_ids = st.text(min_size=1, max_size=24)
_opt_text = st.one_of(st.none(), st.text(max_size=64))

# IntegrationConfig varies enabled, issuer/audience presence, and the
# allowed_client_ids list; None models "external access not configured".
_integration_configs = st.one_of(
    st.none(),
    st.builds(
        IntegrationConfig,
        enabled=st.booleans(),
        issuer=_opt_text,
        audience=_opt_text,
        allowed_client_ids=st.lists(_ids, max_size=4),
    ),
)


_tool_refs = st.builds(
    ToolRef,
    name=st.text(min_size=1, max_size=32),
    version=st.text(min_size=1, max_size=8),
)

# Three-way, and the three are NOT interchangeable: `None` means the row never
# pinned its collections and must resolve them live, `[]` means it was published
# with none and must bind nothing, and a populated list is the frozen tool set.
# A round trip that collapsed None into [] would silently unpin a version.
_tool_collection_snapshots = st.one_of(
    st.none(),
    st.just([]),
    st.lists(
        st.builds(
            ToolCollectionSnapshot,
            asset_id=_ids,
            name=st.text(max_size=32),
            tool_refs=st.lists(_tool_refs, max_size=3),
        ),
        min_size=1,
        max_size=3,
    ),
)


@st.composite
def specialty_agents(draw) -> SpecialtyAgent:
    """Generate arbitrary SpecialtyAgent configs.

    The three Custom Agent extension fields independently vary between
    present and absent so the round-trip is exercised across every
    presence combination.
    """
    version = draw(st.one_of(st.none(), st.integers(min_value=1, max_value=10**6)))
    status = draw(st.sampled_from(["draft", "published", "deprecated"]))
    scope = draw(st.sampled_from(["private", "group", "org"]))
    return SpecialtyAgent(
        asset_id=draw(_ids),
        category="specialty-agent",
        name=draw(st.text(max_size=48)),
        status=status,
        scope=scope,
        s3_prefix=draw(st.text(min_size=1, max_size=64)),
        created_at=draw(st.integers(min_value=0, max_value=10**13)),
        updated_at=draw(st.integers(min_value=0, max_value=10**13)),
        version=version,
        deprecated=draw(st.booleans()),
        current=draw(st.booleans()),
        compatible_skills=draw(st.lists(_ids, max_size=4)),
        # Custom Agent extension fields — vary presence independently.
        model=draw(st.one_of(st.none(), st.text(min_size=1, max_size=48))),
        tool_collections=draw(st.lists(_ids, max_size=4)),
        tool_collection_snapshots=draw(_tool_collection_snapshots),
        integration_config=draw(_integration_configs),
    )


# ---------------------------------------------------------------------------
# Property-Based Test
# ---------------------------------------------------------------------------

# Feature: custom-agents, Property 1: Extended asset serialization round-trip


class TestSpecialtyAgentSerializationRoundTrip:
    """Property-based tests for SpecialtyAgent extended serialization."""

    @given(agent=specialty_agents())
    @settings(max_examples=200)
    def test_property_extended_serialization_round_trip(self, agent: SpecialtyAgent):
        """**Validates: Requirements 1.6, 2.1, 3.1, 8.1**

        Property 1: For any SpecialtyAgent with any combination of ``model``,
        ``tool_collections``, and ``integration_config``, converting it to an
        AssetIndex item and back via ``to_index_item``/``from_index_item``
        yields an equivalent asset, preserving all extended fields.
        """
        item = to_index_item(
            agent,
            owner_user_id="u-test",
            owner_group_id=None,
            owner_org_id=None,
        )
        restored = from_index_item(item)

        assert isinstance(restored, SpecialtyAgent)
        assert restored == agent
