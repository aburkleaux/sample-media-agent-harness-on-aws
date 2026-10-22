"""Hook action registry and built-in actions.

An *action* is what a matched hook *does*. Actions are harness-owned (users
pick from this bounded vocabulary declaratively; there is no user code). Each
action is a callable ``fn(params: dict, context: dict) -> ActionResult`` and is
registered by name. v1 ships ``notify``; ``render`` and ``dispatch`` are added
in later tasks.

Actions may have side effects (push to the progress channel, invoke the render
agent, etc.). The engine wraps every call in try/except so a failing action can
never break the turn.
"""

from __future__ import annotations
import json

import fnmatch
import logging
import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "ActionResult",
    "register_action",
    "get_action",
    "interpolate",
    "notify_action",
]


@dataclass
class ActionResult:
    """Outcome of running one hook action."""

    action_type: str
    status: str  # "ok" | "error" | "skipped"
    detail: str = ""
    extra: dict = field(default_factory=dict)


ActionFn = Callable[[dict, dict], ActionResult]

_REGISTRY: dict[str, ActionFn] = {}


def register_action(name: str, fn: ActionFn) -> None:
    """Register an action handler by name (idempotent — last wins)."""
    _REGISTRY[name] = fn


def get_action(name: str) -> ActionFn | None:
    return _REGISTRY.get(name)


_TOKEN_RE = re.compile(r"\{([^}]+)\}")


def interpolate(template: str, ctx: dict) -> str:
    """Replace ``{dotted.path}`` tokens with values looked up in ``ctx``.

    Missing paths resolve to the empty string. Non-dict intermediates stop
    navigation (→ empty). This is deliberately simple and side-effect free.
    """
    def _repl(m: "re.Match[str]") -> str:
        path = m.group(1).strip()
        cur: Any = ctx
        for part in path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                cur = None
                break
        return "" if cur is None else str(cur)

    return _TOKEN_RE.sub(_repl, template or "")


def notify_action(params: dict, ctx: dict) -> ActionResult:
    """Push a distinct hook step/message to the active progress channel.

    params:
      - ``message``: text (supports ``{dotted.path}`` interpolation from ctx).
      - ``level``: ``info`` (default) | ``error``.
    """
    # Lazy import so this module is importable in contexts without the
    # progress channel (unit tests import the registry directly).
    from orchestration.progress_channel import get_active_channel

    message = interpolate(str(params.get("message", "Hook fired")), ctx)
    level = str(params.get("level", "info"))
    status = "failed" if level == "error" else "done"

    channel = get_active_channel()
    if channel is not None:
        channel.push(status, f"⚙ {message}", agent="hook")
    return ActionResult("notify", "ok", detail=message)


register_action("notify", notify_action)


def _lookup_list(path: str, ctx: dict) -> list[str]:
    """Resolve a dotted ``path`` in ctx to a list of URL strings."""
    cur: Any = ctx
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return []
    if isinstance(cur, str):
        return [cur]
    if isinstance(cur, (list, tuple)):
        return [u for u in cur if isinstance(u, str) and u.strip()]
    return []


def _split_url_blob(s: str) -> list[str]:
    """Split a string that carries MULTIPLE urls joined by whitespace/newlines into individual
    urls. A url never contains whitespace, so ``"https://a/x.mp4\\nhttps://a/y.mp4"`` is TWO
    urls, not one. Passing such a blob through as a single "url" made a render whose
    ``<video src>`` was the whole blob — an unplayable DEAD tab (session d9c9286c, a 9-clip
    crop batch). Returns only the http(s) tokens."""
    return [p for p in re.split(r"\s+", s.strip()) if p.startswith("http")]


def _resolve_urls(spec: Any, ctx: dict) -> list[str]:
    """Resolve the ``urls`` param: a dotted context path, a literal URL, or a
    list mixing both."""
    if isinstance(spec, str):
        raw = [spec] if spec.startswith("http") else _lookup_list(spec, ctx)
    elif isinstance(spec, (list, tuple)):
        raw = []
        for item in spec:
            if isinstance(item, str) and item.strip():
                raw.extend([item] if item.startswith("http") else _lookup_list(item, ctx))
    else:
        return []
    # Defensive: a single "url" that is actually several joined by whitespace/newlines is N urls
    # (a url never contains whitespace). Split so a blob can never become one dead <video src>.
    out: list[str] = []
    for u in raw:
        if isinstance(u, str) and u.startswith("http") and re.search(r"\s", u):
            out.extend(_split_url_blob(u))
        elif isinstance(u, str):
            out.append(u)
    return out


_FEDERATED_SEP = "___"

# `usr-` followed by a hex blob: the per-user gateway target naming from
# owner_naming.py. Matched by shape rather than prefix alone so a legitimately
# named platform target that merely starts with "usr" is not caught.
_OPAQUE_TARGET = re.compile(r"^usr-[0-9a-f]{16,}$", re.I)


def _render_label(ctx: dict) -> str:
    """A human tab name for the view this render produces.

    Federated media tools are named ``<target>___<tool>`` (AgentCore's gateway
    convention), e.g. ``shot-segmentation___get_job_status``. The target is what
    a user thinks of as the job, so titleizing it yields the same shape as the
    job names shown elsewhere ("Shot Segmentation"). Falls back to the bare tool
    name, then to "" so the caller can number the tab instead of inventing one.
    """
    tool = ctx.get("tool") if isinstance(ctx, dict) else None
    name = str((tool or {}).get("name") or "") if isinstance(tool, dict) else ""
    if not name:
        return ""
    head, _, rest = name.partition(_FEDERATED_SEP)
    # A user-registered server's target is `usr-<uuid>` -- an opaque machine
    # identifier, deliberately not human meaningful, so titleizing it produced
    # tab headers like "Usr 8ddf64af24be45c894a7a8c1f254c83f". For those, the
    # TOOL segment is the only part carrying meaning ("Run Model"). Platform
    # targets keep the old behaviour, where the target IS the readable job name
    # ("shot-segmentation" -> "Shot Segmentation").
    #
    # The server's own display name ("fal.ai") would read better still, but it
    # lives in the gateway registry and is not on the hook's tool context.
    source = rest if _OPAQUE_TARGET.match(head) and rest else head
    words = [w for w in re.split(r"[-_\s]+", source) if w]
    return " ".join(w[:1].upper() + w[1:] for w in words)


_S3_CLIENT = None
# Source urls mirrored during the current render_action, so both the original and
# our copy are recorded as dispatched.
_source_urls_seen: list[str] = []


