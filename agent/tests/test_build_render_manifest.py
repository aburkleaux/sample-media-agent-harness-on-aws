"""build_render_manifest is the assembly-as-a-tool: the agent decides which urls to render and
this tool does the deterministic projection (boxes/markers/lanes), writes the manifest, and
returns a compact pointer -- so the LLM never hand-emits a large manifest (which made analysis
renders loop/time out)."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import render_agent as ra


def test_face_tracks_plus_source_video_projects_boxes(monkeypatch):
    # face_tracks.json with 2 timed boxes + a source video -> video-annotations with annotations.
    tracks = {"tracks": [{"faces": [
        {"timestamp": 1.0, "box": {"l": 0.1, "t": 0.1, "w": 0.2, "h": 0.3}, "gender": "M"},
        {"timestamp": 2.0, "box": {"l": 0.4, "t": 0.2, "w": 0.2, "h": 0.3}, "gender": "F"}]}]}
    monkeypatch.setattr(ra, "_fetch_json_many",
                        lambda urls: {u: tracks for u in urls})
    monkeypatch.setattr(ra, "_write_manifest_to_s3",
                        lambda mj, sid, mid: f"artifacts/{sid}/render/{mid}.json")
    ra._RENDER_WRITES.clear()
    out = json.loads(ra.build_render_manifest(
        candidate_urls=json.dumps(["s3://b/wf/EXEC/face_tracks.json"]),
        manifest_id="mid-1", session_id="sess-1",
        source_urls=json.dumps(["https://cdn/wf/EXEC/proxy/transcoded.mp4"])))
    assert out["template"] == "video-annotations"
    assert out["annotations"] >= 2
    assert out["s3_key"] == "artifacts/sess-1/render/mid-1.json"
    # recorded for the entrypoint to emit the output_ref
    assert ra._RENDER_WRITES.get("mid-1", {}).get("s3_key") == out["s3_key"]


def test_plain_video_builds_a_track(monkeypatch):
    monkeypatch.setattr(ra, "_write_manifest_to_s3", lambda mj, sid, mid: f"k/{mid}.json")
    ra._RENDER_WRITES.clear()
    out = json.loads(ra.build_render_manifest(
        candidate_urls=json.dumps(["https://cdn/x/transcoded.mp4"]),
        manifest_id="mid-2", session_id="s"))
    assert out["tracks"] >= 1
    assert out["s3_key"]


def test_source_key_groups_analysis_onto_its_source(monkeypatch):
    # sourceKey lets the UI merge every analysis of the same source onto ONE composite tab.
    # A plain video keys on its OWN output; an analysis keys on the source it was run on — so
    # the two share a sourceKey and the analysis becomes a lane on the video's timeline.
    from render.review import _norm_url
    monkeypatch.setattr(ra, "_write_manifest_to_s3", lambda mj, sid, mid: f"k/{mid}.json")
    ra._RENDER_WRITES.clear()
    src = "https://cdn/wf/EXEC/proxy/transcoded.mp4"
    vid = json.loads(ra.build_render_manifest(
        candidate_urls=json.dumps([src]), manifest_id="mv", session_id="s"))
    assert vid["sourceKey"] == _norm_url(src)
    # a face-boxes analysis of the SAME source video groups with it
    tracks = {"tracks": [{"faces": [
        {"timestamp": 1.0, "box": {"l": 0.1, "t": 0.1, "w": 0.2, "h": 0.3}, "gender": "M"}]}]}
    monkeypatch.setattr(ra, "_fetch_json_many", lambda urls: {u: tracks for u in urls})
    analysis = json.loads(ra.build_render_manifest(
        candidate_urls=json.dumps(["s3://b/wf/EXEC/face_tracks.json"]),
        manifest_id="ma", session_id="s", source_urls=json.dumps([src])))
    assert analysis["sourceKey"] == _norm_url(src)
    assert analysis["sourceKey"] == vid["sourceKey"]


def test_source_video_echoed_into_candidates_is_not_a_track(monkeypatch):
    # Stem separation (session 9f77723a): the render manifest wrongly carried the full-length
    # source video as a track (718s), dwarfing the 60s stems. A source url must never be a
    # deliverable track — even when it reaches candidate_urls, and even as a CloudFront mirror
    # (matched by filename) of the s3 source.
    monkeypatch.setattr(ra, "_write_manifest_to_s3", lambda mj, sid, mid: f"k/{mid}.json")
    ra._RENDER_WRITES.clear()
    json.loads(ra.build_render_manifest(
        candidate_urls=json.dumps([
            "https://cdn/wf/EXEC/audio_speech.wav",
            "https://cdn/wf/EXEC/audio_music.wav",
            "https://cdn/artifacts/1786674628130-Netflix_Open_Content_Meridian.mp4",
        ]),
        manifest_id="mid-stem", session_id="s",
        source_urls=json.dumps(["s3://bkt/artifacts/1786674628130-Netflix_Open_Content_Meridian.mp4"])))
    m = ra._RENDER_WRITES["mid-stem"]["manifest"]
    types = sorted(t["type"] for t in m.get("tracks", []))
    assert "video" not in types, "the source video must not be a deliverable track"
    assert types == ["audio", "audio"]


def test_audio_deliverable_render_stays_audio_only(monkeypatch):
    # Observed 9f77723a stems case: get_plan_results dispatched 4 audio stems, but the render
    # agent ALSO put the session's 718s source video into candidate_urls — and it was NOT in
    # source_urls, so the source-subtract guard cannot catch it. An audio-deliverable render must
    # stay audio-only (no full-length source video track dominating the timeline).
    monkeypatch.setattr(ra, "_write_manifest_to_s3", lambda mj, sid, mid: f"k/{mid}.json")
    ra._RENDER_WRITES.clear()
    json.loads(ra.build_render_manifest(
        candidate_urls=json.dumps([
            "https://cdn/wf/EXEC/audio_speech.wav",
            "https://cdn/wf/EXEC/audio_music.wav",
            "https://cdn/wf/EXEC/audio_sfx.wav",
            "https://cdn/artifacts/1786674628130-Netflix_Open_Content_Meridian.mp4",
        ]),
        manifest_id="mid-stem2", session_id="s"))  # NB: no source_urls
    m = ra._RENDER_WRITES["mid-stem2"]["manifest"]
    types = sorted(t["type"] for t in m.get("tracks", []))
    assert types == ["audio", "audio", "audio"], f"expected 3 audio stems only, got {types}"


def test_plain_video_still_renders_when_no_audio_deliverable(monkeypatch):
    # Guard the rule: a video-only render (no audio tracks) is untouched.
    monkeypatch.setattr(ra, "_write_manifest_to_s3", lambda mj, sid, mid: f"k/{mid}.json")
    ra._RENDER_WRITES.clear()
    json.loads(ra.build_render_manifest(
        candidate_urls=json.dumps(["https://cdn/x/transcoded.mp4"]),
        manifest_id="mid-vid", session_id="s"))
    m = ra._RENDER_WRITES["mid-vid"]["manifest"]
    assert any(t["type"] == "video" for t in m.get("tracks", []))


def test_accepts_list_or_json_string_args(monkeypatch):
    monkeypatch.setattr(ra, "_write_manifest_to_s3", lambda mj, sid, mid: "k.json")
    ra._RENDER_WRITES.clear()
    # list form (some tool runtimes pass parsed lists)
    out = json.loads(ra.build_render_manifest(
        candidate_urls=["https://cdn/x/audio.wav"], manifest_id="mid-3", session_id="s"))
    assert out["s3_key"] == "k.json"


def test_bare_image_builds_image_annotated(monkeypatch):
    monkeypatch.setattr(ra, "_write_manifest_to_s3", lambda mj, sid, mid: f"k/{mid}.json")
    ra._RENDER_WRITES.clear()
    out = json.loads(ra.build_render_manifest(
        candidate_urls=json.dumps(["https://cdn/x/cat.png"]),
        manifest_id="mid-img", session_id="s"))
    assert out["template"] == "image-annotated"
    m = ra._RENDER_WRITES["mid-img"]["manifest"]
    assert m["images"] and m["images"][0]["src"] == "https://cdn/x/cat.png"


def test_image_manifest_has_valid_duration(monkeypatch):
    # The UI rejects duration < 0.001 ("Could not display this render"). A still image has no
    # duration, but the manifest must still carry the schema floor.
    monkeypatch.setattr(ra, "_write_manifest_to_s3", lambda mj, sid, mid: "k.json")
    ra._RENDER_WRITES.clear()
    ra.build_render_manifest(candidate_urls=json.dumps(["https://cdn/x/dog.png"]),
                             manifest_id="mid-img2", session_id="s")
    m = ra._RENDER_WRITES["mid-img2"]["manifest"]
    assert m["template"] == "image-annotated"
    assert m["duration"] >= 0.001


def test_image_manifest_direct_builder_duration():
    m = ra._build_image_manifest("https://cdn/x/dog.png", "mid")
    assert m["duration"] >= 0.001


def test_rebuild_cap_stops_runaway_loop(monkeypatch):
    # The self-recovery loop (review->rebuild) must not spin: after the cap, build returns a hard
    # "finalize now" instead of rebuilding. (Guards against the duration-0 loop pattern recurring.)
    monkeypatch.setattr(ra, "_write_manifest_to_s3", lambda mj, sid, mid: f"k/{mid}.json")
    ra._RENDER_WRITES.clear(); ra._RENDER_BUILD_COUNTS.clear()
    args = dict(candidate_urls=json.dumps(["https://cdn/x/dog.png"]),
                manifest_id="mid-loop", session_id="s")
    outs = [json.loads(ra.build_render_manifest(**args)) for _ in range(5)]
    # first few build normally; once past the cap, a stop note is returned
    assert any("note" in o and "finalize" in o.get("note", "").lower() for o in outs)
    assert ra._RENDER_BUILD_COUNTS["mid-loop"] == 5


def test_image_manifest_renders_all_images():
    # A text-to-image call returns TWO options; both must render (ImageAnnotated shows the gallery).
    m = ra._build_image_manifest(["https://cdn/a.png", "https://cdn/b.jpg"], "mid-multi")
    assert m["template"] == "image-annotated"
    assert [i["src"] for i in m["images"]] == ["https://cdn/a.png", "https://cdn/b.jpg"]


def test_build_render_manifest_galleries_eight_images(monkeypatch):
    # End-to-end proof of the "render these N frames together" fix (session 684bec82): the PUBLIC
    # build_render_manifest, given a SET of image urls, produces ONE image-annotated gallery holding
    # every image in order (never one-picked, never stitched). This is exactly what the render-media
    # skill now instructs the Render Agent to do when handed a set of existing frames.
    monkeypatch.setattr(ra, "_write_manifest_to_s3", lambda mj, sid, mid: f"k/{mid}.json")
    ra._RENDER_WRITES.clear()
    urls = [f"https://cdn/x/frame_{i:02d}.png" for i in range(1, 9)]  # 8 frames
    out = json.loads(ra.build_render_manifest(
        candidate_urls=json.dumps(urls), manifest_id="mid-8frames", session_id="s"))
    assert out["template"] == "image-annotated"
    m = ra._RENDER_WRITES["mid-8frames"]["manifest"]
    assert [i["src"] for i in m["images"]] == urls  # all 8, in order, one gallery
    assert len(m["images"]) == 8


def test_image_manifest_single_str_still_works():
    m = ra._build_image_manifest("https://cdn/only.png", "mid-one")
    assert len(m["images"]) == 1 and m["src"] == "https://cdn/only.png"


def test_build_render_manifest_two_images(monkeypatch):
    monkeypatch.setattr(ra, "_write_manifest_to_s3", lambda mj, sid, mid: "k.json")
    ra._RENDER_WRITES.clear(); ra._RENDER_BUILD_COUNTS.clear()
    ra.build_render_manifest(candidate_urls=json.dumps(["https://cdn/a.png", "https://cdn/b.png"]),
                             manifest_id="mid-2img", session_id="s")
    m = ra._RENDER_WRITES["mid-2img"]["manifest"]
    assert m["template"] == "image-annotated" and len(m["images"]) == 2


def test_manifest_is_renderable_helper():
    assert ra._manifest_is_renderable({"tracks": [{"src": "https://cdn/v.mp4"}]}) is True
    assert ra._manifest_is_renderable({"images": [{"src": "https://cdn/a.png"}]}) is True
    assert ra._manifest_is_renderable({"document": {"previewUrl": "https://cdn/x.pdf"}}) is True
    assert ra._manifest_is_renderable({"tracks": [], "images": []}) is False
    assert ra._manifest_is_renderable({"errors": [{"code": "X"}], "tracks": [{"src": "u"}]}) is False
    assert ra._manifest_is_renderable(None) is False


def test_completeness_all_images_present():
    m = {"template": "image-annotated", "images": [{"src": "https://cdn/a.png"}, {"src": "https://cdn/b.jpg"}]}
    assert ra._dropped_renderable_candidates(["https://cdn/a.png", "https://cdn/b.jpg"], m) == []


def test_completeness_flags_dropped_image():
    m = {"template": "image-annotated", "images": [{"src": "https://cdn/a.png"}]}
    dropped = ra._dropped_renderable_candidates(["https://cdn/a.png", "https://cdn/b.jpg"], m)
    assert dropped == ["https://cdn/b.jpg"]


def test_completeness_json_overlays_not_counted():
    # faces: json candidates are projected as overlays, not rendered as media -> not "dropped"
    m = {"template": "video-annotations", "tracks": [{"src": "https://cdn/video.mp4"}]}
    cands = ["https://cdn/face_tracks.json", "https://cdn/video.mp4"]
    assert ra._dropped_renderable_candidates(cands, m) == []


def test_probe_sidecar_alone_does_not_render_the_source_video(monkeypatch):
    """SOURCE media is a canvas for projections, never a deliverable on its own.

    probe.json DESCRIBES the source rather than projecting onto it, so the annotations build
    declines (no timed boxes) and the base tier used to promote the source video to the timeline
    -- producing a tab that played the untouched 11:58 original with a metadata grid under it,
    i.e. the job's INPUT rendered as its output.
    """
    probe = {"format": {"filename": "https://cdn/artifacts/meridian.mp4",
                        "duration": "718.934878", "format_name": "mov,mp4,m4a"},
             "streams": [{"codec_name": "h264", "profile": "High"}]}
    monkeypatch.setattr(ra, "_fetch_json_many", lambda urls: {u: probe for u in urls})
    monkeypatch.setattr(ra, "_write_manifest_to_s3", lambda mj, sid, mid: f"k/{mid}.json")
    ra._RENDER_WRITES.clear(); ra._RENDER_BUILD_COUNTS.clear()
    ra.build_render_manifest(
        candidate_urls=json.dumps(["s3://b/wf/EXEC/probe.json"]),
        manifest_id="mid-probe", session_id="s",
        source_urls=json.dumps(["https://cdn/artifacts/meridian.mp4"]))
    m = ra._RENDER_WRITES["mid-probe"]["manifest"]
    srcs = [t.get("src") for t in (m.get("tracks") or [])]
    assert "https://cdn/artifacts/meridian.mp4" not in srcs, (
        "the source video must not become a track when nothing projects onto it"
    )


def test_projecting_sidecar_still_gets_the_source_video(monkeypatch):
    """The guard must not break the case the source-video fallback exists for.

    Caption cues, shot markers and speaker lanes all project ONTO the timeline, so a sidecar that
    produces them legitimately needs the source video to sit on -- rendering cues with no video
    would be useless. Only a sidecar that projects NOTHING loses the canvas.
    """
    shots = {"shots": [{"start": 0.0, "end": 12.0}, {"start": 12.0, "end": 30.0}]}
    monkeypatch.setattr(ra, "_fetch_json_many", lambda urls: {u: shots for u in urls})
    monkeypatch.setattr(ra, "_write_manifest_to_s3", lambda mj, sid, mid: f"k/{mid}.json")
    ra._RENDER_WRITES.clear(); ra._RENDER_BUILD_COUNTS.clear()
    ra.build_render_manifest(
        candidate_urls=json.dumps(["s3://b/wf/EXEC/shots.json"]),
        manifest_id="mid-shots", session_id="s",
        source_urls=json.dumps(["https://cdn/artifacts/meridian.mp4"]))
    m = ra._RENDER_WRITES["mid-shots"]["manifest"]
    srcs = [t.get("src") for t in (m.get("tracks") or [])]
    assert "https://cdn/artifacts/meridian.mp4" in srcs, (
        "a sidecar that projects onto the timeline still needs its canvas"
    )


def test_real_media_output_is_unaffected_by_the_guard(monkeypatch):
    """A genuine media OUTPUT is a candidate, not a source -- the guard never applies to it."""
    monkeypatch.setattr(ra, "_write_manifest_to_s3", lambda mj, sid, mid: f"k/{mid}.json")
    ra._RENDER_WRITES.clear(); ra._RENDER_BUILD_COUNTS.clear()
    out = json.loads(ra.build_render_manifest(
        candidate_urls=json.dumps(["s3://b/wf/EXEC/proxy/transcoded.mp4"]),
        manifest_id="mid-out", session_id="s",
        source_urls=json.dumps(["https://cdn/artifacts/meridian.mp4"])))
    assert out["tracks"] >= 1
    m = ra._RENDER_WRITES["mid-out"]["manifest"]
    assert any("transcoded.mp4" in (t.get("src") or "") for t in m.get("tracks") or [])


def test_descriptive_sidecar_helper():
    assert ra._is_descriptive_sidecar("s3://b/wf/EXEC/probe.json") is True
    assert ra._is_descriptive_sidecar("s3://b/wf/EXEC/usage_tracking.json") is True
    # Data that annotates the timeline is not descriptive -- it keeps its canvas.
    assert ra._is_descriptive_sidecar("s3://b/wf/EXEC/shots.json") is False
    assert ra._is_descriptive_sidecar("s3://b/wf/EXEC/face_tracks.json") is False
    assert ra._is_descriptive_sidecar("https://cdn/x/facemetadata.json?sig=1") is False


def test_html_artifact_builds_a_playable_document(monkeypatch):
    # A standalone .html artifact (e.g. a code_interpreter game) must render as a
    # document the frontend iframes -- NOT fall through to a bare download link.
    monkeypatch.setattr(ra, "_write_manifest_to_s3", lambda mj, sid, mid: f"k/{mid}.json")
    ra._RENDER_WRITES.clear()
    out = json.loads(ra.build_render_manifest(
        candidate_urls=json.dumps(["https://cdn/artifacts/snake.html"]),
        manifest_id="mid-html", session_id="s"))
    assert out["template"] == "document-viewer"
    m = ra._RENDER_WRITES["mid-html"]["manifest"]
    assert m["document"]["previewUrl"] == "https://cdn/artifacts/snake.html"
    assert m["mimeType"] == "text/html"


def _fake_face_skills():
    return [
        {"name": "render-face-recognition",
         "declaration": {"deliverable": "facegroup.json",
                         "subsumes": "facemetadata.json, face_clusters.json, identity_clusters.json, face_tracks.json"}},
        {"name": "render-face-people",
         "declaration": {"deliverable": "facemetadata.json"}},
    ]


def test_subsumes_drops_derived_face_files_when_facegroup_present(monkeypatch):
    monkeypatch.setattr(ra, "_discover_render_skills", _fake_face_skills)
    cands = [
        "https://cdn/wf/facemetadata.json",
        "https://cdn/wf/facegroup.json",
        "https://cdn/wf/face_clusters.json",
        # ephemeral raw tracks/clusters (36 items) — these produced 36 unnamed
        # "Person N" entities instead of facegroup's 5 grouped, named people.
        "https://cdn/wf/ephemeral/identity_clusters.json",
        "https://cdn/wf/ephemeral/face_tracks.json",
    ]
    # facegroup (boxes) is present -> ALL flat/derived/ephemeral siblings must be
    # dropped, so only ONE face tab renders from the grouped, named people.
    assert ra._apply_subsumes(cands) == ["https://cdn/wf/facegroup.json"]


def test_subsumes_is_noop_when_subsumer_absent(monkeypatch):
    monkeypatch.setattr(ra, "_discover_render_skills", _fake_face_skills)
    # No facegroup.json -> facemetadata legitimately renders on its own.
    cands = ["https://cdn/wf/facemetadata.json"]
    assert ra._apply_subsumes(cands) == cands


def test_subsumes_ignores_presign_query_and_path(monkeypatch):
    monkeypatch.setattr(ra, "_discover_render_skills", _fake_face_skills)
    cands = [
        "https://cdn/a/b/facemetadata.json?X-Amz-Signature=abc",
        "https://cdn/a/b/facegroup.json?X-Amz-Signature=def",
    ]
    assert ra._apply_subsumes(cands) == ["https://cdn/a/b/facegroup.json?X-Amz-Signature=def"]


def test_face_recognition_md_declares_subsumes():
    from pathlib import Path
    md = (Path(ra.__file__).resolve().parents[1]
          / "skills/render-media/references/face-recognition.md").read_text()
    decl = ra._parse_skill_frontmatter(md)["declaration"]
    assert decl.get("deliverable") == "facegroup.json"
    subs = decl.get("subsumes", "")
    assert "facemetadata.json" in subs and "face_clusters.json" in subs


def test_render_deterministic_json_only_emits_no_tab(monkeypatch):
    # A lone JSON that projects to nothing must NOT open a generic file-viewer tab: no manifest
    # write, no output_ref. (Projectable JSON — e.g. faces -> data-table — is covered in
    # test_render_projection and is unaffected.)
    import asyncio
    writes = []
    monkeypatch.setattr(ra, "_write_manifest_to_s3",
                        lambda mj, sid, mid: (writes.append(mid), "k.json")[1])
    # No network: the sidecar fetch returns nothing, so projection yields nothing.
    monkeypatch.setattr(ra, "_fetch_json_many", lambda urls: {u: None for u in urls})
    monkeypatch.setattr(ra, "_fetch_json", lambda u, timeout=5.0: None)

    async def run():
        events = []
        payload = {"prompt": "Render: https://cdn/artifacts/shots.json", "session_id": "s"}
        async for ev in ra._render_deterministic(payload, {}):
            events.append(ev)
        return events
    events = asyncio.new_event_loop().run_until_complete(run())
    parsed = [json.loads(e) for e in events if e and e.strip().startswith("{")]
    assert not any(p.get("type") == "output_ref" for p in parsed), \
        "JSON-only render must emit no output_ref (no generic-viewer tab)"
    assert writes == [], "JSON-only render must not write a manifest"


def test_render_deterministic_routes_html_to_document(monkeypatch):
    # The agentic/deterministic path classified doc_urls as pdf-only, so a lone
    # .html fell through to generic-preview (a download chip). It must produce a
    # playable document-viewer, same as the build_render_manifest path.
    import asyncio
    cap = {}
    def _cap(mj, sid, mid):
        cap.clear(); cap.update(mj if isinstance(mj, dict) else json.loads(mj)); return "k.json"
    monkeypatch.setattr(ra, "_write_manifest_to_s3", _cap)
    if hasattr(ra, "_find_mvhd"):
        monkeypatch.setattr(ra, "_find_mvhd", lambda *a, **k: None)

    async def run():
        payload = {"prompt": "Render this: https://cdn/artifacts/snake_game.html", "session_id": "s"}
        async for _ in ra._render_deterministic(payload, {}):
            pass
    asyncio.new_event_loop().run_until_complete(run())
    assert cap.get("template") == "document-viewer"
    assert cap["document"]["previewUrl"].endswith("snake_game.html")
    assert cap.get("mimeType") == "text/html"


def test_document_deliverable_wins_over_source_video_in_candidates(monkeypatch):
    # Verified root cause of "PDF renders the source video" (session 2693e705): the render
    # dispatch's deliverable section carried the PDF report AND the analyzed source videos, so
    # `docs and not media` was False and the builder fell through to a video timeline, dropping
    # the PDF. A document is an OUTPUT you never project onto -> it wins; a co-present video is
    # the analyzed source. Generic MIME-class precedence, no name/prefix lists.
    monkeypatch.setattr(ra, "_write_manifest_to_s3", lambda mj, sid, mid: f"k/{mid}.json")
    ra._RENDER_WRITES.clear()
    out = json.loads(ra.build_render_manifest(
        candidate_urls=json.dumps([
            "https://cdn/artifacts/meridian_60s_proxy_analysis_81f1bac4.pdf",
            "https://cdn/artifacts/1786726418780-Netflix_Open_Content_Meridian.mp4",
            "https://cdn/artifacts/faa6fb18250991cf1e550b1dcdc80e27.mp4",
        ]),
        manifest_id="mid-doc", session_id="s"))
    assert out["template"] == "document-viewer", out.get("template")


def test_deterministic_pdf_deliverable_wins_over_source_videos(monkeypatch):
    # Drives the ACTUAL path that failed live (session 2693e705): _render_deterministic. The
    # dispatch prompt's deliverable section listed the PDF plus the prior source videos, so
    # `doc_urls and not media_urls` skipped the document branch and rendered the 718s source
    # (completeness log recorded: "1 candidate not rendered: ...pdf"). With deliverable-type
    # precedence the PDF renders as a document-viewer regardless of co-present source video.
    import asyncio
    cap = {}
    def _cap(mj, sid, mid):
        cap.clear(); cap.update(mj if isinstance(mj, dict) else json.loads(mj)); return "k.json"
    monkeypatch.setattr(ra, "_write_manifest_to_s3", _cap)
    if hasattr(ra, "_find_mvhd"):
        monkeypatch.setattr(ra, "_find_mvhd", lambda *a, **k: None)

    async def run():
        payload = {"prompt": ("Render these: "
                              "https://cdn/artifacts/meridian_60s_proxy_analysis_81f1bac4.pdf "
                              "https://cdn/artifacts/1786726418780-Netflix_Open_Content_Meridian.mp4 "
                              "https://cdn/artifacts/faa6fb18250991cf1e550b1dcdc80e27.mp4"),
                   "session_id": "s"}
        async for _ in ra._render_deterministic(payload, {}):
            pass
    asyncio.new_event_loop().run_until_complete(run())
    assert cap.get("template") == "document-viewer", cap.get("template")
    assert cap["document"]["previewUrl"].endswith(".pdf")


def test_deterministic_pdf_with_source_videos_in_already_rendered_section(monkeypatch):
    # Highest-fidelity repro of session 2693e705: the REAL dispatch prompt shape, where the
    # prior source videos sit in the ALREADY-RENDERED section (not as candidates). The split fix
    # must keep them out of the deliverable scan entirely, so the PDF renders as a document and
    # NO video track appears. Uses the exact heading strings render_artifacts.dispatch_render
    # emits.
    import asyncio
    cap = {}
    def _cap(mj, sid, mid):
        cap.clear(); cap.update(mj if isinstance(mj, dict) else json.loads(mj)); return "k.json"
    monkeypatch.setattr(ra, "_write_manifest_to_s3", _cap)
    if hasattr(ra, "_find_mvhd"):
        monkeypatch.setattr(ra, "_find_mvhd", lambda *a, **k: None)

    prompt = (
        "Render these artifacts as one unified timeline. These are the CANDIDATE OUTPUT URLS "
        "(pass them as candidates to validate_render_urls, and render only the urls it returns "
        "as valid):\n"
        "https://cdn/artifacts/meridian_60s_proxy_analysis_81f1bac4.pdf\n\n"
        "ALREADY RENDERED this session (pass as rendered to validate_render_urls; do not "
        "re-render):\n"
        "https://cdn/artifacts/1786726418780-Netflix_Open_Content_Meridian.mp4\n"
        "https://cdn/artifacts/faa6fb18250991cf1e550b1dcdc80e27.mp4"
    )

    async def run():
        async for _ in ra._render_deterministic({"prompt": prompt, "session_id": "s"}, {}):
            pass
    asyncio.new_event_loop().run_until_complete(run())
    assert cap.get("template") == "document-viewer", cap.get("template")
    assert cap["document"]["previewUrl"].endswith(".pdf")
    # The source videos must not appear as tracks at all — fully excluded, not merely outranked.
    track_srcs = " ".join(t.get("src", "") for t in (cap.get("tracks") or []))
    assert ".mp4" not in track_srcs


def test_facegroup_source_video_projects_named_boxes(monkeypatch):
    # People-detection "boxes on video": facegroup.json nests boxes under NAMED people
    # (people[].tracks[].faces[].box). With a source video, build must produce video-annotations
    # with the boxes projected (labeled by name) — not a boxless composite-timeline.
    import render_agent as ra
    facegroup = {
        "stats": {"num_people": 2, "num_tracks": 2, "faces_detected": 2},
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
    monkeypatch.setattr(ra, "_fetch_json_many", lambda urls: {u: facegroup for u in urls})
    monkeypatch.setattr(ra, "_write_manifest_to_s3", lambda mj, sid, mid: f"k/{mid}.json")
    ra._RENDER_WRITES.clear()
    out = json.loads(ra.build_render_manifest(
        candidate_urls=json.dumps(["s3://b/micro-workflow/face-recognition/EXEC/facegroup.json"]),
        manifest_id="mid-fg", session_id="s",
        source_urls=json.dumps(["https://cdn/face/EXEC/proxy/transcoded.mp4"])))
    assert out["template"] == "video-annotations", out
    assert out["annotations"] >= 2, out       # both named people's boxes projected


# ---------------------------------------------------------------------------
# _split_candidate_urls — a joined multi-URL candidate must split into one URL
# per entry (session 46480f72: 8 frame URLs arrived as a single "\n"-joined
# string and became ONE image with an unloadable multi-URL src -> blank gallery).
# ---------------------------------------------------------------------------

_FRAMES = [
    "https://dexamplecdn002.cloudfront.net/artifacts/s/output/frames-x/frame_01_00_01.jpg",
    "https://dexamplecdn002.cloudfront.net/artifacts/s/output/frames-x/frame_02_00_06.jpg",
    "https://dexamplecdn002.cloudfront.net/artifacts/s/output/frames-x/frame_03_00_10.jpg",
]


def test_split_urls_real_newline_joined_blob():
    assert ra._split_candidate_urls("\n".join(_FRAMES)) == _FRAMES


def test_split_urls_literal_backslash_n_joined_blob():
    # The ACTUAL 46480f72 encoding: URLs separated by a literal backslash-n (two chars).
    blob = "\\n".join(_FRAMES)
    assert ra._split_candidate_urls(blob) == _FRAMES


def test_split_urls_json_array_passthrough():
    assert ra._split_candidate_urls(json.dumps(_FRAMES)) == _FRAMES


def test_split_urls_json_array_with_joined_element_is_flattened():
    assert ra._split_candidate_urls(json.dumps(["\\n".join(_FRAMES)])) == _FRAMES


def test_split_urls_single_url_unchanged():
    assert ra._split_candidate_urls(_FRAMES[0]) == [_FRAMES[0]]


def test_split_urls_s3_uris_split():
    s3s = ["s3://bucket/a.json", "s3://bucket/c.json"]
    assert ra._split_candidate_urls(" ".join(s3s)) == s3s


def test_split_urls_non_url_string_preserved():
    assert ra._split_candidate_urls("just some text") == ["just some text"]


def test_split_urls_empty_and_blank():
    assert ra._split_candidate_urls("") == []
    assert ra._split_candidate_urls("   ") == []
    assert ra._split_candidate_urls([]) == []



# ---------------------------------------------------------------------------
# _sanitize_url_list + assemble_manifest — session 3445660b: a keyframe gallery
# arrived with 7 of 8 srcs shaped ".../shot_0N_key.jpg\nshot_0(N+1)_key.jpg"
# (literal backslash-n + the NEXT filename glued on). Every object existed, so
# it was not expiry or a missing file — the URL itself was unfetchable, so 7
# tiles 404'd and only the last (clean) one loaded. Refresh could never fix it
# because the dead string was baked into the persisted manifest.
# ---------------------------------------------------------------------------

_KF_BASE = "https://dexamplecdn002.cloudfront.net/artifacts/3445660b/output/"
# The exact broken shape: a sliding window of two filenames per entry, last one clean.
_KF_BROKEN = [
    f"{_KF_BASE}meridian_shot_0{i}_key.jpg\\nmeridian_shot_0{i + 1}_key.jpg"
    for i in range(1, 8)
] + [f"{_KF_BASE}meridian_shot_08_key.jpg"]
_KF_CLEAN = [f"{_KF_BASE}meridian_shot_0{i}_key.jpg" for i in range(1, 9)]


def test_sanitize_url_list_recovers_all_frames_from_sliding_window_blob():
    # Each frame is the FIRST valid token of exactly one entry, so cleaning to the
    # first token per entry recovers all 8 distinct keyframes.
    assert ra._sanitize_url_list(_KF_BROKEN) == _KF_CLEAN


def test_sanitize_url_list_is_idempotent_on_clean_urls():
    assert ra._sanitize_url_list(_KF_CLEAN) == _KF_CLEAN


def test_assemble_manifest_gallery_srcs_are_clean_not_multi_filename():
    m, _projected, _skill = ra.assemble_manifest(
        artifact_urls=_KF_BROKEN, source_urls=[], tool=None, manifest_id="kf")
    assert m["template"] == "image-annotated"
    srcs = [i["src"] for i in m["images"]]
    assert srcs == _KF_CLEAN
    # No persisted src may carry an embedded escape + second filename.
    assert all("\\n" not in s and ".jpg" == s[-4:] for s in srcs)



def test_deterministic_multi_video_defaults_to_clip_gallery(monkeypatch):
    """N INDEPENDENT peer video clips (a lip-sync per-segment review), with no source canvas,
    document, or JSON overlay, render as a clip-gallery MULTIVIEW — not a stacked N-track
    nle-timeline (session 20f7315e: 8 segments came out as one 8-track timeline)."""
    import asyncio
    cap = {}
    def _cap(mj, sid, mid):
        cap.clear(); cap.update(mj if isinstance(mj, dict) else json.loads(mj)); return "k.json"
    monkeypatch.setattr(ra, "_write_manifest_to_s3", _cap)

    async def run():
        payload = {"prompt": ("Render these: "
                              "https://cdn/artifacts/external/a.mp4 "
                              "https://cdn/artifacts/external/b.mp4 "
                              "https://cdn/artifacts/external/c.mp4"),
                   "session_id": "s"}
        async for _ in ra._render_deterministic(payload, {}):
            pass
    asyncio.new_event_loop().run_until_complete(run())
    assert cap.get("template") == "clip-gallery", cap.get("template")
    assert len(cap.get("galleryClips", [])) == 3


def test_deterministic_single_video_stays_a_timeline_not_a_gallery(monkeypatch):
    """A lone clip is not a multiview — the >=2 guard keeps one video on the normal path."""
    import asyncio
    cap = {}
    def _cap(mj, sid, mid):
        cap.clear(); cap.update(mj if isinstance(mj, dict) else json.loads(mj)); return "k.json"
    monkeypatch.setattr(ra, "_write_manifest_to_s3", _cap)
    monkeypatch.setattr(ra, "media_probe", lambda u: '{"mime_type": "video/mp4"}')
    monkeypatch.setattr(ra, "peak_extractor", lambda u, n: '{"peaks": []}')
    monkeypatch.setattr(ra, "_container_duration", lambda u, timeout=4.0: 12.0)
    monkeypatch.setattr(ra, "_fetch_json", lambda u, timeout=5.0: None)

    async def run():
        payload = {"prompt": "Render: https://cdn/artifacts/external/only.mp4", "session_id": "s"}
        async for _ in ra._render_deterministic(payload, {}):
            pass
    asyncio.new_event_loop().run_until_complete(run())
    assert cap.get("template") != "clip-gallery", cap.get("template")


_REPORTS = ["https://cdn/projects/p1/sessions/s/output/compliance_report.pdf",
            "https://cdn/projects/p1/sessions/s/output/nba_compliance_report.pdf",
            "https://cdn/projects/p1/sessions/s/output/tidal_line_compliance_report.pdf"]


def test_every_pdf_of_a_render_is_in_the_document_manifest(monkeypatch):
    """Staging session d6a560a8: three per-video compliance PDFs were rendered together and only
    doc_urls[0] reached the manifest ("2 candidate(s) not rendered")."""
    monkeypatch.setattr(ra, "_write_manifest_to_s3", lambda mj, sid, mid: f"k/{mid}.json")
    ra._RENDER_WRITES.clear()
    out = json.loads(ra.build_render_manifest(
        candidate_urls=json.dumps(_REPORTS), manifest_id="mid-reports", session_id="s"))
    assert out["template"] == "document-viewer"
    m = ra._RENDER_WRITES["mid-reports"]["manifest"]
    assert m["document"]["previewUrl"] == _REPORTS[0]  # older viewers read only this
    assert [d["previewUrl"] for d in m["documents"]] == _REPORTS
    assert [d["title"] for d in m["documents"]] == [u.rsplit("/", 1)[-1] for u in _REPORTS]
    assert ra._dropped_renderable_candidates(_REPORTS, m) == []


def test_the_deterministic_path_also_keeps_every_pdf(monkeypatch):
    import asyncio
    cap = {}

    def _cap(mj, sid, mid):
        cap.clear(); cap.update(mj if isinstance(mj, dict) else json.loads(mj)); return "k.json"
    monkeypatch.setattr(ra, "_write_manifest_to_s3", _cap)
    if hasattr(ra, "_find_mvhd"):
        monkeypatch.setattr(ra, "_find_mvhd", lambda *a, **k: None)

    async def run():
        payload = {"prompt": "Render these reports: " + " ".join(_REPORTS), "session_id": "s"}
        async for _ in ra._render_deterministic(payload, {}):
            pass
    asyncio.new_event_loop().run_until_complete(run())
    assert cap.get("template") == "document-viewer"
    assert [d["previewUrl"] for d in cap["documents"]] == _REPORTS
    assert not any("not rendered" in f for f in cap.get("reviewFindings") or [])


def test_a_single_pdf_manifest_is_unchanged():
    m = ra._build_documents_manifest([_REPORTS[0]], "m1")
    assert "documents" not in m and m["document"]["previewUrl"] == _REPORTS[0]


# ---------------------------------------------------------------------------
# _validate_manifest_json — a dead analysis node must NOT be able to write an invalid TIMELINE
# manifest (staging session 12f6f088): sg4 conversation-analysis produced no usable timeline yet
# emitted duration=0 with an overlay track carrying NO start/end, which the old validator (checking
# start/end only when BOTH present, and with no duration floor) waved through. The UI then threw the
# hard "Field duration must be >= 0.001" / "tracks[0].start is missing" error. Hardened validation
# rejects that shape so the agentic path falls back to a clean error manifest; the valid sibling renders.
# ---------------------------------------------------------------------------

_CREATED = "2026-10-09T00:00:00.000Z"


def test_validate_rejects_the_malformed_sg4_timeline():
    # The EXACT bad shape read from S3: duration 0, a single overlay track with no start/end.
    bad = json.dumps({
        "id": "736211b1-9fbe-4b42-85b4-82fe6536db4e",
        "duration": 0,
        "tracks": [{"id": "conversation-analysis-track", "type": "overlay",
                    "src": "https://cdn/conversation_analysis.json", "label": "Conversation Analysis",
                    "metadata": {"analysis_type": "content_moderation"}}],
        "clips": [],
        "createdAt": _CREATED,
        "errors": [{"code": "SOURCE_VIDEO_UNREACHABLE", "message": "HTTP 403"}],
    })
    ok, errors = ra._validate_manifest_json(bad)
    assert ok is False
    joined = " ".join(errors)
    assert "duration" in joined and "start/end" in joined


def test_validate_rejects_duration_zero_even_with_a_valid_track():
    bad = json.dumps({
        "id": "x", "duration": 0, "clips": [], "createdAt": _CREATED,
        "tracks": [{"id": "t", "type": "video", "src": "u", "label": "l", "start": 0.0, "end": 10.0}],
    })
    ok, errors = ra._validate_manifest_json(bad)
    assert ok is False
    assert any("duration" in e for e in errors)


def test_validate_rejects_track_missing_start_or_end():
    bad = json.dumps({
        "id": "x", "duration": 300.167, "clips": [], "createdAt": _CREATED,
        "tracks": [{"id": "t", "type": "video", "src": "u", "label": "l"}],
    })
    ok, errors = ra._validate_manifest_json(bad)
    assert ok is False
    assert any("start/end" in e for e in errors)


def test_validate_accepts_the_valid_sg3_timeline():
    # The good sibling (sg3_enrich_shot_metadata): duration 300.167, track with start/end.
    good = json.dumps({
        "id": "736211b1-sg3", "duration": 300.167, "clips": [], "createdAt": _CREATED,
        "template": "nle-timeline", "renderSkill": "render-generic",
        "tracks": [{"id": "video-nba", "type": "video", "src": "https://cdn/nba.mp4", "label": "nba",
                    "start": 0.0, "end": 300.167}],
    })
    ok, errors = ra._validate_manifest_json(good)
    assert ok is True, errors


def test_validate_accepts_the_error_and_generic_manifests_with_floor_duration():
    # _build_error_manifest / _build_generic_manifest use duration 0.001 (the floor) + empty tracks;
    # the floor check must NOT reject them.
    for mk in (ra._build_error_manifest("X", "msg"), ra._build_generic_manifest(["https://cdn/x.bin"], "mid")):
        ok, errors = ra._validate_manifest_json(json.dumps(mk))
        assert ok is True, errors
