"""Async job tracking — Job_Detector.

Classifies MCP tool descriptors as start / status / results jobs and
groups matched triplets into :class:`JobTrio` records. Convention-based
detection (``start_*`` prefix, ``*_status`` / ``*_results`` suffixes) is
the zero-config path; an opt-in ``_meta.job_role`` annotation overrides
the convention whenever it carries one of the three legal role values
(R2.3).

The detector is invoked at MCP tool-list time by
:class:`MCPManager.GatewayConnection` (task 3.1). The resulting
:class:`DetectionResult` is cached on the gateway connection so the
per-tool job-tracking wrap (see
:mod:`agent.src.jobs.strands_tool_wrap`) can resolve trio membership
in O(1) when wrapping a freshly-listed tool set, via
:meth:`DetectionResult.find_trio_with_start` and
:meth:`DetectionResult.find_trio_with_results`.

See ``.kiro/specs/async-job-tracking/design.md`` (Job_Detector section,
including the ``classify_and_group`` Python pseudocode block) for the
canonical algorithm and ``requirements.md`` (sections 1 and 2) for the
acceptance criteria.

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.2, 2.3, 2.4, 2.5
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from .models import JobTrio

logger = logging.getLogger(__name__)


# Convention affixes (R1.1, R1.2, R1.3). Centralised so the strip
# logic in :func:`_base_token_from` and the match logic in
# :func:`_classify_one` stay in lock-step.
_START_PREFIX = "start_"
_STATUS_SUFFIX = "_status"
_RESULTS_SUFFIX = "_results"

# Optional ``get_`` prefix on status/results tools. Many MCP servers
# name their read-only status/results tools with a ``get_`` prefix
# (e.g., ``get_stem_separation_status``) to mirror REST-style naming.
# Without stripping that prefix during base-token derivation, a tool
# trio of ``start_widget`` / ``get_widget_status`` / ``get_widget_results``
# resolves to two distinct groups (``widget`` and ``get_widget``) and
# never forms a trio. The strip is intentionally applied only to the
# status/results roles — ``start_`` tools never carry a ``get_`` prefix
# in practice, and stripping it from ``start_*`` would silently merge
# unrelated trios.
#
# This is an extension to the convention as written in the design doc;
# the canonical reference example (audiostem in
# m2c-micro-workflow-mcp) uses the ``get_*_status`` / ``get_*_results``
# shape.
_GET_PREFIX = "get_"

# AgentCore Gateway namespace separator. When the agent loads tools
# through an AgentCore Gateway, every tool name comes back prefixed
# with the gateway *target* name plus a literal ``___`` separator
# (three underscores), e.g. ``audio-stem-separation___start_stem_separation``.
# Without splitting on this separator the detector would never
# classify the start peer (it doesn't ``startswith("start_")``), and
# the status/results peers would group under the wrong base token.
#
# The split is asymmetric vs. the affix strips: the prefix is preserved
# in the resulting group key so that two MCP servers behind the same
# gateway can each own a trio with the same base token (e.g., a hypo-
# thetical ``foo___start_widget`` and ``bar___start_widget`` resolve
# to distinct groups ``foo|widget`` and ``bar|widget`` rather than
# colliding on ``widget``). The chosen group separator is ``|`` —
# avoids round-tripping through ``___`` when consumers split the group
# back out, and is unlikely to appear in MCP tool names.
_GATEWAY_NAMESPACE_SEPARATOR = "___"
_GROUP_NAMESPACE_SEPARATOR = "|"

# Allowed values for the ``_meta.job_role`` annotation (R2.1). Anything
# outside this set is treated as an annotation author error and
# excludes the tool from any trio.
_VALID_ROLES: frozenset[str] = frozenset({"start", "status", "results"})


@dataclass(frozen=True)
class ToolDescriptor:
    """Minimal MCP tool descriptor consumed by the detector.

    The detector only needs the tool's ``name`` and the optional
    ``_meta`` block from the MCP ``tools/list`` response. Wrapping in a
    plain dataclass keeps the detector decoupled from the Strands
    tool-object internals and from the wire shape MCPManager exposes —
    task 3.1 is responsible for adapting the gateway's raw tools into
    :class:`ToolDescriptor` instances.
    """

    name: str
    meta: Optional[dict] = None


@dataclass(frozen=True)
class ClassifiedTool:
    """Internal record carrying a tool plus its resolved role and group.

    ``from_annotation`` records whether the role came from the
    ``_meta.job_role`` annotation path so the downstream orphan logger
    can pick the correct severity (warning for annotation-orphans per
    R2.4, info for convention-orphans per R1.5).
    """

    tool: ToolDescriptor
    role: str
    group: str
    from_annotation: bool


@dataclass
class DetectionResult:
    """Output of :func:`classify_and_group`.

    ``trios`` and ``orphans`` are the public surface consumed by
    diagnostics and the catalog endpoint. The two ``_by_*_tool`` dicts
    back the O(1) lookup helpers used by the per-tool job-tracking
    wrap and any future server-side caller — they are populated
    alongside ``trios`` so a fully-formed trio is always discoverable
    from either its start or its results tool name.
    """

    trios: list[JobTrio] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    # Internal lookup indices. Stored as fields rather than computed on
    # demand so cache lifetime matches ``trios`` exactly and so the
    # lookup hot-path cost stays O(1).
    _by_start_tool: dict[str, JobTrio] = field(default_factory=dict, repr=False)
    _by_results_tool: dict[str, JobTrio] = field(default_factory=dict, repr=False)

    def find_trio_with_start(self, tool_name: str) -> Optional[JobTrio]:
        """Return the :class:`JobTrio` whose start tool is ``tool_name``.

        Returns ``None`` when ``tool_name`` is not the start tool of
        any fully-formed trio (callers use the ``None`` case to mean
        "this is not a job-bearing call").
        """
        return self._by_start_tool.get(tool_name)

    def find_trio_with_results(self, tool_name: str) -> Optional[JobTrio]:
        """Return the :class:`JobTrio` whose results tool is ``tool_name``.

        Returns ``None`` when ``tool_name`` is not the results tool of
        any fully-formed trio.
        """
        return self._by_results_tool.get(tool_name)


def _split_gateway_namespace(tool_name: str) -> tuple[str, str]:
    """Split a tool name on the AgentCore Gateway ``___`` separator.

    Returns ``(prefix, suffix)`` where ``prefix`` is the empty string
    for un-namespaced tools (so callers can prepend ``prefix`` to a
    derived base token unconditionally without an extra branch).

    The separator (``___``) is treated as a single split point. If the
    name contains the separator multiple times, the first occurrence
    wins — gateway target names don't contain ``___``, so subsequent
    occurrences belong to the underlying tool name and should be
    preserved verbatim in the suffix.
    """
    if _GATEWAY_NAMESPACE_SEPARATOR not in tool_name:
        return ("", tool_name)
    prefix, suffix = tool_name.split(_GATEWAY_NAMESPACE_SEPARATOR, 1)
    return (prefix, suffix)


def _namespaced_group(prefix: str, base_token: str) -> str:
    """Combine a gateway prefix and a base token into a group key.

    When ``prefix`` is empty, returns the base token unchanged so
    un-namespaced tools land on the same group keys they did before
    the gateway-aware extension (preserves test fixtures and the
    annotation-fallback path).
    """
    if not prefix:
        return base_token
    return f"{prefix}{_GROUP_NAMESPACE_SEPARATOR}{base_token}"


def _strip_get_prefix(token: str) -> str:
    """Strip a leading ``get_`` prefix from a status/results base token.

    See ``_GET_PREFIX`` for the rationale (REST-style ``get_*_status``
    naming is common; without this strip the trio matcher would orphan
    canonical references like the audiostem server's tools).

    Returns the token unchanged when:

    - The token does not begin with ``get_``.
    - Stripping would yield an empty string (``get_`` alone). An empty
      group key would collide across unrelated tools that all happen
      to be named exactly ``get_status`` or ``get_results``, which is
      worse than orphaning the malformed pair.
    """
    if token.startswith(_GET_PREFIX) and len(token) > len(_GET_PREFIX):
        return token[len(_GET_PREFIX):]
    return token


def _base_token_from(tool_name: str, role: str) -> str:
    """Derive the trio group token by stripping the role's conventional affix.

    Falls back to the raw tool name when the affix is absent — this is
    the path taken by an annotation-classified tool whose name does not
    fit the convention and whose author did not supply an explicit
    ``job_group``. Such tools effectively orphan themselves unless
    their peers happen to share the same raw name as a group, which
    matches the spirit of R2.4 (incomplete annotation trio → exclude).

    For status and results roles the derived token is additionally
    passed through :func:`_strip_get_prefix` so a leading ``get_`` does
    not break grouping against a sibling start tool whose name lacks
    that prefix.
    """
    if role == "start" and tool_name.startswith(_START_PREFIX):
        return tool_name[len(_START_PREFIX):]
    if role == "status" and tool_name.endswith(_STATUS_SUFFIX):
        return _strip_get_prefix(tool_name[: -len(_STATUS_SUFFIX)])
    if role == "results" and tool_name.endswith(_RESULTS_SUFFIX):
        return _strip_get_prefix(tool_name[: -len(_RESULTS_SUFFIX)])
    return tool_name


def _classify_one(tool: ToolDescriptor) -> Optional[ClassifiedTool]:
    """Classify a single tool, applying annotation-override (R2.3).

    Returns ``None`` when the tool matches neither annotation nor
    convention, when the annotation carries an out-of-set role value,
    when ``job_group`` resolves to a non-string or empty value, or when
    classifying an annotated tool raises an unexpected exception
    (R2.5).
    """
    # Annotation-override path (R2.3). Wrap the entire annotation read
    # in a try/except so any unexpected internal error during
    # annotation handling excludes the tool with a logged error rather
    # than aborting classification of the whole tool list (R2.5).
    if tool.meta is not None:
        try:
            # ``meta`` may legitimately be a non-dict in odd MCP servers.
            # Treat that as "no annotation" and fall through to
            # convention rather than raising.
            annotation = tool.meta.get("job_role") if hasattr(tool.meta, "get") else None
            if annotation is not None:
                if annotation not in _VALID_ROLES:
                    # R2.1 constrains the value set; an out-of-set
                    # value is an author bug — exclude the tool with a
                    # warning so the operator can fix the annotation.
                    logger.warning(
                        "Job_Detector: tool %r has invalid job_role=%r; excluded",
                        tool.name, annotation,
                    )
                    return None

                role = annotation
                # Explicit ``job_group`` wins over derived base token
                # (R2.2). Empty / non-string group is treated as
                # malformed and excludes the tool — without a group key
                # the tool can never join a trio.
                explicit_group = tool.meta.get("job_group")
                if explicit_group is not None:
                    if not isinstance(explicit_group, str) or not explicit_group:
                        logger.warning(
                            "Job_Detector: tool %r has invalid job_group=%r; excluded",
                            tool.name, explicit_group,
                        )
                        return None
                    group = explicit_group
                else:
                    group = _base_token_from(tool.name, role)

                return ClassifiedTool(
                    tool=tool,
                    role=role,
                    group=group,
                    from_annotation=True,
                )
        except Exception as e:  # noqa: BLE001 — R2.5 explicitly requires "any internal error"
            logger.error(
                "Job_Detector: failed to classify annotated tool %r: %s",
                tool.name, e,
            )
            return None

    # Convention path (R1.1, R1.2, R1.3). Reached when the tool either
    # has no ``_meta`` block, has no ``job_role`` key inside it, or
    # carries a non-dict ``_meta`` value. Order matches the design
    # pseudocode: start prefix first, then status suffix, then results
    # suffix.
    #
    # AgentCore Gateway namespace handling: tool names returned from
    # the gateway are shaped ``<target>___<tool>``, where the target
    # prefix has no semantic meaning to the convention rules. Split it
    # off, run convention matching against the suffix, and re-attach
    # the prefix to the derived group key so trios stay isolated
    # across gateway targets. Un-namespaced names (no ``___``) yield
    # an empty prefix, which collapses to the original behaviour.
    prefix, suffix = _split_gateway_namespace(tool.name)
    if suffix.startswith(_START_PREFIX):
        return ClassifiedTool(
            tool=tool,
            role="start",
            group=_namespaced_group(prefix, suffix[len(_START_PREFIX):]),
            from_annotation=False,
        )
    if suffix.endswith(_STATUS_SUFFIX):
        return ClassifiedTool(
            tool=tool,
            role="status",
            group=_namespaced_group(
                prefix,
                _strip_get_prefix(suffix[: -len(_STATUS_SUFFIX)]),
            ),
            from_annotation=False,
        )
    if suffix.endswith(_RESULTS_SUFFIX):
        return ClassifiedTool(
            tool=tool,
            role="results",
            group=_namespaced_group(
                prefix,
                _strip_get_prefix(suffix[: -len(_RESULTS_SUFFIX)]),
            ),
            from_annotation=False,
        )
    return None


# M2C media-workflow shape helpers. Services expose async start tools (e.g.
# ``diarize_speech``, ``separate_audio_stems``, ``process_media``) that return a
# job_id, a SHARED ``get_job_status`` per service, and NO ``*_results`` tool.
_SHARED_STATUS_TOOL = "get_job_status"
_SHARED_STATUS_NON_START = frozenset({"get_job_status", "get_tool_version", "describe_tool"})


def _shared_status_job_trios(
    tools: list[ToolDescriptor], gateway_id: str = "",
) -> list[JobTrio]:
    """Detect the M2C media-workflow job shape and emit one trio per start tool.

    For each gateway target that exposes a ``<target>___get_job_status`` tool,
    treat its other action tools (not ``get_job_status`` / ``get_tool_version``,
    and not already matching the ``start_*`` / ``*_status`` / ``*_results``
    convention) as async start tools that share that status tool. ``results_tool``
    is left empty — there is no results tool to gate; the polling worker tracks
    the job via the SFN-direct path (the job_id is a Step Functions execution
    ARN) and the agent can poll ``get_job_status`` inline.
    """
    by_target: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for tool in tools:
        prefix, suffix = _split_gateway_namespace(tool.name)
        if prefix:
            by_target[prefix].append((suffix, tool.name))

    trios: list[JobTrio] = []
    for prefix, members in by_target.items():
        status_full = next(
            (full for bare, full in members if bare == _SHARED_STATUS_TOOL), None
        )
        if not status_full:
            continue
        for bare, full in members:
            if (
                bare in _SHARED_STATUS_NON_START
                or bare.startswith(_START_PREFIX)
                or bare.endswith(_STATUS_SUFFIX)
                or bare.endswith(_RESULTS_SUFFIX)
            ):
                continue
            trios.append(
                JobTrio(
                    trio_id=_namespaced_group(prefix, bare),
                    gateway_id=gateway_id,
                    start_tool=full,
                    status_tool=status_full,
                    results_tool="",
                )
            )
    return trios


def classify_and_group(
    tools: list[ToolDescriptor],
    gateway_id: str = "",
) -> DetectionResult:
    """Classify ``tools`` and group fully-covered trios.

    Convention matching uses ``start_*`` / ``*_status`` / ``*_results``
    name affixes (R1.1–R1.3). An ``_meta.job_role`` annotation that
    carries one of the three legal role values overrides the
    convention regardless of the tool's name (R2.3); an explicit
    ``_meta.job_group`` annotation overrides the derived base token
    (R2.2). Tools whose group does not resolve to all three roles are
    excluded from any trio and logged for diagnostics (R1.5, R2.4);
    tools that raise during annotation handling are likewise excluded
    with a logged error (R2.5).

    Args:
        tools: The tool descriptors from one gateway's ``tools/list``
            response.
        gateway_id: The gateway id stamped onto each emitted
            :class:`JobTrio`. Defaults to the empty string so unit
            tests that do not care about gateway provenance can omit
            it; production callers (MCPManager, task 3.1) supply the
            owning :class:`GatewayConnection`'s id.

    Returns:
        A :class:`DetectionResult` containing the formed trios, the
        list of orphan tool names, and the O(1) lookup helpers used
        by the dispatch interceptor.
    """
    classified: list[ClassifiedTool] = []
    for tool in tools:
        ct = _classify_one(tool)
        if ct is not None:
            classified.append(ct)

    # Group classified tools first by their derived/declared group
    # key, then by role within the group. Using ``defaultdict(dict)``
    # mirrors the design pseudocode exactly.
    by_group: dict[str, dict[str, ClassifiedTool]] = defaultdict(dict)
    for ct in classified:
        # Note: when two tools resolve to the same (group, role) pair
        # (e.g., two ``start_*`` tools that strip to the same base
        # token, or two annotated tools claiming the same slot) the
        # later write wins. This matches the design pseudocode and is
        # consistent with the spec, which does not address the
        # collision case explicitly.
        by_group[ct.group][ct.role] = ct

    result = DetectionResult()
    consumed_tool_names: set[str] = set()
    for group, roles in by_group.items():
        if _VALID_ROLES <= roles.keys():
            # Fully-formed trio (R1.4): start, status, and results
            # peers all present under the same group key.
            trio = JobTrio(
                trio_id=group,
                gateway_id=gateway_id,
                start_tool=roles["start"].tool.name,
                status_tool=roles["status"].tool.name,
                results_tool=roles["results"].tool.name,
            )
            result.trios.append(trio)
            result._by_start_tool[trio.start_tool] = trio
            result._by_results_tool[trio.results_tool] = trio
            consumed_tool_names.update(ct.tool.name for ct in roles.values())
        else:
            # Incomplete group: every classified tool in it is an
            # orphan (R1.5 / R2.4) UNLESS the gateway-namespace fallback
            # below reconciles it. Decide log severity based on whether
            # annotation participated — annotation-grouped misses get a
            # single warning identifying the missing roles (R2.4),
            # convention-only misses get per-tool info logs of the
            # excluded names (R1.5).
            missing_roles = sorted(_VALID_ROLES - roles.keys())
            tool_names = [ct.tool.name for ct in roles.values()]
            result.orphans.extend(tool_names)

            any_from_annotation = any(ct.from_annotation for ct in roles.values())
            if any_from_annotation:
                logger.warning(
                    "Job_Detector: trio group %r missing roles %s; "
                    "excluded tools: %s",
                    group, missing_roles, tool_names,
                )
            else:
                for tool_name in tool_names:
                    logger.info(
                        "Job_Detector: orphan tool %r (group %r, "
                        "missing roles %s)",
                        tool_name, group, missing_roles,
                    )

    # Gateway-namespace fallback reconciliation. Strict base-token
    # matching (start_X / get_X_status / get_X_results all deriving the
    # exact same X) fails whenever a service's start tool name carries
    # extra words its status/results tools don't share — e.g. the
    # deployed m2c speech-diarization target exposes
    # ``start_speech_diarization`` alongside ``get_diarization_status``
    # / ``get_diarization_results``: base tokens "speech_diarization" vs
    # "diarization" never match, so the trio silently fails to form and
    # the job is left with no results tool even though one genuinely
    # exists. Since AgentCore Gateway tools are already disambiguated by
    # their target-name prefix (the "___" namespace), it's safe to merge
    # any still-orphaned start/status/results tools that share the same
    # prefix without requiring their base tokens to match too — cross-
    # target collisions can't happen because the prefix itself scopes
    # the merge. Deliberately NOT applied to un-namespaced tools (empty
    # prefix), where there is no scoping boundary and merging orphans by
    # role alone could wire together two unrelated tools that happen to
    # both be classified but never intended as a pair.
    by_prefix_orphans: dict[str, dict[str, ClassifiedTool]] = defaultdict(dict)
    for ct in classified:
        if ct.tool.name in consumed_tool_names:
            continue
        prefix, _ = _split_gateway_namespace(ct.tool.name)
        if not prefix:
            continue
        by_prefix_orphans[prefix][ct.role] = ct

    for prefix, roles in by_prefix_orphans.items():
        if "start" not in roles:
            continue
        if "status" not in roles and "results" not in roles:
            continue
        start_ct = roles["start"]
        status_ct = roles.get("status")
        results_ct = roles.get("results")
        trio = JobTrio(
            trio_id=start_ct.group,
            gateway_id=gateway_id,
            start_tool=start_ct.tool.name,
            status_tool=status_ct.tool.name if status_ct else "",
            results_tool=results_ct.tool.name if results_ct else "",
        )
        result.trios.append(trio)
        result._by_start_tool[trio.start_tool] = trio
        if trio.results_tool:
            result._by_results_tool[trio.results_tool] = trio
        reconciled_names = [start_ct.tool.name]
        if status_ct:
            reconciled_names.append(status_ct.tool.name)
        if results_ct:
            reconciled_names.append(results_ct.tool.name)
        result.orphans = [n for n in result.orphans if n not in reconciled_names]
        logger.info(
            "Job_Detector: reconciled trio for gateway-target %r via namespace "
            "fallback (base tokens did not match): %s",
            prefix, reconciled_names,
        )

    # M2C media-workflow shape: async start tools + a SHARED get_job_status,
    # no *_results tool — doesn't fit the start/status/results convention.
    # Register these so the polling worker tracks them (job_ids are SFN
    # execution ARNs → the SFN-direct polling path). An empty results_tool
    # means no results gate is installed, so the agent can still poll
    # get_job_status inline.
    for trio in _shared_status_job_trios(tools, gateway_id):
        if trio.start_tool in result._by_start_tool:
            continue
        result.trios.append(trio)
        result._by_start_tool[trio.start_tool] = trio

    return result


__all__ = [
    "ToolDescriptor",
    "ClassifiedTool",
    "DetectionResult",
    "classify_and_group",
]
