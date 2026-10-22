"""Contract test: render-media renders a SET of images as ONE gallery.

Regression guard for the "generally a challenge to get the renderer to render the images"
UX gap (session 684bec82): asking to render N already-existing frames "together" repeatedly
failed because the skill's only image guidance was single (`image -> image-annotated -> the
image`, "render only its deliverable"), so the Render Agent picked one image or the agent
stitched a contact sheet. The assembler already galleries N images (see
test_build_render_manifest.test_image_manifest_renders_all_images); the skill must invoke it.
"""

from pathlib import Path

SKILL_MD = Path(__file__).parents[1] / "skills" / "render-media" / "SKILL.md"
MAIN_PY = Path(__file__).parents[1] / "src" / "prompts" / "orchestrator.py"


def test_render_media_renders_multiple_images_as_one_gallery() -> None:
    content = SKILL_MD.read_text(encoding="utf-8")
    # A set of images (a frame gallery, several generated options) renders ALL of them...
    assert "MULTIPLE images given" in content
    assert "render ALL of them" in content
    # ...as one image-annotated gallery, never one-picked and never stitched into a contact sheet.
    assert "image-annotated" in content
    assert "contact-sheet image" in content


def test_orchestrator_prompt_renders_existing_artifacts_as_one_gallery() -> None:
    # The main orchestrator prompt (_build_system_prompt) needs a live gateway to call, so assert
    # against the source literals (same approach as test_custom_agent_tool_registration). The
    # prompt must give a first-class path to (re)render EXISTING artifacts as one gallery tab via
    # a single dispatch_tool(target=render) call — not markdown-only inline images, not a stitch.
    src = MAIN_PY.read_text(encoding="utf-8")
    assert "dispatch_tool(target='render')" in src
    assert "ONE gallery tab holding the whole set" in src
    assert "contact-sheet image" in src

