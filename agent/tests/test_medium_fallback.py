"""Every artifact gets a sane view from its MEDIUM alone — no tool, no content match.

This is the "inherited tool" / "code sandbox" case. A tool we have never seen, or one
whose output is nondeterministic (a code interpreter emitting a PNG one turn and a CSV
the next), cannot be covered by any tool→view map or content-detect rule. MIME can, and
it needs zero registration.

Two defects this pins, both found by probing the real selector:

1. HOLES — `image/png` and `text/csv` resolved to None, so a generated chart or table fell
   through to render-generic. Images are one of the commonest code-sandbox outputs.

2. A DANGEROUS DEFAULT — `application/json` resolved to `render-face-people`, because four
   analysis skills all declare `handles: application/json` and MIME selection took whichever
   sorted first. An unknown tool's JSON was therefore rendered as a FACE view: confidently
   wrong, which is worse than generic.

The rule that fixes (2): a skill declaring `detect:` is SPECIALISED and requires positive
evidence (matching content, or its declared tool). A MIME match is not evidence — five media
tools all emit application/json. Only MEDIUM skills (no `detect:`) are MIME-selectable.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _mime(mime, ext):
    from render_agent import select_render_skill
    s = select_render_skill(mime, ext)
    return (s["name"] if s else None, (s.get("template") if s else None))


# --- holes: every medium must resolve ---------------------------------------

def test_png_resolves_to_an_image_view():
    name, template = _mime("image/png", "png")
    assert name is not None, "a generated PNG must not fall through to render-generic"
    assert template == "image-annotated"


def test_jpeg_resolves_to_an_image_view():
    assert _mime("image/jpeg", "jpg")[1] == "image-annotated"


def test_csv_resolves_to_a_table_view():
    name, template = _mime("text/csv", "csv")
    assert name is not None, "a generated CSV must not fall through to render-generic"
    assert template == "data-table"


def test_the_existing_media_mediums_still_resolve():
    assert _mime("video/mp4", "mp4")[1] == "nle-timeline"
    assert _mime("audio/wav", "wav")[1] == "nle-timeline"
    assert _mime("application/pdf", "pdf")[1] == "document-viewer"


# --- the dangerous default --------------------------------------------------

def test_bare_json_does_not_resolve_to_a_specialised_analysis_view():
    """An unknown tool's JSON must NOT be rendered as a face/shot/speaker view."""
    name, _ = _mime("application/json", "json")
    assert name not in {
        "render-face-people", "render-face-recognition",
        "render-shot-segmentation", "render-speaker-diarization",
    }, f"bare MIME picked a specialised analysis skill: {name}"


def test_specialised_skills_are_never_mime_selectable():
    """The general rule: `detect:` means 'I need proof', and MIME is not proof."""
    from render_agent import select_render_skill, _discover_render_skills
    specialised = {s["name"] for s in _discover_render_skills()
                   if (s.get("declaration") or {}).get("detect")}
    assert specialised, "expected some detect-declaring skills to exist"
    for mime, ext in [("application/json", "json"), ("video/mp4", "mp4"),
                      ("image/png", "png"), ("text/csv", "csv"),
                      ("audio/wav", "wav"), ("application/pdf", "pdf")]:
        s = select_render_skill(mime, ext)
        if s:
            assert s["name"] not in specialised, (
                f"{mime} MIME-selected specialised skill {s['name']}")


# --- specialised skills stay reachable by their real evidence ---------------

def test_specialised_skills_still_reachable_by_tool():
    from render_agent import select_render_skill_by_tool
    assert (select_render_skill_by_tool("shot-segmentation") or {}).get("name") \
        == "render-shot-segmentation"


def test_specialised_skills_still_reachable_by_content():
    from render_agent import select_render_skill_by_content
    picked = select_render_skill_by_content(
        {"shots.json": {"shots": [{"start_time": 0, "end_time": 1, "shot_id": "0"}]}})
    assert picked and picked["name"] == "render-shot-segmentation"
