"""LOCAL END-TO-END VALIDATION of the "one durable copy per source" fix.

Replays the REAL payloads from prod session c80b7144 — where a trimmed scene's face
detection and speaker diarization each opened their own tab instead of folding onto the
scene's timeline — through:

  1. the two resolvers (polling worker + plan-chainer) that both import the deliverable,
  2. the render agent's OWN identity function (`_canonical_source_identity`), and
  3. the server's grouping predicate (mirrored here) that decides which manifests composite.

The point is to prove the whole chain, not just the changed line: before the fix the two
resolvers produced two S3 keys, which produced two identities, which is why the analyses
never grouped with the clip. This asserts they now produce ONE key, ONE identity, and that
the clip + its analyses land in ONE composite group.

Run:  python3 infra/lib/lambdas/_tests/validate_source_identity_e2e.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "_layers" / "result-resolution" / "python"))
import result_resolution as rr  # noqa: E402

# ---------------------------------------------------------------------------------------
# Real values from prod session c80b7144-ad08-464d-ab29-664eecd26c3c
# ---------------------------------------------------------------------------------------
SESSION = "c80b7144-ad08-464d-ab29-664eecd26c3c"
SCENE1_EXEC = "8dbfe4fa-8598-5d47-bcc4-d6c336a4d947"   # media-processing execution (scene 1)
SCENE2_EXEC = "4ac89bbf-2863-55c4-9979-5632d462e0ea"   # media-processing execution (scene 2)
PLAN_RUN = "6d93ebc8-317f-4cd8-8f1e-719fdcfbdfca"      # plan-clip-trim-meridian run
PROXY = "m2cv6-0e0000000003-100000000003-us-west-2-proxy"

SCENE1_SRC = (f"micro-workflow/media-processing/{SCENE1_EXEC}/scene1_trim/"
              "scene1_violence_harm_trim.mp4")
SCENE2_SRC = (f"micro-workflow/media-processing/{SCENE2_EXEC}/scene2_trim/"
              "scene2_firearm_trim.mp4")

# ---------------------------------------------------------------------------------------
# The render agent's identity rule, verbatim in behaviour (render_agent.py).
# Copied rather than imported: the agent module pulls in strands/bedrock at import.
# ---------------------------------------------------------------------------------------
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def canonical_source_identity(path: str) -> str:
    path = path.split("?", 1)[0]
    base = path.rsplit("/", 1)[-1]
    uuids = _UUID_RE.findall(path)
    return f"{uuids[-1].lower()}/{base}" if (uuids and base) else path


def timeline_group_key(source_key: str) -> str:
    """The server groups timeline manifests by sourceKey (renderSession.ts)."""
    return source_key


# ---------------------------------------------------------------------------------------
def resolve(plan_id: str, node_id: str, src_key: str, import_prefix: str) -> str:
    """Run one resolver over one artifact and return the FINAL durable S3 key."""
    captured: list[str] = []
    records = [{"node_id": node_id, "artifacts": [
        {"role": "output", "url": f"s3://{PROXY}/{src_key}",
         "name": src_key.rsplit("/", 1)[-1]},
    ]}]
    rr._auto_import_deliverables(
        plan_id, records,
        lambda sb, sk, rp: (captured.append(rp), f"https://cdn/{import_prefix}{rp}")[1])
    return f"{import_prefix}{captured[0]}"


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if detail:
        print(f"           {detail}")
    return ok


def main() -> int:
    ok = True
    out_prefix = f"artifacts/{SESSION}/output/"
    print("\n1. The two resolvers that both import a plan node's deliverable")

    # Polling worker: lone job settle — plan_id == node_id (the execution ARN), and its
    # import_prefix already scopes by the execution.
    arn = (f"arn:aws:states:us-west-2:100000000003:execution:"
           f"m2cv6-0e0000000003-media-processing:{SCENE1_EXEC}")
    worker_key = resolve(arn, arn, SCENE1_SRC, f"{out_prefix}{SCENE1_EXEC}/")

    # Plan-chainer: plan settle — plan run + node id, bare session output prefix.
    plan_key = resolve(PLAN_RUN, "clip1", SCENE1_SRC, out_prefix)

    print(f"     polling worker -> {worker_key}")
    print(f"     plan-chainer   -> {plan_key}")
    ok &= check("both resolvers write ONE durable key", worker_key == plan_key)
    ok &= check("the plan-run UUID is not in the key (it split the identity before)",
                PLAN_RUN not in plan_key)

    print("\n2. Render identity derived from those keys (render_agent rule)")
    wid, pid = canonical_source_identity(worker_key), canonical_source_identity(plan_key)
    print(f"     worker copy identity -> {wid}")
    print(f"     plan copy identity   -> {pid}")
    ok &= check("one video has ONE identity", wid == pid)
    ok &= check("identity is keyed on the producing execution",
                wid == f"{SCENE1_EXEC}/scene1_violence_harm_trim.mp4")

    print("\n3. The ephemeral source the ANALYSES ran on agrees with the durable copy")
    # face-recognition + speech-diarization both consumed the proxy path (job rows'
    # sourceMedia), so their manifests' sourceKey derives from it.
    analysis_identity = canonical_source_identity(SCENE1_SRC)
    print(f"     analysis source identity -> {analysis_identity}")
    ok &= check("analysis identity == clip identity (so lanes fold onto the clip)",
                analysis_identity == pid,
                f"{analysis_identity} vs {pid}")

    print("\n4. Grouping: clip video + its two analyses land in ONE composite")
    group = {}
    for label, key in (
        ("clip1 video", pid),
        ("speaker diarization", analysis_identity),
        ("face detection", analysis_identity),
    ):
        group.setdefault(timeline_group_key(key), []).append(label)
    for k, members in group.items():
        print(f"     {k} -> {members}")
    ok &= check("exactly ONE timeline group", len(group) == 1)
    ok &= check("all three members composite together (>=2 triggers composeTimeline)",
                len(next(iter(group.values()))) == 3)

    print("\n5. The two scenes stay SEPARATE (ranges must never mix)")
    s1 = canonical_source_identity(resolve(PLAN_RUN, "clip1", SCENE1_SRC, out_prefix))
    s2 = canonical_source_identity(resolve(PLAN_RUN, "clip2", SCENE2_SRC, out_prefix))
    print(f"     scene1 -> {s1}")
    print(f"     scene2 -> {s2}")
    ok &= check("scene 1 and scene 2 have distinct identities", s1 != s2)

    print("\n6. Regression guard: the OLD layout would have split the identity")
    old_plan_key = f"{out_prefix}{PLAN_RUN}/clip1/scene1_violence_harm_trim.mp4"
    old_identity = canonical_source_identity(old_plan_key)
    print(f"     old plan-scoped identity -> {old_identity}")
    ok &= check("the old key really did produce a DIFFERENT identity (the bug)",
                old_identity != analysis_identity,
                f"{old_identity} != {analysis_identity}")

    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
