"""media-references guidance pins.

The planner does not read skill BODIES -- it sees name + a truncated description -- so the
routing-critical guidance has to be pinned by test rather than trusted to be read.

This skill was standardized on **MiniMax H3 Max** (primary video, text+image→video) with **Kling
v3 Pro** as the second pick, and ALL combining (mux/concatenate/crossfade) routed to ffmpeg in
`code_interpreter` per the `video-processing` skill. seedance and the fal `ffmpeg-api/*` mux path
were removed, as was the content-moderation guidance. These tests pin that state.
"""
from __future__ import annotations

from pathlib import Path

SKILL = (Path(__file__).resolve().parents[1] / "skills" / "media-references" / "SKILL.md")


def _text() -> str:
    return SKILL.read_text()


# --- Video model shortlist: MiniMax H3 Max primary, Kling v3 Pro second -----------------

def test_video_models_are_minimax_h3_max_and_kling_v3_pro():
    t = _text()
    assert "minimax/h3-max/text-to-video" in t
    assert "minimax/h3-max/image-to-video" in t
    assert "fal-ai/kling-video/v3/pro/text-to-video" in t
    assert "fal-ai/kling-video/v3/pro/image-to-video" in t


def test_no_seedance_anywhere():
    """Standardized off seedance — it must not linger and reintroduce a conflicting default."""
    assert "seedance" not in _text().lower()


def test_image_to_video_names_its_field_per_model():
    """MiniMax h3-max takes image_url (optional), Kling takes start_image_url. Falling back
    without changing the field is a silent unconditioned generation that ignores the image."""
    t = _text()
    assert "minimax/h3-max/image-to-video" in t
    assert "`image_url`" in t and "`start_image_url`" in t


def test_minimax_h3_max_has_no_native_audio_so_plan_music_separately():
    """h3-max outputs video only (fal schema) — the seedance 'native audio → one node' rule does
    NOT carry over; music is a separate step muxed in via code_interpreter."""
    t = _text()
    assert "no native audio" in t.lower() or "no audio" in t.lower()
    assert "generate_audio" in t  # kling's opt-in native audio is the one-call exception


# --- Combining is code_interpreter / video-processing, NOT fal --------------------------

def test_combining_routes_to_code_interpreter_via_video_processing():
    t = _text()
    assert "video-processing" in t
    assert "code_interpreter" in t
    # The old fal ffmpeg-api mux/concat endpoints must be gone.
    assert "fal-ai/ffmpeg-api/merge-audio-video" not in t
    assert "fal-ai/ffmpeg-api/merge-videos" not in t


def test_m2c_process_media_is_ruled_out_for_muxing():
    t = _text().lower()
    assert "process_media" in t
    assert "cannot mux" in t or "cannot do this" in t


def test_the_company_name_collision_is_called_out():
    """So the next person researching "fal mux" does not conclude it is unsupported."""
    assert "Mux the company" in _text()


# --- Content moderation guidance removed ------------------------------------------------

def test_content_filter_guidance_is_removed():
    t = _text().lower()
    assert "content_policy_violation" not in t
    assert "content-policy" not in t and "content policy" not in t


# --- Still-valid, model-agnostic pins ---------------------------------------------------

def test_every_at_risk_skill_says_what_it_is_NOT_for():
    """Routing needs both halves, and the planner only sees the (truncated) description."""
    from pathlib import Path
    import re

    skills = Path(__file__).resolve().parents[1] / "skills"
    render_ref = {"render-face-people": "face-people", "render-face-recognition": "face-recognition"}
    for name in ("media-references", "video-processing",
                 "render-face-people", "render-face-recognition"):
        if name in render_ref:
            body = (skills / "render-media" / "references" / f"{render_ref[name]}.md").read_text()
        else:
            body = (skills / name / "SKILL.md").read_text()
        m = re.search(r"^description:\s*(.+?)(?=\n[a-z_]+:|\n---)", body, re.S | re.M)
        assert m, name
        desc = " ".join(m.group(1).split())
        assert "NOT for" in desc or "NOT to" in desc, f"{name} never says what it is not for"
        cut = desc.index("NOT ")
        assert cut < 420, f"{name}: NOT clause at {cut}, beyond what the planner sees"


def test_native_audio_one_node_rule_is_in_the_description():
    """The planner reads only the description. It must carry the h3-max-no-audio default AND the
    native-audio (kling) one-node exception, or the planner mis-plans music+mux."""
    import re
    from pathlib import Path
    body = (Path(__file__).resolve().parents[1] / "skills" / "media-references" / "SKILL.md").read_text()
    m = re.search(r'^description:\s*(.+?)(?=\n[a-z_]+:|\n---)', body, re.S | re.M)
    desc = " ".join(m.group(1).split())
    assert "native audio" in desc and "ONE node" in desc
    assert "music+mux" in desc.lower() or "separate music" in desc.lower()


def test_the_listed_edit_models_need_no_schema_lookup():
    t = _text()
    assert "No schema lookup for either pick" in t
    assert "61 seconds" in t


def test_the_reference_image_must_be_https_not_s3():
    t = _text()
    assert "never `s3://`" in t
    assert "dispatch ONCE" in t


def test_the_param_hygiene_rule_is_near_the_top_not_buried():
    body = _text().split("---", 2)[-1]
    pos = body.find("Never guess a tuning parameter")
    assert pos >= 0, "param rule missing"
    assert pos < len(body) * 0.15, f"param rule at {pos}/{len(body)} -- too far down"


def test_the_two_plan_killing_rules_lead_the_body():
    body = _text()
    assert "Generation rules that fail plans" in body
    lead = body[:body.find("## Never put file content")] if "## Never put file content" in body else body[:3000]
    assert "num_inference_steps" in lead
    assert "MiniMax H3 Max" in lead
