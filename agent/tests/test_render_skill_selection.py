"""Pin which render skill each analysis payload selects.

Selection is deterministic code, not an LLM decision: select_render_skill_by_content
scores every skill whose ``detect:`` paths resolve in the payload and takes the max of
(number of projections declared, number of detect paths matched). So it is fully
testable locally -- and it was never tested, which is how four analysis tabs in one
session all rendered as render-face-people.

The payloads are the same real sidecar shapes emit_render_fixtures.py uses, so a
case here cannot represent a request production never makes.
"""
from __future__ import annotations

import importlib.util
import pytest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _shapes():
    """Real sidecar payloads, borrowed from the fixture generator."""
    spec = importlib.util.spec_from_file_location(
        "_efx", ROOT / "scripts" / "emit_render_fixtures.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_efx"] = mod
    try:
        spec.loader.exec_module(mod)
    except SystemExit:  # the script calls main() under __main__ only, but be safe
        pass
    return mod.SHOTS, mod.SPEAKERS, mod.FACEMETA


SHOTS, SPEAKERS, FACEMETA = _shapes()


def _select(sidecars):
    from render_agent import select_render_skill_by_content
    skill = select_render_skill_by_content(sidecars)
    return skill["name"] if skill else None


def test_shot_payload_selects_the_shot_skill():
    assert _select({"shots.json": SHOTS}) == "render-shot-segmentation"


def test_diarization_payload_selects_the_diarization_skill():
    assert _select({"speaker_manifest.json": SPEAKERS}) == "render-speaker-diarization"


def test_face_payload_selects_a_face_skill():
    assert _select({"facemetadata.json": FACEMETA}).startswith("render-face")


def test_each_analysis_is_isolated_from_the_others():
    """The regression that produced four identical tabs.

    Ranking is (projections, detect matches) and render-face-people declares the most
    projections of any skill, so it wins ANY dict in which `people` resolves --
    including a dict that also carries shots and diarization. Bundling every
    analysis JSON into one dispatch therefore gave every tab the face view.

    Per-node dispatch is what makes selection correct, so it is asserted here rather
    than left as an implementation detail of the hook.
    """
    per_node = {
        "shot_segmentation": {"shots.json": SHOTS},
        "diarization": {"speaker_manifest.json": SPEAKERS},
        "face_recognition": {"facemetadata.json": FACEMETA},
    }
    chosen = {node: _select(sc) for node, sc in per_node.items()}
    assert chosen["shot_segmentation"] == "render-shot-segmentation"
    assert chosen["diarization"] == "render-speaker-diarization"
    assert chosen["face_recognition"].startswith("render-face")
    assert len(set(chosen.values())) == 3, chosen


def test_bundling_analyses_collapses_them_onto_one_skill():
    """Documents the hazard, so nobody reintroduces a shared dispatch.

    This is not desired behaviour being blessed -- it is the mechanism that broke the
    NBA session, pinned so that a change to bundling fails here with an explanation
    instead of silently reproducing four identical tabs.
    """
    bundled = {
        "shots.json": SHOTS,
        "speaker_manifest.json": SPEAKERS,
        "facemetadata.json": FACEMETA,
    }
    assert _select(bundled) == "render-face-people", (
        "bundled selection changed; per-node dispatch is what keeps analyses distinct"
    )


def test_no_payload_falls_through_to_the_generic_fallback():
    for name, payload in (("shots", SHOTS), ("speakers", SPEAKERS), ("faces", FACEMETA)):
        assert _select({f"{name}.json": payload}) is not None, name


def test_ranking_is_not_decided_by_skill_count():
    """Adding a skill must not change an unrelated payload's answer.

    Limiting the skills available to the Render Agent was proposed as a way to reduce
    selection complexity. It does not help a content-scored selector: the candidate
    set is already filtered to skills whose detect paths resolve, so a skill that does
    not match cannot win. What removing skills WOULD do is remove capability. This
    test states the property that makes the skill count irrelevant.
    """
    from render_agent import _discover_render_skills
    detecting = [s for s in _discover_render_skills()
                 if str((s.get("declaration") or {}).get("detect") or "").strip()]
    assert len(detecting) >= 4, "expected the four analysis skills to declare detect"
    # shots payload resolves only `shots`, so only one candidate can score at all.
    candidates = []
    for s in detecting:
        paths = [p.strip() for p in
                 str((s["declaration"]).get("detect")).split(",") if p.strip()]
        if any(k in SHOTS for k in paths):
            candidates.append(s["name"])
    assert candidates == ["render-shot-segmentation"], candidates


