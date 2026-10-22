"""render_types is the single source of truth for renderable types. These tests
lock the mime-first behavior and prove all agent-side layers derive from it, so a
new type is one registration rather than a patch per module."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import render.types as rt  # noqa: E402


def test_mime_for_is_declared_first_then_extension():
    # Producer-declared mime / Content-Type wins over the extension guess.
    assert rt.mime_for("x.bin", declared="image/png") == "image/png"
    # Extension fallback when nothing declared.
    assert rt.mime_for("https://cdn/artifacts/snake_game.html") == "text/html"
    assert rt.mime_for("s3://b/out/audio_0_0.wav") == "audio/wav"
    assert rt.mime_for("x.unknownext") is None


def test_is_renderable_mime_first():
    assert rt.is_renderable("game.html") is True
    assert rt.is_renderable("clip.mp4") is True
    assert rt.is_renderable("notes.txt") is False          # text/* is NOT blanket-renderable
    # A declared renderable mime with an unknown extension still renders.
    assert rt.is_renderable("blob", declared="image/png") is True


def test_render_category():
    assert rt.render_category("video/mp4") == "video"
    assert rt.render_category("audio/wav") == "audio"
    assert rt.render_category("image/png") == "image"
    assert rt.render_category("application/pdf") == "document"
    assert rt.render_category("text/html") == "document"
    assert rt.render_category("application/json") == "data"


def test_derived_views_follow_the_map():
    # Every renderable extension's mime is actually renderable, and the category
    # sets exactly partition the video/audio/image mimes — no hand-maintained list.
    for ext in rt.RENDERABLE_EXTS:
        assert rt._mime_is_renderable(rt.MIME_BY_EXT[ext])
    assert rt.VIDEO_EXTS == {e.lstrip(".") for e, m in rt.MIME_BY_EXT.items() if m.startswith("video/")}
    assert rt.AUDIO_EXTS == {e.lstrip(".") for e, m in rt.MIME_BY_EXT.items() if m.startswith("audio/")}
    assert rt.IMAGE_EXTS == {e.lstrip(".") for e, m in rt.MIME_BY_EXT.items() if m.startswith("image/")}


def test_all_agent_layers_share_the_one_registry():
    # Anti-drift: provider (hook gate) and render_agent (classifier) point at the
    # SAME registry objects. The poller uses a vendored copy (test_render_types_parity).
    from hooks import provider
    import render_agent
    assert provider._MIME_BY_EXT is rt.MIME_BY_EXT
    assert provider._RENDERABLE_EXTS is rt.RENDERABLE_EXTS
    assert render_agent._VIDEO_EXTS is rt.VIDEO_EXTS
    assert render_agent._AUDIO_EXTS is rt.AUDIO_EXTS
    assert render_agent._IMAGE_EXTS is rt.IMAGE_EXTS
    assert render_agent._EXT_MIME is rt.EXT_MIME_NODOT


def test_html_renders_across_the_stack():
    # The .html regression, now guaranteed by the shared registry: it is renderable
    # at the hook gate AND classified as a document by the render agent.
    assert ".html" in rt.RENDERABLE_EXTS
    assert rt.render_category(rt.mime_for("game.html")) == "document"
