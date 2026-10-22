"""Integration test for cross-caller / cross-issuer / cross-agent deny.

Feature: custom-agents, Task 12.6 (example / integration test — not
property-based, no min-iteration requirement).

Validates: Requirements 10.3

Requirement 10.3 (requirements.md): "IF a request would read or write session
or memory state belonging to a different End_User, a different issuer, or a
different agentId, THEN THE Task_Agent_Runtime SHALL deny that access."

How the deny is realized (design Component 5, ``agent/src/identity.py``):
the external AgentCore Memory ``actor_id``, ``session_id`` and ``namespace``
are all set to the single derived ``EXT#`` key for the caller's
``(agentId, issuer, sub)`` triple. Because a load/store operation is scoped to
that exact key, a request keyed by one triple can only ever touch memory under
its own derived key. "Cross-triple deny" is therefore realized structurally by
the keys being *distinct*: an operation issued under the base triple's key
targets a different actor/session/namespace than any variation's key, so it
cannot read or write the variation's state (and vice versa).

This test demonstrates the deny semantics at the key/config level by building
the key and the ``AgentCoreMemoryConfig`` for a base triple and for three
variations — different ``sub``, different ``agentId``, and (critically) the
same ``sub`` under a different ``issuer`` — and asserting each variation's
key/config is disjoint from the base. The "same sub, different issuer" case is
included explicitly because a bare-``sub`` key would wrongly collide there;
here we assert it does NOT collide.
"""

from __future__ import annotations

import os
import sys

# Add agent/src to path so imports resolve (mirrors the other agent tests).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from security.identity import (  # noqa: E402
    EXTERNAL_KEYSPACE_PREFIX,
    build_external_memory_config,
    derive_external_memory_key,
)


# ---------------------------------------------------------------------------
# Fixtures: a base triple and the three deny variations.
# ---------------------------------------------------------------------------

MEMORY_ID = "mem-shared-task-agent"

# Base caller: agent `a`, issuer `i1`, subject `u1`.
BASE = ("agent-a", "https://issuer-one.example.com", "user-1")

# Variation 1: same agent + issuer, DIFFERENT sub (cross-caller).
DIFF_SUB = ("agent-a", "https://issuer-one.example.com", "user-2")

# Variation 2: DIFFERENT agent, same issuer + sub (cross-agent).
DIFF_AGENT = ("agent-a2", "https://issuer-one.example.com", "user-1")

# Variation 3: same agent + sub, DIFFERENT issuer (cross-issuer). A bare-`sub`
# key would collide with BASE here — it MUST NOT.
DIFF_ISSUER = ("agent-a", "https://issuer-two.example.com", "user-1")

VARIATIONS = {
    "different sub (cross-caller)": DIFF_SUB,
    "different agentId (cross-agent)": DIFF_AGENT,
    "same sub, different issuer (cross-issuer)": DIFF_ISSUER,
}


# ---------------------------------------------------------------------------
# Key-level deny: the derived MemoryKey for each variation is disjoint from
# the base across actor_id / session_id / namespace.
# ---------------------------------------------------------------------------


def test_base_key_is_self_consistent_and_in_external_keyspace():
    """The base triple derives a single EXT# key shared by all three fields.

    Establishes the baseline the deny assertions build on: a load/store keyed
    for the base triple targets exactly one actor/session/namespace value.
    """
    base_key = derive_external_memory_key(*BASE)

    # All three fields collapse to one value, so the caller's memory subject,
    # session, and namespace are scoped identically and cannot drift apart.
    assert base_key.actor_id == base_key.session_id == base_key.namespace
    # And it lives in the segregated external keyspace.
    assert base_key.actor_id.startswith(EXTERNAL_KEYSPACE_PREFIX)


