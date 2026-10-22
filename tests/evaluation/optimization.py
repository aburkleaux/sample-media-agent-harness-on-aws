"""Optimization runner — AgentCore Optimization integration (Phase 5).

Wraps the data-plane ``StartRecommendation`` API (SYSTEM_PROMPT_RECOMMENDATION |
TOOL_DESCRIPTION_RECOMMENDATION), polls to a terminal state, and packages an
accepted recommendation into a versioned, immutable config bundle.

The recommendation analyzes agent traces in CloudWatch (from eval runs where a
target evaluator scored low) and returns an optimized system prompt or tool
descriptions with rationale. Async: StartRecommendation → 202 → poll
GetRecommendation until COMPLETED/FAILED.
"""
from __future__ import annotations

from .deployment import deployment_region

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

RECOMMENDATION_TYPES = ("SYSTEM_PROMPT_RECOMMENDATION", "TOOL_DESCRIPTION_RECOMMENDATION")
_TERMINAL = {"COMPLETED", "FAILED", "DELETING"}


@dataclass
class ConfigBundle:
    bundle_id: str
    bundle_name: str
    version: int
    system_prompt: str | None = None
    tool_descriptions: dict | None = None
    model_id: str | None = None
    created_at: str = ""
    source_recommendation_id: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


class OptimizationRunner:
    def __init__(self, region: str | None = None):
        self.region = deployment_region(region)
        self._client = None

    def _dp(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("bedrock-agentcore", region_name=self.region)
        return self._client

    def start_recommendation(self, name: str, recommendation_type: str,
                             recommendation_config: dict,
                             description: str = "") -> dict:
        """Start a recommendation job. Returns the raw StartRecommendation response
        (recommendationId, status, ...). ``recommendation_config`` is the API's
        RecommendationConfig union (input to optimize + traces + eval settings)."""
        if recommendation_type not in RECOMMENDATION_TYPES:
            raise ValueError(f"type must be one of {RECOMMENDATION_TYPES}")
        return self._dp().start_recommendation(
            name=name,
            type=recommendation_type,
            recommendationConfig=recommendation_config,
            description=description,
            clientToken=uuid.uuid4().hex + uuid.uuid4().hex,  # >= 33 chars
        )

    def poll_recommendation(self, recommendation_id: str,
                            interval_s: float = 10.0, max_wait_s: float = 900.0) -> dict:
        """Poll GetRecommendation until terminal or timeout."""
        deadline = time.time() + max_wait_s
        last: dict = {}
        while time.time() < deadline:
            last = self._dp().get_recommendation(recommendationId=recommendation_id)
            if last.get("status") in _TERMINAL:
                return last
            time.sleep(interval_s)
        return last

    def create_config_bundle(self, bundle_name: str, recommendation: dict,
                             version: int = 1) -> ConfigBundle:
        """Package a COMPLETED recommendation into an immutable config bundle."""
        rec_id = recommendation.get("recommendationId")
        # The completed recommendation carries the optimized text under
        # recommendationResult.systemPromptRecommendationResult (real API), or
        # under recommendationConfig for older/tool-description variants.
        result = recommendation.get("recommendationResult") or {}
        sysp_result = result.get("systemPromptRecommendationResult") or {}
        tool_result = result.get("toolDescriptionRecommendationResult") or {}
        cfg = recommendation.get("recommendationConfig") or {}
        system_prompt = (
            sysp_result.get("recommendedSystemPrompt")
            or cfg.get("systemPrompt")
        )
        tool_descriptions = (
            tool_result.get("recommendedToolDescriptions")
            or cfg.get("toolDescriptions")
        )
        return ConfigBundle(
            bundle_id=f"bundle-{uuid.uuid4().hex[:12]}",
            bundle_name=bundle_name,
            version=version,
            system_prompt=system_prompt,
            tool_descriptions=tool_descriptions,
            model_id=cfg.get("modelId"),
            created_at=datetime.now(timezone.utc).isoformat(),
            source_recommendation_id=rec_id,
        )


def target_low_scorers(reports: list[dict], target_evaluator: str, threshold: float = 0.8) -> list[dict]:
    """Filter eval reports to those scoring below threshold on the target
    evaluator (custom_results or builtin_scores). These feed the recommendation."""
    low = []
    for r in reports:
        for c in r.get("custom_results", []):
            if c.get("evaluator_name") == target_evaluator and c.get("score", 1.0) < threshold:
                low.append(r)
                break
        else:
            bs = r.get("builtin_scores", {})
            if target_evaluator in bs and bs[target_evaluator] < threshold:
                low.append(r)
    return low
