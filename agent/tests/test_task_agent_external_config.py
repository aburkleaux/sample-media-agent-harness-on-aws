"""Seam test: external invoke payload → server-side Custom Agent config resolution.

The proxy's ``stampInvocationIdentity`` (ui/server/invoke/stampIdentity.ts)
dispatches an outbound body carrying ONLY identity + the published-version
reference::

    {
      "prompt":       <caller>,
      "actor_id":     <ownerUserId>,
      "actor_groups": [...],
      "principal":    {"issuer": ..., "sub": ...},
      "session_id":   "EXT#...",
      "agent_id":     <agentId>,
      "version":      <int | None>,
    }

— no ``custom_agent`` dict, no persona, no skills, no tools. This suite locks
the agent side of that contract: ``task_agent._fetch_external_custom_agent_config``
must resolve the referenced published config itself (persona.md, skill defs,
tool collections, external opt-in) from the AssetIndex + lab artifacts bucket,
Owner-scoped, and the result must drive ``custom_agent_resolver.resolve_config``
exactly as an inline ``custom_agent`` dict would.

This is the regression test for the custom-agents external-invoke seam defect
(proxy and runtime each tested green against their own assumption while the
payload shapes disagreed).

Validates: Requirements 5.7, 6.2, 7.1, 7.5, 8.7, 11.1 (seam-level)
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

TABLE_NAME = "seam-test-asset-index"
BUCKET = "seam-test-lab-artifacts"
REGION = "us-east-1"

OWNER = "owner-user-1"
AGENT_ID = "agent-abc"
SKILL_ID = "skill-caption"
TC_ID = "tc-weather"
ISSUER = "https://issuer.example.com"
SUB = "external-caller-42"

PERSONA_V1 = "You are the Caption Bot v1. Always answer in captions."
PERSONA_V2 = "You are the Caption Bot v2."

SKILL_MD = """---
name: caption-writer
description: Writes broadcast captions.
---
Write captions in sentence case, max 32 chars per line.
"""


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _create_table(dynamodb):
    dynamodb.create_table(
        TableName=TABLE_NAME,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "byPublishedId",
                "KeySchema": [
                    {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def _published_agent(version: int, *, current: bool, persona_version: int | None = None,
                     owner: str = OWNER):
    from lab.models import IntegrationConfig, PublishPolicy, ScopeHistoryEntry, SpecialtyAgent

    now = int(time.time() * 1000)
    return SpecialtyAgent(
        asset_id=AGENT_ID,
        category="specialty-agent",
        name="Caption Bot",
        status="published",
        scope="private",
        s3_prefix=f"users/{owner}/published/specialty-agents/{AGENT_ID}/v{version}/",
        created_at=now,
        updated_at=now,
        version=version,
        current=current,
        publish_policy=PublishPolicy(owner_ids=[owner]),
        scope_history=[
            ScopeHistoryEntry(
                from_scope=None, to_scope="private", actor_id=owner,
                timestamp="2026-07-22T00:00:00Z",
            )
        ],
        compatible_skills=[SKILL_ID],
        model="us.anthropic.claude-sonnet-5",
        tool_collections=[TC_ID],
        integration_config=IntegrationConfig(
            enabled=True,
            issuer=ISSUER,
            audience="caption-bot",
            external_tool_collections=[TC_ID],
        ),
    )


def _published_skill(version: int = 1, *, current: bool = True):
    from lab.models import PublishPolicy, ScopeHistoryEntry, Skill

    now = int(time.time() * 1000)
    return Skill(
        asset_id=SKILL_ID,
        category="skill",
        name="caption-writer",
        status="published",
        scope="private",
        s3_prefix=f"users/{OWNER}/published/skills/{SKILL_ID}/v{version}/",
        created_at=now,
        updated_at=now,
        version=version,
        current=current,
        publish_policy=PublishPolicy(owner_ids=[OWNER]),
        scope_history=[
            ScopeHistoryEntry(
                from_scope=None, to_scope="private", actor_id=OWNER,
                timestamp="2026-07-22T00:00:00Z",
            )
        ],
    )


def _tool_collection():
    from lab.models import ToolCollection, ToolRef

    now = int(time.time() * 1000)
    return ToolCollection(
        asset_id=TC_ID,
        category="tool-collection",
        name="weather-tools",
        status="draft",
        scope="private",
        s3_prefix=f"tool-collections/{TC_ID}/",
        created_at=now,
        updated_at=now,
        tool_refs=[ToolRef(name="get_weather", version="1")],
    )


def _external_payload(*, version=1, actor=OWNER, agent_id=AGENT_ID) -> dict:
    """The EXACT outbound shape stampInvocationIdentity dispatches (post-slim)."""
    return {
        "prompt": "What's the weather, in captions?",
        "actor_id": actor,
        "actor_groups": [],
        "principal": {"issuer": ISSUER, "sub": SUB},
        "session_id": f"EXT#deadbeefdeadbeef#{SUB}",
        "agent_id": agent_id,
        "version": version,
    }


@pytest.fixture()
def seam_env(monkeypatch):
    """Moto-backed AssetIndex table + lab artifacts bucket, seeded and wired
    into the task_agent module globals."""
    with mock_aws():
        dynamodb = boto3.client("dynamodb", region_name=REGION)
        _create_table(dynamodb)
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET)

        import task_agent
        from lab.asset_index import AssetIndex

        table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)
        monkeypatch.setattr(task_agent, "ASSET_INDEX_TABLE", TABLE_NAME)
        monkeypatch.setattr(task_agent, "LAB_ARTIFACTS_BUCKET", BUCKET)
        monkeypatch.setattr(task_agent, "_lab_ddb_table", table)
        monkeypatch.setattr(task_agent, "_lab_s3_client", s3)

        index = AssetIndex(ddb_table=table, actor_id=OWNER)

        agent_v1 = _published_agent(1, current=True)
        _run(index.put(agent_v1, owner_user_id=OWNER))
        s3.put_object(
            Bucket=BUCKET,
            Key=f"{agent_v1.s3_prefix}persona.md",
            Body=PERSONA_V1.encode(),
        )

        skill = _published_skill()
        _run(index.put(skill, owner_user_id=OWNER))
        s3.put_object(
            Bucket=BUCKET, Key=f"{skill.s3_prefix}SKILL.md", Body=SKILL_MD.encode()
        )

        _run(index.put(_tool_collection(), owner_user_id=OWNER))

        yield {"task_agent": task_agent, "index": index, "s3": s3}


# ---------------------------------------------------------------------------
# Happy path: the stamped reference resolves to the full published config
# ---------------------------------------------------------------------------


class TestExternalConfigResolution:
    def test_fetch_resolves_persona_skills_tools_from_reference(self, seam_env):
        ta = seam_env["task_agent"]
        cfg = _run(ta._fetch_external_custom_agent_config(_external_payload()))

        assert cfg is not None
        assert cfg["persona"] == PERSONA_V1
        assert cfg["model"] == "us.anthropic.claude-sonnet-5"
        assert cfg["compatible_skills"] == [SKILL_ID]
        assert cfg["external_tool_collections"] == [TC_ID]

        assert len(cfg["skills"]) == 1
        sk = cfg["skills"][0]
        assert sk["id"] == SKILL_ID
        assert sk["name"] == "caption-writer"
        assert sk["description"] == "Writes broadcast captions."
        assert "max 32 chars" in sk["instructions"]

        assert len(cfg["tool_collections"]) == 1
        refs = getattr(cfg["tool_collections"][0], "tool_refs", None)
        assert refs and refs[0].name == "get_weather"

    def test_fetched_config_drives_resolver_like_inline_custom_agent(self, seam_env):
        """The seam end-to-end: fetched dict → resolve_config → payload fields."""
        ta = seam_env["task_agent"]
        from custom_agent_resolver import resolve_config

        cfg = _run(ta._fetch_external_custom_agent_config(_external_payload()))
        resolved = resolve_config(
            persona=cfg.get("persona"),
            model=cfg.get("model"),
            skills=cfg.get("skills") or [],
            tool_collections=cfg.get("tool_collections") or [],
            available_tool_names={"get_weather", "unrelated_tool"},
            known_model_ids=None,
            compatible_skills=cfg.get("compatible_skills"),
        )

        payload: dict = {}
        resolved.apply_to_payload(payload)

        assert payload["system_prompt"] == PERSONA_V1
        assert payload["model_id"] == "us.anthropic.claude-sonnet-5"
        assert payload["tools"] == ["get_weather"]  # explicit list, never None
        assert [s["name"] for s in payload["skills"]] == ["caption-writer"]

    def test_external_opt_in_narrowing_uses_fetched_collections(self, seam_env):
        ta = seam_env["task_agent"]
        from custom_agent_resolver import external_opt_in_tool_names

        cfg = _run(ta._fetch_external_custom_agent_config(_external_payload()))
        opted = external_opt_in_tool_names(
            cfg.get("tool_collections") or [], cfg.get("external_tool_collections") or []
        )
        assert opted == {"get_weather"}

        no_opt_in = external_opt_in_tool_names(cfg.get("tool_collections") or [], [])
        assert no_opt_in == set()


# ---------------------------------------------------------------------------
# Version pinning + fallback
# ---------------------------------------------------------------------------


class TestVersionSelection:
    def _publish_v2(self, seam_env):
        index, s3 = seam_env["index"], seam_env["s3"]
        v2 = _published_agent(2, current=True)
        v1_row = _published_agent(1, current=False)
        # Rewrite v1 as non-current (moto put is create-only via condition; use
        # the raw table to flip the flag the way the canonical ops would).
        _run(index.update(
            pk=f"USER#{OWNER}#VERSIONS",
            sk=f"specialty-agent#{AGENT_ID}#v0000000001",
            update_expression="SET #c = :c",
            expression_attribute_names={"#c": "current"},
            expression_attribute_values={":c": False},
        ))
        _run(index.put(v2, owner_user_id=OWNER))
        s3.put_object(
            Bucket=BUCKET, Key=f"{v2.s3_prefix}persona.md", Body=PERSONA_V2.encode()
        )
        return v1_row, v2

    def test_stamped_version_pins_exact_row(self, seam_env):
        ta = seam_env["task_agent"]
        self._publish_v2(seam_env)

        cfg = _run(ta._fetch_external_custom_agent_config(_external_payload(version=1)))
        assert cfg["persona"] == PERSONA_V1

        cfg2 = _run(ta._fetch_external_custom_agent_config(_external_payload(version=2)))
        assert cfg2["persona"] == PERSONA_V2

    def test_unknown_stamped_version_falls_back_to_current(self, seam_env):
        ta = seam_env["task_agent"]
        self._publish_v2(seam_env)

        cfg = _run(ta._fetch_external_custom_agent_config(_external_payload(version=99)))
        assert cfg is not None
        assert cfg["persona"] == PERSONA_V2  # current row

    def test_null_version_uses_current(self, seam_env):
        ta = seam_env["task_agent"]
        cfg = _run(ta._fetch_external_custom_agent_config(_external_payload(version=None)))
        assert cfg is not None
        assert cfg["persona"] == PERSONA_V1


# ---------------------------------------------------------------------------
# Degradation + scope guards (fail closed, never raise)
# ---------------------------------------------------------------------------


class TestDegradationAndScope:
    def test_unknown_agent_returns_none(self, seam_env):
        ta = seam_env["task_agent"]
        cfg = _run(ta._fetch_external_custom_agent_config(
            _external_payload(agent_id="no-such-agent")
        ))
        assert cfg is None

    def test_owner_mismatch_returns_none(self, seam_env):
        """A stamped actor that doesn't own the published row must not have
        the config resolved under it (Req 6.2 owner-scoped resolution)."""
        ta = seam_env["task_agent"]
        cfg = _run(ta._fetch_external_custom_agent_config(
            _external_payload(actor="some-other-user")
        ))
        assert cfg is None

    def test_missing_table_returns_none(self, seam_env, monkeypatch):
        ta = seam_env["task_agent"]
        monkeypatch.setattr(ta, "_lab_ddb_table", None)
        monkeypatch.setattr(ta, "ASSET_INDEX_TABLE", "")
        cfg = _run(ta._fetch_external_custom_agent_config(_external_payload()))
        assert cfg is None

    def test_unresolvable_skill_is_skipped_not_fatal(self, seam_env):
        ta = seam_env["task_agent"]
        index, s3 = seam_env["index"], seam_env["s3"]

        agent_v2 = _published_agent(2, current=True)
        agent_v2.compatible_skills = [SKILL_ID, "ghost-skill"]
        _run(index.put(agent_v2, owner_user_id=OWNER))
        s3.put_object(
            Bucket=BUCKET, Key=f"{agent_v2.s3_prefix}persona.md",
            Body=PERSONA_V2.encode(),
        )

        cfg = _run(ta._fetch_external_custom_agent_config(_external_payload(version=2)))
        assert cfg is not None
        assert [s["id"] for s in cfg["skills"]] == [SKILL_ID]

    def test_missing_persona_yields_none_persona_but_config_survives(self, seam_env):
        ta = seam_env["task_agent"]
        seam_env["s3"].delete_object(
            Bucket=BUCKET,
            Key=f"users/{OWNER}/published/specialty-agents/{AGENT_ID}/v1/persona.md",
        )
        cfg = _run(ta._fetch_external_custom_agent_config(_external_payload()))
        assert cfg is not None
        assert cfg["persona"] is None
        assert cfg["skills"], "skills still resolved"

    def test_unresolvable_tool_collection_becomes_dropped_marker(self, seam_env):
        ta = seam_env["task_agent"]
        from custom_agent_resolver import resolve_config

        index, s3 = seam_env["index"], seam_env["s3"]
        agent_v2 = _published_agent(2, current=True)
        agent_v2.tool_collections = [TC_ID, "ghost-collection"]
        _run(index.put(agent_v2, owner_user_id=OWNER))
        s3.put_object(
            Bucket=BUCKET, Key=f"{agent_v2.s3_prefix}persona.md",
            Body=PERSONA_V2.encode(),
        )

        cfg = _run(ta._fetch_external_custom_agent_config(_external_payload(version=2)))
        resolved = resolve_config(
            persona=cfg.get("persona"),
            model=cfg.get("model"),
            skills=cfg.get("skills") or [],
            tool_collections=cfg.get("tool_collections") or [],
            available_tool_names={"get_weather"},
            known_model_ids=None,
            compatible_skills=cfg.get("compatible_skills"),
        )
        assert resolved.tools == ["get_weather"]
        assert "ghost-collection" in resolved.dropped_collections


# ---------------------------------------------------------------------------
# The version's pinned tool collections
# ---------------------------------------------------------------------------
#
# A published version stores its `tool_collections` as bare ids pointing at the
# owner's flat, MUTABLE collection rows. Resolving those live at every invoke
# meant an immutable version was not immutable where tools were concerned:
# editing a draft collection retroactively changed every published version (and
# widened the external allowlist) with no publish or version bump, and deleting
# one reduced them to zero tools while the invoke still returned 200 in persona.
#
# `tool_collection_snapshots` is the pin. These tests are written against the
# hazards rather than the mechanism: mutate or delete the collection out from
# under a pinned version and assert what it binds does not move.


def _pinned_agent(version: int, *, snapshots, current: bool = True):
    """A published agent version carrying an explicit pin.

    ``snapshots`` is passed through verbatim so a test can pin ``[]`` — which
    must bind nothing — distinctly from ``None``, which means unpinned.
    """
    agent = _published_agent(version, current=current)
    agent.tool_collection_snapshots = snapshots
    return agent


def _snapshot_of(tool_name: str, *, collection_id: str = TC_ID):
    from lab.models import ToolCollectionSnapshot, ToolRef

    return ToolCollectionSnapshot(
        asset_id=collection_id,
        name="weather-tools",
        tool_refs=[ToolRef(name=tool_name, version="1")],
    )


def _seed_pinned_v2(seam_env, *, snapshots):
    """Publish-shaped v2 with a pin, plus its persona. Returns the fetched config."""
    index, s3 = seam_env["index"], seam_env["s3"]
    agent_v2 = _pinned_agent(2, snapshots=snapshots)
    _run(index.put(agent_v2, owner_user_id=OWNER))
    s3.put_object(
        Bucket=BUCKET, Key=f"{agent_v2.s3_prefix}persona.md", Body=PERSONA_V2.encode()
    )
    return _run(
        seam_env["task_agent"]._fetch_external_custom_agent_config(
            _external_payload(version=2)
        )
    )


class TestPinnedToolCollections:
    def test_pin_wins_over_the_live_collection_row(self, seam_env):
        """The mutation hazard. The live row says one thing, the pin another."""
        cfg = _seed_pinned_v2(
            seam_env, snapshots=[_snapshot_of("get_weather_as_published")]
        )

        assert len(cfg["tool_collections"]) == 1
        refs = cfg["tool_collections"][0].tool_refs
        # The seeded live row carries `get_weather`; the pin must be preferred.
        assert [r.name for r in refs] == ["get_weather_as_published"]

    def test_editing_the_collection_does_not_change_a_published_version(
        self, seam_env
    ):
        """Edit the collection after publish; the version binds what it froze."""
        from lab.models import ToolRef

        cfg_before = _seed_pinned_v2(seam_env, snapshots=[_snapshot_of("get_weather")])
        assert [
            r.name for r in cfg_before["tool_collections"][0].tool_refs
        ] == ["get_weather"]

        # Owner edits the draft collection: swaps the tool out entirely. Written
        # through the table rather than `index.put`, which carries an
        # attribute_not_exists guard and so cannot overwrite an existing row.
        from lab.asset_index import make_pk_user_owned, make_sk_owned
        from lab.models import to_index_item

        edited = _tool_collection()
        edited.tool_refs = [ToolRef(name="totally_different_tool", version="1")]
        item = to_index_item(
            edited, owner_user_id=OWNER, owner_group_id=None, owner_org_id=None
        )
        item["PK"] = make_pk_user_owned(OWNER)
        item["SK"] = make_sk_owned("tool-collection", TC_ID)
        seam_env["index"]._table.put_item(Item=item)

        cfg_after = _run(
            seam_env["task_agent"]._fetch_external_custom_agent_config(
                _external_payload(version=2)
            )
        )
        assert [
            r.name for r in cfg_after["tool_collections"][0].tool_refs
        ] == ["get_weather"]

    def test_deleting_the_collection_does_not_disarm_a_published_version(
        self, seam_env
    ):
        """The deletion hazard, which used to degrade silently to zero tools."""
        from custom_agent_resolver import resolve_config
        from lab.asset_index import make_pk_user_owned, make_sk_owned

        _seed_pinned_v2(seam_env, snapshots=[_snapshot_of("get_weather")])

        # Hard-delete the collection row, as ToolCollectionCRUD.delete does.
        seam_env["index"]._table.delete_item(
            Key={
                "PK": make_pk_user_owned(OWNER),
                "SK": make_sk_owned("tool-collection", TC_ID),
            }
        )

        cfg = _run(
            seam_env["task_agent"]._fetch_external_custom_agent_config(
                _external_payload(version=2)
            )
        )
        resolved = resolve_config(
            persona=cfg.get("persona"),
            model=cfg.get("model"),
            skills=cfg.get("skills") or [],
            tool_collections=cfg.get("tool_collections") or [],
            available_tool_names={"get_weather"},
            known_model_ids=None,
            compatible_skills=cfg.get("compatible_skills"),
        )
        # Previously: tools == [] and a log line nobody sees.
        assert resolved.tools == ["get_weather"]
        assert resolved.dropped_collections == []

    def test_a_version_pinned_to_no_collections_binds_nothing(self, seam_env):
        """``[]`` is a real pin, not a missing one — it must not resolve live."""
        from custom_agent_resolver import resolve_config

        cfg = _seed_pinned_v2(seam_env, snapshots=[])

        assert cfg["tool_collections"] == []
        resolved = resolve_config(
            persona=cfg.get("persona"),
            model=cfg.get("model"),
            skills=cfg.get("skills") or [],
            tool_collections=cfg.get("tool_collections") or [],
            available_tool_names={"get_weather"},
            known_model_ids=None,
            compatible_skills=cfg.get("compatible_skills"),
        )
        assert resolved.tools == []

    def test_an_unpinned_version_still_resolves_live(self, seam_env):
        """Rows published before the pin existed keep working, unchanged."""
        cfg = _run(
            seam_env["task_agent"]._fetch_external_custom_agent_config(
                _external_payload(version=1)
            )
        )
        refs = cfg["tool_collections"][0].tool_refs
        assert [r.name for r in refs] == ["get_weather"]

    def test_external_opt_in_is_computed_from_the_pin(self, seam_env):
        """Else editing a collection silently widens what callers can reach."""
        from custom_agent_resolver import external_opt_in_tool_names

        cfg = _seed_pinned_v2(
            seam_env, snapshots=[_snapshot_of("get_weather_as_published")]
        )

        opted = external_opt_in_tool_names(
            cfg["tool_collections"], cfg["external_tool_collections"]
        )
        # Matched by collection id, which is why the pin is per-collection rather
        # than a flattened union of refs.
        assert opted == {"get_weather_as_published"}
