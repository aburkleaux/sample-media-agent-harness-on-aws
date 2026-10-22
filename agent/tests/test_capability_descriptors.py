"""Descriptor builder and prohibition-rule tests (migration Task 14).

These cover the properties the shared Registry's safety rests on:

* no prohibited internal tool can enter a record (R10),
* no credential-shaped field can (R4),
* one MCP record per Gateway endpoint, with repeated bare names preserved,
* descriptors are deterministic, so the digest is a usable idempotency signal,
* only org-scoped, non-deprecated published versions are eligible (R3).
"""

from __future__ import annotations

import json

import pytest

from capability.descriptors import (
    MAX_DESCRIPTOR_DATA_BYTES,
    PROHIBITED_TOOLS,
    PoisonDescriptorError,
    assert_no_prohibited_tools,
    assert_no_secrets,
    build_descriptor,
    build_mcp_endpoint_descriptor,
    build_package_reference,
    build_skill_descriptor,
    build_specialty_agent_descriptor,
    build_tool_collection_descriptor,
    build_workflow_descriptor,
    descriptor_digest,
    is_eligible,
    schema_digest,
)


def _org_skill_row(**overrides):
    row = {
        "assetId": "asset-123",
        "category": "skill",
        "name": "Video Processing",
        "description": "Transcode and probe media",
        "scope": "org",
        "version": 2,
        "deprecated": False,
        "s3Prefix": "org/o-acme/published/skills/asset-123/v2/",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# Eligibility (R3 / Property 2)
# ---------------------------------------------------------------------------


def test_only_org_scoped_published_versions_are_eligible():
    assert is_eligible(_org_skill_row()) is True
    assert is_eligible(_org_skill_row(scope="private")) is False
    assert is_eligible(_org_skill_row(scope="group")) is False
    assert is_eligible(_org_skill_row(deprecated=True)) is False
    # Working copy — no immutable version to project.
    assert is_eligible(_org_skill_row(version=None)) is False
    # `hook` has no Registry mapping at all.
    assert is_eligible(_org_skill_row(category="hook")) is False


def test_zero_or_negative_version_is_not_eligible():
    """A malformed version must not be projected as a lineage.

    ``v0`` would collide with nothing yet still create a record whose
    ``recordVersion`` cannot be produced by any real publish, leaving an
    orphan in the catalog that reconciliation could never match.
    """
    assert is_eligible(_org_skill_row(version=0)) is False
    assert is_eligible(_org_skill_row(version=-1)) is False


# ---------------------------------------------------------------------------
# Prohibited internal tools (R10)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool",
    ["file_read", "shell", "artifact", "code_interpreter", "dispatch_tool",
     "get_job", "lab_asset", "persist_workflow", "http_request", "current_time"],
)
def test_prohibited_tools_are_rejected(tool):
    with pytest.raises(PoisonDescriptorError) as excinfo:
        assert_no_prohibited_tools([tool])
    assert excinfo.value.reason == "prohibited_internal_tool"


def test_target_qualified_alias_cannot_smuggle_a_prohibited_tool():
    """`some-target___shell` must be rejected exactly like `shell`.

    The check is on the bare name because that is what a target actually
    exposes; matching the qualified string would let any target name act as a
    bypass.
    """
    with pytest.raises(PoisonDescriptorError):
        assert_no_prohibited_tools(["media-processing___shell"])


def test_legitimate_gateway_tools_are_allowed():
    assert_no_prohibited_tools(
        [
            "process_media",
            "diarize_speech",
            "separate_stems",
            "get_job_status",
            "media-processing___process_media",
        ]
    )


def test_there_is_no_file_write_tool_but_writes_are_still_excluded():
    """Documented invariant: write behaviour goes through excluded tools.

    R10 notes there is no separately named ``file_write``; the write path is
    ``artifact`` / ``shell`` / asset CRUD, all of which are prohibited. This
    asserts that remains true rather than leaving it as prose.
    """
    assert "file_write" not in PROHIBITED_TOOLS
    for writer in ("artifact", "shell", "lab_asset"):
        assert writer in PROHIBITED_TOOLS


# ---------------------------------------------------------------------------
# Secret scanning (R4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"clientSecret": "shhh"},
        {"nested": {"credentialProviderArn": "arn:aws:..."}},
        {"authorizerConfiguration": {"customJWTAuthorizer": {}}},
        {"items": [{"presignedUrl": "https://..."}]},
        {"secretArn": "arn:aws:secretsmanager:..."},
    ],
)
def test_secret_fields_are_rejected_anywhere_in_the_tree(payload):
    with pytest.raises(PoisonDescriptorError) as excinfo:
        assert_no_secrets(payload)
    assert excinfo.value.reason == "secret_field_in_descriptor"


