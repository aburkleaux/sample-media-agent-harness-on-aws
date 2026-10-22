"""Property-based test for per-caller/per-agent session & memory isolation.

Feature: custom-agents, Property 12: Per-caller/per-agent session & memory isolation

Validates: Requirements 10.1, 10.2, 10.3, 10.4, 10.5

Property 12 (design.md): For any two external invocations with issuer-qualified
principals ``(i1,u1)``, ``(i2,u2)`` on agents ``a1``, ``a2``, the resolved
session/memory key (runtimeSessionId and AgentCore Memory actor + namespace) is
a **pure, collision-free function of ``(agentId, issuer, sub)``**: identical
``(a, i, u)`` triples yield identical keys, and any triple differing in
``agentId``, ``issuer``, **or** ``sub`` yields a distinct, non-colliding key.
The key is never a function of Owner alone and never of ``sub`` alone (two
issuers minting the same ``sub`` never collide).

This drives the *real* key-derivation code:
``identity.derive_external_memory_key(agent_id, issuer, sub) -> MemoryKey``.
The MemoryKey's ``actor_id``/``session_id``/``namespace`` are all the same
segregated external id ``EXT#<sha256(issuer)[:16]>#<sha256(canonical(...))>``,
whose canonical material is a length-prefixed injective encoding of the triple
— so the collision-free guarantee rests on the encoding, not on chance.

Note on Owner: the derivation takes no Owner parameter at all, so the key
*cannot* be a function of Owner. The tests assert the callable signature and
that keys depend only on ``(agentId, issuer, sub)``, documenting that Owner is
structurally not an input (Req 10.2/10.3).
"""

from __future__ import annotations

import inspect
import re
import os
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

# Add agent/src to sys.path so imports resolve (mirrors the other agent tests).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from security.identity import (  # noqa: E402
    EXTERNAL_KEYSPACE_PREFIX,
    MemoryKey,
    derive_external_memory_key,
)

# --- Generators -------------------------------------------------------------
#
# The identity components are opaque strings (agentId, OIDC issuer URL, JWT
# sub). We generate non-empty text across the whole unicode space — including
# the ``#`` delimiter and ``:`` used inside the readable/canonical key — so the
# injective-encoding claim is genuinely stressed rather than only tested on
# "well-behaved" ASCII. ``derive_external_memory_key`` requires each component
# to be a non-empty string (it rejects blank/whitespace-only), so we constrain
# the generator to strings with at least one non-whitespace character.

_nonblank_text = st.text(min_size=1, max_size=40).filter(lambda s: s.strip() != "")


@st.composite
def _triples(draw):
    """An arbitrary ``(agent_id, issuer, sub)`` triple of non-blank strings."""
    return (draw(_nonblank_text), draw(_nonblank_text), draw(_nonblank_text))


@st.composite
def _distinct_triple_pairs(draw):
    """Two triples guaranteed to differ in at least one component."""
    t1 = draw(_triples())
    t2 = draw(_triples())
    if t1 == t2:
        # Perturb one component so the pair is genuinely distinct. Appending a
        # marker keeps it non-blank and different.
        t2 = (t2[0] + "\u0394", t2[1], t2[2])
    return t1, t2


def _key_id(mk: MemoryKey) -> str:
    """The single external id shared by all three MemoryKey fields."""
    # All three are asserted equal elsewhere; use actor_id as the canonical id.
    return mk.actor_id


# --- Properties -------------------------------------------------------------


@settings(max_examples=200)
@given(_triples())
def test_determinism_same_triple_yields_identical_key(triple):
    """Purity/determinism: same triple -> identical key on repeated calls.

    Feature: custom-agents, Property 12: Per-caller/per-agent session & memory
    isolation. Validates: Requirements 10.1, 10.5
    """
    agent_id, issuer, sub = triple
    k1 = derive_external_memory_key(agent_id, issuer, sub)
    k2 = derive_external_memory_key(agent_id, issuer, sub)
    assert k1 == k2
    # And the id itself is stable.
    assert _key_id(k1) == _key_id(k2)


@settings(max_examples=200)
@given(_triples())
def test_all_fields_share_one_external_id_in_ext_keyspace(triple):
    """actor_id == session_id == namespace, all in the EXT# keyspace.

    Feature: custom-agents, Property 12: Per-caller/per-agent session & memory
    isolation. Validates: Requirements 10.1, 10.3
    """
    agent_id, issuer, sub = triple
    key = derive_external_memory_key(agent_id, issuer, sub)
    # Segregated keyspace: every derived key lives under EXT#.
    assert key.actor_id.startswith(EXTERNAL_KEYSPACE_PREFIX)
    # Memory subject, session, and namespace are scoped identically.
    assert key.actor_id == key.session_id == key.namespace


