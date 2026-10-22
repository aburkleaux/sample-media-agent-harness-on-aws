"""The agent must be TOLD the plan result is complete, or it will chain anyway.

get_plan_results was changed to return fully resolved results, but three
instruction sites still told the agent to fan out afterwards:

  * this tool's own docstring -- "Any s3:// refs in the values can be passed to
    read_result_files";
  * the orchestrator system prompt -- "then read_result_files on any s3:// refs it
    returns";
  * the video-processing skill -- pointing at media-outputs___get_download_url.

The agent obeyed all three. Its narration in a live run was verbatim: "The
read_result_files tool can't resolve these directly. Let me get presigned download
URLs via the media-outputs gateway tool." One retrieval cost 61 model steps and
392 seconds. A tool description IS the routing decision, not documentation, so
these are asserted rather than trusted.
"""
from __future__ import annotations

from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
_SKILLS = Path(__file__).resolve().parents[1] / "skills"


def _tool_docstring() -> str:
    import sys

    sys.path.insert(0, str(_SRC))
    from jobs.get_plan_results_tool import get_plan_results

    fn = getattr(get_plan_results, "__wrapped__", None) or getattr(
        get_plan_results, "_tool_func", None
    )
    doc = (fn.__doc__ if fn else None) or get_plan_results.__doc__ or ""
    if not doc:  # Strands may wrap; fall back to the source text.
        doc = (_SRC / "jobs" / "get_plan_results_tool.py").read_text()
    return doc


def test_the_tool_does_not_instruct_the_agent_to_read_result_files():
    doc = _tool_docstring()
    assert "do not chain" in doc.lower() or "do NOT then call" in doc
    # The old instruction must be gone, not merely softened.
    assert "can be passed to ``read_result_files``" not in doc


def test_the_tool_documents_the_resolved_shape():
    doc = _tool_docstring()
    for token in ("kind", "artifacts", "findings", "pending", "error"):
        assert token in doc, token


def test_the_system_prompt_does_not_instruct_the_chain():
    prompt = (_SRC / "prompts" / "orchestrator.py").read_text()
    assert "then `read_result_files` on any `s3://` refs it returns" not in prompt
    assert "RESOLVED results" in prompt


def test_the_skill_reframes_the_download_url_tool_as_user_facing():
    skill = (_SKILLS / "video-processing" / "SKILL.md").read_text()
    assert "not a way to read a result yourself" in skill
    assert "already resolved on `get_plan_results`" in skill


def test_the_prompt_routes_a_single_job_to_the_aggregate():
    """A lone job's results must go through get_plan_results, not a fan-out.

    The poller writes jobs/<jobId>/result.json and the tool reads it, but the prompt
    still framed get_plan_results as "For a PLAN" -- so the agent called get_job and
    then fanned out to read probe.json by hand: 30 steps and 114s for one proxy job.
    Implementation changed, instruction did not. Again.
    """
    prompt = (_SRC / "prompts" / "orchestrator.py").read_text()
    assert "For a SINGLE async job whose RESULTS the user wants" in prompt
    assert "get_plan_results(job_id)" in prompt
