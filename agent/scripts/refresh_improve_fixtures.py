#!/usr/bin/env python3
"""Regenerate tests/fixtures/plan_state_golden.json from the offline harvest.

REQ-017 requires the offline improve tests to run on REAL harvested data, but the
harvest lives outside the repo (OURS-2), so a minimal real slice is committed instead.
Run this when the golden set changes.

  python3 scripts/refresh_improve_fixtures.py [--harvest DIR]

Deliberately excludes node prompts, goal text, S3 URIs, tool arguments and tool
results — arguments/results are sensitive (OURS-4) and none is needed by the tests.
"""
import argparse, datetime as _dt, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))

# An error message is neither a tool argument nor a tool result, so OURS-4 does not exclude
# it, and the objectives for the non-conversational classes quote it: without the message a
# control-plane failure reads "S3.HeadObject: no message" and names no defect. But the
# fixture is COMMITTED, so a message that embeds an account id, a bucket or an S3 key would
# ship one. All 8 distinct messages in the current harvest are clean AWS error strings; this
# guard is for the next harvest, and it drops rather than redacts so nothing is half-scrubbed.
_SENSITIVE = re.compile(r"\b\d{12}\b|s3://|arn:aws|mediaworks-harness-[\w.-]+")

# Sorts before every real timestamp, for spans whose `ts` will not parse.
_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)


def _safe_msg(raw):
    text = (raw or "").strip()
    if not text:
        return None
    return "(message withheld: matched a sensitive pattern)" if _SENSITIVE.search(text) else text