@settings(max_examples=300)
@given(_distinct_triple_pairs())
def test_collision_free_across_any_differing_component(pair):
    """Any two triples differing in agentId, issuer, OR sub -> distinct keys.

    Because the key is a hash over an injective canonical encoding, equal keys
    for distinct triples would be a structural collision (astronomically
    unlikely by chance), so we treat equality as a failure.

    Feature: custom-agents, Property 12: Per-caller/per-agent session & memory
    isolation. Validates: Requirements 10.5
    """
    (t1, t2) = pair
    assert t1 != t2  # generator guarantees distinctness
    k1 = derive_external_memory_key(*t1)
    k2 = derive_external_memory_key(*t2)
    assert _key_id(k1) != _key_id(k2)
    # Distinctness holds across every field, not just actor_id.
    assert k1 != k2


@settings(max_examples=200)
@given(
    agent_id=_nonblank_text,
    sub=_nonblank_text,
    issuer1=_nonblank_text,
    issuer2=_nonblank_text,
)
def test_never_sub_alone_two_issuers_same_sub_never_collide(
    agent_id, sub, issuer1, issuer2
):
    """Same (agentId, sub) but different issuer -> distinct keys.

    Two issuers minting the same ``sub`` must never collide: the issuer is
    always folded into the key, so the key is never a function of ``sub``
    alone.

    Feature: custom-agents, Property 12: Per-caller/per-agent session & memory
    isolation. Validates: Requirements 10.4, 10.5
    """
    # Only meaningful when the issuers genuinely differ.
    if issuer1 == issuer2:
        issuer2 = issuer2 + "\u0394"
    k1 = derive_external_memory_key(agent_id, issuer1, sub)
    k2 = derive_external_memory_key(agent_id, issuer2, sub)
    assert _key_id(k1) != _key_id(k2)


@settings(max_examples=100)
@given(_triples())
def test_never_owner_alone_owner_is_not_an_input(triple):
    """The derivation takes no Owner parameter -> key cannot depend on Owner.

    We assert the callable signature is exactly ``(agent_id, issuer, sub)`` so
    no owner value can influence the key, and (trivially) that the key for a
    fixed triple is identical regardless of any external owner context. This
    documents Req 10.2/10.3: memory/session scope is per-caller/per-agent, not
    per-Owner.

    Feature: custom-agents, Property 12: Per-caller/per-agent session & memory
    isolation. Validates: Requirements 10.2, 10.3
    """
    params = list(inspect.signature(derive_external_memory_key).parameters)
    # The identity inputs are exactly (agent_id, issuer, sub); an optional
    # trailing `conversation_id` sub-partitions the caller's OWN memory and is
    # never an owner/identity input. The key invariant is that NO owner value
    # can influence the key.
    assert params[:3] == ["agent_id", "issuer", "sub"]
    assert params[3:] in ([], ["conversation_id"])
    assert not any("owner" in p.lower() for p in params)

    agent_id, issuer, sub = triple
    # No owner is threaded in; the key is fully determined by the triple.
    assert derive_external_memory_key(agent_id, issuer, sub) == (
        derive_external_memory_key(agent_id, issuer, sub)
    )


# --- Delimiter + conversation-nonce coverage (post `#`→`_` fix) -------------

_AGENTCORE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-_/]*(?::[a-zA-Z0-9\-_/]+)*[a-zA-Z0-9\-_/]*$")


@given(triple=_triples())
@settings(max_examples=200)
def test_external_key_is_agentcore_id_valid_no_hash(triple):
    """The derived key must satisfy AgentCore Memory's actor/session id charset.

    AgentCore Memory rejects `#` in actor_id/session_id with a
    ValidationException. Regression guard for the `EXT#`→`EXT_` fix: every
    derived field must match the service regex and contain no `#`.
    """
    agent_id, issuer, sub = triple
    key = derive_external_memory_key(agent_id, issuer, sub)
    for field in (key.actor_id, key.session_id, key.namespace):
        assert "#" not in field
        assert field.startswith("EXT_")
        assert _AGENTCORE_ID_RE.match(field), f"invalid AgentCore id: {field}"


@given(triple=_triples())
@settings(max_examples=200)
def test_conversation_nonce_sub_partitions_same_caller(triple):
    """A conversation id yields a DISTINCT key for the same caller, and is
    deterministic; the empty default reproduces the base 3-tuple key."""
    agent_id, issuer, sub = triple
    base = derive_external_memory_key(agent_id, issuer, sub)
    base_default = derive_external_memory_key(agent_id, issuer, sub, "")
    convo_a = derive_external_memory_key(agent_id, issuer, sub, "c-aaa")
    convo_b = derive_external_memory_key(agent_id, issuer, sub, "c-bbb")

    # Empty conversation id == the original 3-tuple key (backward compatible).
    assert base_default.actor_id == base.actor_id
    # Different conversations → different keys (fresh thread).
    assert convo_a.actor_id != base.actor_id
    assert convo_a.actor_id != convo_b.actor_id
    # Deterministic.
    assert convo_a.actor_id == derive_external_memory_key(agent_id, issuer, sub, "c-aaa").actor_id
    # Still valid + segregated.
    assert convo_a.actor_id.startswith("EXT_") and "#" not in convo_a.actor_id
