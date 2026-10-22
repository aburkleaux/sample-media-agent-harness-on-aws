"""Render-manifest keys for plan nodes — pure, no AWS.

A plan node's render manifest lives at ``artifacts/<sid>/render/<key>.json``. Node ids repeat across the
plans of one session (every guided phase, and one plan per video, uses ``sg1_…``/``sg2_…``), so the node id
alone is not an identity: staging session d6a560a8 ran three Phase 1 plans, all three videos wrote
``render/sg2_diarize_speech.json``, and the richer-wins guard kept NBA's diarization on Meridian's tab.
The key is therefore ``<plan_id>--<node_id>``, built HERE for all three paths that must agree on it:
the in-turn plan-result render groups (``hooks.actions``), a job reached by id
(``hooks.provider.canonical_render_key``), and an explicit render keyed from the artifact path
(``render_agent.render_key_from_artifact_paths``, ``output/<plan_id>/<node_id>/<file>``).
"""
from __future__ import annotations

import re
from typing import Optional

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def sanitize(value: str) -> str:
    """S3-key-safe form, matching ``render.artifacts._render_manifest_id``'s sanitisation."""
    return _UNSAFE.sub("_", value)[:120]


def plan_node_render_key(plan_id: Optional[str], node_id: Optional[str]) -> str:
    """``<plan_id>--<node_id>`` (sanitized) for a plan node; "" with no node.

    Returned unchanged (callers sanitize, as before) when no plan is known, or when the node id is an
    execution ARN: a standalone job's group uses its job id as node id, which is already unique, and
    ``canonical_render_key`` keys that job by the same id.
    """
    node = str(node_id or "").strip()
    if not node:
        return ""
    plan = str(plan_id or "").strip()
    if not plan or node.startswith("arn:"):
        return node
    return sanitize(f"{plan}--{node}")