OUT = os.path.join(HERE, "..", "tests", "fixtures", "plan_state_golden.json")
GOLDEN = os.path.join(HERE, "..", "..", "tests", "evaluation", "datasets", "golden", "golden.jsonl")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--harvest", default=os.path.join(
        HERE, "..", "..", "..", "ideas", "staging-data"))
    a = ap.parse_args()

    gold = [json.loads(l)["scenario_id"] for l in open(GOLDEN) if l.strip()]
    p = json.load(open(os.path.join(a.harvest, "fresh", "plan-state-fresh.json")))
    rows = p["Items"] if isinstance(p, dict) and "Items" in p else p
    # Read the spans through the SAME merged source production reads, not one hardcoded
    # file. `discover_sessions` once read a single file while `HarvestedSpanSource` merged
    # several, and the disagreement made discovery blind to exactly the six golden sessions.
    # A fixture generator standing on a different view of the harvest is the same bug.
    sys.path.insert(0, os.path.join(HERE, "..", "src"))
    from improve.contract import parse_utc  # noqa: E402
    from improve.observe.span_trajectory import HarvestedSpanSource  # noqa: E402
    spans = HarvestedSpanSource()._load()

    def in_order(raw_spans):
        """Sort on the PARSED instant, the way `trajectory_for_session` does.

        Sorting on the raw ISO string is wrong across the two formats this system emits
        (`'...19.5Z' > '...19.5+00:00'` because `'Z' > '+'`), and step ORDER is what the
        async-lifecycle facets read. A fixture ordered differently from production is a fake
        looser than the real thing.
        """
        return sorted(raw_spans, key=lambda x: (parse_utc(x.get("ts")) is None,
                                                parse_utc(x.get("ts")) or _EPOCH))

    out = {}
    for sid in gold:
        mine = [r for r in rows if r.get("session_id", {}).get("S") == sid]
        if mine:
            edges = {}
            for r in mine:
                if r.get("node_id", {}).get("S") == "PLAN" and r.get("nodes_json", {}).get("S"):
                    edges = {n["id"]: list(n.get("depends_on") or [])
                             for n in json.loads(r["nodes_json"]["S"]) if n.get("id")}
            out[sid] = {"nodes": sorted(({
                "node_id": r.get("node_id", {}).get("S", ""),
                "status": r.get("status", {}).get("S", ""),
                "updated_at": r.get("updated_at", {}).get("S", ""),
                "gateway_status": r.get("gateway_status", {}).get("S", ""),
                "depends_on": edges.get(r.get("node_id", {}).get("S", ""), []),
            } for r in mine), key=lambda n: n["node_id"])}
        if sid in spans:
            sp = in_order(spans[sid].get("spans", []))
            tool_spans = [x for x in sp
                          if "execute_tool" in (x.get("name") or "")
                          or "tools/call" in (x.get("name") or "")]
            out.setdefault(sid, {})["tool_span_names"] = [x.get("name", "") for x in tool_spans]
            # `status` is required, not decorative: `_retry_present` looks for an ERROR span,
            # so a fixture that defaults every status to UNSET can never exercise that branch
            # and silently inverts `no_retry_after_failed_node` between tests and production.
            # It is neither an argument nor a result, so OURS-4 does not exclude it.
            out[sid]["tool_spans"] = [
                {"name": x.get("name", ""), "status": x.get("status", "UNSET")}
                for x in tool_spans]
            # EVERY span, which is what production's trajectory holds. The tool-only slice
            # above was a faithfulness gap the old fixture docstring already flagged: it meant
            # no test could exercise anything reading a non-tool span, and `improve.classify`
            # reads exactly those (`chat`, `execute_event_loop_cycle`, `mcp tools/list`,
            # `POST /invocations`). A fixture that cannot represent the shape production sees
            # is how the workload class silently came out `unclassified` for all six golden
            # sessions. `duration_ms` rides along because the discovery objective reports it.
            out[sid]["spans"] = [
                {"name": x.get("name", ""), "status": x.get("status", "UNSET"),
                 "duration_ms": x.get("duration_ms"), "msg": _safe_msg(x.get("msg"))}
                for x in sp]

    # One real exemplar per non-conversational workload class, so the offline tests can
    # exercise those objectives on harvested data instead of on a hand-built Run (REQ-017).
    # Selected BY SHAPE and not by a hardcoded id: an id list would rot on the next harvest,
    # and the shape is the thing the classifier actually reads. Sorted so the choice is
    # deterministic across runs.
    MODEL = ("chat", "execute_event_loop_cycle", "invoke_agent")
    exemplars = {}
    for sid in sorted(spans):
        sp = in_order(spans[sid].get("spans", []))
        names = [x.get("name") or "" for x in sp]
        if not names or any(n.startswith(MODEL) for n in names):
            continue
        errored = any(x.get("status") == "ERROR" for x in sp)
        if sid.startswith("plan-session-"):
            key = "plan_node"
        elif sid.startswith("lab-") or errored:
            key = "control_plane_error" if errored else "control_plane"
        elif "mcp tools/list" in names:
            key = "tool_discovery"
        else:
            continue
        if key in exemplars:
            continue
        exemplars[key] = {
            "session_id": sid,
            "spans": [{"name": x.get("name", ""), "status": x.get("status", "UNSET"),
                       "duration_ms": x.get("duration_ms"),
                       "msg": _safe_msg(x.get("msg"))} for x in sp],
        }
    # A plan sub-session DOES invoke a model, so it is skipped by the loop above; take it
    # separately rather than relaxing the no-model filter, which the other classes rely on.
    for sid in sorted(spans):
        if "plan_node" in exemplars:
            break
        if not sid.startswith("plan-session-"):
            continue
        sp = in_order(spans[sid].get("spans", []))
        exemplars["plan_node"] = {
            "session_id": sid,
            "spans": [{"name": x.get("name", ""), "status": x.get("status", "UNSET"),
                       "duration_ms": x.get("duration_ms"),
                       "msg": _safe_msg(x.get("msg"))} for x in sp],
        }

    meta = {"_provenance": {
        "source": "ideas/staging-data plan-state + the MERGED span harvest via improve.observe.span_trajectory.HarvestedSpanSource (staging 000000000000, read-only)",
        "sessions": "the golden-set sessions listed in tests/evaluation/datasets/golden/golden.jsonl",
        "fields_included": "node_id/status/updated_at/gateway_status/depends_on; ALL ordered spans with name+status+duration_ms+msg (msg dropped if it matches an account id / bucket / S3 URI), plus the tool-only slice",
        "deliberately_excluded": "node prompts, goal text, S3 URIs, tool arguments, tool results (args+results are sensitive — OURS-4)",
        "why_committed": "REQ-017 needs real fixtures; the harvest is outside the repo (OURS-2)",
        "regenerate": "python3 scripts/refresh_improve_fixtures.py",
        "exemplars": "one real session per non-conversational workload class, chosen by span "
                     "shape (not by id, which would rot); span name+status+duration_ms only",
    }}
    with open(OUT, "w") as f:
        json.dump({**meta, "sessions": out, "exemplars": exemplars}, f, indent=1,
                  sort_keys=True)
    print(f"wrote {os.path.relpath(OUT)}: {len(out)} sessions, "
          f"{len(exemplars)} class exemplars ({', '.join(sorted(exemplars))})")


if __name__ == "__main__":
    main()