def _mirror_external_media(urls: list[str]) -> list[str]:
    """Replace externally-hosted media URLs with copies in our own bucket.

    Done HERE, immediately before dispatching to the Render Agent, rather than as
    a separate hook. The render path is the only consumer that needs a URL it can
    actually display, and a second hook would have to fire on the first hook's
    output -- which the engine does not do, since hooks observe tool results.

    Three things this buys, in the order they matter:

      * the follow-up edit works -- "change the cat to a dog" has to send the
        image back as an input, and a third-party URL may not survive until then
      * downstream media tools can reach it -- trim, transcode, frame extraction
        and the NLE timeline all take s3:// in the skills-store bucket
      * render history stops rotting -- entries persist per session, and a foreign
        URL leaves a conversation that looks intact and displays nothing

    Failure is never fatal: any URL that cannot be copied is passed through
    unchanged, so the worst case is the previous behaviour rather than a lost
    render.
    """
    global _S3_CLIENT
    try:
        import os
        from media import persist_media
    except Exception as e:
        logger.warning("mirror: persist_media unavailable (%s); rendering source urls", e)
        return urls

    # These are the names main.py reads, and they are NOT the names of its module
    # level variables. main.py does:
    #
    #   S3_BUCKET = os.environ.get("SKILLS_S3_BUCKET", ...)
    #   S3_PREFIX = os.environ.get("ARTIFACTS_PREFIX", "artifacts/")
    #
    # I originally read "S3_BUCKET"/"S3_PREFIX" here -- the variable names rather
    # than the environment names. Neither exists in the environment, so `bucket`
    # was always "" and this function returned on its first guard every time it
    # was ever called. The mirror never ran, and because the guard was silent
    # there was nothing in the logs to say so.
    bucket = os.environ.get("SKILLS_S3_BUCKET") or os.environ.get("S3_BUCKET", "")
    domain = os.environ.get("ARTIFACTS_DOMAIN", "")
    prefix = os.environ.get("ARTIFACTS_PREFIX") or os.environ.get("S3_PREFIX", "artifacts/")
    if not bucket:
        # Deliberately noisy. A silent return here is indistinguishable from
        # "there was nothing to mirror", which is what hid the bug.
        logger.warning(
            "mirror: no bucket configured (SKILLS_S3_BUCKET unset); "
            "rendering %d source url(s) unmirrored", len(urls),
        )
        return urls

    external = [u for u in urls
                if persist_media.media_type_for(u)
                and not persist_media.is_already_ours(u, domain)]
    if not external:
        return urls

    if _S3_CLIENT is None:
        try:
            import boto3
            _S3_CLIENT = boto3.client("s3")
        except Exception as e:
            logger.warning("mirror: no s3 client (%s); rendering source urls", e)
            return urls

    replacement: dict[str, str] = {}
    _source_urls_seen.extend(external)
    for url in external:
        try:
            copied = persist_media.persist_url(
                url, s3_client=_S3_CLIENT, bucket=bucket, prefix=prefix,
                artifacts_domain=domain)
            replacement[url] = copied["url"]
        except Exception as e:
            logger.warning("mirror: keeping source url %s (%s)", url[:90], e)
    if replacement:
        logger.info("mirror: copied %d external asset(s) into %s",
                    len(replacement), bucket)
    return [replacement.get(u, u) for u in urls]


def _foreign_session_in(ctx: dict) -> str:
    """Return a session id named by this tool result that is NOT the current session.

    Job records carry sessionId, so a result fetched for another session identifies itself.
    Returns "" when the result names no session, when it names this one, or when the current
    session is unknown -- absence of evidence must not suppress a legitimate render.
    """
    try:
        from jobs.context import current_session_id

        current = current_session_id() or ""
    except Exception:  # noqa: BLE001
        return ""
    if not current:
        return ""
    result = (ctx or {}).get("result")
    named: list[str] = []

    def walk(node, depth=0):
        if depth > 6 or len(named) > 8:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if str(k) in ("sessionId", "session_id") and isinstance(v, str) and v.strip():
                    named.append(v.strip())
                else:
                    walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)
        elif isinstance(node, str) and node.lstrip().startswith(("{", "[")):
            try:
                walk(json.loads(node), depth + 1)
            except (ValueError, TypeError):
                return

    walk(result)
    if not named:
        return ""
    # Only suppress when EVERY session named is someone else's. A result that mentions
    # this session alongside another is still ours to render.
    if current in named:
        return ""
    return named[0]


# A session id is a UUID, and this session's media lives under `artifacts/<session-uuid>/...`
# (plus the SHARED `artifacts/external/...` fal-output namespace). A URL whose artifacts segment
# is a UUID OTHER than the current session is a DIFFERENT session's asset that a tool result
# merely REFERENCED — e.g. an agent-olive continuation that loaded a prior reel as reusable work
# (session 4f9d0d02 rendered e27e5945's `final_reel_crossfade.mp4` this way). The auto-harvester
# must never open a tab for another session's asset. `_foreign_session_in` above catches a result
# whose OWN sessionId is foreign; this catches a same-session result that quotes a foreign URL.
#
# Deliberately narrow so it only ever DROPS a proven cross-session asset:
#   * `external/` and any non-UUID segment (uploads, current session) pass;
#   * a URL with no `artifacts/<seg>/` at all (presigned M2C / gateway output) passes — it has no
#     session in its path and is a genuine this-turn output;
#   * unknown current session → fail OPEN (return unchanged), never suppress on absence of evidence.
#
# Both session roots count (asset-lineage Phase 3 (a)): `projects/<pid>/sessions/<uuid>/` is the same
# session folder under the project layout, so a foreign uuid there is just as foreign.
_UUID_SEG = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_ARTIFACT_SESSION_RE = re.compile(
    rf"(?:^|/)(?:artifacts/({_UUID_SEG})|projects/p[0-9a-f]{{32}}/sessions/({_UUID_SEG}))/"
)


def _url_is_foreign_session(url: str, current: str) -> bool:
    """True only when ``url`` sits under a session root (``artifacts/<uuid>/`` or
    ``projects/<pid>/sessions/<uuid>/``) for a uuid that is NOT ``current``."""
    if not (isinstance(url, str) and current):
        return False
    m = _ARTIFACT_SESSION_RE.search(url)
    return bool(m) and (m.group(1) or m.group(2)).lower() != current.lower()


def _drop_foreign_session_urls(urls: list) -> list:
    """Filter auto-harvest DELIVERABLE URLs down to ones this session may render.

    Applied on the AUTOMATIC end-of-turn harvest path only, and never to SOURCE media: a source is
    the canvas an analysis draws on, not a tab of its own, and stripping it left analyses with no
    video to fold onto (staging 833a079f). Fails open: if the current session is unknown, returns
    the list unchanged so a missing id can never blank a legitimate render.

    A path is not an identity. A URL this runtime WROTE during the turn (``media.turn_outputs``:
    artifact uploads/adopts/imports, crop_video outputs, code_interpreter results) is this
    session's output even when it sits under another session-shaped prefix -- the main agent's
    code tool once invented ``artifacts/<random uuid>/output/`` for the user's clip, and dropping
    it here hid the clip.
    """
    try:
        from jobs.context import current_session_id

        current = current_session_id() or ""
    except Exception:  # noqa: BLE001
        return list(urls or [])
    if not current:
        return list(urls or [])
    try:
        from media.turn_outputs import is_own_write
    except Exception:  # noqa: BLE001 - without the ledger, behave exactly as before
        def is_own_write(_u):  # type: ignore[no-redef]
            return False
    kept: list = []
    dropped: list = []
    for u in urls or []:
        if _url_is_foreign_session(u, current) and not is_own_write(u):
            dropped.append(u)
        else:
            kept.append(u)
    if dropped:
        logger.info(
            "render: dropped %d cross-session artifact url(s) from auto-render "
            "(belong to another session, not %s): %s",
            len(dropped), current[:8], [str(d)[:90] for d in dropped[:3]],
        )
    return kept


# Tools whose media are catalog samples (a voice's preview), never something this session produced.
_CATALOG_LOOKUP_TOOLS = frozenset({"get_voice", "list_voice_collection"})