def test_presigned_url_shaped_values_are_rejected_even_under_a_safe_key():
    """A credential can leak through an innocent-looking field name.

    An ``X-Amz-Signature`` in a ``url`` field is still a usable credential, and
    a record is world-readable to every authorized consumer of the registry.
    """
    with pytest.raises(PoisonDescriptorError) as excinfo:
        assert_no_secrets({"url": "https://b.s3/k?X-Amz-Signature=deadbeef"})
    assert excinfo.value.reason == "secret_value_in_descriptor"


def test_package_reference_never_contains_a_presigned_url():
    reference = build_package_reference(
        bucket="mediaworks-harness-lab-artifacts",
        s3_prefix="org/o-acme/published/skills/asset-123/v2/",
        manifest_digest="sha256:abc",
    )
    assert reference["s3Uri"].startswith("s3://")
    assert reference["bucketArn"] == "arn:aws:s3:::mediaworks-harness-lab-artifacts"
    assert reference["manifestDigest"] == "sha256:abc"
    assert_no_secrets(reference)


# ---------------------------------------------------------------------------
# Asset descriptors
# ---------------------------------------------------------------------------


def test_skill_descriptor_carries_definition_and_immutable_package_pointer():
    descriptor = build_skill_descriptor(
        _org_skill_row(),
        artifacts_bucket="bucket",
        skill_md="---\nname: video-processing\n---\nDo the thing.",
        manifest_digest="sha256:abc",
    )
    payload = json.loads(descriptor["agentSkillsDefinition"]["data"])
    assert payload["id"] == "asset-123"
    assert payload["name"] == "Video Processing"
    assert payload["mediaworks"]["version"] == 2
    assert payload["mediaworks"]["package"]["manifestDigest"] == "sha256:abc"
    # SKILL.md rides along for discovery only.
    assert "Do the thing." in descriptor["agentSkillsDefinition"]["additionalData"]["skillMd"]["data"]


def test_skill_descriptor_omits_skill_md_when_unavailable():
    """A skill whose SKILL.md could not be read is still projectable.

    The markdown is a search aid; the executable package is referenced by
    pointer. Failing the projection over one unreadable object read would trade
    a real capability for a nicety.
    """
    descriptor = build_skill_descriptor(_org_skill_row(), artifacts_bucket="bucket")
    assert "additionalData" not in descriptor["agentSkillsDefinition"]


def test_oversize_skill_md_is_truncated_not_rejected():
    huge = "x" * (MAX_DESCRIPTOR_DATA_BYTES + 5_000)
    descriptor = build_skill_descriptor(
        _org_skill_row(), artifacts_bucket="bucket", skill_md=huge
    )
    data = descriptor["agentSkillsDefinition"]["additionalData"]["skillMd"]["data"]
    assert len(data.encode("utf-8")) <= MAX_DESCRIPTOR_DATA_BYTES
    assert "truncated for discovery" in data


def test_oversize_definition_payload_is_a_poison_descriptor():
    """Unlike markdown, a definition that will not fit cannot be trimmed.

    Truncating structured data would ship a record that misrepresents the
    asset, so this fails locally and lands in the DLQ instead of being sent.
    """
    row = _org_skill_row(tags=["t" * 1000] * 200)
    with pytest.raises(PoisonDescriptorError) as excinfo:
        build_skill_descriptor(row, artifacts_bucket="bucket")
    assert excinfo.value.reason == "descriptor_too_large"


def test_specialty_agent_reduces_integration_config_to_a_boolean():
    """Issuer/audience/client-ids are authorization config, not discovery data.

    Projecting them would publish an agent's external-invoke trust
    configuration to every authorized consumer of the registry.
    """
    row = {
        "assetId": "agent-1",
        "category": "specialty-agent",
        "name": "Editor",
        "scope": "org",
        "version": 1,
        "model": "us.anthropic.claude-sonnet-4",
        "compatibleSkills": ["asset-123"],
        "toolCollections": ["tc-1"],
        "integrationConfig": {
            "enabled": True,
            "issuer": "https://issuer.example",
            "audience": "aud",
            "allowedClientIds": ["abc"],
        },
        "s3Prefix": "org/o/published/specialty-agents/agent-1/v1/",
    }
    payload = json.loads(build_specialty_agent_descriptor(row)["custom"]["data"])
    assert payload["externalInvokeEnabled"] is True
    assert "issuer" not in json.dumps(payload)
    assert "allowedClientIds" not in json.dumps(payload)
    assert payload["compatibleSkills"] == ["asset-123"]


