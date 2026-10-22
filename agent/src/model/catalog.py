"""Discover available Bedrock inference profiles at startup.

Queries ListInferenceProfiles and filters to text/chat models,
providing a catalog the main agent can reference in its system prompt
and pass to dispatch_tool.
"""

import logging
import os
from typing import Optional

import boto3

from model.factory import supports_reasoning_effort

logger = logging.getLogger(__name__)

# Non-text model keywords to filter out
_EXCLUDE = {
    "stable", "embed", "pegasus", "upscale", "outpaint", "inpaint",
    "background", "recolor", "replace", "style", "sketch", "structure",
    "erase", "image",
}


# Superseded generations. Every one of these is strictly replaced by something
# else already in the list, and together they were a quarter of the picker.
# Matched against the profile ID (stable) rather than the display name.
_LEGACY = (
    "claude-3-haiku", "claude-3-sonnet",      # 2024, no prompt caching
    "claude-opus-4-1",                        # superseded by 4.5+
    "llama3-1", "llama3-2", "llama3-3",       # superseded by Llama 4
    "meta.llama3-1", "meta.llama3-2", "meta.llama3-3",
)


def discover_models(region: Optional[str] = None) -> list[dict]:
    """Return available text/chat inference profiles as [{id, name, supportsEffort}].

    ``supportsEffort`` is whether the model accepts the reasoning-effort cap
    (``output_config.effort``); the UI uses it to show the effort slider only for
    capable models, and sending the field to an incapable model is a hard 400.
    """
    region = region or os.environ.get("AWS_REGION", "us-west-2")
    try:
        client = boto3.client("bedrock", region_name=region)
        profiles = []
        paginator = client.get_paginator("list_inference_profiles")
        for page in paginator.paginate(typeEquals="SYSTEM_DEFINED", maxResults=100):
            for p in page.get("inferenceProfileSummaries", []):
                if p.get("status") != "ACTIVE":
                    continue
                name_lower = (p.get("inferenceProfileName") or "").lower()
                if any(kw in name_lower for kw in _EXCLUDE):
                    continue
                if any(tag in p["inferenceProfileId"].lower() for tag in _LEGACY):
                    continue
                profiles.append({
                    "id": p["inferenceProfileId"],
                    "name": p["inferenceProfileName"],
                    "supportsEffort": supports_reasoning_effort(p["inferenceProfileId"]),
                })
        logger.info("Discovered %d text/chat models from Bedrock", len(profiles))
        return profiles
    except Exception as e:
        logger.error("Failed to discover models: %s", e)
        return []
