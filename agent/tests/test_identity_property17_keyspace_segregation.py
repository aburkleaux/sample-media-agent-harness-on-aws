"""Property-based test for Principal keyspace segregation.

Feature: custom-agents, Property 17: Principal keyspace segregation

Validates: Requirements 19.2, 19.3, 19.4, 19.5

Property 17 (design.md): For any issuer-qualified Principal ``(issuer, sub)`` —
including a ``sub`` deliberately chosen to equal an internal platform actor id
(e.g. a Cognito ``sub`` or the ``LAB_DEFAULT_ACTOR_ID``) minted by any
owner-supplied issuer — the derived external identifier (memory/session/audit
key) lies in the external ``EXT#`` keyspace and is **disjoint** from the
internal platform actor keyspace (``USER#<cognitoSub>#OWNED``): no
``(issuer, sub)`` maps to an internal ``USER#`` key, so an external caller can
never alias an internal user. Conversely, resource resolution is driven only by
the resource actor (``actor_id`` = Owner) and never by the Principal — the
external key derivation takes no owner/actor_id parameter at all.

The functions under test are pure, so this test constructs no S3/DDB
collaborators: it exercises the key-derivation logic directly.

Semantics asserted (agent/src/identity.py, agent/src/lab/asset_index.py):

* ``derive_external_memory_key(agentId, issuer, sub)`` returns a
  :class:`MemoryKey` whose ``actor_id``/``session_id``/``namespace`` all begin
  with :data:`EXTERNAL_KEYSPACE_PREFIX` (``EXT#``) (Req 19.3).
* Internal actor ids — raw platform subjects returned by
  ``resolve_memory_actor_id`` (Cognito ``sub`` / ``LAB_DEFAULT_ACTOR_ID``) —
  never begin with ``EXT#``, and internal partition keys built by
  ``make_pk_user_owned`` start with ``USER#``; therefore the external key can
  never equal either, even when ``sub`` is crafted to look internal
  (Req 19.2, 19.3, 19.4).
* The external key derivation is principal-only: its signature exposes no
  owner/actor_id parameter, and the derived key never equals
  ``resolve_memory_actor_id(owner)`` for an arbitrary Owner — resource
  resolution is driven solely by ``actor_id`` (Owner) (Req 19.5).
"""

from __future__ import annotations

import inspect
import os
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

# Add agent/src to path so imports resolve (mirrors the other agent tests).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from security.identity import (  # noqa: E402
    EXTERNAL_KEYSPACE_PREFIX,
    LAB_DEFAULT_ACTOR_ID,
    derive_external_memory_key,
    resolve_memory_actor_id,
)
from lab.asset_index import make_pk_user_owned  # noqa: E402


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# Multiple *different* issuers, so the same `sub` is minted across issuers.
_issuers = st.sampled_from(
    [
        "https://issuer-a.example.com",
        "https://issuer-b.example.com/oauth2",
        "https://login.microsoftonline.com/tenant/v2.0",
        "https://accounts.google.com",
        "https://auth.example.org:8443/realms/main",
        "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_PoolId",
    ]
)

# Agent ids (arbitrary but non-empty / non-whitespace).
_agent_ids = st.sampled_from(
    ["agent-1", "agent-2", "cust-agent-abc", "AGENT#weird", "a"]
)

# Cognito-sub-like UUID strings — the canonical internal platform subject.
_cognito_sub_like = st.builds(
    lambda a, b, c, d, e: f"{a:08x}-{b:04x}-{c:04x}-{d:04x}-{e:012x}",
    st.integers(min_value=0, max_value=0xFFFFFFFF),
    st.integers(min_value=0, max_value=0xFFFF),
    st.integers(min_value=0, max_value=0xFFFF),
    st.integers(min_value=0, max_value=0xFFFF),
    st.integers(min_value=0, max_value=0xFFFFFFFFFFFF),
)

# `sub` values DELIBERATELY chosen to equal internal-looking platform actor ids:
#   * a Cognito-sub-like UUID,
#   * the platform's default actor id constant,
#   * a raw internal actor id string,
#   * a string that itself looks like an internal partition key `USER#...`.
_internal_looking_subs = st.one_of(
    _cognito_sub_like,
    st.just(LAB_DEFAULT_ACTOR_ID),
    st.sampled_from(["internal-user", "admin", "owner-123", "local-dev-user"]),
    st.builds(lambda u: f"USER#{u}", st.sampled_from(["abc", "owner-123", "admin"])),
    st.builds(lambda u: f"USER#{u}#OWNED", st.sampled_from(["abc", "owner-123"])),
)

