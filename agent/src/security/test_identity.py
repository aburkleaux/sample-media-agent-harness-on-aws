"""Characterization tests for the durable-memory keying seam.

These lock the CURRENT behavior of the memory-key derivation in
``security.identity`` so the upcoming long-term-memory change (which will start
using the ``namespace`` argument that is threaded-but-dormant today) has a
regression net. They are pure and offline — no AWS creds, no network.

What they pin (and why it matters for the memory work):
  * the internal path is an identity mapping and ``namespace`` is currently a
    NO-OP in the built config (the change will have to make it actually apply);
  * the external ``EXT_`` keyspace is segregated, collision-free over the full
    ``(agentId, issuer, sub[, conversation_id])`` tuple, and free of ``#``.
Design refs: Property 12 (collision-free), Property 17 (segregation),
Req 10.4/10.5.
"""
from __future__ import annotations

import pytest

from security import identity


# --------------------------------------------------------------------------
# Internal path: derive_memory_keys / build_memory_config / resolve actor
# --------------------------------------------------------------------------

def test_derive_memory_keys_is_identity_mapping_no_namespace():
    key = identity.derive_memory_keys("actor-1", "sess-1")
    assert key.actor_id == "actor-1"
    assert key.session_id == "sess-1"
    assert key.namespace is None


def test_derive_memory_keys_threads_namespace_through():
    key = identity.derive_memory_keys("actor-1", "sess-1", namespace="ns/actor-1")
    assert key.actor_id == "actor-1"
    assert key.session_id == "sess-1"
    assert key.namespace == "ns/actor-1"


def test_memory_key_is_frozen():
    key = identity.derive_memory_keys("a", "s")
    with pytest.raises(Exception):
        key.actor_id = "mutated"  # type: ignore[misc]


def test_build_memory_config_uses_actor_and_session_verbatim():
    cfg = identity.build_memory_config(
        memory_id="mem-123", session_id="sess-9", actor_id="actor-9"
    )
    assert cfg.memory_id == "mem-123"
    assert cfg.session_id == "sess-9"
    assert cfg.actor_id == "actor-9"


def test_build_memory_config_namespace_is_currently_a_noop():
    # DOCUMENTS TODAY'S BEHAVIOR: `namespace` is threaded through
    # derive_memory_keys but NOT applied to AgentCoreMemoryConfig, so passing it
    # changes nothing about the built config. The long-term-memory change will
    # deliberately break this assertion when it starts honoring namespace — at
    # which point this test should be updated to assert the new behavior.
    with_ns = identity.build_memory_config(
        memory_id="m", session_id="s", actor_id="a", namespace="assets/a"
    )
    without_ns = identity.build_memory_config(
        memory_id="m", session_id="s", actor_id="a"
    )
    assert (with_ns.memory_id, with_ns.session_id, with_ns.actor_id) == (
        without_ns.memory_id,
        without_ns.session_id,
        without_ns.actor_id,
    )


def test_resolve_memory_actor_id_prefers_bound_actor():
    assert identity.resolve_memory_actor_id("cognito-sub-abc") == "cognito-sub-abc"


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_resolve_memory_actor_id_falls_back_when_absent(empty):
    assert identity.resolve_memory_actor_id(empty) == identity.LAB_DEFAULT_ACTOR_ID


# --------------------------------------------------------------------------
# External EXT_ keyspace: derive_external_memory_key
# --------------------------------------------------------------------------

def test_external_key_shape_and_segregation():
    key = identity.derive_external_memory_key("agent-1", "https://issuer.example", "sub-1")
    # All three fields share the same derived value (cannot drift apart).
    assert key.actor_id == key.session_id == key.namespace
    val = key.actor_id
    # Segregated keyspace: prefixed EXT_, disjoint from internal USER#/raw subs.
    assert val.startswith(identity.EXTERNAL_KEYSPACE_PREFIX)
    assert not val.startswith("USER#")
    # `#` is rejected by AgentCore Memory's id charset — the key must not contain it.
    assert "#" not in val
    # Shape: EXT_<issuerhash16>_<tuplehash64>
    body = val[len(identity.EXTERNAL_KEYSPACE_PREFIX):]
    issuer_hash, _, tuple_hash = body.partition("_")
    assert len(issuer_hash) == 16
    assert len(tuple_hash) == 64  # sha256 hex


def test_external_key_is_deterministic():
    a = identity.derive_external_memory_key("agent-1", "iss", "sub-1")
    b = identity.derive_external_memory_key("agent-1", "iss", "sub-1")
    assert a.actor_id == b.actor_id


def test_external_key_collision_free_over_each_tuple_component():
    base = identity.derive_external_memory_key("agent-1", "iss-A", "sub-1").actor_id
    diff_agent = identity.derive_external_memory_key("agent-2", "iss-A", "sub-1").actor_id
    diff_issuer = identity.derive_external_memory_key("agent-1", "iss-B", "sub-1").actor_id
    diff_sub = identity.derive_external_memory_key("agent-1", "iss-A", "sub-2").actor_id
    assert len({base, diff_agent, diff_issuer, diff_sub}) == 4


def test_external_key_folds_issuer_so_same_sub_across_issuers_differs():
    # Two IdPs minting the same `sub` must NOT collide (Req 10.4).
    k1 = identity.derive_external_memory_key("agent-1", "https://idp-one", "shared-sub").actor_id
    k2 = identity.derive_external_memory_key("agent-1", "https://idp-two", "shared-sub").actor_id
    assert k1 != k2