def render_action(params: dict, ctx: dict) -> ActionResult:

    """Render artifact URLs via the Render Agent.

    params:
      - ``urls``: dotted context path (default ``"result.urls"``), a literal
        URL, or a list of either. Resolved against the event context.
      - ``source_urls``: optional source media (default
        ``"result.source_urls"``) handed to the Render Agent as context so
        analysis outputs can be drawn over the media they describe. Source
        media alone never triggers a render.
    """
    urls = _resolve_urls(params.get("urls", "result.urls"), ctx)
    if not urls:
        return ActionResult("render", "skipped", "no renderable urls")
    # Never AUTO-render another session's assets. A same-session tool result can quote a
    # foreign-session artifact URL (a continuation loading prior reusable work); drop those
    # before anything is buffered so they cannot open a tab in this session's viewer. Explicit
    # displays and this session's own `external/`/current-prefix outputs are unaffected.
    urls = _drop_foreign_session_urls(urls)
    if not urls:
        return ActionResult("render", "skipped", "only cross-session urls; nothing to render")
    tool_name = str(((ctx.get("tool") or {}) if isinstance(ctx, dict) else {}).get("name") or "")
    if tool_name.rsplit("___", 1)[-1] in _CATALOG_LOOKUP_TOOLS:
        # A voice's preview clip is a catalog sample, not session output: an audition presents it in a
        # named gallery. Auto-rendering it opened an unlabelled "Text To Speech" timeline beside the
        # gallery (staging 2d841a23), and a composing tab flickered in mid-turn before that.
        return ActionResult("render", "skipped", f"{tool_name}: catalog lookup, not session output")
    import web_search as _ws
    if _ws.is_source_tool(tool_name):
        # Web-search / AWS-docs results are sources to CITE (as links), never session output to render: a
        # searched PDF opened as a "Document" tab on a web-research question (staging ca859a24).
        return ActionResult("render", "skipped", f"{tool_name}: a source to cite, not session output")
    foreign = _foreign_session_in(ctx)
    if foreign:
        # An agent hunting for a job read one belonging to a DIFFERENT session, and its
        # media was rendered into this session's viewer -- a 12-minute Netflix demo clip
        # appeared as a tab beside a 5-second cat video. The hook fires wherever a tool
        # result lands, so "this result mentions media" is not the same as "this session
        # produced media".
        return ActionResult("render", "skipped",
                            f"result belongs to session {foreign[:8]}, not this one")
    # Record BEFORE dispatching, and regardless of whether the dispatch
    # succeeds. This is what makes rendering once-only now that the condition
    # keys off artifact identity rather than a tool-name allowlist. Recording
    # only on success would re-render on every subsequent tool result that
    # mentions the URL whenever the Render Agent was briefly unavailable --
    # turning one transient failure into a duplicate preview per remaining turn.
    # Mirror BEFORE recording, so the dispatched set holds the URL the renderer
    # was actually given. Recording the source url instead would let a later
    # mention of our own copy look novel and render a second time.
    urls = _mirror_external_media(urls)
    try:
        from .provider import mark_artifacts_dispatched
        # Both forms: the source url must also count as dispatched, or the next
        # tool result quoting the ORIGINAL third-party url would mirror and render
        # it all over again.
        mark_artifacts_dispatched(urls + _source_urls_seen)
        _source_urls_seen.clear()
    except Exception as e:  # never let bookkeeping break a render
        logger.warning("could not record dispatched artifacts: %s", e)
    source_urls = _resolve_urls(params.get("source_urls", "result.source_urls"), ctx)
    # Standalone analyses (shots/faces/diarization dispatched directly) don't echo their
    # source in the result — M2C omits it — so source_urls is empty and build_render_manifest
    # can only key sourceKey on the OUTPUT (segments/crops), giving every analysis of one
    # video its OWN tab instead of merging. Recover the job's REGISTERED input asset (recorded
    # at dispatch via source_media) as the source canvas so sourceKey traces to the source
    # video and the UI (composeTimeline) merges them onto ONE composite timeline. Additive:
    # a no-op when nothing was recorded, so it can only add merges, never mis-drop a render.
    if not source_urls:
        try:
            from .provider import _recorded_source_media
            _rid = (ctx.get("result") or {}).get("job_id") if isinstance(ctx, dict) else ""
            source_urls = _recorded_source_media(_rid) or source_urls
        except Exception as exc:  # noqa: BLE001 - source recovery is a nicety, never a blocker
            logger.warning("render: could not recover job source media: %s", exc)
    # Source media is NOT session-filtered (lineage R7.4): it is the canvas, never a tab of its own.
    label = _render_label(ctx)

    # ONE TASK -> ONE RENDER TAB. The hook fires per artifact-producing tool result,
    # so a request needing two calls (two image options) produced two tabs while a
    # single-call request (the media gateway returning all its outputs at once)
    # produced one. The unit was the tool RESULT, which only coincided with the task.
    #
    # Buffer instead, and flush once when the invocation ends. Grouped by media class
    # because one manifest maps to one template -- images can share a view, a video
    # and a data table cannot.
    groups = ctx.get("result", {}).get("render_groups") if isinstance(ctx, dict) else None
    # SUPPRESS a plan-node's per-node clip render. A DAG fan-out runs each node in its OWN
    # invocation (async via the Plan Chainer), so each sibling clip flushes its own manifest →
    # one LOOSE tab per node, beside the ONE combined gallery that the ``get_plan_results``
    # render_groups path (below) builds in the orchestrator's invocation. Those render_groups
    # results run in the MAIN invocation and carry no run_id, so they are never suppressed here;
    # only a bare per-node clip (no groups, run_id bound, video/audio) is. Mirrors the UI's
    # autoRenderOnComplete `job.runId` skip. Fails OPEN when the run context is unknown.
    if not groups and _is_plan_node_clip(urls):
        return ActionResult("render", "skipped",
                            "plan-run node clip; the combined gallery renders via get_plan_results")
    if _batch_is_open():
        if groups:
            # Session-scope every group's DELIVERABLES up front (so the video-fanout counts below
            # see the same filtered set the buffer will): a per-node group can also quote a
            # foreign-session URL. Its source_urls are the canvas and are left alone (R7.4).
            groups = [
                {**g, "urls": _drop_foreign_session_urls(g.get("urls") or [])}
                for g in groups
            ]
            # Per-node grouping wins when the result carries it: one manifest per
            # analysis, titled after the work. Without this all of a plan's
            # analyses shared one slot (same tool label, same "other" media class)
            # and collapsed back into a single generic tab.
            #
            # EXCEPTION — a same-tool VIDEO FAN-OUT collapses back into ONE review gallery.
            # A lip-sync plan emits N sibling nodes with the SAME tool, each carrying ONE clip;
            # per-node keying then opened N tabs (session 99246b15: 8 `validate_seg` tabs
            # instead of the ONE multiview review the skill asks for). When a video-bearing
            # tool appears in MORE THAN ONE group, those groups get a SHARED render_key so
            # `_buffer_render` collects them into one slot -> one dispatch -> one gallery.
            # Distinct analyses carry DIFFERENT tools (shot-segmentation vs face-recognition),
            # so their counts are 1 and they never coalesce; images already coalesce in
            # `_buffer_render`, so this only affects a genuine video fan-out.
            def _grp_is_video(g: dict) -> bool:
                return any(_media_class(u) == "video" for u in (g.get("urls") or []))

            # A fan-out is N siblings over the SAME source (or none). Nodes of one tool over
            # DIFFERENT sources are per-video work and each folds onto its own source tab:
            # session b8a59e30 ran shot segmentation on three videos, and counting by tool alone
            # merged their segment clips into one "Segment 1..3" gallery spanning all three.
            def _fanout_key(g: dict) -> tuple:
                srcs = frozenset(str(u).split("?", 1)[0] for u in (g.get("source_urls") or []) if u)
                return ((g.get("tool") or "").strip(), srcs)
            _video_tool_counts: dict[tuple, int] = {}
            for g in groups:
                if (g.get("tool") or "").strip() and _grp_is_video(g):
                    _video_tool_counts[_fanout_key(g)] = _video_tool_counts.get(_fanout_key(g), 0) + 1
            for group in groups:
                gtool = (group.get("tool") or "").strip()
                from render.keys import plan_node_render_key
                # Plan-scoped: node ids repeat across a session's plans (one per guided phase or
                # video), and a bare node key let three videos overwrite one manifest (d6a560a8).
                grender_key = plan_node_render_key(group.get("plan_id"), group.get("node_id"))
                glabel = group.get("label") or label
                if gtool and _grp_is_video(group) and _video_tool_counts.get(_fanout_key(group), 0) > 1:
                    # Plan-scoped like every node key: a session's second lip-sync plan must not
                    # overwrite the first plan's gallery. Titled after the plan, never the tool id.
                    scope = re.sub(r"[^A-Za-z0-9._-]", "_", str(group.get("plan_id") or ""))[:40]
                    grender_key = "seggallery--" + (scope + "--" if scope else "") + \
                        re.sub(r"[^A-Za-z0-9._-]", "_", gtool)[:100]
                    plan_label = (group.get("plan_label") or "").strip()
                    glabel = f"{plan_label} — review" if plan_label else (glabel or "Review")
                _buffer_render(
                    group.get("urls") or [],
                    # Fall back to the ctx-recovered source when the group carries none: all
                    # of a plan's analyses ran on the same media, so the session's source is
                    # the right overlay canvas (design #1 — covers plan-node ids that aren't
                    # the job's registry key, which the per-group recovery can't match).
                    group.get("source_urls") or source_urls or [],
                    glabel,
                    render_key=grender_key,
                    tool_name=gtool,
                )
        else:
            # SINGLE DISPATCH: key the render on the JOB, not on the url content hash
            # (redesign §4 Rule 1). The plan path above already supplies a `render_key`
            # (`node_id`); this branch supplied none, so identity fell through to
            # `_manifest_id_for(urls)`.
            #
            # `canonical_render_key` resolves through the job-registry row's `node_id`, so a job
            # reached OUT-OF-TURN by execution ARN (autoRenderOnComplete re-firing the turn)
            # lands on the SAME manifest key as the in-turn plan-node render — which is the
            # ARN-keyed-vs-node-keyed split seen in session a2ebd1e9. It returns "" when there
            # is no job at all, and the content hash then applies exactly as before.
            # `result.job_id` is resolved in ctx_after_tool_result, where the RAW result and
            # tool_use are in scope. It cannot be recomputed here: `ctx["result"]` is the
            # DERIVED dict, and reading a job id out of it silently yields nothing.
            _job_key = ""
            try:
                from .provider import canonical_render_key
                _job_key = canonical_render_key(
                    (ctx.get("result") or {}).get("job_id") if isinstance(ctx, dict) else "")
            except Exception as exc:  # noqa: BLE001 - identity must degrade, never break a render
                logger.warning("could not derive a job render key: %s", exc)
            _buffer_render(urls, source_urls, label, render_key=_job_key,
                           tool_name=(ctx.get("tool") or {}).get("name") if isinstance(ctx, dict) else "")
        return ActionResult("render", "batched", f"{len(urls)} url(s) held for turn flush")

    # Lazy import: keeps this module importable in unit tests without the
    # render/dispatch stack, and avoids an import cycle.
    from render.artifacts import dispatch_render

    key = dispatch_render(urls, source_urls=source_urls or None, label=label or None)
    extra: dict = {}
    if key:
        extra["render_manifest_key"] = key
        if label:
            extra["render_label"] = label
    return ActionResult(
        "render",
        "ok" if key else "skipped",
        detail=key or "no manifest produced",
        extra=extra,
    )