# Arbitrary Owner ids used to prove the external key is independent of the Owner.
_owner_ids = st.sampled_from(
    ["owner-123", "admin", "internal-user", LAB_DEFAULT_ACTOR_ID, "abc"]
)


# ---------------------------------------------------------------------------
# Property 17: Principal keyspace segregation
# ---------------------------------------------------------------------------


# Feature: custom-agents, Property 17: Principal keyspace segregation
@settings(max_examples=200, deadline=None)
@given(
    agent_id=_agent_ids,
    issuer=_issuers,
    sub=_internal_looking_subs,
    owner=_owner_ids,
)
def test_property17_principal_keyspace_segregation(agent_id, issuer, sub, owner):
    """**Validates: Requirements 19.2, 19.3, 19.4, 19.5**

    The external key derived for a caller whose ``sub`` is deliberately equal to
    an internal platform actor id still lands in the ``EXT#`` keyspace and never
    aliases the internal ``USER#``/raw-actor keyspace, and the derivation is
    principal-only (independent of the Owner / resource actor).
    """
    key = derive_external_memory_key(agent_id, issuer, sub)

    # --- Req 19.3: every derived field lives in the segregated EXT# keyspace.
    for field in (key.actor_id, key.session_id, key.namespace):
        assert field is not None
        assert field.startswith(EXTERNAL_KEYSPACE_PREFIX), (
            f"external key field {field!r} not in {EXTERNAL_KEYSPACE_PREFIX} keyspace"
        )

    # The internal keyspaces this external key must be disjoint from.
    internal_actor_id = resolve_memory_actor_id(sub)  # raw platform subject
    internal_user_pk = make_pk_user_owned(sub)  # USER#<sub>#OWNED

    # Internal identifiers never carry the EXT# prefix — disjoint by construction.
    assert not internal_actor_id.startswith(EXTERNAL_KEYSPACE_PREFIX)
    assert not internal_user_pk.startswith(EXTERNAL_KEYSPACE_PREFIX)
    assert internal_user_pk.startswith("USER#")

    # --- Req 19.2 / 19.4: a caller sub equal to an internal id does NOT alias
    # that internal user. The external key can never equal the internal raw
    # actor id nor the internal USER# partition key, on any derived field.
    for field in (key.actor_id, key.session_id, key.namespace):
        assert field != internal_actor_id, (
            "external key aliased the internal raw actor id"
        )
        assert field != internal_user_pk, (
            "external key aliased the internal USER# partition key"
        )
        assert field != sub, "external key leaked the raw caller sub"

    # --- Req 19.5: resource resolution is driven only by actor_id (Owner);
    # the external key derivation is principal-only and independent of the Owner.
    # The external key never equals the internal memory actor id for ANY owner.
    for field in (key.actor_id, key.session_id, key.namespace):
        assert field != resolve_memory_actor_id(owner), (
            "external key collided with an internal owner-derived actor id"
        )


# Feature: custom-agents, Property 17: Principal keyspace segregation
def test_property17_derivation_is_principal_only_no_owner_parameter():
    """**Validates: Requirements 19.5**

    Resource resolution is driven solely by ``actor_id`` (Owner); the external
    memory-key derivation must therefore be independent of the Owner. Assert
    structurally that ``derive_external_memory_key`` accepts only the Principal
    triple ``(agent_id, issuer, sub)`` and takes NO owner/actor_id parameter, so
    the key can never be a function of the resource actor.
    """
    params = list(inspect.signature(derive_external_memory_key).parameters)
    # Identity inputs are exactly (agent_id, issuer, sub); an optional trailing
    # `conversation_id` sub-partitions the caller's own memory and is never an
    # owner/actor input.
    assert params[:3] == ["agent_id", "issuer", "sub"], (
        f"derive_external_memory_key exposes unexpected params: {params}"
    )
    assert params[3:] in ([], ["conversation_id"]), (
        f"unexpected trailing params: {params[3:]}"
    )
    for forbidden in ("owner", "actor_id", "actor", "resource_actor"):
        assert forbidden not in params