def test_external_key_conversation_id_sub_partitions_and_is_backward_compatible():
    no_conv = identity.derive_external_memory_key("agent-1", "iss", "sub-1").actor_id
    empty_conv = identity.derive_external_memory_key("agent-1", "iss", "sub-1", "").actor_id
    conv_a = identity.derive_external_memory_key("agent-1", "iss", "sub-1", "conv-a").actor_id
    conv_b = identity.derive_external_memory_key("agent-1", "iss", "sub-1", "conv-b").actor_id
    # Empty conversation id reproduces the original 3-component key byte-for-byte.
    assert no_conv == empty_conv
    # A conversation id yields a distinct thread, and different ids differ.
    assert conv_a != no_conv
    assert conv_a != conv_b


def test_external_material_is_length_prefixed_injective():
    # The length-prefixed encoding must not let component-boundary shifts alias:
    # ("a","bc","d") and ("ab","c","d") would collide under naive concatenation.
    k1 = identity.derive_external_memory_key("a", "bc", "d").actor_id
    k2 = identity.derive_external_memory_key("ab", "c", "d").actor_id
    assert k1 != k2


@pytest.mark.parametrize(
    "agent_id,issuer,sub",
    [
        ("", "iss", "sub"),
        ("   ", "iss", "sub"),
        ("agent", "", "sub"),
        ("agent", "   ", "sub"),
        ("agent", "iss", ""),
        ("agent", "iss", "   "),
    ],
)
def test_external_key_fails_loud_on_empty_components(agent_id, issuer, sub):
    with pytest.raises(ValueError):
        identity.derive_external_memory_key(agent_id, issuer, sub)


def test_build_external_memory_config_keys_on_external_not_owner():
    key = identity.derive_external_memory_key("agent-1", "iss", "sub-1")
    cfg = identity.build_external_memory_config(
        memory_id="mem-1", agent_id="agent-1", issuer="iss", sub="sub-1"
    )
    assert cfg.memory_id == "mem-1"
    assert cfg.actor_id == key.actor_id
    assert cfg.session_id == key.session_id
    # It is the segregated external key, never a raw owner actor.
    assert cfg.actor_id.startswith(identity.EXTERNAL_KEYSPACE_PREFIX)


def test_internal_and_external_keyspaces_are_disjoint():
    internal = identity.derive_memory_keys("cognito-sub-xyz", "sess-1").actor_id
    external = identity.derive_external_memory_key("agent-1", "iss", "cognito-sub-xyz").actor_id
    assert not internal.startswith(identity.EXTERNAL_KEYSPACE_PREFIX)
    assert external.startswith(identity.EXTERNAL_KEYSPACE_PREFIX)
    assert internal != external



# --------------------------------------------------------------------------
# Long-term retrieval wiring (retrieval_config attached to the config)
# --------------------------------------------------------------------------

def test_namespace_constants_carry_expected_placeholders():
    # These templates are resolved by the session manager with .format(actorId=,
    # sessionId=, memoryStrategyId=) — a typo here silently breaks retrieval.
    # Leading+trailing slashes follow the AWS Strands+AgentCore doc convention.
    assert identity.SESSION_SUMMARY_NAMESPACE == "/summaries/{actorId}/{sessionId}/"
    assert identity.USER_PREFERENCE_NAMESPACE == "/preferences/{actorId}/"


def test_build_memory_config_attaches_retrieval_by_default():
    cfg = identity.build_memory_config(
        memory_id="m", session_id="s", actor_id="a"
    )
    rc_map = cfg.retrieval_config
    assert rc_map is not None
    # The same-session summary namespace is NOT retrieved: the main agent already has this
    # session's history loaded (session 764a71c0: a bare "2" retrieved 23 records / 18 KB).
    # Preferences ONLY: long-term facts were removed (2026-10, session 07703a35).
    assert set(rc_map.keys()) == {identity.USER_PREFERENCE_NAMESPACE}
    assert identity.SESSION_SUMMARY_NAMESPACE not in rc_map
    prefs = rc_map[identity.USER_PREFERENCE_NAMESPACE]
    assert prefs.top_k == 10
    assert prefs.relevance_score == 0.4


@pytest.mark.parametrize("include_session_summary", [False, True])
def test_retrieval_namespaces_never_include_facts(include_session_summary):
    cfg = identity.build_memory_config(
        memory_id="m", session_id="s", actor_id="a",
        include_session_summary=include_session_summary,
    )
    assert cfg.retrieval_config
    assert all("/facts/" not in ns for ns in cfg.retrieval_config)
    assert not hasattr(identity, "SEMANTIC_FACTS_NAMESPACE")


def test_build_memory_config_session_summary_is_opt_in():
    cfg = identity.build_memory_config(
        memory_id="m", session_id="s", actor_id="a", include_session_summary=True
    )
    assert identity.SESSION_SUMMARY_NAMESPACE in cfg.retrieval_config


def test_build_memory_config_retrieval_can_be_disabled():
    cfg = identity.build_memory_config(
        memory_id="m", session_id="s", actor_id="a", enable_retrieval=False
    )
    # Write-only config: history persists, nothing is injected.
    assert cfg.retrieval_config is None
    # Keying is unaffected by the retrieval toggle.
    assert cfg.actor_id == "a" and cfg.session_id == "s"


def test_build_memory_config_retrieval_params_passthrough():
    cfg = identity.build_memory_config(
        memory_id="m", session_id="s", actor_id="a",
        retrieval_top_k=3, retrieval_relevance=0.5,
    )
    prefs = cfg.retrieval_config[identity.USER_PREFERENCE_NAMESPACE]
    assert prefs.top_k == 3
    assert prefs.relevance_score == 0.5