def test_the_render_agents_tool_surface_stays_minimal():
    """Reducing the Render Agent's tools was proposed to cut selection complexity.

    There is nothing to cut: skill selection never reaches the model. It is decided by
    select_render_skill / select_render_skill_by_content before the agent runs, and the
    tools it does carry are media INSPECTION utilities (probe a mime, extract peaks,
    parse captions, publish an artifact) -- none of them choose a skill or a template.

    Pinned anyway, so that adding a tool here is a deliberate act with a test to
    update rather than a quiet expansion of what the model can do mid-render.
    """
    import render_agent

    names = {getattr(t, "__name__", getattr(t, "tool_name", str(t)))
             for t in render_agent.RENDER_TOOLS}
    # review_render_manifest is the F8 reviewer construct -- the agent calls it to check its
    # own manifest against the source data and self-correct. It is a review tool, not a
    # skill/template selector, so the "no tool chooses a skill or template" intent still holds.
    # review_render_manifest + validate_render_urls are the F8/F4 guard constructs -- the agent
    # calls them to check its own manifest and its resolved urls against ground truth and
    # self-correct. Neither chooses a skill or template, so "no tool selects a view" still holds.
    # build_render_manifest is the assembly-as-a-tool: the agent DECIDES which urls to render and
    # this tool does the deterministic projection (boxes/markers/lanes) + writes the manifest, so
    # the model never hand-emits a large manifest. It assembles from the agent's chosen inputs; it
    # does not choose the inputs.
    # build_clip_gallery_manifest is the same idea for INDEPENDENT clips: given the agent's chosen
    # {label,url,kind} list it writes a clip-gallery manifest (per-clip player + mark-in/out) — it
    # assembles a view from chosen inputs, it does not select a skill or template.
    assert names == {"media_probe", "peak_extractor", "caption_parser", "artifact",
                     "validate_render_urls", "review_render_manifest", "build_render_manifest",
                     "build_clip_gallery_manifest"}, names


def test_selection_happens_before_the_model_and_not_inside_it():
    """The property that makes the tool surface irrelevant to selection.

    Both selectors are plain functions over (mime, extension) or the sidecar payloads,
    so a render's skill is reproducible offline -- which is what makes this whole file
    possible.
    """
    import inspect

    import render_agent

    for fn in (render_agent.select_render_skill, render_agent.select_render_skill_by_content):
        src = inspect.getsource(fn)
        for model_call in ("Agent(", "stream_async", "converse", "invoke_model"):
            assert model_call not in src, f"{fn.__name__} reaches a model via {model_call}"


def test_projection_count_outranks_detect_count():
    """The one payload where the ranking ORDER is observable.

    Two skills legitimately apply to a face result: render-face-people (detect
    `people`, 5 projections) and render-face-recognition (detect `tracks, clusters`,
    4 projections). A real face result carries all three keys, so face-recognition
    matches MORE detect paths while face-people surfaces MORE of the payload.

    Ranking on detect count first is wrong -- declaring two detect paths makes a skill
    broader, not more specific -- and it demoted the richer face view. That reasoning
    was written down but nothing tested it: flipping the two score terms left every
    other case in this file passing, because in those payloads only one skill matches
    at all and the order never comes into play.
    """
    both = dict(FACEMETA)
    both["tracks"] = [{"track_id": 0, "person_id": "p0"}]
    both["clusters"] = [{"cluster_id": 0, "size": 4}]
    assert _select({"facemetadata.json": both}) == "render-face-people"


# --------------------------------------------------------------------------------
# Which TEMPLATE a payload lands on, driven through the real manifest builder.
#
# Selection picks a skill; the template is picked separately from what correlation
# actually filled. Those are different decisions and the second one is where work got
# thrown away, so it is asserted against real output rather than against a model of
# what a template "should" consume. An earlier version of this file derived template
# capability from skill DECLARATIONS and got it wrong twice -- declaration keys are not
# manifest prop names, and "skills that feed a template" is not "props it reads".
# --------------------------------------------------------------------------------

