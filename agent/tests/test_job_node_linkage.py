"""An SFN-backed plan node's job row must carry its node_id.

LAYER 3 — identity linkage. This is the production blocker behind the empty `sourceUrl`
in sessions a2ebd1e9 and a5a1b4fa: one logical job has TWO registry rows and they are not
linked.

  `<PLAN>#sg_shots`   nodeId=sg_shots   sourceMedia=ABSENT    (written by the chainer)
  `arn:...execution:...`  nodeId=ABSENT  sourceMedia=PRESENT  (written by task_agent)

The render keys off the node id, looks up source media on the `<PLAN>#node` row, finds
none, and renders an analysis with no video to overlay — the black/data-table views.

Cause is a plain inconsistency between two adjacent registration sites in task_agent:
the QUEUE path passes `node_id=node_id`; the SFN path does not, even though `node_id` is
in scope there (it is already used for `trio_id=f"plan-node|{node_id}"` and the log line).
`JobRecord.node_id` has existed all along.

Why linkage and not a run-level fallback: a fanout plan runs the SAME tool over DIFFERENT
media (session a5a1b4fa: three shot-segmentation jobs on three videos), so "borrow any
source from this run" would attach the wrong video. Only a per-node link is correct, and
matching on the ARN string cannot disambiguate three executions of one tool.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def test_jobrecord_carries_node_id_and_run_id_together():
    """The two fields that make a row joinable to its plan node."""
    from jobs.models import JobRecord
    rec = JobRecord(
        job_id="arn:aws:states:us-west-2:1:execution:sm:exec-1",
        actor_id="a", session_id="s", trio_id="plan-node|sg_shots",
        gateway_id="__aws_sfn__", status_tool="", results_tool="",
        status="PENDING", start_at="t", run_id="plan-1", node_id="sg_shots",
        source_media=["s3://b/in/video.mp4"],
    )
    assert rec.node_id == "sg_shots"
    assert rec.run_id == "plan-1"
    assert rec.source_media == ["s3://b/in/video.mp4"]


def test_registry_round_trips_node_id():
    """`nodeId` must survive into the DDB item, or the join has nothing to read."""
    from jobs.models import JobRecord
    from jobs.registry import JobRegistry
    rec = JobRecord(
        job_id="arn:x", actor_id="a", session_id="s", trio_id="plan-node|n1",
        gateway_id="__aws_sfn__", status_tool="", results_tool="",
        status="PENDING", start_at="t", run_id="p1", node_id="n1",
        source_media=["s3://b/v.mp4"],
    )
    item = JobRegistry._to_item(rec) if hasattr(JobRegistry, "_to_item") else None
    if item is None:
        import pytest
        pytest.skip("registry has no _to_item helper to assert against")
    assert item.get("nodeId") == "n1", item
    assert item.get("sourceMedia") == ["s3://b/v.mp4"], item


def test_sfn_registration_site_passes_node_id():
    """The actual defect, asserted against the source.

    A behavioural test would need a live SFN dispatch, so this pins the call site: the SFN
    `JobRecord(...)` must set `node_id`, exactly as the queue site one screen below does.
    Both sites register a plan node's job; only one of them was joinable.
    """
    import ast

    tree = ast.parse((ROOT / "src" / "task_agent.py").read_text())
    sfn_calls = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "JobRecord"):
            continue
        kwargs = {kw.arg for kw in node.keywords if kw.arg}
        # The SFN registration is the one routed to the SFN sentinel gateway.
        is_sfn = any(
            kw.arg == "gateway_id" and isinstance(kw.value, ast.Constant)
            and kw.value.value == "__aws_sfn__"
            for kw in node.keywords
        )
        if is_sfn:
            sfn_calls.append(kwargs)

    assert sfn_calls, "expected an SFN JobRecord registration in task_agent"
    for kwargs in sfn_calls:
        assert "node_id" in kwargs, (
            "the SFN JobRecord registration must pass node_id so the row can be joined to "
            f"its plan node (the queue path already does). Got: {sorted(kwargs)}")
        # source_media is the payload the join exists to deliver.
        assert "source_media" in kwargs, sorted(kwargs)
        assert "run_id" in kwargs, sorted(kwargs)


# --- the render-side join ---------------------------------------------------

def test_plan_node_source_lookup_tries_the_plan_scoped_key_first():
    """`<plan>#<node>` is the row a plan node is filed under; try it before the bare id."""
    from hooks import provider
    seen = []

    def _fake(job_id):
        seen.append(job_id)
        return ["s3://b/in/right.mp4"] if job_id == "plan-1#sg_shots" else []

    provider._job_source_media_lookup = _fake
    try:
        got = provider._recorded_source_media_for_node("plan-1", "sg_shots")
    finally:
        provider._job_source_media_lookup = None

    assert got == ["s3://b/in/right.mp4"]
    assert seen[0] == "plan-1#sg_shots", f"plan-scoped key must be tried first: {seen}"


def test_standalone_job_still_resolves_by_bare_id():
    """Single dispatch: the 'node id' IS the execution arn, so the bare key must still work."""
    from hooks import provider
    arn = "arn:aws:states:us-west-2:1:execution:sm:exec-9"

    provider._job_source_media_lookup = lambda jid: ["s3://b/in/solo.mp4"] if jid == arn else []
    try:
        assert provider._recorded_source_media_for_node(None, arn) == ["s3://b/in/solo.mp4"]
    finally:
        provider._job_source_media_lookup = None


def test_no_source_anywhere_returns_empty_not_an_error():
    from hooks import provider
    provider._job_source_media_lookup = lambda jid: []
    try:
        assert provider._recorded_source_media_for_node("p", "n") == []
        assert provider._recorded_source_media_for_node(None, None) == []
    finally:
        provider._job_source_media_lookup = None


def test_fanout_nodes_get_their_OWN_media_not_a_shared_guess():
    """The reason this is a per-node join and not a run-level fallback.

    session a5a1b4fa ran three shot-segmentations over three DIFFERENT videos. Each node must
    resolve its own source; borrowing any source from the run would attach the wrong video.
    """
    from hooks import provider
    per_node = {
        "plan-9#seg_a": ["s3://b/in/a.mp4"],
        "plan-9#seg_b": ["s3://b/in/b.mp4"],
        "plan-9#seg_c": ["s3://b/in/c.mp4"],
    }
    provider._job_source_media_lookup = lambda jid: per_node.get(jid, [])
    try:
        for node, expected in [("seg_a", "a.mp4"), ("seg_b", "b.mp4"), ("seg_c", "c.mp4")]:
            got = provider._recorded_source_media_for_node("plan-9", node)
            assert got and got[0].endswith(expected), f"{node} got {got}"
    finally:
        provider._job_source_media_lookup = None


def test_the_mirror_write_is_non_destructive_and_guarded():
    """The plan-node mirror must not clobber an existing value or fail a dispatch."""
    src = (ROOT / "src" / "task_agent.py").read_text()
    i = src.find("could not mirror sourceMedia")
    assert i != -1, "expected the plan-node sourceMedia mirror to exist"
    block = src[max(0, i - 1600):i]
    assert "if_not_exists(sourceMedia" in block, "mirror must not overwrite a recorded source"
    assert 'Key={"jobId": f"{plan_id}#{node_id}"}' in block, "mirror must target the plan-node row"
    assert "if _srcs and plan_id and node_id" in block, "mirror must be guarded on all three facts"


# ---------------------------------------------------------------------------
# Render-time recovery: when the dispatch-time mirror left the node row empty
# (best-effort, and it demonstrably did on session feb23706 despite being
# deployed), recover the run's SINGLE shared source. Generic + guarded: every
# analysis in one run is about the same upload, but a multi-source run is never
# borrowed from (that would undo the clip-identity rule / a5a1b4fa).
# ---------------------------------------------------------------------------

def test_empty_node_row_recovers_the_runs_single_shared_source():
    """feb23706: node row has no sourceMedia, its sibling gateway jobs all recorded
    the one uploaded video -> recover it so the analysis folds onto the video timeline."""
    from hooks import provider
    VIDEO = "s3://b/artifacts/sess/input/nba.mp4"
    _orig_row = provider._job_row_lookup
    provider._job_source_media_lookup = lambda jid: []          # node-key lookups miss
    provider._job_row_lookup = lambda jid: {"actorId": "act-1", "runId": "run-1"}
    provider._run_source_media_lookup = lambda a, r: [VIDEO] if (a, r) == ("act-1", "run-1") else []
    try:
        assert provider._recorded_source_media_for_node("run-1", "sg2_diarize") == [VIDEO]
    finally:
        provider._job_source_media_lookup = None
        provider._run_source_media_lookup = None
        provider._job_row_lookup = _orig_row


def test_multi_source_run_is_NOT_borrowed_from():
    """The guard: a run with >1 distinct source (clips / several uploads, e.g. a5a1b4fa)
    cannot be disambiguated cheaply, so decline rather than fold onto the wrong timeline."""
    from hooks import provider
    _orig_row = provider._job_row_lookup
    provider._job_source_media_lookup = lambda jid: []
    provider._job_row_lookup = lambda jid: {"actorId": "act-1", "runId": "run-2"}
    provider._run_source_media_lookup = lambda a, r: ["s3://b/a.mp4", "s3://b/b.mp4", "s3://b/c.mp4"]
    try:
        assert provider._recorded_source_media_for_node("run-2", "seg_a") == []
        # and the pure predicate declines too
        assert provider._run_shared_source_media("act-1", "run-2") == []
    finally:
        provider._job_source_media_lookup = None
        provider._run_source_media_lookup = None
        provider._job_row_lookup = _orig_row


def test_node_key_hit_never_reaches_the_run_fallback():
    """A node that recorded its own source is authoritative — the run fallback must not fire."""
    from hooks import provider
    called = {"run": 0}

    def _run(a, r):
        called["run"] += 1
        return ["s3://b/should-not-be-used.mp4"]

    provider._job_source_media_lookup = lambda jid: ["s3://b/own.mp4"] if jid == "run-3#n" else []
    provider._run_source_media_lookup = _run
    try:
        assert provider._recorded_source_media_for_node("run-3", "n") == ["s3://b/own.mp4"]
        assert called["run"] == 0, "run fallback must not run when the node has its own source"
    finally:
        provider._job_source_media_lookup = None
        provider._run_source_media_lookup = None


def test_run_shared_needs_both_actor_and_run():
    from hooks import provider
    provider._run_source_media_lookup = lambda a, r: ["s3://b/x.mp4"]
    try:
        assert provider._run_shared_source_media(None, "run") == []
        assert provider._run_shared_source_media("act", None) == []
    finally:
        provider._run_source_media_lookup = None


def test_node_row_lookup_failure_degrades_to_empty_not_raises():
    """The run-fallback's row read is a live boto3 call. Any failure (no creds in CI/unit
    tests, missing table, throttling) MUST degrade to [] — a source-recovery miss can never
    raise into a render or a test. Regression pin for the NoCredentialsError CI failure the
    unguarded lookup caused."""
    from hooks import provider
    _orig_row = provider._job_row_lookup

    def _boom(_jid):
        raise RuntimeError("Unable to locate credentials")

    provider._job_source_media_lookup = lambda jid: []  # node-keys miss -> fallback fires
    provider._job_row_lookup = _boom
    try:
        assert provider._recorded_source_media_for_node("p", "n") == []
    finally:
        provider._job_source_media_lookup = None
        provider._job_row_lookup = _orig_row
