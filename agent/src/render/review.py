"""Data-aware review of a render manifest — the correctness guarantee (F8).

The render decision is agent-driven and deliberately NOT reproducible: two runs may differ so
long as both put the RIGHT ELEMENTS on screen for the data. That means determinism is not the
safety net -- this reviewer is. It validates a finished manifest against the SOURCE DATA it was
built from, catching the two failures that actually shipped:

  * an empty-but-schema-valid manifest (8e38c66: a video-annotations manifest with every list
    empty -- a blank tab). A schema check passes it; a data-aware check does not.
  * source data that carries boxes (face_tracks.json tracks[].faces[].box) rendered WITHOUT box
    annotations, because the box-capable path was not taken.

Pure and dependency-free so it is testable without AWS and callable from any render path. It
REPORTS problems; acting on them (re-dispatch with the critique, or fall back) is the caller's
choice -- validate-and-flag first, agentic-fix later.
"""
from __future__ import annotations

from typing import Any

# Manifest keys that put something on screen. A manifest with none of these renders blank,
# regardless of whether it is schema-valid.
_RENDERABLE_KEYS = (
    "tracks", "markers", "segments", "annotations", "tables", "rows", "images",
    "thumbnails", "peaks", "clips", "artifacts", "sources",
)

# Box-like coordinate key sets the face/vision tools use.
_BOX_KEYSETS = (
    frozenset({"l", "t", "w", "h"}),
    frozenset({"x", "y", "w", "h"}),
    frozenset({"x", "y", "width", "height"}),
    frozenset({"Left", "Top", "Width", "Height"}),
)


def _is_box(d: Any) -> bool:
    if not isinstance(d, dict):
        return False
    keys = set(d.keys())
    return any(ks <= keys for ks in _BOX_KEYSETS)


def _count_boxes(node: Any, depth: int = 0) -> int:
    """Boxes anywhere in a sidecar payload (e.g. tracks[].faces[].box)."""
    if depth > 8:
        return 0
    n = 0
    if isinstance(node, dict):
        for k, v in node.items():
            if str(k).lower() in ("box", "bbox", "bounding_box", "region") and _is_box(v):
                n += 1
            else:
                n += _count_boxes(v, depth + 1)
    elif isinstance(node, list):
        for v in node:
            n += _count_boxes(v, depth + 1)
    return n


def _renderable_elements(manifest: dict) -> list[str]:
    """Which renderable keys the manifest actually populated."""
    out = []
    for k in _RENDERABLE_KEYS:
        v = manifest.get(k)
        if k == "tracks":
            # A tracks list only counts if at least one track has a real src -- an empty-src
            # track (e.g. {"type":"video"} with no url) draws nothing. Handled by the caller's
            # track_has_src check, so it is NOT counted here.
            continue
        if isinstance(v, list) and v:
            out.append(k)
        elif isinstance(v, dict) and v:
            out.append(k)
        elif isinstance(v, str) and v.strip() and k in ("sources",):
            out.append(k)
    return out


def _norm_url(u: Any) -> str:
    """Normalize a media url to its full OBJECT KEY for comparison.

    The same object is referenced as `s3://bucket/key` and as a presigned
    `https://bucket.s3.../key?X-Amz-...`; both must compare equal, so drop the scheme, host/
    bucket, and the presign query and keep the whole key. Keeping the WHOLE key (not just the
    last two segments) matters: job outputs live under an exec-id prefix
    (`micro-workflow/media-processing/<exec>/proxy/transcoded.mp4`), so the exec-id is what tells
    one job's transcoded.mp4 from another's. Truncating to the last two segments discarded that
    and manufactured a cross-job collision the data does not have.
    """
    if not isinstance(u, str) or not u.strip():
        return ""
    base = u.split("?", 1)[0].strip()
    if base.startswith("s3://"):
        rest = base[len("s3://"):]
        # bucket/key -> key
        key = rest.split("/", 1)[1] if "/" in rest else rest
    elif "://" in base:
        after = base.split("://", 1)[1]  # host/key
        key = after.split("/", 1)[1] if "/" in after else after
    else:
        key = base
    return key.strip("/").lower()


# A uuid token (M2C step-function execution ids look like this) anywhere in a key.
_EXEC_UUID_RE = __import__("re").compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", __import__("re").I)


def _media_identity(u: Any) -> str:
    """A stable identity for the SAME media object surfaced under DIFFERENT key forms.

    The M2C proxy uri (`.../media-processing/<exec-uuid>/proxy/transcoded.mp4`) and the imported
    copy (`artifacts/<...exec-uuid...>/output/.../transcoded.mp4`) are the same file but normalise
    to different object keys, so `_norm_url` dedup missed the second render (the duplicate "Video"
    tab). Both forms carry the SAME execution uuid + basename, so identity = `<exec-uuid>/<basename>`
    collapses them. Returns "" when no uuid is present, so it NEVER collapses unrelated files
    (guarding the cross-job `transcoded.mp4` collision `_norm_url`'s comment warns about)."""
    if not isinstance(u, str) or not u.strip():
        return ""
    base = u.split("?", 1)[0]
    name = base.rstrip("/").rsplit("/", 1)[-1].lower()
    ids = _EXEC_UUID_RE.findall(base)
    if not ids or not name:
        return ""
    return f"{ids[-1].lower()}/{name}"


