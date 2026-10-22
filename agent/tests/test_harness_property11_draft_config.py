"""Property-based test that the in-editor Test_Harness runs the DRAFT config.

Feature: custom-agents, Property 11: In-editor test executes the draft configuration

Validates: Requirements 9.2

Property 11 (design.md): For any Custom_Agent with a draft working copy and any
(possibly differing) current published version, an in-editor test run resolves
its Task_Agent_Runtime payload (``system_prompt``, ``model_id``, ``skills``,
``tools``) from the **draft** configuration — never from a published snapshot.
This is the complement of Property 10 (external invoke -> published version;
in-editor test -> draft).

Chosen test layer
-----------------
The in-editor test path is the internal ``runHarness`` lab-run route, which
executes through ``TestHarness``. The seam that actually performs the
draft-vs-published *selection* is ``TestHarness._lookup_asset`` (test_harness.py):
it resolves the target agent from the owner's **OWNED** working-copy partition
(``USER#<actor>#OWNED``) and only returns a row whose ``status == "draft"``. It
never queries the immutable **VERSIONS** partition (``USER#<actor>#VERSIONS``)
where published snapshots live. A dedicated pure config->payload resolver
(tasks 3.1) is not yet wired into the harness, so ``_lookup_asset`` is the most
faithful layer at which the draft-config guarantee can be asserted today.

This test therefore drives the *real* harness resolution code:

  1. Populate the index with BOTH a draft working copy (OWNED, status="draft")
     AND a differing current published version (VERSIONS, status="published",
     current=True) for the same ``agentId``.
  2. Run ``harness._lookup_asset(ctx, agentId)`` and read the draft persona via
     the real ``harness._read_s3_text`` — i.e. the code the harness actually
     uses to gather the payload inputs.
  3. Map the resolved record onto the Task Agent payload using the design's
     Component 3 config->payload mapping (persona -> system_prompt;
     model -> model_id, omitted when unset; compatible_skills -> skills;
     tool_collections -> tools).

Then assert every payload field derives from the DRAFT and never from the
(guaranteed-differing) published snapshot.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Any, Optional

from hypothesis import given, settings
from hypothesis import strategies as st

# Add agent/src to path so imports resolve (mirrors the other agent tests).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from lab.asset_index import (  # noqa: E402
    make_pk_user_owned,
    make_pk_user_versions,
    make_sk_owned,
    make_sk_version,
)
from lab.context import LabRequestContext  # noqa: E402
from lab.models import SpecialtyAgent  # noqa: E402
from lab.test_harness import TestHarness  # noqa: E402

_ACTOR = "actor-p11"
_AGENT_ID = "agent-p11"


# ---------------------------------------------------------------------------
# Lightweight in-memory fakes for the two collaborators the resolution path
# touches: the AssetIndex (``get``) and the S3 tool (``read``). Keeping them
# in-memory lets the property run 100+ examples without moto/table overhead
# while still driving the real ``_lookup_asset`` / ``_read_s3_text`` code.
# ---------------------------------------------------------------------------


class _FakeIndex:
    """Minimal AssetIndex stand-in keyed by ``(PK, SK)``."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], Any] = {}

    def put_row(self, pk: str, sk: str, record: Any) -> None:
        self._store[(pk, sk)] = record

    async def get(self, pk: str, sk: str) -> Optional[Any]:
        return self._store.get((pk, sk))