def test_specialty_agent_uses_custom_not_a2a_agent_card():
    """An agent card advertises a callable A2A endpoint that does not exist.

    Today's specialty agents are Task-Agent configurations the Runtime resolves
    from canonical storage (R4), so claiming an A2A endpoint would point
    consumers at nothing.
    """
    row = {
        "assetId": "agent-1",
        "category": "specialty-agent",
        "name": "Editor",
        "scope": "org",
        "version": 1,
    }
    descriptor = build_specialty_agent_descriptor(row)
    assert set(descriptor) == {"custom"}


def test_tool_collection_preserves_target_qualified_tool_identity():
    row = {
        "assetId": "tc-1",
        "category": "tool-collection",
        "name": "Media basics",
        "scope": "org",
        "version": 3,
        "tools": [
            {"targetName": "media-processing", "name": "get_job_status"},
            {"targetName": "speech-diarization", "name": "get_job_status"},
        ],
    }
    payload = json.loads(build_tool_collection_descriptor(row)["custom"]["data"])
    qualified = [t["qualifiedToolName"] for t in payload["tools"]]
    assert qualified == [
        "media-processing___get_job_status",
        "speech-diarization___get_job_status",
    ]
    # Repeated bare names are NOT collapsed — each keeps its own association.
    assert [t["bareToolName"] for t in payload["tools"]] == [
        "get_job_status",
        "get_job_status",
    ]


def test_tool_collection_containing_a_prohibited_tool_is_rejected():
    row = {
        "assetId": "tc-2",
        "category": "tool-collection",
        "name": "Sneaky",
        "scope": "org",
        "version": 1,
        "tools": [{"name": "shell"}],
    }
    with pytest.raises(PoisonDescriptorError):
        build_tool_collection_descriptor(row)


def test_workflow_descriptor_projects_step_shape_only():
    row = {
        "assetId": "wf-1",
        "category": "workflow",
        "name": "Ingest",
        "scope": "org",
        "version": 1,
        "steps": [
            {
                "stepId": "s1",
                "agent": {"kind": "core", "ref": "task-agent"},
                "skill": {"assetId": "asset-123"},
                "toolCollection": {},
                "prompt": "SECRET PROMPT TEXT",
                "inputs": [],
            }
        ],
    }
    payload = json.loads(build_workflow_descriptor(row)["custom"]["data"])
    assert payload["stepCount"] == 1
    assert payload["steps"][0]["skillAssetId"] == "asset-123"
    # The prompt body is not discovery metadata and is not projected.
    assert "SECRET PROMPT TEXT" not in json.dumps(payload)


def test_build_descriptor_rejects_an_unprojectable_category():
    with pytest.raises(PoisonDescriptorError) as excinfo:
        build_descriptor("hook", {"assetId": "h1", "category": "hook"})
    assert excinfo.value.reason == "unprojectable_category"


# ---------------------------------------------------------------------------
# MCP endpoint records (R4)
# ---------------------------------------------------------------------------


def _m2c_targets():
    return [
        {
            "targetId": "t-media",
            "targetName": "media-processing",
            "tools": [
                {"name": "process_media", "inputSchema": {"type": "object"}},
                {"name": "get_job_status", "inputSchema": {"type": "object"}},
            ],
        },
        {
            "targetId": "t-stem",
            "targetName": "audiostem-separation",
            "tools": [
                {"name": "separate_stems", "inputSchema": {"type": "object"}},
                {"name": "get_job_status", "inputSchema": {"type": "object"}},
            ],
        },
        {
            "targetId": "t-diar",
            "targetName": "speech-diarization",
            "tools": [{"name": "diarize_speech"}, {"name": "get_job_status"}],
        },
    ]


def test_one_record_per_endpoint_with_every_target_nested():
    descriptor = build_mcp_endpoint_descriptor(
        gateway_id="gw-123",
        gateway_url="https://gw-123.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp",
        display_name="M2C media workflows",
        targets=_m2c_targets(),
    )
    # Exactly one descriptor member, i.e. one record — not one per target.
    assert set(descriptor) == {"mcpServer"}
    server = json.loads(descriptor["mcpServer"]["data"])
    assert server["gatewayId"] == "gw-123"
    assert {t["targetName"] for t in server["targets"]} == {
        "media-processing",
        "audiostem-separation",
        "speech-diarization",
    }
    assert server["toolCount"] == 6


