"""A render must point the browser at something it can actually fetch.

dispatch_render turns s3:// artifacts into presigned GET urls, and a presigned url
carries only the SIGNER's authority. So a runtime that renders media from a bucket it
cannot read produces urls that are well-formed, unexpired and permanently 403 -- the
browser gets XML and shows a black player stuck at 0:00.

That happened: MediaWorkflowOutputRead was granted to taskAgentRuntime only, but the
render hook fires wherever the tool result lands, and a plan-results retrieval lands
on the MAIN agent. Every analysis view referencing the M2C proxy bucket was born
dead, while the task agent's identical renders worked.

Asserted from SOURCE rather than from a deployed stack, the same way
templateContract.test.ts pins skill-declared templates against the registry: drift
fails here instead of on staging.
"""
from __future__ import annotations

import re
from pathlib import Path

STACK = Path(__file__).resolve().parents[2] / "infra" / "lib" / "agent-infra-stack.ts"

# Every runtime that can reach dispatch_render. The main agent renders on a
# tool-result hook, the task agent renders inside a plan node, and the render agent
# fetches what it is given.
RENDER_CAPABLE_RUNTIMES = ("taskAgentRuntime", "renderAgentRuntime", "agentRuntime")


def _source() -> str:
    return STACK.read_text()


def test_every_render_capable_runtime_can_read_the_media_proxy_bucket():
    src = _source()
    # Grants may be written as a loop over runtimes or as individual calls, so look
    # for each runtime name inside a MediaWorkflowOutputRead-bearing region.
    regions = [
        src[max(0, m.start() - 1200): m.start() + 400]
        for m in re.finditer(r'sid:\s*"MediaWorkflowOutputRead"', src)
    ]
    assert regions, "MediaWorkflowOutputRead grant is missing entirely"
    covered = {r for r in RENDER_CAPABLE_RUNTIMES if any(r in region for region in regions)}
    missing = set(RENDER_CAPABLE_RUNTIMES) - covered
    assert not missing, (
        f"{sorted(missing)} can dispatch a render but is not granted read on the M2C "
        "proxy bucket; its presigned urls will 403 in the browser"
    )


def test_the_grant_is_read_only():
    """Rendering needs GetObject/ListBucket and nothing more."""
    src = _source()
    for m in re.finditer(r'sid:\s*"MediaWorkflowOutputRead"', src):
        window = src[m.start(): m.start() + 400]
        actions = re.findall(r'"(s3:[A-Za-z*]+)"', window)
        assert actions, "grant names no actions"
        assert set(actions) <= {"s3:GetObject", "s3:ListBucket"}, actions


def test_the_surface_path_has_no_manifest_discarding_gate():
    """A manifest the Render Agent wrote must not be dropped on a content guess.

    A gate here treated `tracks: []` as "nothing to draw" and silently discarded
    image renders, because generic-preview carries its content in artifacts/sources.
    Correct manifests were written and never became tabs.
    """
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import render.artifacts as render_artifacts

    assert not hasattr(render_artifacts, "_manifest_has_content"), (
        "a content gate was reintroduced; verify it against real manifests first -- "
        "images live in artifacts/sources, not tracks"
    )