register_action("render", render_action)


def dispatch_action(params: dict, ctx: dict) -> ActionResult:
    """Fire a sub-agent with a context-interpolated prompt.

    params:
      - ``prompt_template`` (or ``prompt``): supports ``{dotted.path}`` tokens.
      - ``target``: ``"task"`` (default) | ``"render"``.

    Note: this invokes another runtime synchronously from within a hook
    callback. Author conditions narrowly to avoid recursive fan-out (a
    task-agent hook that dispatches back to the task agent can re-trigger).
    """
    import json as _json

    template = params.get("prompt_template") or params.get("prompt") or ""
    prompt = interpolate(str(template), ctx).strip()
    if not prompt:
        return ActionResult("dispatch", "skipped", "empty prompt after interpolation")

    target = str(params.get("target", "task")).lower()
    from orchestration.dispatch_tool import (
        _consume_stream,
        _get_client,
        render_agent_arn,
        task_agent_arn,
    )
    from render.artifacts import _iter_lines

    # Resolved at call time (env snapshot, else the runtime-ARN SSM parameters).
    arn = render_agent_arn() if target == "render" else task_agent_arn()
    if not arn:
        return ActionResult("dispatch", "error", f"no runtime ARN for target '{target}'")

    payload: dict = {"prompt": prompt}
    try:
        from jobs.context import current_actor_id, current_session_id
        payload["actor_id"] = current_actor_id()
        payload["session_id"] = current_session_id()
    except Exception:
        pass

    client = _get_client()
    from jobs.context import invoke_agent_runtime_traced as _invoke_traced
    response = _invoke_traced(
        client,
        session_id=payload.get("session_id"),
        agentRuntimeArn=arn,
        qualifier="DEFAULT",
        payload=_json.dumps(payload).encode(),
        contentType="application/json",
    )
    text = _consume_stream(_iter_lines(response.get("response", b"")))
    return ActionResult("dispatch", "ok", detail=(text or "")[:500])


register_action("dispatch", dispatch_action)

# ---------------------------------------------------------------- render batching

# Buffered renders for the CURRENT invocation, grouped by media class. Empty when no
# invocation is active, which is why _batch_is_open() gates on the flag rather than on
# the buffer: an async job whose result arrives outside a turn must still render
# immediately rather than wait for a flush that will never come.
_render_batch: dict[str, dict] = {}
_batch_open = False


# Human words for a media class, used ONLY to qualify a tab title when one node produced two
# views ("Audio Stems — Audio" / "Audio Stems — Data"). `other` is `_media_class`'s catch-all for
# json/csv/pdf sidecars, which in practice are what a table view renders — hence "Data" rather
# than the literal class name, which would be meaningless on a tab.
_VIEW_QUALIFIER = {"audio": "Audio", "video": "Video", "image": "Images", "other": "Data"}