def test_repeated_bare_names_stay_distinct_by_target():
    """Three targets expose `get_job_status`; all three must survive.

    Collapsing them would leave the catalog unable to say which target a
    consumer should call — the exact failure R4/R10 name.
    """
    descriptor = build_mcp_endpoint_descriptor(
        gateway_id="gw-123",
        gateway_url="https://gw-123.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp",
        targets=_m2c_targets(),
    )
    tools = json.loads(descriptor["mcpServer"]["additionalData"]["tools"]["data"])["tools"]
    status_tools = [t for t in tools if t["bareToolName"] == "get_job_status"]
    assert len(status_tools) == 3
    assert {t["qualifiedToolName"] for t in status_tools} == {
        "media-processing___get_job_status",
        "audiostem-separation___get_job_status",
        "speech-diarization___get_job_status",
    }
    assert {t["targetId"] for t in status_tools} == {"t-media", "t-stem", "t-diar"}
    for tool in tools:
        assert tool["gatewayId"] == "gw-123"
        assert tool["schemaDigest"].startswith("sha256:")


def test_already_qualified_tool_name_is_not_double_qualified():
    descriptor = build_mcp_endpoint_descriptor(
        gateway_id="gw-1",
        gateway_url="https://gw-1.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp",
        targets=[
            {
                "targetId": "t1",
                "targetName": "media-processing",
                "tools": [{"name": "media-processing___process_media"}],
            }
        ],
    )
    tools = json.loads(descriptor["mcpServer"]["additionalData"]["tools"]["data"])["tools"]
    assert tools[0]["qualifiedToolName"] == "media-processing___process_media"
    assert tools[0]["bareToolName"] == "process_media"


def test_mcp_descriptor_omits_credential_bearing_source_config():
    """The endpoint URL is discovery data; what makes it callable is not.

    `source.fromUrl` carries `credentialProviderConfigurations`, so it is
    deliberately never populated (R4).
    """
    descriptor = build_mcp_endpoint_descriptor(
        gateway_id="gw-1",
        gateway_url="https://gw-1.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp",
        targets=_m2c_targets(),
    )
    assert "source" not in descriptor["mcpServer"]
    assert_no_secrets(descriptor)


def test_endpoint_with_a_prohibited_tool_is_rejected():
    with pytest.raises(PoisonDescriptorError):
        build_mcp_endpoint_descriptor(
            gateway_id="gw-1",
            gateway_url="https://gw-1.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp",
            targets=[{"targetId": "t", "targetName": "x", "tools": [{"name": "load_tool"}]}],
        )


# ---------------------------------------------------------------------------
# Determinism (Property 4)
# ---------------------------------------------------------------------------


def test_descriptor_digest_is_stable_across_key_ordering():
    """Two logically identical rows must digest identically.

    Without this the projector would see every replay as a change and rewrite
    the record forever, so convergence depends on it.
    """
    a = build_skill_descriptor(_org_skill_row(), artifacts_bucket="b")
    reordered = dict(reversed(list(_org_skill_row().items())))
    b = build_skill_descriptor(reordered, artifacts_bucket="b")
    assert descriptor_digest(a) == descriptor_digest(b)


def test_descriptor_digest_changes_when_content_changes():
    a = build_skill_descriptor(_org_skill_row(), artifacts_bucket="b")
    b = build_skill_descriptor(
        _org_skill_row(description="different"), artifacts_bucket="b"
    )
    assert descriptor_digest(a) != descriptor_digest(b)


def test_display_name_change_does_not_change_identity_only_the_digest():
    """A rename must not fork a lineage (R4).

    The record name/version come from canonical identity, so only the
    descriptor digest moves — which is what makes the projector update the
    existing record rather than create a second one.
    """
    a = build_skill_descriptor(_org_skill_row(), artifacts_bucket="b")
    b = build_skill_descriptor(_org_skill_row(name="Renamed"), artifacts_bucket="b")
    assert descriptor_digest(a) != descriptor_digest(b)
    assert json.loads(a["agentSkillsDefinition"]["data"])["id"] == json.loads(
        b["agentSkillsDefinition"]["data"]
    )["id"]


def test_schema_digest_detects_an_upstream_schema_change():
    before = schema_digest({"type": "object", "properties": {"a": {"type": "string"}}})
    after = schema_digest({"type": "object", "properties": {"a": {"type": "number"}}})
    assert before != after
    assert schema_digest(None) == schema_digest({})
