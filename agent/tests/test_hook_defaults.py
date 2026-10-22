"""render-artifacts default hook (broadened to all artifacts) + sole-renderer cleanup."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from hooks.defaults import default_hook_specs  # noqa: E402
from hooks.engine import HookEngine  # noqa: E402


def test_default_render_artifacts_present_and_enabled():
    specs = default_hook_specs()
    names = {s.name for s in specs}
    assert "render-artifacts" in names
    hook = next(s for s in specs if s.name == "render-artifacts")
    assert hook.enabled is True
    assert hook.event == "after_tool_result"
    assert hook.action_type == "render"
    # Was "result.urls" -- every url in the tool result, which put fal.ai's
    # queue endpoints in the render panel. Must match what the condition gates on.
    assert hook.action_params.get("urls") == "result.new_artifacts"


def test_fires_on_producer_results_and_artifact_tool_not_relays():
    eng = HookEngine(default_hook_specs())
    # A *results* tool with media → fires. (run_id "" = standalone job, not a
    # plan node; the merged condition gates auto-render on run_id == "".)
    results_ctx = {"tool": {"role": "results", "name": "x___get_diarization_results"}, "result": {"urls": ["https://cf/x.wav"], "mimes": ["audio/wav"], "new_artifacts": ["https://cf/x.wav"]}, "run_id": ""}
    # A media-workflow *status* tool (M2C get_job_status) whose SUCCEEDED poll
    # returns output URLs → fires (the new gateway shape).
    status_ctx = {"tool": {"role": "status", "name": "speech-diarization___get_job_status"}, "result": {"urls": ["https://cf/x.vtt"], "mimes": ["text/vtt"], "job_status": "SUCCEEDED", "new_artifacts": ["https://cf/x.vtt"]}, "run_id": ""}
    # An in-progress status poll (RUNNING) with URLs echoed back → no fire
    # (prevents the render loop where every poll triggers a render).
    status_running_ctx = {"tool": {"role": "status", "name": "speech-diarization___get_job_status"}, "result": {"urls": ["https://cf/x.vtt"], "mimes": ["text/vtt"], "job_status": "RUNNING", "new_artifacts": ["https://cf/x.vtt"]}, "run_id": ""}
    # The `artifact` tool (ad-hoc generated file) with media → fires.
    artifact_ctx = {"tool": {"role": "other", "name": "artifact"}, "result": {"urls": ["https://cf/y.mp4"], "mimes": ["video/mp4"], "new_artifacts": ["https://cf/y.mp4"]}, "run_id": ""}
    # An orchestration RELAY whose text echoes artifact URLs → does NOT fire
    # (prevents the dispatched-results double render).
    # An orchestration RELAY echoing an artifact URL the sub-agent already
    # rendered. `new_artifacts` is empty because the real context builder
    # excludes anything already dispatched -- see
    # test_relay_does_not_re_render_via_the_real_context_builder, which proves
    # that rather than asserting it by hand here.
    relay_ctx = {"tool": {"role": "other", "name": "dispatch_tool"}, "result": {"urls": ["https://cf/x.wav"], "mimes": ["audio/wav"], "new_artifacts": []}, "run_id": ""}
    # URLs present but no recognized artifact mime (bare web link) → no fire.
    weblink_ctx = {"tool": {"role": "results", "name": "z_results"}, "result": {"urls": ["https://example.com/page"], "mimes": [], "new_artifacts": []}, "run_id": ""}
    # A results tool that IS part of a plan run (run_id set) → suppressed; the
    # Plan Chainer handles plan-node results, not the render hook.
    plan_node_ctx = {"tool": {"role": "results", "name": "x___get_diarization_results"}, "result": {"urls": ["https://cf/x.wav"], "mimes": ["audio/wav"], "new_artifacts": ["https://cf/x.wav"]}, "run_id": "plan-123"}
    assert len(eng.matching("after_tool_result", results_ctx)) == 1
    assert len(eng.matching("after_tool_result", status_ctx)) == 1
    assert eng.matching("after_tool_result", status_running_ctx) == []
    assert len(eng.matching("after_tool_result", artifact_ctx)) == 1
    assert eng.matching("after_tool_result", relay_ctx) == []
    assert eng.matching("after_tool_result", weblink_ctx) == []
    assert eng.matching("after_tool_result", plan_node_ctx) == []


def test_prompt_no_longer_contains_render_results_rule():
    prompt_path = os.path.join(os.path.dirname(__file__), "..", "src", "main.py")
    with open(prompt_path) as f:
        src = f.read()
    assert "PROACTIVELY RENDER JOB RESULTS" not in src


def test_main_agent_no_longer_registers_render_artifacts_tool():
    """The hook is the sole renderer — render_artifacts must not be an LLM tool."""
    main_path = os.path.join(os.path.dirname(__file__), "..", "src", "main.py")
    with open(main_path) as f:
        src = f.read()
    assert '"render_artifacts": render_artifacts_tool' not in src
    assert "import render_artifacts as render_artifacts_tool" not in src
    # Prompt should tell the model rendering is automatic (now in prompts/orchestrator.py).
    orch = open(os.path.join(os.path.dirname(__file__), "..", "src", "prompts", "orchestrator.py")).read()
    assert "Rendering is AUTOMATIC" in orch


def test_skill_tier3_removed():
    skill_path = os.path.join(
        os.path.dirname(__file__), "..", "skills", "video-processing", "SKILL.md"
    )
    with open(skill_path) as f:
        md = f.read()
    assert "Tier 3 — ALWAYS render" not in md
    assert "call `render_artifacts` with the isolated" not in md


def test_relay_does_not_re_render_via_the_real_context_builder():
    """The relay guard, exercised through the mechanism instead of a hand-set flag.

    This replaces what the tool-name allowlist used to do. Previously
    `dispatch_tool` was excluded by NAME; now it is excluded because the URL it
    echoes has already been handed to the renderer. The distinction matters: the
    old rule also excluded every unknown producer, which is why a
    user-registered MCP server returning an image rendered nothing.
    """
    from hooks import provider
    from hooks.engine import HookEngine
    from hooks.defaults import default_hook_specs

    provider.reset_dispatched_artifacts()
    eng = HookEngine(default_hook_specs())
    url = "https://cf/relay-check.wav"

    class Ev:
        def __init__(self, name, result):
            self.tool_use = {"name": name, "toolUseId": "t1"}
            self.result = result

    # 1. The producer runs. Novel artifact -> fires.
    produced = Ev("x___get_diarization_results", {"content": [{"text": f"out: {url}"}]})
    ctx = provider.ctx_after_tool_result(produced)
    assert ctx["result"]["new_artifacts"] == [url]
    assert len(eng.matching("after_tool_result", ctx)) == 1

    # 2. Rendering it records the dispatch.
    provider.mark_artifacts_dispatched([url])

    # 3. The relay echoes the same URL -> no longer novel -> does not fire.
    relayed = Ev("dispatch_tool", {"content": [{"text": f"the sub-agent produced {url}"}]})
    ctx2 = provider.ctx_after_tool_result(relayed)
    assert ctx2["result"]["new_artifacts"] == [], "relay must not re-render"
    assert eng.matching("after_tool_result", ctx2) == []

    # 4. A relay does not fire even for an artifact this container has never
    #    seen. This assertion was the OPPOSITE way round when first written --
    #    "the guard is about the artifact, not about the tool being a relay" --
    #    and that belief shipped two render tabs per image.
    #
    #    Novelty cannot cover the relay case: the sub-agent renders inside its
    #    OWN container, with its own dedupe set, so by the time the orchestrator
    #    relays the url it is still novel HERE. Relays are excluded by name
    #    because "this tool only forwards another agent's output" is a fact about
    #    the tool, not a naming-convention guess about which tools produce
    #    artifacts -- which is what the removed allowlist was.
    other = "https://cf/relay-check-2.wav"
    ctx3 = provider.ctx_after_tool_result(
        Ev("dispatch_tool", {"content": [{"text": f"also {other}"}]}))
    assert ctx3["result"]["new_artifacts"] == [], \
        "a relay never produces artifacts, however novel the url looks here"
    assert eng.matching("after_tool_result", ctx3) == []

    # 5. The producing tool still fires for that same novel url -- the exclusion
    #    is on relays, not on the artifact.
    ctx4 = provider.ctx_after_tool_result(
        Ev("x___get_diarization_results", {"content": [{"text": f"out: {other}"}]}))
    assert ctx4["result"]["new_artifacts"] == [other]
    assert len(eng.matching("after_tool_result", ctx4)) == 1
    provider.reset_dispatched_artifacts()


def test_an_unknown_producer_now_renders():
    """The case that motivated all of this.

    A gateway tool from a user-registered MCP server. role_for_tool gives it
    'other', so the old allowlist excluded it and its image rendered nowhere.
    """
    from hooks import provider
    from hooks.engine import HookEngine
    from hooks.defaults import default_hook_specs

    provider.reset_dispatched_artifacts()
    eng = HookEngine(default_hook_specs())

    class Ev:
        tool_use = {"name": "usr-8ddf64af24be45c894a7a8c1f254c83f___run_model",
                    "toolUseId": "t9"}
        result = {"content": [{"text":
            "Generated: https://v3b.fal.media/files/b/0aa50f49/8c4ABgOJ45Lp.jpg"}]}

    ctx = provider.ctx_after_tool_result(Ev())
    assert provider.role_for_tool(Ev.tool_use["name"]) == "other", \
        "still 'other' -- the fix is that role no longer decides"
    assert ctx["result"]["new_artifacts"], "an unknown producer's output is novel"
    assert len(eng.matching("after_tool_result", ctx)) == 1
    provider.reset_dispatched_artifacts()


def test_plan_and_execute_is_a_render_point_not_a_relay():
    """The two orchestration tools look alike and behave oppositely.

    task_agent loads hooks only when `node_id` is absent, so:

      dispatch_tool          sub-agent renders -> the relay must not render again
      plan_and_execute_tool  sub-agent hooks SUPPRESSED inside the plan, and the
                             "renders ONCE at the end" the code comments describe
                             IS this hook firing on the plan tool's result --
                             plan_and_execute_tool.py contains no render call

    Treating both as relays removed the only render path for anything executed
    through a plan. An image generated inside a plan rendered nowhere: the
    sub-agent was silenced by node_id, the orchestrator by name.
    """
    from hooks import provider
    from hooks.engine import HookEngine
    from hooks.defaults import default_hook_specs

    provider.reset_dispatched_artifacts()
    eng = HookEngine(default_hook_specs())
    url = "https://cf/plan-output.png"

    class Ev:
        def __init__(self, name):
            self.tool_use = {"name": name, "toolUseId": "t1"}
            self.result = {"content": [{"text": f"plan produced {url}"}]}

    assert provider.is_relay_tool("dispatch_tool") is True
    assert provider.is_relay_tool("plan_and_execute_tool") is False

    ctx = provider.ctx_after_tool_result(Ev("plan_and_execute_tool"))
    assert ctx["result"]["new_artifacts"] == [url], \
        "the plan result is the render point for a plan run"
    assert len(eng.matching("after_tool_result", ctx)) == 1

    provider.reset_dispatched_artifacts()
    ctx2 = provider.ctx_after_tool_result(Ev("dispatch_tool"))
    assert ctx2["result"]["new_artifacts"] == [], \
        "a dispatched sub-agent already rendered this itself"
    provider.reset_dispatched_artifacts()


def test_render_tab_label_skips_opaque_per_user_targets():
    """A user-registered server's target is `usr-<uuid>`, an opaque machine id.

    Titleizing it produced tab headers like
    "Usr 8ddf64af24be45c894a7a8c1f254c83f". For those the tool segment is the
    only meaningful part. Platform targets keep the old behaviour, where the
    target IS the readable job name.
    """
    from hooks.actions import _render_label

    cases = {
        "usr-8ddf64af24be45c894a7a8c1f254c83f___run_model": "Run Model",
        "shot-segmentation___get_job_status": "Shot Segmentation",
        "plan_and_execute_tool": "Plan And Execute Tool",
        # starts with "usr" but is not the opaque shape -- must not be caught
        "usrsomething___x": "Usrsomething",
    }
    for name, expected in cases.items():
        assert _render_label({"tool": {"name": name}}) == expected, name

    assert _render_label({}) == ""
    assert _render_label({"tool": {"name": ""}}) == ""


def test_render_dispatches_only_the_artifacts_the_condition_matched():
    """The action used to render `result.urls` while the condition gated on
    `result.new_artifacts`.

    fal.ai returns its queue endpoints alongside the output, so a single image
    generation put three entries in the render panel -- the image plus
    `.../status` and `.../cancel` shown as "data files" -- and handed all three
    to the external-media mirror to copy as if they were media.
    """
    from hooks import provider
    from hooks.engine import HookEngine
    from hooks.defaults import default_hook_specs

    provider.reset_dispatched_artifacts()
    img = "https://v3b.fal.media/files/b/0aa52038/Rq1VGB.jpg"
    text = (
        f"Result: {img} "
        "status https://queue.fal.run/fal-ai/flux/requests/019fd23a/status "
        "cancel https://queue.fal.run/fal-ai/flux/requests/019fd23a/cancel"
    )

    class Ev:
        tool_use = {"name": "usr-8ddf64af24be45c894a7a8c1f254c83f___run_model",
                    "toolUseId": "t1"}
        result = {"content": [{"text": text}]}

    ctx = provider.ctx_after_tool_result(Ev())
    assert len(ctx["result"]["urls"]) == 3, "all three are still detected as urls"
    assert ctx["result"]["new_artifacts"] == [img], "but only one is an artifact"

    matched = HookEngine(default_hook_specs()).matching("after_tool_result", ctx)
    assert len(matched) == 1
    assert matched[0].action_params["urls"] == "result.new_artifacts", (
        "the action must render the same set the condition gated on"
    )
    provider.reset_dispatched_artifacts()