VIDEO = "https://cdn.example/proxy/transcoded.mp4"


def _build(payloads: dict) -> dict:
    """Run the real media-manifest builder over a video plus analysis sidecars."""
    import json as _json

    import render_agent as ra

    urls = {f"https://cdn.example/{n}": d for n, d in payloads.items()}
    ra._fetch_json = lambda url, timeout=5.0: urls.get(url)
    ra.media_probe = lambda url: _json.dumps(
        {"mime_type": "video/mp4" if url.endswith(".mp4") else "application/json"})
    ra.peak_extractor = lambda url, n: _json.dumps({"peaks": [], "duration": 300.0})
    return ra._build_media_manifest([VIDEO, *urls], "t", {})


def test_shots_alone_still_uses_the_marker_scrubber():
    m = _build({"shots.json": SHOTS})
    assert m["template"] == "nle-timeline"  # one container for the time axis (video-markers is now an alias)
    assert m["markers"], "shot boundaries must survive"


def test_diarization_alone_still_uses_the_composite_timeline():
    m = _build({"global_speakers.json": SPEAKERS})
    assert m["template"] == "composite-timeline"
    assert m["segments"], "speaker segments must survive"


def test_shots_plus_diarization_keeps_both_instead_of_dropping_segments():
    """The fix.

    Both kinds are filled, and only composite-timeline renders both -- it composes
    MarkerTrack with SegmentTable/SegmentLanes. The old first-match chain matched the
    markers branch and handed the result to video-markers, which reads only markers, so
    the speaker segments were computed and discarded.
    """
    m = _build({"shots.json": SHOTS, "global_speakers.json": SPEAKERS})
    assert m["template"] == "composite-timeline", m["template"]
    assert m["markers"], "shot boundaries dropped"
    assert m["segments"], "speaker segments dropped"


def test_no_analysis_kind_is_filled_and_then_ignored():
    """Generalises the fix past the one combination that exposed it.

    Whatever correlation fills, the chosen template must render it -- with the single
    documented exception that boxes are exclusive to video-annotations.
    """
    renders = {
        # `video-markers` folded into the timeline container, which composes MarkerTrack with
        # the segment lanes — so the markers-only branch routes here too.
        "nle-timeline": {"markers", "segments"},
        "composite-timeline": {"markers", "segments"},
        "video-annotations": {"annotations"},
    }
    for payloads in (
        {"shots.json": SHOTS},
        {"global_speakers.json": SPEAKERS},
        {"shots.json": SHOTS, "global_speakers.json": SPEAKERS},
    ):
        m = _build(payloads)
        filled = {k for k in ("markers", "segments", "annotations") if m.get(k)}
        consumed = renders.get(m["template"], set())
        assert filled <= consumed, (
            f"{sorted(payloads)} -> '{m['template']}' renders {sorted(consumed)} "
            f"but correlation filled {sorted(filled)}")


def test_face_boxes_payload_prefers_the_boxes_view():
    """User regression: a face job with boxes must render boxes-on-video, not the boxless people
    timeline. facegroup.json carries BOTH stats.num_people (render-face-people matches) AND
    people[].tracks[].faces[].box (render-face-recognition matches), so both compete and the SCORING
    decides. b4ed0dd let face-people win (more projection keys) -> no boxes ever drawn. The boxes
    view (video-annotations) must now win."""
    facegroup = {
        "stats": {"num_people": 2, "num_tracks": 80, "faces_detected": 93},
        "people": [
            {"name": "Kevin Kilner", "named": True, "gender": "Male",
             "appearances": [{"begin_ms": 30030, "end_ms": 31030}],
             "tracks": [{"faces": [{"timestamp": 30.03, "frame_key": "frame_0000900.jpg",
                                    "box": {"l": 0.3048, "t": 0.1736, "w": 0.1408, "h": 0.3558}}]}]},
            {"name": "Reid Scott", "named": True, "gender": "Male",
             "appearances": [{"begin_ms": 5005, "end_ms": 6005}],
             "tracks": [{"faces": [{"timestamp": 5.005, "frame_key": "frame_0000150.jpg",
                                    "box": {"l": 0.489, "t": 0.112, "w": 0.249, "h": 0.643}}]}]},
        ],
    }
    assert _select({"facegroup.json": facegroup}) == "render-face-recognition"