def validate_resolved_urls(chosen: Any, candidates: Any, *,
                           source_urls: Any = None, rendered_urls: Any = None) -> dict:
    """Validate the URLs an agent chose to render against the job's ACTUAL outputs.

    The render agent resolves deliverables by judgment (Sonnet 5), which is flexible but can
    pick a url that is not really a job output (hallucination), pick the INPUT, or re-pick
    something already rendered. This checks its choice against ground truth and returns which
    urls are sound and which to drop, with a reason -- the resolve-step guard, mirroring the
    manifest reviewer.

    Args:
        chosen: urls the agent selected to render.
        candidates: the job's REAL output urls (ground truth: artifactUrls / enumerated prefix).
        source_urls: the job's input/source urls, which must never be rendered as output.
        rendered_urls: urls already rendered this session (dedup).

    Returns {"valid": [...], "rejected": [{"url", "reason"}]}.
    """
    def _fname(u: Any) -> str:
        return _norm_url(u).rsplit("/", 1)[-1]

    chosen = [c for c in (chosen or []) if isinstance(c, str) and c.strip()]
    cand = {_norm_url(c) for c in (candidates or []) if isinstance(c, str)}
    # Input exclusion is safety-critical, so match it MORE leniently -- on the bare filename as
    # well as the path. An input referenced with a different host/prefix (a CloudFront mirror vs
    # the s3 key) must still be caught; a distinctive upload name makes filename match safe here.
    src_paths = {_norm_url(s) for s in (source_urls or []) if isinstance(s, str)}
    src_names = {_fname(s) for s in (source_urls or []) if isinstance(s, str)}
    seen = {_norm_url(r) for r in (rendered_urls or []) if isinstance(r, str)}

    valid: list = []
    rejected: list = []
    for url in chosen:
        n = _norm_url(url)
        if (src_paths and n in src_paths) or (src_names and _fname(url) in src_names):
            rejected.append({"url": url, "reason": "this is an input/source, not an output"})
        elif seen and n in seen:
            rejected.append({"url": url, "reason": "already rendered this session"})
        elif cand and n not in cand:
            rejected.append({"url": url,
                             "reason": "not among the job's actual outputs (possibly hallucinated)"})
        else:
            valid.append(url)
    return {"valid": valid, "rejected": rejected}


def review_manifest(manifest: Any, sidecars: Any = None) -> list[str]:
    """Return a list of problems with this manifest given its source data. Empty == OK.

    ``sidecars`` maps a source name/url to its parsed JSON (the files the manifest was built
    from). When omitted, only manifest-internal checks run.
    """
    problems: list[str] = []
    if not isinstance(manifest, dict):
        return ["manifest is not an object"]

    # An error manifest is a legitimate terminal state, not a review failure.
    if manifest.get("template") in ("error", "render-error") or manifest.get("error"):
        return problems

    elements = _renderable_elements(manifest)

    # 1. Renders SOMETHING. The empty-but-valid manifest is the bug this exists to stop.
    tracks = manifest.get("tracks") or []
    track_has_src = any(isinstance(t, dict) and (t.get("src") or "").strip() for t in tracks)
    if not elements and not track_has_src:
        problems.append("manifest renders nothing: no populated tracks/markers/segments/"
                        "annotations/tables/thumbnails/peaks")

    # 2. Boxes in the data must reach the manifest. This is the face-boxes gap: face_tracks.json
    #    carries tracks[].faces[].box, and a manifest without annotations silently drops them.
    if isinstance(sidecars, dict):
        data_boxes = sum(_count_boxes(v) for v in sidecars.values())
        if data_boxes > 0 and not (manifest.get("annotations")):
            problems.append(
                f"source data carries {data_boxes} bounding box(es) but the manifest has no "
                "annotations -- boxes were dropped (wrong skill/template chosen for the data)")

    # 3. Annotation coords must be normalized 0..1, or the overlay draws off-canvas.
    for a in (manifest.get("annotations") or []):
        box = a.get("box") if isinstance(a, dict) else None
        if isinstance(box, dict):
            vals = [box.get(k) for k in ("x", "y", "width", "height", "w", "h", "l", "t")
                    if box.get(k) is not None]
            if any(isinstance(v, (int, float)) and (v < 0 or v > 1.5) for v in vals):
                problems.append("annotation box coords are not normalized 0..1 "
                                f"(got {vals}); overlay will draw off-canvas")
                break

    return problems