class _FakeS3:
    """Minimal S3UserFilesTool stand-in keyed by relative path."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def put_text(self, path: str, text: str) -> None:
        self._objects[path] = text.encode("utf-8")

    async def read(self, path: str) -> bytes:
        try:
            return self._objects[path]
        except KeyError as exc:  # pragma: no cover - defensive
            raise FileNotFoundError(path) from exc


def _persona_draft_path(agent_id: str) -> str:
    """Draft persona S3 path used by ``TestHarness._execute_specialty``."""
    return f"drafts/specialty-agents/{agent_id}/persona.md"


def _persona_published_path(agent_id: str) -> str:
    """A distinct versions-partition persona path (must never be read)."""
    return f"versions/specialty-agents/{agent_id}/persona.md"


def _specialty_record(
    *,
    status: str,
    version: Optional[int],
    current: bool,
    s3_prefix: str,
    model: Optional[str],
    compatible_skills: list[str],
    tool_collections: list[str],
) -> SpecialtyAgent:
    now = int(time.time() * 1000)
    return SpecialtyAgent(
        asset_id=_AGENT_ID,
        category="specialty-agent",
        name="Custom Agent P11",
        status=status,
        scope="private",
        s3_prefix=s3_prefix,
        created_at=now,
        updated_at=now,
        version=version,
        current=current,
        compatible_skills=list(compatible_skills),
        model=model,
        tool_collections=list(tool_collections),
    )


def _derive_harness_payload(record: SpecialtyAgent, persona_text: str) -> dict[str, Any]:
    """Map a resolved specialty record onto the Task Agent payload.

    Mirrors the design's Component 3 config->payload mapping:
      - persona.md          -> ``system_prompt``
      - model               -> ``model_id`` (OMITTED when unset)
      - compatible_skills   -> ``skills``
      - tool_collections    -> ``tools`` (always an explicit list)
    """
    payload: dict[str, Any] = {
        "system_prompt": persona_text,
        "skills": list(record.compatible_skills),
        "tools": list(record.tool_collections),
    }
    if record.model is not None:
        payload["model_id"] = record.model
    return payload


# ---------------------------------------------------------------------------
# Strategies: a DRAFT config plus a PUBLISHED snapshot that is guaranteed to
# differ from the draft in EVERY payload-relevant field, so each example is a
# genuine "draft vs differing published" case.
# ---------------------------------------------------------------------------

_id_text = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters=".:-_",
    ),
    min_size=1,
    max_size=12,
)
_persona_text = st.text(min_size=0, max_size=80)
_id_list = st.lists(_id_text, min_size=0, max_size=5, unique=True)


@st.composite
def _draft_and_published(draw):
    """Generate ``(draft_cfg, published_cfg)`` differing in every field."""
    draft_persona = draw(_persona_text)
    draft_model = draw(st.one_of(st.none(), _id_text))
    draft_skills = draw(_id_list)
    draft_tcs = draw(_id_list)

    # Published snapshot: force a difference in every payload-relevant field.
    published_persona = draft_persona + "\n<<PUBLISHED-SENTINEL>>"
    # Model differs: if the draft omits it, the published one sets it (and vice
    # versa); when both set, use a distinct value.
    if draft_model is None:
        published_model = "published-model-sentinel"
    else:
        published_model = draft_model + "-published"
    published_skills = list(draft_skills) + ["published-skill-sentinel"]
    published_tcs = list(draft_tcs) + ["published-tc-sentinel"]

    draft_cfg = {
        "persona": draft_persona,
        "model": draft_model,
        "skills": draft_skills,
        "tcs": draft_tcs,
    }
    published_cfg = {
        "persona": published_persona,
        "model": published_model,
        "skills": published_skills,
        "tcs": published_tcs,
    }
    return draft_cfg, published_cfg


# ---------------------------------------------------------------------------
# Property 11: In-editor test executes the draft configuration
# ---------------------------------------------------------------------------

# Feature: custom-agents, Property 11: In-editor test executes the draft configuration
@settings(max_examples=200, deadline=None)
@given(configs=_draft_and_published())
def test_property11_in_editor_test_uses_draft_config(configs):
    """The harness resolves the payload from the DRAFT, never the published snapshot.

    Drives the real ``TestHarness._lookup_asset`` / ``_read_s3_text`` resolution
    path against an index holding both a draft working copy and a differing
    current published version, then maps the resolved record onto the Task
    Agent payload and asserts every field derives from the draft.

    Validates: Requirements 9.2
    """
    draft_cfg, published_cfg = configs

    index = _FakeIndex()
    s3 = _FakeS3()

    # DRAFT working copy under the OWNED partition (status="draft").
    draft_record = _specialty_record(
        status="draft",
        version=None,
        current=False,
        s3_prefix=f"users/{_ACTOR}/drafts/specialty-agents/{_AGENT_ID}/",
        model=draft_cfg["model"],
        compatible_skills=draft_cfg["skills"],
        tool_collections=draft_cfg["tcs"],
    )
    index.put_row(
        make_pk_user_owned(_ACTOR),
        make_sk_owned("specialty-agent", _AGENT_ID),
        draft_record,
    )
    s3.put_text(_persona_draft_path(_AGENT_ID), draft_cfg["persona"])

    # DIFFERING current published version under the VERSIONS partition. This is
    # the snapshot the harness must NEVER resolve for an in-editor test run.
    published_record = _specialty_record(
        status="published",
        version=1,
        current=True,
        s3_prefix=f"users/{_ACTOR}/versions/specialty-agents/{_AGENT_ID}/v1/",
        model=published_cfg["model"],
        compatible_skills=published_cfg["skills"],
        tool_collections=published_cfg["tcs"],
    )
    index.put_row(
        make_pk_user_versions(_ACTOR),
        make_sk_version("specialty-agent", _AGENT_ID, 1),
        published_record,
    )
    s3.put_text(_persona_published_path(_AGENT_ID), published_cfg["persona"])

    harness = TestHarness(s3=s3, index=index)
    ctx = LabRequestContext(
        actor_id=_ACTOR,
        session_id="session-p11",
        request_id="req-p11",
    )

    # --- Run the real harness draft-selection + persona-read path -----------
    resolved = asyncio.run(harness._lookup_asset(ctx, _AGENT_ID))

    # The harness selects the DRAFT working copy, never the published snapshot.
    assert resolved is not None
    assert resolved.status == "draft"
    assert resolved.version is None
    assert resolved is draft_record

    persona_text = asyncio.run(
        harness._read_s3_text(_persona_draft_path(_AGENT_ID))
    )
    payload = _derive_harness_payload(resolved, persona_text)

    # --- Every payload field derives from the DRAFT, never the published ----
    assert payload["system_prompt"] == draft_cfg["persona"]
    assert payload["system_prompt"] != published_cfg["persona"]

    assert payload["skills"] == list(draft_cfg["skills"])
    assert payload["skills"] != list(published_cfg["skills"])

    assert payload["tools"] == list(draft_cfg["tcs"])
    assert payload["tools"] != list(published_cfg["tcs"])

    # model_id: present iff the draft sets a model; and never the published one.
    if draft_cfg["model"] is None:
        assert "model_id" not in payload
    else:
        assert payload["model_id"] == draft_cfg["model"]
    assert payload.get("model_id") != published_cfg["model"]
