"""Built-in ``get_plan_results`` Strands tool — read a plan's node outputs.

WHY THIS EXISTS
---------------
A completed DAG plan had no readable results. ``list_my_jobs`` correctly showed
the plan and its nodes as ``SUCCEEDED``, but every coordinator job row carries an
EMPTY ``results_tool`` (by design — a plan's lifecycle is driven by the Plan
Chainer, not by a poll trio), so the orchestrator had no way to get from "it
succeeded" to "here is what it produced". The node outputs live in the plan
manifest in S3, which until now only ``task_agent`` could read. Asked to
"retrieve the results", the orchestrator therefore looped through model
round-trips looking for file references that nothing would ever hand it.

This tool closes that gap: it reads the manifest and returns each node's output,
so the agent can either summarise the values directly or feed any ``s3://`` refs
it finds to ``read_result_files``.

An honest answer is part of the point. A node's output may record an ASYNC
SUBMISSION (a queue ``request_id`` plus a ``status_url``) rather than a finished
artifact, because a node completes when its tool call returns — and for a queued
media job that return is an accepted submission. Surfacing that verbatim lets the
agent say "the render was submitted and is still running, here is its status url"
instead of hunting for a file that does not exist yet.

ACTOR SCOPING
-------------
The plan id is resolved through :meth:`JobRegistry.get`, which enforces equality
with the caller's bound actor and returns ``None`` for another actor's job. Both
"missing" and "wrong actor" collapse to the same ``not_found`` response so the
caller cannot infer that a plan belongs to someone else — the same contract
``get_job`` follows. The manifest's S3 location is derived from the session id on
that verified record rather than from the caller's current session, so the lookup
is both correctly scoped and usable from a later session.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

from strands import tool

from . import context as jobs_context
from .registry import JobRegistry

logger = logging.getLogger(__name__)

_registry: Optional[JobRegistry] = None
_bucket: str = ""
_region: str = "us-west-2"
# Lazily-created S3 client for mirroring external (fal.media) plan outputs to our
# durable store. Module-level so it is reused across calls (mirrors the direct
# tool-result path's _MIRROR_S3_CLIENT).
_MIRROR_S3 = None


def set_registry(registry: Optional[JobRegistry]) -> None:
    """Wire the job registry used to resolve and actor-scope the plan id."""
    global _registry
    _registry = registry


def set_manifest_location(*, bucket: str, region: str = "us-west-2") -> None:
    """Wire the S3 bucket holding plan manifests.
    Kept separate from :func:`set_registry` because the two come from different
    environment variables and either can be absent independently; a missing
    bucket is a configuration problem and is reported as such rather than as a
    missing plan.
    """
    global _bucket, _region
    _bucket = bucket or ""
    _region = region or "us-west-2"


def _read_plan_result(session_id: str, plan_id: str):
    """The resolved aggregate for this plan OR lone job, or None if there isn't one.

    Two locations, because both producers write the same schema: the chainer writes
    ``plans/<session>/<plan>/result.json`` at plan settle, and the polling worker
    writes ``jobs/<job>/result.json`` at job terminal. A LONE job had no aggregate
    at all, which is why retrieving one still cost ~27 model steps of fan-out while
    a four-node plan cost 18.

    Returns None -- not an error -- for any miss, so the caller falls back to the
    raw manifest. A plan that settled before this artifact existed must keep
    working, and a transient S3 failure should degrade to the old behaviour rather
    than fail the retrieval.
    """
    if not _bucket:
        return None
    candidates = [
        f"plans/{session_id}/{plan_id}/result.json",
        f"jobs/{plan_id}/result.json",
    ]
    try:
        import boto3

        client = boto3.client("s3", region_name=_region)
    except Exception:
        return None
    for key in candidates:
        try:
            body = client.get_object(Bucket=_bucket, Key=key)["Body"].read()
            data = json.loads(body.decode("utf-8"))
        except Exception:
            continue
        if isinstance(data, dict) and data.get("schema"):
            return data
    logger.debug("get_plan_results: no resolved aggregate for %s", plan_id)
    return None


def _mirror_external_media_in(obj):
    """Mirror external (e.g. fal.media) media in a plan-results payload to our durable
    store and rewrite the URLs to the permanent CloudFront copies.

    WHY HERE: the direct MCP tool-result path mirrors third-party media via
    ``_ResilientMCPMixin._mirror_result_media`` — but that wrap does NOT fire on the
    plan-execution path (``task_agent`` notes a fal render "fell through both this
    branch and the automatic JobTrackingMCPAgentTool wrap … which may not fire
    depending on the Strands execution path"). So a fal output produced by a plan node
    is persisted to plan state as an ephemeral ``fal.media`` URL, and ``get_plan_results``
    returns it verbatim; once fal purges the file it 404s in chat and render history
    (prod session be3e9c00 — 8 restyled frames). Mirroring at this boundary is the plan
    counterpart of the tool-result chokepoint: the agent then writes/render the durable
    CloudFront URL, not the ephemeral one.

    Best-effort and never fatal: any failure (unconfigured bucket, a fal URL that is
    already dead, a copy error) returns the payload unchanged. Idempotent with the
    other mirrors (``is_already_ours`` URLs are never re-copied).
    """
    try:
        from media import persist_media

        bucket = os.environ.get("SKILLS_S3_BUCKET") or os.environ.get("S3_BUCKET", "")
        if not bucket:
            return obj
        domain = os.environ.get("ARTIFACTS_DOMAIN", "")
        prefix = os.environ.get("ARTIFACTS_PREFIX") or os.environ.get("S3_PREFIX", "artifacts/")
        global _MIRROR_S3
        if _MIRROR_S3 is None:
            import boto3

            _MIRROR_S3 = boto3.client("s3", region_name=_region)
        # limit generously: a plan fans out to many nodes, each of which may emit a
        # third-party asset — the direct path's default of 8 is per single tool call.
        new_obj, copied = persist_media.swap_result_media(
            obj, s3_client=_MIRROR_S3, bucket=bucket, prefix=prefix,
            artifacts_domain=domain, limit=32)
        if copied:
            logger.info(
                "get_plan_results: mirrored %d external asset(s) into durable store", len(copied))
        return new_obj
    except Exception:  # noqa: BLE001 - mirroring must never break results retrieval
        logger.warning("get_plan_results: media mirror passthrough", exc_info=True)
        return obj


@tool
def get_plan_results(plan_id: str) -> str:
    """Read a DAG plan's RESOLVED results. This is the whole answer -- do not chain.

    Use this after ``list_my_jobs``/``get_job`` shows work settled and the user
    wants its RESULTS. Plan job records carry no results tool, so this is the only
    way to see what the nodes produced.

    Works for a SINGLE async job too, not just a multi-node plan: pass the job id
    and you get the same one-record-per-unit shape. Retrieving one lone job used to
    cost ~27 model steps of fetching and presigning; it is now this one call.

    Everything is already resolved for you. Do NOT then call ``read_result_files``,
    ``media-outputs___get_download_url``, or any status/result tool on what this
    returns: the ``s3://`` refs have been read, the analysis files fetched, and the
    queue handles turned into urls, at the moment the plan settled. Chaining those
    calls is what turned a single retrieval into ~40 model steps and several
    minutes, and it re-renders inputs as though they were results. Summarise what
    is here and stop.

    Args:
        plan_id: The plan's job id, as shown by ``list_my_jobs`` (the plan row,
            not a ``planId#nodeId`` row).

    Returns:
        JSON with ``plan_id``, ``display_name``, ``goal``, ``status``,
        ``job_status``, ``counts``, ``artifact_count`` and ``nodes``. Each node
        carries:

        * ``kind`` -- ``media`` (playable output, urls in ``artifacts``),
          ``analysis`` (the result files' contents in ``findings``), ``pending``
          (submitted, not finished -- report as still running), or ``error``.
        * ``artifacts`` -- ``{url, media_kind, role}`` per output. Already
          displayable; the renderer has these too, so just cite them.
        * ``findings`` -- the parsed result files, keyed by filename
          (e.g. ``facemetadata.json``, ``speaker_manifest.json``). Read the
          numbers straight out of here.
        * ``error`` -- why that node has no result. Report it as a failure.

        Top-level ``downloads`` -- a flat, deduped list of ``{url, media_kind,
        role}`` for EVERY produced artifact across all nodes (video, audio, images,
        AND data/analysis files like ``shots.json`` / ``facemetadata.json``). When
        you summarise, present EACH ``downloads`` entry as a markdown link — e.g.
        ``[facemetadata.json](https://…/facemetadata.json)`` — so the user gets a
        download chip for every output, not just the ones you happen to mention.
        Emit these links IN ADDITION to reading numbers out of ``findings``; a file
        being summarised does not make it downloadable, the link does.

        A plan may settle FAILED with some nodes resolved: summarise what
        succeeded AND what failed. ``not_found`` if the plan is missing or another
        actor's; ``registry_unavailable``/``manifest_unavailable`` on
        misconfiguration; older plans predating resolution fall back to raw node
        values, and only then may ``s3://`` refs need ``read_result_files``.
    """
    if _registry is None:
        return json.dumps({"error": "registry_unavailable", "plan_id": plan_id})
    if not _bucket:
        return json.dumps({"error": "manifest_unavailable", "plan_id": plan_id})

    pid = (plan_id or "").strip()
    if not pid:
        return json.dumps({"error": "not_found", "plan_id": plan_id})
    # A node row id ("<plan>#<node>") is a plausible thing for the model to pass
    # here; accept it by reducing to the plan it belongs to rather than failing.
    if "#" in pid:
        pid = pid.split("#", 1)[0]

    # An unbound actor is a configuration/no-request condition, not a missing
    # plan: current_actor_id raises rather than returning a placeholder identity.
    try:
        actor_id = jobs_context.current_actor_id()
    except Exception:
        return json.dumps({"error": "registry_unavailable", "plan_id": pid})

    try:
        record = _registry.get(pid, actor_id=actor_id)
    except Exception:
        logger.exception("get_plan_results: registry lookup failed for %s", pid)
        return json.dumps({"error": "registry_unavailable", "plan_id": pid})

    # Missing and cross-actor are deliberately indistinguishable.
    if record is None:
        # The model very often passes the plan's DISPLAY NAME (e.g. "plan-awsmcp01")
        # rather than its job id (a uuid). A direct get() then misses and returns
        # not_found, which sends the agent into a long manual retrieval loop instead
        # of reading the resolved aggregate (observed: session 9ca1ac85 flailed on
        # get_plan_results("plan-awsmcp01")). A plan row's trio_id is
        # "plan|<display_name>", so resolve the name to the real plan job id before
        # giving up. Bounded single GSI query; newest-first so the latest plan of a
        # reused name wins.
        try:
            candidates = _registry.list_for_actor(actor_id, limit=100)
        except Exception:
            candidates = []
        match = next(
            (r for r in candidates
             if r.trio_id == f"plan|{pid}" or r.trio_id == pid),
            None,
        )
        if match is not None:
            pid = match.job_id
            record = match
    if record is None:
        return json.dumps({"error": "not_found", "plan_id": pid})

    # Prefer the RESOLVED aggregate the chainer publishes at settle. It carries
    # each node's outputs already resolved -- s3 refs read, queue handles turned
    # into urls -- so the caller needs no follow-up calls at all.
    #
    # This is the whole point of the change. Returning raw node values made every
    # retrieval fan out to 1-3 further tool calls PER NODE (~38 steps for a
    # four-node plan), and because every one of those results passed the render
    # hook, session 842bb417 produced 10 render manifests for 4 analyses -- of
    # which only 8 surfaced, and the two dropped included the richest view in the
    # run. It also left the model guessing queue endpoint ids, which is how a
    # video fal had already produced got reported as "unreachable".
    #
    # Keyed by convention rather than by reading resultKey off the record, so this
    # needs no change to JobRecord and still works if the row write failed after
    # the artifact landed.
    resolved = _read_plan_result(record.session_id, pid)
    if resolved is not None:
        resolved["job_status"] = record.status
        # Mirror any external (fal.media) node outputs to our durable store BEFORE
        # building the downloads list, so both the per-node artifacts AND the flat
        # downloads carry permanent CloudFront URLs rather than ephemeral third-party
        # links that 404 once the host purges them (the plan-path mirror gap).
        resolved = _mirror_external_media_in(resolved)
        # Surface a flat, deduped list of EVERY produced artifact url (media AND
        # data/analysis — e.g. shots.json, facemetadata.json, output .wav) so the
        # agent can present each as a download link in chat. Without this the model
        # described analysis-file CONTENTS (from `findings`) but never emitted their
        # urls, so metadata/audio outputs were readable yet not downloadable (#2/#3).
        # Generic: whatever the aggregate already resolved as an artifact — no
        # filename allowlist here.
        try:
            seen: set = set()
            downloads = []
            for node in resolved.get("nodes", []) or []:
                for art in (node.get("artifacts") or []):
                    if not isinstance(art, dict):
                        continue
                    u = art.get("url")
                    if isinstance(u, str) and u.strip() and u not in seen:
                        seen.add(u)
                        downloads.append({
                            "url": u,
                            "media_kind": art.get("media_kind", ""),
                            "role": art.get("role", ""),
                        })
            resolved["downloads"] = downloads
        except Exception:  # noqa: BLE001 - presentation aid must never fail the read
            resolved.setdefault("downloads", [])
        return json.dumps(resolved)

    # Fall back to the raw manifest for plans that settled before the aggregate
    # existed. Same shape as before, so older plans keep working.
    try:
        from planning.plan_manifest import PlanManifest

        manifest = PlanManifest(
            bucket=_bucket,
            session_id=record.session_id,
            plan_id=pid,
            region=_region,
        )
        data = manifest.read()
    except Exception:
        logger.exception("get_plan_results: manifest read failed for %s", pid)
        return json.dumps({"error": "manifest_unavailable", "plan_id": pid})

    if not data:
        return json.dumps(
            {
                "error": "manifest_missing",
                "plan_id": pid,
                "job_status": record.status,
            }
        )

    outputs = data.get("outputs") or {}
    declared = data.get("nodes") or {}
    nodes = {}
    for node_id in {*declared.keys(), *outputs.keys()}:
        entry = outputs.get(node_id) or {}
        nodes[node_id] = {
            "status": entry.get("status", "pending"),
            "output_type": entry.get("output_type")
            or (declared.get(node_id) or {}).get("output_type", ""),
            "value": entry.get("value"),
            "completed_at": entry.get("completed_at", ""),
        }

    return json.dumps(
        _mirror_external_media_in(
            {
                "plan_id": pid,
                "display_name": data.get("display_name", ""),
                "goal": data.get("goal", ""),
                "status": data.get("status", ""),
                "job_status": record.status,
                "nodes": nodes,
            }
        )
    )
