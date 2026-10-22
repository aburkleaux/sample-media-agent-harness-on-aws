"""Main agent: session-scoped output guidance every turn + turn-end lineage (asset-lineage §4).

Staging session 833a079f: main.py never told the model its session id (task_agent.py does, via
``_s3_context_guidance``), so the orchestrator's code tool invented ``artifacts/<random uuid>/
output/`` for the user's clip. The orchestrator prompt now names the real session id, the exact
``artifacts/<sid>/output/`` prefix and the bucket, rebuilt every turn.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from prompts.orchestrator import (  # noqa: E402
    build_orchestrator_system_prompt,
    build_session_output_guidance,
)

SID = "833a079f-1111-4222-8333-444455556666"


def test_guidance_names_the_real_session_prefix_and_bucket():
    text = build_session_output_guidance(SID, "skills-bucket", "artifacts/", "us-west-2", "d1.cloudfront.net")
    assert f"`{SID}`" in text
    assert f"s3://skills-bucket/artifacts/{SID}/output/<filename>" in text
    assert f"`artifacts/{SID}/output/`" in text
    assert "region_name='us-west-2'" in text
    assert f"https://d1.cloudfront.net/artifacts/{SID}/output/<filename>" in text
    assert "Never invent another session id" in text


def test_no_session_means_no_guidance_and_an_unchanged_prompt():
    assert build_session_output_guidance("", "b", "artifacts/") == ""
    base = build_orchestrator_system_prompt("", "", "b", "artifacts/", "u", "")
    assert build_orchestrator_system_prompt("", "", "b", "artifacts/", "u", "",
                                            session_output_guidance="") == base


def test_orchestrator_prompt_carries_the_guidance():
    g = build_session_output_guidance(SID, "b", "artifacts/")
    prompt = build_orchestrator_system_prompt("", "", "b", "artifacts/", "u", "", session_output_guidance=g)
    assert g in prompt
    assert prompt.index("S3 BUCKETS YOU MAY ENCOUNTER") < prompt.index(g) < prompt.index("ASYNC JOBS:")


def test_main_builds_the_prompt_with_this_turns_session(monkeypatch):
    import main
    monkeypatch.setattr(main, "_format_catalog_text", lambda *a, **k: "<CATALOG>")
    monkeypatch.setattr(main, "_build_catalog", lambda *a, **k: {})
    turn_a = main._build_system_prompt(prompt="", session_id=SID)
    turn_b = main._build_system_prompt(prompt="", session_id="another-session")
    assert f"{main.S3_PREFIX}{SID}/output/" in turn_a
    assert f"{main.S3_PREFIX}another-session/output/" in turn_b and SID not in turn_b
    assert main.S3_BUCKET in turn_a


def test_main_invoke_resolves_the_session_before_rebuilding_the_prompt():
    """The prompt is rebuilt per turn inside invoke; the session id must be passed to it."""
    src = (Path(__file__).resolve().parents[1] / "src" / "main.py").read_text()
    resolve = src.index('session_id = payload.get("session_id") or _synthesize_session_id()')
    rebuild = src.index("inventory_snapshot=inventory_snapshot, prompt=prompt, session_id=session_id")
    assert resolve < rebuild


def test_execution_summary_carries_the_turn_lineage(monkeypatch):
    import main
    from media import turn_outputs
    monkeypatch.setenv("SKILLS_S3_BUCKET", "skills-bucket")
    turn_outputs.open_turn()
    try:
        summary = {"type": "execution_summary"}
        main._attach_turn_lineage(summary)
        assert "lineage" not in summary  # nothing written -> field absent
        turn_outputs.note_write(f"artifacts/{SID}/output/crop_1.mp4",
                                ["projects/p1/a1/source.mp4"], tool="crop_video")
        main._attach_turn_lineage(summary)
        assert summary["lineage"] == [{
            "output": f"s3://skills-bucket/artifacts/{SID}/output/crop_1.mp4",
            "inputs": ["s3://skills-bucket/projects/p1/a1/source.mp4"],
            "tool": "crop_video"}]
    finally:
        turn_outputs.open_turn()
