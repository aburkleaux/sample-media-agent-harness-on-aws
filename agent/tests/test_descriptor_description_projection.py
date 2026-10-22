"""The specialty-agent descriptor projects a real description, same schema.

``build_specialty_agent_descriptor`` has always emitted
``"description": _row_str(row, "description") or ""``. Before Phase 1 nothing
wrote the attribute, so every projected Custom Agent carried "".

Populating an ALREADY-DECLARED field is not a shape change, so
``SCHEMA_VERSIONS["specialty-agent"]`` must NOT move. The version assertion is
exact on purpose: an incidental bump would tell every Registry consumer that a
breaking change happened when none did.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capability.descriptors import (  # noqa: E402
    SCHEMA_VERSIONS,
    build_specialty_agent_descriptor,
)


def _row(**overrides):
    row = {
        "assetId": "ag-1",
        "name": "Transcript Cleaner",
        "version": 3,
        "scope": "private",
        "s3Prefix": "users/u1/published/specialty-agents/ag-1/v3/",
    }
    row.update(overrides)
    return row


def _payload(descriptor):
    """Decode whichever descriptor block carries the encoded definition."""
    block = next(iter(descriptor.values()))
    data = block["data"]
    return json.loads(data) if isinstance(data, str) else data


def test_schema_version_is_unchanged():
    assert SCHEMA_VERSIONS["specialty-agent"] == "mediaworks.specialty-agent/2026-08-13"


def test_descriptor_carries_a_populated_description():
    descriptor = build_specialty_agent_descriptor(
        _row(description="Cleans up ASR transcripts")
    )
    assert _payload(descriptor)["description"] == "Cleans up ASR transcripts"


def test_row_without_a_description_still_builds():
    descriptor = build_specialty_agent_descriptor(_row())
    assert _payload(descriptor)["description"] == ""
