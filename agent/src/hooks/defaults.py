"""Harness-provided built-in hooks (defaults).

Built-ins are read-only in the UI (clone to customize) and enabled by default.
Task 6 adds the flagship ``render-on-results`` default here.
"""

from __future__ import annotations

from .engine import HookSpec

# Built-in hooks a user spec may neither replace nor disable. A user hook whose ``name`` is in
# this set is dropped BEFORE override computation, so it can never turn the built-in off or edit
# it. ``render-moments`` is locked because "exactly one Moments_Gallery tab per turn" (Req 3.4,
# 3.5) depends on the rule firing: a disabled override named ``render-moments`` would silently
# break it. ``lab.service._handle_list_hooks`` marks these rows ``locked`` so the UI offers no
# Clone/override, and ``lab.hook_crud._VALID_ACTIONS`` excludes ``moments_render`` so no user hook
# can name the action either.
LOCKED_BUILTINS = frozenset({"render-moments", "render-graph"})


def default_hook_specs() -> list[HookSpec]:
    """Return the built-in hook specs shipped with the harness.

    ``render-artifacts`` is the deterministic, sole renderer: whenever an
    artifact-PRODUCING tool returns renderable artifact URLs it dispatches them
    to the Render Agent. It matches:
      * job *results* tools (``*_results`` → role ``results``: the legacy
        diarization/stem-separation shape), and
      * job *status* tools (``get_job_status`` → role ``status``: the M2C
        media-workflow-gateway shape) — but ONLY when the job has actually
        completed (``result.job_status == "SUCCEEDED"``). Without this gate,
        every intermediate poll (RUNNING) that echoes output field names triggers
        a render, producing 10+ duplicate manifests per job, and
      * the ``artifact`` tool (ad-hoc generated files).
    No LLM decision, no prompt tokens, exactly once. The Render Agent then picks
    a render-* skill by artifact type (audio/video/pdf) or falls back to a
    generic preview when no skill matches.

    The condition deliberately excludes orchestration RELAY tools
    (``dispatch_tool``, ``plan_and_execute_tool``) whose result *text* merely
    echoes a sub-agent's artifact URLs — otherwise a dispatched results turn
    would render twice (once on the sub-agent that ran the results tool, once on
    the main agent relaying the summary). ``result.mimes`` is truthy only when
    the result carried URLs with recognized artifact extensions (see
    ``provider._MIME_BY_EXT``), so plain text, bare web links, and in-progress
    status polls (no URLs yet) don't trigger.
    """
    return [
        HookSpec(
            name="render-artifacts",
            event="after_tool_result",
            condition=(
                # Three orthogonal questions, each answering something real:
                #
                #   is it an output?    new_artifacts excludes source media, so a
                #                      job's INPUT asset is never rendered
                #   is it ready?       an unfinished job has no outputs worth
                #                      showing, whatever its poll echoes
                #   is it new?         not already handed to the renderer this
                #                      session
                #
                # What was REMOVED is the tool-name allowlist: `tool.role ==
                # "results" or tool.name == "artifact"`, which used naming
                # convention as a proxy for "did this call produce the artifact".
                # That proxy silently rendered nothing for any producer nobody
                # had enumerated -- a user-registered MCP server returning images
                # being the case that exposed it. Identity answers that question
                # directly, so novelty replaces the allowlist while the two
                # genuine semantic gates stay.
                #
                # The relay double-render the allowlist also prevented now falls
                # out of novelty: dispatch_tool quoting a sub-agent's URL has
                # already been seen, so it cannot fire twice.
                #
                # The old condition was:
                #   (tool.role == "results" or tool.name == "artifact"
                #    or (tool.role == "status" and result.job_status == "SUCCEEDED"))
                #   and result.mimes and run_id == ""
                # which meant every new artifact-producing tool rendered nothing
                # until someone widened the allowlist.
                'result.new_artifacts '
                'and (tool.role != "status" or result.job_status == "SUCCEEDED") '
                'and run_id == ""'
            ),
            action_type="render",
            action_params={
                # Render exactly what the CONDITION matched. This used to be
                # `result.urls`, i.e. every URL appearing anywhere in the tool
                # result, while the condition gated on `result.new_artifacts` --
                # so a fal.ai response put its queue endpoints (`.../status`,
                # `.../cancel`) into the render panel as "3 data files", and every
                # one of them was handed to the mirror to copy as media.
                #
                # `new_artifacts` is already the right set: renderable mime, not
                # source media, not previously dispatched.
                "urls": "result.new_artifacts",
                # Source media is context for the Render Agent (e.g. the video a
                # detection result describes). It never gates the condition.
                "source_urls": "result.source_urls",
            },
            priority=50,
            enabled=True,
        ),
        # render-moments: the second built-in render rule (design Component 6). It fires once
        # per Main_Agent turn at session_end, reads the TurnOutcome that finish_turn stored
        # (Component 4), selects the turn's Moments, and buffers one clip-gallery manifest that
        # carries Asset_References and NO URL. Unlike render-artifacts it keys off nothing in the
        # tool result -- the condition is just the event -- because the action itself reads the
        # stored outcome and returns `skipped` whenever there is nothing to show (no Projects turn
        # context, no outcome, an unreadable registry, or no eligible Moment).
        #
        # LOCKED built-in (LOCKED_BUILTINS above): a user hook can neither replace nor
        # disable it, and `moments_render` is not in lab.hook_crud._VALID_ACTIONS, so no user hook
        # can name the action. A disabled override named `render-moments` would otherwise silently
        # break "exactly one Moments_Gallery tab" (Req 3.4, 3.5).
        HookSpec(
            name="render-moments",
            event="session_end",
            condition='event == "session_end"',
            action_type="moments_render",
            action_params={},
            priority=40,
            enabled=True,
        ),
        # render-graph: the knowledge-graph extension of render-moments. Once per Main_Agent turn at
        # session_end it renders what the graph tools touched this turn as one "↳ Graph" tab
        # (hooks.actions.graph_render; skipped when no graph tool ran). LOCKED for the same reason as
        # render-moments, and `graph_render` is not a user-selectable action.
        HookSpec(
            name="render-graph",
            event="session_end",
            condition='event == "session_end"',
            action_type="graph_render",
            action_params={},
            priority=41,
            enabled=True,
        ),
    ]