def test_each_variation_key_differs_from_base_on_every_field():
    """A request keyed by BASE cannot touch state under any variation.

    For each of the three deny dimensions (cross-caller, cross-agent,
    cross-issuer) the derived actor_id, session_id and namespace must all
    differ from the base — so an operation scoped to the base key addresses a
    different actor/session/namespace than the variation's state.
    """
    base_key = derive_external_memory_key(*BASE)

    for label, triple in VARIATIONS.items():
        var_key = derive_external_memory_key(*triple)

        assert var_key.actor_id != base_key.actor_id, (
            f"{label}: actor_id collided with base — base could read/write "
            f"the variation's memory"
        )
        assert var_key.session_id != base_key.session_id, (
            f"{label}: session_id collided with base — base could resume the "
            f"variation's session"
        )
        assert var_key.namespace != base_key.namespace, (
            f"{label}: namespace collided with base — base could read/write "
            f"the variation's namespace"
        )


def test_same_sub_different_issuer_does_not_collide():
    """Explicit cross-issuer case: same `sub`, different `issuer` is distinct.

    A naive bare-`sub` key would map both issuers' `user-1` to the same key
    and leak state across issuers. Assert the issuer-qualified key keeps them
    apart (Req 10.3, 10.4).
    """
    base_key = derive_external_memory_key(*BASE)
    cross_issuer_key = derive_external_memory_key(*DIFF_ISSUER)

    # Same subject, same agent, only the issuer differs — must NOT collide.
    assert BASE[2] == DIFF_ISSUER[2]  # same sub
    assert BASE[0] == DIFF_ISSUER[0]  # same agentId
    assert BASE[1] != DIFF_ISSUER[1]  # different issuer
    assert cross_issuer_key.actor_id != base_key.actor_id
    assert cross_issuer_key.session_id != base_key.session_id
    assert cross_issuer_key.namespace != base_key.namespace


def test_all_four_triples_yield_four_distinct_keys():
    """No two of {base, diff-sub, diff-agent, diff-issuer} share a key."""
    keys = [
        derive_external_memory_key(*t)
        for t in (BASE, DIFF_SUB, DIFF_AGENT, DIFF_ISSUER)
    ]
    actor_ids = {k.actor_id for k in keys}
    assert len(actor_ids) == 4, "expected four distinct external keys"


# ---------------------------------------------------------------------------
# Config-level deny: AgentCoreMemoryConfig is what actually scopes the
# AgentCore Memory read/write, so assert the base config's actor_id/session_id
# differ from every variation's config.
# ---------------------------------------------------------------------------


def test_memory_config_scopes_base_to_its_own_key():
    """The base config's actor_id/session_id equal the base derived key."""
    base_key = derive_external_memory_key(*BASE)
    base_cfg = build_external_memory_config(MEMORY_ID, *BASE)

    assert base_cfg.memory_id == MEMORY_ID
    assert base_cfg.actor_id == base_key.actor_id
    assert base_cfg.session_id == base_key.session_id


def test_each_variation_config_differs_from_base_config():
    """The config that scopes AgentCore Memory differs for every variation.

    Since ``AgentCoreMemoryConfig.actor_id``/``session_id`` are what the
    runtime uses to address durable memory, distinct configs mean a base
    request physically cannot read or write a variation's memory (Req 10.3).
    """
    base_cfg = build_external_memory_config(MEMORY_ID, *BASE)

    for label, triple in VARIATIONS.items():
        var_cfg = build_external_memory_config(MEMORY_ID, *triple)

        assert var_cfg.actor_id != base_cfg.actor_id, (
            f"{label}: config actor_id collided — AgentCore Memory would be "
            f"shared with base"
        )
        assert var_cfg.session_id != base_cfg.session_id, (
            f"{label}: config session_id collided — session would be shared "
            f"with base"
        )
        # The shared memory_id is expected — isolation is by actor/session,
        # not by a separate store.
        assert var_cfg.memory_id == base_cfg.memory_id


def test_deny_is_symmetric():
    """Deny holds in both directions: no variation can touch base's state.

    The key derivation is a pure function, so distinctness is symmetric — but
    assert it explicitly to document that neither direction of a cross-triple
    read/write is possible.
    """
    base_key = derive_external_memory_key(*BASE)
    for label, triple in VARIATIONS.items():
        var_key = derive_external_memory_key(*triple)
        # base -> variation blocked AND variation -> base blocked.
        assert base_key.actor_id != var_key.actor_id, label
        assert var_key.actor_id != base_key.actor_id, label
