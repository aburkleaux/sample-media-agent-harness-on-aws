"""Hybrid-search tools are withheld from every gateway connection (media-harness-projects-access task 11.8).

The runtime half of Component 8: ``GatewayConnection._withhold_tools`` must remove exactly the tools whose
full name — or name after one ``___`` split — begins with a prefix in the shared
``project_search/withheld-tool-prefixes.json`` fixture, on EVERY gateway, so the hybrid-search tools are
never exposed to the model (Req 5.4). The proxy's ``ui/server/toolCatalog.ts`` reads the same fixture.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mcp_manager import WITHHELD_TOOL_PREFIXES, GatewayConnection  # noqa: E402

FIXTURE = os.path.join(
    os.path.dirname(__file__), "..", "src", "project_search", "withheld-tool-prefixes.json"
)


class _Tool:
    """Minimal stand-in that ``_extract_tool_name`` reads via ``tool_spec['name']``."""

    def __init__(self, name: str) -> None:
        self.tool_spec = {"name": name, "description": ""}


def _conn() -> GatewayConnection:
    return GatewayConnection("g", {"url": "https://example.invalid/mcp", "name": "any-gateway"})


def _withhold(names):
    kept = _conn()._withhold_tools([_Tool(n) for n in names])
    return [t.tool_spec["name"] for t in kept]


def test_prefixes_come_from_the_shared_fixture():
    with open(FIXTURE, encoding="utf-8") as fh:
        on_disk = json.load(fh)
    assert on_disk == ["hybrid-search___", "hybrid-search-admin___", "project-graph___",
                       "prompt-registry-admin___", "prompt-registry___put_prompt", "prompt-registry___promote_prompt",
                       "prompt-registry___delete_prompt", "prompt-registry___deprecate_prompt", "media-uploads___"]
    assert WITHHELD_TOOL_PREFIXES == tuple(on_disk)


def test_withholds_bare_and_namespaced_hybrid_search_names():
    withheld = [
        "hybrid-search___search_content",
        "hybrid-search___index_content",
        "hybrid-search-admin___create_collection",
        "mediaworks-harness-m2c-search___hybrid-search___search_content",
        "project-graph___graph_query",           # the knowledge-graph tools are Search_Gateway-only too
        "tgt___project-graph___graph_find",
        "tgt___hybrid-search-admin___delete_collection",
    ]
    assert _withhold(withheld) == []


def test_keeps_every_non_hybrid_search_tool():
    kept = [
        "media-processing___process_media",
        "face-recognition___recognize_faces",
        "tgt___aws___search_documentation",  # two separators, unrelated
        "x_amz_bedrock_agentcore_search",
        "search_content",  # not prefixed by a withheld prefix on its own
        "hybrid-search",  # no trailing separator → not a withheld prefix match
        "hybrid-search-admins___x",  # 'admins' ≠ 'admin___'
    ]
    assert _withhold(kept) == kept


def test_removes_only_the_matching_names_from_a_mixed_list():
    tools = [
        "media-processing___process_media",
        "tgt___hybrid-search___search_content",
        "shot-segmentation___segment_shots",
        "hybrid-search-admin___rebuild_index",
    ]
    assert _withhold(tools) == [
        "media-processing___process_media",
        "shot-segmentation___segment_shots",
    ]


def test_prompt_registry_is_read_only_for_the_model():
    """The prompts are shared M2C workflow configuration: reads stay, every write and the admin target go."""
    names = ["prompt-registry___put_prompt", "prompt-registry___promote_prompt", "prompt-registry___delete_prompt",
             "prompt-registry___deprecate_prompt", "prompt-registry-admin___put_prompt",
             "prompt-registry-admin___list_prompts", "prompt-registry___get_prompt", "prompt-registry___list_prompts",
             "prompt-registry___describe_tool"]
    assert _withhold(names) == ["prompt-registry___get_prompt", "prompt-registry___list_prompts", "prompt-registry___describe_tool"]


def test_withholds_the_m2c_upload_tools():
    # M2C alpha.20261017a: presigned uploads into the shared ingest bucket with the gateway's role (any key).
    assert _withhold(["media-uploads___start_upload", "tgt___media-uploads___complete_upload",
                      "media-ingest___list_files"]) == ["media-ingest___list_files"]