def _media_class(url: str) -> str:
    """Coarse bucket for a url, so unlike media do not share one manifest."""
    lowered = url.lower().split("?")[0]
    for ext, kind in (
        ((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"), "image"),
        ((".mp4", ".mov", ".webm", ".mkv", ".m4v"), "video"),
        ((".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac"), "audio"),
    ):
        if lowered.endswith(ext):
            return kind
    return "other"


# Media classes ("audio", "video", "image") a named gallery presented in THIS turn. The same file reaches
# a turn in several forms -- a voice preview as `s3://…/voices/<id>/preview.wav`, its CloudFront mirror
# `artifacts/external/<hash>.wav`, and a presigned link mirrored under ANOTHER hash -- so matching by URL
# cannot tie them together (staging 2d841a23: 4 of 12 matched; two extra "Audio" tabs opened). When the
# agent chose to present a class of media in a gallery, the turn's auto-render of that class is a duplicate.
_gallery_classes: set[str] = set()


def note_gallery_classes(urls) -> None:
    """Record the media classes a named gallery presented in this turn."""
    for u in urls or ():
        if isinstance(u, str) and u.strip():
            cls = _media_class(u)
            if cls in ("audio", "video", "image"):
                _gallery_classes.add(cls)


def open_render_batch() -> None:
    """Start buffering renders for a new invocation."""
    global _batch_open, _moments_slot
    _render_batch.clear()
    _gallery_classes.clear()
    _moments_slot = None
    _batch_open = True


def _batch_is_open() -> bool:
    return _batch_open


def _is_plan_node_clip(urls: list) -> bool:
    """True when THIS invocation is a plan/DAG node (``run_id`` bound) AND it produced a
    video/audio clip — i.e. a fan-out sibling whose per-node render would open one LOOSE tab
    beside the ONE combined gallery that ``get_plan_results``' ``render_groups`` path already
    builds in the orchestrator's invocation. The UI guards this symmetrically
    (``autoRenderOnComplete.ts``: ``if (job.runId) return``); the agent lacked the guard.

    Fails OPEN (returns False → the clip renders) when the run context is unknown or unbound —
    absence of evidence must not suppress a render, matching ``_drop_foreign_session_urls``.
    Only video/audio: image galleries already coalesce in ``_buffer_render``, so a per-node
    image render is the harmless case and is left alone.
    """
    try:
        from jobs.context import current_run_id
        if not (current_run_id() or "").strip():
            return False
    except Exception:  # noqa: BLE001 - unknown run context must not suppress a render
        return False
    return any(_media_class(u) in ("video", "audio") for u in urls)


def _object_identity(url: str) -> str:
    """The underlying object a url points at, ignoring presign noise.

    Two presigned URLs for the same S3 object differ only in their query string
    (`X-Amz-Signature`, `Expires`), so string equality treated them as different
    artifacts -- which is how one manifest ended up with the SAME transcoded video
    as two separate tracks.
    """
    return (url or "").split("?", 1)[0]


_SOURCE_VIDEO_RE = re.compile(r"\.(mp4|mov|m4v|webm|mkv|avi)$", re.IGNORECASE)


def _name_tokens(url: str) -> set:
    base = _object_identity(url).rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    return {t for t in re.findall(r"[a-z0-9]+", base) if len(t) >= 4 and not t.isdigit()}


def _unambiguous_source_urls(urls: list, source_urls: list) -> list:
    """The slot's sources, minus a source VIDEO the render would have to guess.

    A slot's sources are the union of every tool result that fed it, with no link from an output
    to the input it came from. With one source video that is fine. With several, the render used
    the first as the canvas, so one upload's video was shown under the OTHER upload's analysis
    (session fd4ce1bd). Keep a source video only when exactly one is named by the outputs
    (`meridian_metadata.json` names `..._Meridian.mp4`); otherwise render with no source video.
    """
    videos: list = []
    for s in source_urls or []:
        if _SOURCE_VIDEO_RE.search(_object_identity(s)) and _object_identity(s) not in videos:
            videos.append(_object_identity(s))
    if len(videos) <= 1:
        return list(source_urls or [])
    out_tokens = set().union(*(_name_tokens(u) for u in urls or [])) if urls else set()
    named = [v for v in videos if _name_tokens(v) & out_tokens]
    keep = named[0] if len(named) == 1 else None
    logger.info("render: %d source videos in one slot; keeping %s", len(videos), keep or "none")
    return [s for s in source_urls
            if not _SOURCE_VIDEO_RE.search(_object_identity(s)) or _object_identity(s) == keep]


def _drop_slots_shown_in_a_gallery() -> None:
    """Drop this turn's auto-render slots of a media class a named gallery already presented.

    Each dropped slot's composing tab (announced at buffer time) is resolved with ``render_failed``, which
    the UI uses to remove the tab, so nothing is left composing. Never raises.
    """
    if not _gallery_classes or not _render_batch:
        _gallery_classes.clear()
        return
    try:
        from orchestration.progress_channel import get_active_channel
        ch = get_active_channel()
        for key, slot in list(_render_batch.items()):
            cls = key[1] if isinstance(key, tuple) and len(key) > 1 else None
            if cls not in _gallery_classes:
                continue
            _render_batch.pop(key, None)
            rid = slot.get("rid") or slot.get("render_request_id") or ""
            logger.info("render batch: %s slot %r dropped; this turn showed its media in a gallery", cls, key[0])
            if ch is not None and rid:
                ch.push("failed", "↳ Shown in the gallery", agent="render",
                        extra={"render_request_id": rid, "render_failed": True})
    except Exception as exc:  # noqa: BLE001 - never break the flush
        logger.debug("gallery slot drop skipped: %s", exc)
    finally:
        _gallery_classes.clear()


def _emit_composing_marker(rid: str, label: str) -> None:
    """Announce a composing render at BUFFER time, while the event loop is still free.

    The actual dispatch runs later inside the synchronous session_end hook and blocks
    the loop for the whole render (plus the durable copy), which starves the progress
    poller — so a marker emitted there would not reach the UI until the manifest did
    (both drained together at teardown) and the per-tab composing animation would never
    paint. Emitting here lets the poller flush the marker live, and the animation then
    covers the entire render + copy. Never raises: a failed announce must not break
    buffering.
    """
    try:
        from orchestration.progress_channel import get_active_channel
        ch = get_active_channel()
        if ch is not None:
            extra = {"render_request_id": rid}
            if label:
                extra["render_label"] = label
            ch.push("running", "↳ Composing render…", agent="render", extra=extra)
    except Exception as exc:  # noqa: BLE001 - never break buffering
        logger.debug("could not emit composing marker: %s", exc)


def _slot_rid(key, render_key: str) -> str:
    """Deterministic tab identity for a KEYED render slot; a random id otherwise.

    The UI keys a render tab by ``render_request_id`` (WorkspaceView dedups composing
    markers and correlates the manifest to a tab by it). The manifest S3 key is already
    content/job-addressed via ``_render_manifest_id``, so two dispatches of the SAME job
    converge on ONE manifest — but each slot used to mint a random ``uuid4`` rid, so a
    render emitted by a sub-agent (forwarded up by dispatch_tool) AND the main agent's own
    hook render of the same job surfaced as TWO tabs even though they wrote one manifest
    (session f0598806's trim: rids 408145645c "Media Processing" + c068536d "Video", one
    ``arn_…352875c6….json``). Deriving the rid from the slot key (``render_key`` + media
    class) makes the tab identity match the render identity, so those converge on ONE tab
    across processes. Two VIEWS of one node keep distinct slot keys (different media class)
    → distinct rids → two tabs, as intended. Keyless slots (no ``render_key``: image
    galleries, label-only) keep a random id — their manifest already falls back to a
    content hash and cross-process convergence is not derivable here.
    """
    if not (render_key and isinstance(key, tuple) and key and key[0]):
        return uuid.uuid4().hex
    import hashlib
    basis = "|".join(str(p) for p in key)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def _buffer_render(urls: list, source_urls: list, label: str, render_key: str = "",
                   tool_name: str = "") -> None:
    """Buffer urls into per-(label, media class) slots for one flush.

    Keyed on the LABEL as well as the media class. Keying on media class alone
    collapsed every video produced anywhere in a turn into a single slot whose
    title was whichever tool fired first -- so in session 842bb417 the source
    video, the transcoded proxy and the face-analysis overlay all merged, and the
    face view (video-annotations, 5 identities, 40 boxes) never surfaced as its
    own tab. The label names the work that produced the artifact, so it is the
    axis a user actually distinguishes: "Detect Faces" and "Segment Shots" are
    different views even though both are video.

    Media class stays in the key because unlike media cannot share one manifest;
    a wav and an mp4 from the same node still need separate views.

    Each new slot gets a render_request_id and announces its composing marker
    immediately (see _emit_composing_marker), so the animation shows live rather
    than being batched with the manifest at teardown.
    """
    # A byproduct output must not open its own render tab beside the real deliverable.
    # The producing tool's render skill declares its output contract via `deliverable:`
    # (e.g. shot-segmentation -> shots.json); everything else it emits — the proxy
    # segment_*.mp4, recurrence*.json — is a byproduct. When a declared deliverable is
    # actually present in THIS call, byproducts ride the deliverable's slot instead of
    # opening their own tab. This is the same "secondary stills" move below, generalised
    # from images to any non-deliverable, and it is safe because the render is
    # deliverable-focused: it projects the deliverable onto the source video and ignores the
    # extra candidates (which is exactly why the Data tab already renders clean).
    #
    # The contract is read through the SAME catalog parser select_render_skill_by_tool uses,
    # so there is no second declaration reader to drift. Best-effort: any failure falls back
    # to the image-only rule below.
    deliverable_globs: set[str] = set()
    if tool_name:
        try:
            from render.skill_catalog import deliverables_for_tool
            deliverable_globs = deliverables_for_tool(tool_name)
        except Exception as exc:  # noqa: BLE001 - never break buffering over skill discovery
            logger.debug("buffer: deliverable lookup unavailable for %r: %s", tool_name, exc)

    def _is_deliverable(u: str) -> bool:
        if not deliverable_globs:
            return False
        bn = u.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1].lower()
        return any(fnmatch.fnmatch(bn, g) for g in deliverable_globs)

    # When the declared deliverable is present, it defines the single slot its byproducts ride.
    deliverable_cls = next((_media_class(u) for u in urls if _is_deliverable(u)), None)

    # Fallback axis (no declared deliverable present in this call): images are SECONDARY to
    # some other deliverable from the same call.
    #
    # Images coalesce into one shared gallery slot (below) so that image-GENERATION jobs do
    # not fan out — every fal image-gen job has a unique job-id label, which turned a
    # 12-image storyboard into 19 tabs (session ccbc9559). But an ANALYSIS node also ships
    # stills — shot_*.jpg thumbnails, speaker avatars — and those took the shared image slot
    # too, so ONE shot-segmentation node emitted TWO manifests: its marker view plus a stray
    # gallery. That is a duplicate tab, and the local E2E harness reproduced it end to end
    # (unit tests missed it because they buffer one media kind at a time).
    #
    # So: images are secondary when the SAME call also carried non-image artifacts, and they
    # ride in that deliverable's slot instead of opening their own. `primary_cls` is taken
    # from the first non-image artifact, which is deterministic for a given result.
    _non_image = [_media_class(u) for u in urls if _media_class(u) != "image"]
    primary_cls = _non_image[0] if _non_image else None

    for url in urls:
        cls = _media_class(url)
        if deliverable_cls is not None:
            # Deliverable-anchored: the declared deliverable owns the one slot; every
            # non-deliverable byproduct rides it rather than opening its own tab.
            if not _is_deliverable(url):
                cls = deliverable_cls
        elif cls == "image" and primary_cls:
            # Secondary stills: join the node's primary deliverable rather than forming a tab.
            cls = primary_cls
        # A pure image job (no primary_cls) still coalesces across labels into ONE gallery.
        # Video/audio/other keep label-based separation, so distinct analyses stay apart.
        key = ("", cls) if cls == "image" else (render_key or label or "", cls)
        slot = _render_batch.get(key)
        if slot is None:
            slot = {"urls": [], "source_urls": [], "label": label or "",
                    "rid": _slot_rid(key, render_key), "render_key": render_key or "",
                    "tool": tool_name or ""}
            _render_batch[key] = slot
            _emit_composing_marker(slot["rid"], slot["label"])
        # First non-empty tool wins the slot, mirroring the label rule below.
        if not slot.get("tool") and tool_name:
            slot["tool"] = tool_name
        identities = {_object_identity(u) for u in slot["urls"]}
        if _object_identity(url) not in identities:
            slot["urls"].append(url)
        source_identities = {_object_identity(u) for u in slot["source_urls"]}
        for src in source_urls or []:
            if _object_identity(src) not in source_identities:
                slot["source_urls"].append(src)
                source_identities.add(_object_identity(src))
        # Keep the FIRST label: it names the work that started the task, which is a
        # better tab title than whatever tool happened to finish last.
        if not slot["label"] and label:
            slot["label"] = label


def flush_render_batch() -> list:
    """Dispatch one render per media class and stop buffering. Never raises.

    Called when the invocation ends. Marking happened at buffer time, so a flush that
    failed would leave artifacts marked-but-unrendered -- hence every dispatch is
    attempted independently and a failure is logged rather than propagated.
    """
    global _batch_open
    _batch_open = False
    # The Moments_Gallery slot flushes FIRST and INDEPENDENTLY of _render_batch (which may be
    # empty): moments_render buffers it separately, so it must be written even on a turn that
    # produced no artifact render. Never raises.
    _flush_moments_slot()
    _flush_graph_slot()
    _drop_slots_shown_in_a_gallery()
    if not _render_batch:
        return []
    try:
        from render.artifacts import dispatch_render
    except Exception as exc:  # pragma: no cover - import guard
        logger.warning("cannot flush render batch: %s", exc)
        _render_batch.clear()
        return []
    # SUB-KEY when one node produced more than one VIEW.
    #
    # Slots are keyed `(render_key or label, media class)`, so a node emitting audio stems AND a
    # data sidecar correctly yields two slots -- two tabs. But both carry the same `render_key`
    # (the node id), so `_render_manifest_id` returned the same string for both, they wrote the
    # same `artifacts/<sid>/render/<node>.json`, and the second SILENTLY OVERWROTE the first. One
    # view just vanished, with no error anywhere. The batching layer and the identity layer
    # disagreed about what a tab is; this makes identity follow the batching layer, which has the
    # right answer -- the unit of a tab is the SLOT.
    #
    # Suffix only on an actual collision. Doing it unconditionally would move EVERY existing
    # manifest key, so every live session would re-render its tabs once and old manifests would
    # stop deduping against new ones. The suffix is the slot's own media class, i.e. the very
    # thing that already separated the slots -- no new concept, and deterministic for a given
    # node (same node, same classes => same keys => a re-render converges).
    _slots_per_key: dict[str, int] = {}
    for _kind, _slot in _render_batch.items():
        _rk = _slot.get("render_key") or ""
        if _rk:
            _slots_per_key[_rk] = _slots_per_key.get(_rk, 0) + 1

    keys = []
    for kind, slot in list(_render_batch.items()):
        render_key = slot.get("render_key") or ""
        label = slot["label"] or None
        # An EMPTY render_key means "fall back to the content hash" -- never suffix it, or every
        # keyless audio render in the session would share the key `--audio`.
        if render_key and _slots_per_key.get(render_key, 0) > 1:
            cls = kind[1] if isinstance(kind, tuple) and len(kind) > 1 else ""
            if cls:
                render_key = f"{render_key}--{cls}"
                # ...and QUALIFY THE TAB TITLE. The split means the user now sees two tabs where
                # they saw one; leaving both with the node's single label makes them read as
                # identical duplicates -- i.e. exactly like the duplicate-tab bug we removed.
                if label:
                    label = f"{label} — {_VIEW_QUALIFIER.get(cls, cls.title())}"
                logger.info("render: node %s has %d views; keying this one %s",
                            slot.get("render_key"), _slots_per_key[slot["render_key"]], render_key)
        try:
            key = dispatch_render(
                slot["urls"],
                source_urls=_unambiguous_source_urls(slot["urls"], slot["source_urls"]) or None,
                label=label,
                render_request_id=slot.get("rid"),
                render_key=render_key,
                tool_name=slot.get("tool") or "",
            )
            if key:
                keys.append(key)
            logger.info(
                "flushed render batch: %s x%d -> %s",
                "|".join(str(part) for part in kind) if isinstance(kind, tuple) else kind,
                len(slot["urls"]),
                key or "no key",
            )
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.warning("render flush failed for %s: %s", kind, exc)
    _render_batch.clear()
    return keys


# ---------------------------------------------------------------- moments rule

# The Moments_Gallery buffered for the CURRENT invocation. Set by moments_render when the render
# batch is open; consumed and cleared by the batch flush (Task 7.7). None when this turn produced
# no Moments. Reset to None by open_render_batch so a prior turn's slot never leaks forward.
_moments_slot: Optional[dict] = None

# Turn ids whose Moments_Gallery has already been buffered/written this process, bounded to the
# most recent 64 (oldest evicted). Makes the action at most once per turn even if the engine fires
# session_end twice. An OrderedDict is used as a bounded FIFO set.
_MOMENTS_DONE_MAX = 64
_moments_done: "OrderedDict[str, bool]" = OrderedDict()


def _mark_moments_done(turn_id: str) -> None:
    """Record ``turn_id`` as rendered, evicting the oldest id past the bound."""
    _moments_done[turn_id] = True
    _moments_done.move_to_end(turn_id)
    while len(_moments_done) > _MOMENTS_DONE_MAX:
        _moments_done.popitem(last=False)


# turn_id -> asset ids `show_asset` opened in that turn (bounded like `_moments_done`), so the turn's
# Moments gallery does not show the same picture a second time.
_shown_assets: "OrderedDict[str, set]" = OrderedDict()


def note_shown_asset(turn_id: str, asset_id: str) -> None:
    """Called by ``show_asset`` once it has opened ``asset_id`` in the Renderer."""
    if not turn_id or not asset_id:
        return
    _shown_assets.setdefault(turn_id, set()).add(asset_id)
    _shown_assets.move_to_end(turn_id)
    while len(_shown_assets) > _MOMENTS_DONE_MAX:
        _shown_assets.popitem(last=False)


def shown_assets_for(turn_id: str) -> set:
    return set(_shown_assets.get(turn_id) or ())


def _harvested_image_asset_ids(project_id: str) -> set:
    """Asset ids of the standalone IMAGE tab the harvest/render path buffered this invocation.

    ISSUE 4a: a harvested image is buffered into the ``("", "image")`` slot of ``_render_batch`` by
    ``render_action``/``_buffer_render`` (which key off a URL, never an asset id and never
    ``note_shown_asset``). If that SAME picture is also a Moment, the user sees it twice — once as the
    standalone image tab and once inside the Moments gallery (session aa9e30ba). ``moments_render``
    step 6b already drops an image Moment whose ``asset_id`` is in the turn's ``shown`` set, but that
    set was fed only by the explicit ``show_asset`` path. Fold the harvested image into the same set so
    the one dedup rule covers both, keeping the standalone tab and dropping the duplicate card.

    Pure and I/O-free (fits the I/O-free buffer/flush path): a session/M2C output is registered IN
    PLACE, so its asset id is a deterministic function of ``(kind="image", project_id, bucket, key)``
    via ``location_asset_id`` — no table read. Any URL that is not one of our own locations, or any
    import failure, is skipped; this can only ADD dedup, never drop a legitimate Moment on its own.
    """
    out: set = set()
    slot = _render_batch.get(("", "image"))
    if not slot:
        return out
    try:
        from media.artifact_fetch import canonical_location
        from projects.ids import location_asset_id
    except Exception as exc:  # noqa: BLE001 - resolver stack absent -> no extra dedup
        logger.debug("moments: image asset-id resolver unavailable: %s", exc)
        return out
    for url in slot.get("urls") or []:
        try:
            loc = canonical_location(url)
            if not loc or not loc.startswith("s3://"):
                continue
            bucket, _, key = loc[len("s3://"):].partition("/")
            if not bucket or not key:
                continue
            out.add(location_asset_id("image", project_id, bucket, key))
        except Exception as exc:  # noqa: BLE001 - one bad url must not break dedup
            logger.debug("moments: could not resolve harvested image %r: %s", url, exc)
    return out


def _moments_rid(session_id: str, turn_id: str) -> str:
    """Deterministic render-request id for a turn's Moments tab.

    ``sha256("moments|" + session_id + "|" + turn_id)[:32]`` — the same basis
    ``render.moments.build_moments_manifest`` uses for the manifest id, so a repeated flush
    overwrites one object and the SPA's ``render_request_id`` dedup keeps exactly one tab.
    """
    import hashlib
    basis = "moments|" + session_id + "|" + turn_id
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def _write_moments_manifest(session_id: str, manifest: dict, rid: str, label: str) -> str:
    """Degraded path: write a Moments manifest at once when the render batch never opened.

    A single small PutObject through ``render.artifacts.write_session_manifest`` (added in
    Task 7.7). On success push the render step so the SPA opens the tab. Never raises; returns the
    manifest S3 key, or ``""`` when the write could not happen (helper absent or PutObject failed).
    This is the only blocking call ``moments_render`` can make, and only on this degraded path.
    """
    try:
        from render.artifacts import write_session_manifest
    except Exception as exc:  # noqa: BLE001 - helper lands in Task 7.7; degrade cleanly
        logger.debug("moments: write_session_manifest unavailable (%s); manifest not written", exc)
        return ""
    try:
        key = write_session_manifest(session_id, manifest.get("id", ""), manifest) or ""
    except Exception as exc:  # noqa: BLE001 - never break the hook path
        logger.warning("moments: degraded manifest write failed: %s", exc)
        return ""
    if key:
        try:
            from orchestration.progress_channel import get_active_channel
            ch = get_active_channel()
            if ch is not None:
                ch.push("done", "↳ Moments", agent="render",
                        extra={"render_manifest_key": key, "render_request_id": rid,
                               "render_label": label})
        except Exception as exc:  # noqa: BLE001 - the tab is written; announcing is best-effort
            logger.debug("moments: could not announce degraded manifest: %s", exc)
    return key


def _flush_moments_slot() -> Optional[str]:
    """Write the buffered Moments_Gallery manifest and announce its tab. Never raises.

    Runs first in ``flush_render_batch``, independently of ``_render_batch`` (which may be empty):
    ``moments_render`` buffers the slot separately, so a turn that produced Moments but no artifact
    render still writes its tab. PutObjects the slot's manifest via
    ``render.artifacts.write_session_manifest`` under ``artifacts/<sid>/render/<manifest id>.json``.
    On success pushes a terminal ``("done", "↳ Moments", agent="render")`` step carrying
    ``render_manifest_key`` / ``render_request_id`` / ``render_label`` so the SPA fills the
    composing tab emitted at buffer time; on failure pushes the existing ``render_failed`` signal
    for the slot's rid so that composing tab is removed instead of spinning forever. Clears the
    slot either way. Returns the manifest S3 key, or ``None`` when there was nothing to flush or
    the write could not happen.
    """
    global _moments_slot
    slot = _moments_slot
    _moments_slot = None
    if not slot:
        return None
    manifest = slot.get("manifest") or {}
    rid = slot.get("rid") or ""
    label = slot.get("label") or "Moments"
    session_id = slot.get("session_id") or ""

    key = ""
    try:
        from render.artifacts import write_session_manifest
        key = write_session_manifest(session_id, manifest.get("id", ""), manifest) or ""
    except Exception as exc:  # noqa: BLE001 - a flush failure must never break teardown
        logger.warning("moments flush: manifest write failed: %s", exc)
        key = ""

    try:
        from orchestration.progress_channel import get_active_channel
        ch = get_active_channel()
        if ch is not None:
            if key:
                ch.push("done", "↳ Moments", agent="render",
                        extra={"render_manifest_key": key, "render_request_id": rid,
                               "render_label": label})
            else:
                # The existing render_failed signal: a terminal event the UI uses to REMOVE the
                # composing tab emitted at buffer time (mirrors dispatch_render's orphan resolve).
                ch.push("failed", "↳ Render produced no viewable output", agent="render",
                        extra={"render_request_id": rid, "render_failed": True})
    except Exception as exc:  # noqa: BLE001 - the tab is written; announcing is best-effort
        logger.debug("moments flush: could not announce: %s", exc)
    return key or None


def moments_render(params: dict, ctx: dict) -> ActionResult:
    """Buffer the turn's Moments_Gallery (design Component 6; Req 3.1–3.7, 3.19).

    Fires once per Main_Agent turn at ``session_end``. Synchronous and I/O-free on the event loop
    (it runs inside ``engine.fire``): Moment selection and manifest building are pure, and the only
    buffering is into ``_moments_slot`` plus a composing progress marker. The PutObject happens
    later in the batch flush on a worker thread (Task 7.7). Only on the degraded path — the render
    batch never opened at ``session_start`` — does it write the manifest at once. Never raises;
    each guard returns a ``skipped`` result with a reason.
    """
    global _moments_slot
    try:
        from project_search import turn_end
        from project_search.context import current_turn_context
        from render import moments
    except Exception as exc:  # noqa: BLE001 - the Projects search / render stack may be absent
        logger.debug("moments_render: search/render stack unavailable: %s", exc)
        return ActionResult("moments_render", "skipped", "search/render stack unavailable")

    # 1. Projects turn context under an Active_Project (Req 3.3, "Active_Project unknown").
    tc = current_turn_context.get()
    if tc is None or not tc.turn_id or not tc.active_project.id:
        return ActionResult("moments_render", "skipped", "no active-project turn context")
    turn_id = tc.turn_id
    project_id = tc.active_project.id

    # 2. Actor and session; the manifest key needs the session (Req 3.3, "Actor unknown"). Both
    #    raise when unbound (Task_Agent, non-Projects invokes), which is a skip, not an error.
    try:
        from jobs.context import current_actor_id, current_session_id
        actor_id = current_actor_id()
        session_id = current_session_id()
    except Exception:  # noqa: BLE001 - unbound identity -> nothing to render
        return ActionResult("moments_render", "skipped", "actor or session unknown")
    if not actor_id or not session_id:
        return ActionResult("moments_render", "skipped", "actor or session unknown")

    # 3. At most once per turn, even if session_end fires twice.
    if turn_id in _moments_done:
        return ActionResult("moments_render", "skipped", "moments already handled this turn")
    _mark_moments_done(turn_id)

    # 4. The TurnOutcome finish_turn stored (Component 4). Only the Main_Agent has one; a project
    #    mismatch means this outcome is not for the Active_Project of this turn.
    outcome = turn_end.outcome_for(turn_id)
    if outcome is None:
        return ActionResult("moments_render", "skipped", "no turn outcome")
    if outcome.project_id != project_id:
        return ActionResult("moments_render", "skipped", "outcome project mismatch")

    # 5. An unreadable Project_Registry builds no Moment, even if other data is present (Req 3.3).
    if outcome.assets is None:
        return ActionResult("moments_render", "skipped", "project registry unreadable")

    # 6. Select the Moments (pure). None/empty -> no tab (Req 3.6).
    selection = moments.select_moments(outcome)
    if selection is None:
        return ActionResult("moments_render", "skipped", "no moments selected")
    # 6b. An image already shown this turn is not shown again as a Moment: its card is the same
    #     picture. Two paths put an image on screen directly: an explicit `show_asset` (recorded via
    #     note_shown_asset, staging 1a5a3b78), and the harvest/render path that buffers a standalone
    #     image tab (ISSUE 4a, session aa9e30ba) — fold both into one `shown` set. Video/audio Moments
    #     stay: they point at specific times.
    shown = shown_assets_for(turn_id) | _harvested_image_asset_ids(project_id)
    if shown:
        kept = [m for m in selection.items if not (m.kind == "image" and m.ref.asset_id in shown)]
        if not kept:
            return ActionResult("moments_render", "skipped", "every moment was already shown")
        if len(kept) != len(selection.items):
            import dataclasses
            selection = dataclasses.replace(selection, items=kept)

    # 7. Build the manifest (pure, no URL by construction).
    created_at = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    manifest = moments.build_moments_manifest(
        selection, session_id=session_id, turn_id=turn_id, created_at=created_at,
        query=(getattr(outcome, "queries", None) or [None])[0],
        query_ref=getattr(outcome, "query_ref", None),
    )
    rid = _moments_rid(session_id, turn_id)
    label = "Moments"

    # 8. Buffer for the turn flush when the render batch is open (the normal path): the slot the
    #    flush (Task 7.7) consumes, plus the composing marker emitted live while the loop is free.
    #    Otherwise (the batch failed to open at session_start) write it at once -- the only blocking
    #    call, and only on that degraded path.
    if _batch_is_open():
        _moments_slot = {"manifest": manifest, "rid": rid, "label": label,
                         "session_id": session_id}
        _emit_composing_marker(rid, label)
        return ActionResult(
            "moments_render", "ok",
            f"{len(selection.items)} moment(s) buffered for turn flush",
            extra={"render_request_id": rid},
        )

    key = _write_moments_manifest(session_id, manifest, rid, label)
    return ActionResult(
        "moments_render",
        "ok" if key else "skipped",
        detail=key or "no manifest written",
        extra={"render_manifest_key": key, "render_request_id": rid} if key else {},
    )


register_action("moments_render", moments_render)


# ---- render-graph: the turn's "↳ Graph" tab (knowledge-graph extension of the Moments gallery) ----------

_graph_slot: Optional[dict] = None
_graph_done: "OrderedDict[str, bool]" = OrderedDict()


def _write_and_announce_graph(slot: dict) -> Optional[str]:
    """PutObject the slot's manifest and push the render step that opens the tab. Never raises."""
    manifest, rid, label = slot.get("manifest") or {}, slot.get("rid") or "", slot.get("label") or "Graph"
    key = ""
    try:
        from render.artifacts import write_session_manifest
        key = write_session_manifest(slot.get("session_id") or "", manifest.get("id", ""), manifest) or ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("graph flush: manifest write failed: %s", exc)
    try:
        from orchestration.progress_channel import get_active_channel
        ch = get_active_channel()
        if ch is not None:
            if key:
                ch.push("done", "↳ Graph", agent="render",
                        extra={"render_manifest_key": key, "render_request_id": rid, "render_label": label})
            else:
                ch.push("failed", "↳ Render produced no viewable output", agent="render",
                        extra={"render_request_id": rid, "render_failed": True})
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph flush: could not announce: %s", exc)
    return key or None


def _flush_graph_slot() -> Optional[str]:
    """Write the buffered graph-view manifest (runs in ``flush_render_batch``, beside Moments). Never raises."""
    global _graph_slot
    slot, _graph_slot = _graph_slot, None
    return _write_and_announce_graph(slot) if slot else None


def graph_render(params: dict, ctx: dict) -> ActionResult:
    """Buffer the turn's "↳ Graph" tab: everything the knowledge-graph tools touched this turn, as one graph.

    Fires once per Main_Agent turn at ``session_end`` (locked built-in ``render-graph``). Reads the turn's
    entries from ``project_search.graph_ledger`` (only the Main_Agent records — sub-agents and plan nodes never
    open a tab per call), builds a ``graph-view`` manifest (ids/names only, no URL), and buffers it for the
    batch flush like ``moments_render``. Never raises; every guard is a ``skipped`` result.
    """
    global _graph_slot
    try:
        from project_search import graph_ledger
        from project_search.context import current_turn_context
        from render import graph_view
    except Exception as exc:  # noqa: BLE001
        return ActionResult("graph_render", "skipped", f"graph render stack unavailable: {type(exc).__name__}")
    tc = current_turn_context.get()
    if tc is None or not tc.turn_id or not tc.active_project.id:
        return ActionResult("graph_render", "skipped", "no active-project turn context")
    try:
        from jobs.context import current_actor_id, current_session_id
        actor_id, session_id = current_actor_id(), current_session_id()
    except Exception:  # noqa: BLE001
        return ActionResult("graph_render", "skipped", "actor or session unknown")
    if not actor_id or not session_id:
        return ActionResult("graph_render", "skipped", "actor or session unknown")
    if tc.turn_id in _graph_done:
        return ActionResult("graph_render", "skipped", "graph already handled this turn")
    _graph_done[tc.turn_id] = True
    while len(_graph_done) > _MOMENTS_DONE_MAX:
        _graph_done.popitem(last=False)

    turn_graph = graph_ledger.take(tc.turn_id, tc.active_project.id)
    if turn_graph is None:
        return ActionResult("graph_render", "skipped", "no graph calls this turn")
    manifest = graph_view.build_graph_manifest(
        turn_graph.calls, session_id=session_id, turn_id=tc.turn_id, project_id=tc.active_project.id,
        project_name=tc.active_project.name, created_at=time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()))
    if manifest is None:
        return ActionResult("graph_render", "skipped", "nothing to draw")
    slot = {"manifest": manifest, "rid": manifest["id"], "label": "Graph", "session_id": session_id}
    if _batch_is_open():
        _graph_slot = slot
        _emit_composing_marker(manifest["id"], "Graph")
        return ActionResult("graph_render", "ok", f"{len(manifest['nodes'])} node(s) buffered for turn flush",
                            extra={"render_request_id": manifest["id"]})
    key = _write_and_announce_graph(slot)
    return ActionResult("graph_render", "ok" if key else "skipped", detail=key or "no manifest written",
                        extra={"render_manifest_key": key, "render_request_id": manifest["id"]} if key else {})


register_action("graph_render", graph_render)
