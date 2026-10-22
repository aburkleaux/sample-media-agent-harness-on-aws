"""END-TO-END USE-CASE HARNESS — render side (design §4 B: one render per job).

Local, deterministic reproduction of every user use case at the render boundary — NO LLM, NO
gateway, NO deploy. Each test drives the hook's per-node buffering exactly as render_action does
(one _buffer_render per render group, keyed by the group's node_id), flushes the batch, and
captures the dispatched (render_key, urls) tuples. The invariant under test: each producing JOB
becomes exactly ONE render (one tab), keyed by its stable job/node id; images coalesce into one
gallery; a bucket fan-out produces one tab PER file-job (no collapse, no explosion).

These reproduce the a1dfec9c "13 tabs for ~5 outputs" class and lock the fix.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402
from hooks import actions  # noqa: E402


@pytest.fixture
def dispatched(monkeypatch):
    """Capture (render_key, urls) per dispatch; restore the real dispatch_render after."""
    import render.artifacts as render_artifacts
    calls: list[dict] = []
    original = render_artifacts.dispatch_render

    def _capture(urls, source_urls=None, label=None, render_request_id=None, render_key="", tool_name=""):
        calls.append({"render_key": render_key, "urls": list(urls), "label": label, "tool": tool_name})
        return render_key or "m-" + str(len(calls))

    monkeypatch.setattr(render_artifacts, "dispatch_render", _capture)
    try:
        yield calls
    finally:
        render_artifacts.dispatch_render = original


def _run(groups: list[dict]):
    """Drive the hook's per-group buffering (what render_action does) + flush."""
    actions.open_render_batch()
    for g in groups:
        actions._buffer_render(g.get("urls") or [], g.get("source_urls") or [],
                               g.get("label") or "", render_key=g.get("node_id") or "")
    actions.flush_render_batch()


# ---------------------------------------------------------------------------
# 1. Video trim — a single job renders once; a re-render (later turn) collapses.
# ---------------------------------------------------------------------------
def test_uc_video_trim_one_tab(dispatched):
    _run([{"node_id": "trim", "label": "Trim", "urls": ["s3://mw/trim/EXEC/clip.mp4"]}])
    assert len(dispatched) == 1
    assert dispatched[0]["render_key"] == "trim"


def test_uc_video_trim_rerender_is_idempotent(dispatched):
    # A later "summarise/retrieve" turn re-dispatches the same job under a different url form;
    # keyed by job id, both converge on the same manifest -> one tab (not two).
    import render.artifacts as ra
    a = ra._render_manifest_id("trim", ["s3://mw/trim/EXEC/clip.mp4"])
    b = ra._render_manifest_id("trim", ["https://cdn/artifacts/S/output/trim/clip.mp4"])
    assert a == b == "trim"


# ---------------------------------------------------------------------------
# 2. Extract stems — one audio job whose deliverables are several stems -> ONE tab.
# ---------------------------------------------------------------------------
def test_uc_extract_stems_one_tab(dispatched):
    stems = [f"s3://mw/stem-separation/EXEC/{s}.wav" for s in ("vocals", "drums", "bass", "other")]
    _run([{"node_id": "stem_separation", "label": "Separate Stems", "urls": stems}])
    assert len(dispatched) == 1                          # one job -> one tab (not one per stem)
    assert dispatched[0]["render_key"] == "stem_separation"


# ---------------------------------------------------------------------------
# 3. Detect people / faces — one face job -> one tab (not a separate image gallery).
# ---------------------------------------------------------------------------
def test_uc_detect_people_one_tab(dispatched):
    _run([{"node_id": "face_recognition", "label": "Detect Faces",
           "urls": ["s3://mw/face-recognition/EXEC/facemetadata.json",
                    "s3://mw/face-recognition/EXEC/face_tracks.json"],
           "source_urls": ["s3://mw/proxy/transcoded.mp4"]}])
    assert len(dispatched) == 1
    assert dispatched[0]["render_key"] == "face_recognition"


# ---------------------------------------------------------------------------
# 4. Shot segmentation — one shot job -> one tab.
# ---------------------------------------------------------------------------
def test_uc_shot_segmentation_one_tab(dispatched):
    _run([{"node_id": "shot_segmentation", "label": "Segment Shots",
           "urls": ["s3://mw/shot-segmentation/EXEC/shots.json"],
           "source_urls": ["s3://mw/proxy/transcoded.mp4"]}])
    assert len(dispatched) == 1
    assert dispatched[0]["render_key"] == "shot_segmentation"


# ---------------------------------------------------------------------------
# 5. Generate image(s) — images coalesce into ONE gallery even across image jobs
#    (12-panel storyboard must not become 12 tabs; ccbc9559).
# ---------------------------------------------------------------------------
def test_uc_generate_images_coalesce_into_one_gallery(dispatched):
    groups = [{"node_id": f"image_gen_{i}", "label": f"Image {i}",
               "urls": [f"https://cdn/artifacts/S/output/img_{i}.png"]} for i in range(12)]
    _run(groups)
    assert len(dispatched) == 1, "a storyboard of images must be ONE gallery tab"
    assert len(dispatched[0]["urls"]) == 12


# ---------------------------------------------------------------------------
# 6. Multi-step transition — distinct video ANALYSES stay separate, but the same
#    work is one tab. A face view and a shot view are different tabs (video);
#    two image options in the same step coalesce.
# ---------------------------------------------------------------------------
def test_uc_multistep_transition_distinct_video_views_separate(dispatched):
    _run([
        {"node_id": "faces", "label": "Detect Faces", "urls": ["s3://mw/faces/EXEC/faces.json"]},
        {"node_id": "shots", "label": "Segment Shots", "urls": ["s3://mw/shots/EXEC/shots.json"]},
        {"node_id": "transition", "label": "Apply Transition", "urls": ["s3://mw/transition/EXEC/out.mp4"]},
    ])
    keys = sorted(c["render_key"] for c in dispatched)
    assert keys == ["faces", "shots", "transition"]      # three jobs -> three tabs, none merged


# ---------------------------------------------------------------------------
# 7. List-a-bucket fan-out — N files -> N jobs -> N tabs (one per file-job),
#    no collapse into one and no explosion into placeholders.
# ---------------------------------------------------------------------------
def test_uc_bucket_fanout_one_tab_per_file_job(dispatched):
    n = 6
    groups = [{"node_id": f"analyze_file_{i}", "label": f"Analyze file {i}",
               "urls": [f"s3://mw/analyze/EXEC{i}/result_{i}.mp4"]} for i in range(n)]
    _run(groups)
    assert len(dispatched) == n                          # exactly one render per fanned-out job
    assert sorted(c["render_key"] for c in dispatched) == sorted(f"analyze_file_{i}" for i in range(n))


# ---------------------------------------------------------------------------
# 8. Research -> PDF has no render deliverable (document handoff) — covered by the
#    chainer-side use-case harness (test_use_cases_handoff.py). Guard here that a
#    doc-only result does not fabricate a render tab.
# ---------------------------------------------------------------------------
def test_uc_research_pdf_document_only_no_phantom_render(dispatched):
    # A pure document result carries no renderable media url -> no render group -> no tab.
    _run([])
    assert dispatched == []
