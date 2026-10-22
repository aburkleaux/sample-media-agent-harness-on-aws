"""``ingestion.rendition`` (Req 8.5, 8.6, 8.13 rendition property) and ``ingestion.image_description``."""
from __future__ import annotations

import base64
import io
import json
import os
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ingestion import image_description as ID  # noqa: E402
from ingestion import rendition as R  # noqa: E402


def encode(im: Image.Image, fmt: str, **kw) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format=fmt, **kw)
    return buf.getvalue()


def decode(b: bytes) -> Image.Image:
    im = Image.open(io.BytesIO(b))
    im.load()
    return im


@settings(max_examples=40, deadline=None)
@given(st.integers(1, 4000), st.integers(1, 4000), st.sampled_from(["PNG", "JPEG", "WEBP", "GIF"]))
def test_rendition_property(w, h, fmt):
    src = encode(Image.new("RGB", (w, h), (10, 120, 200)), fmt)
    out = R.render(src)
    im = decode(out.jpeg)
    assert im.format == "JPEG" and im.size == (out.width, out.height)
    assert max(im.size) <= min(R.MAX_SIDE, max(w, h))          # no upscaling, ≤ 1,568
    assert len(out.jpeg) <= R.MAX_BYTES
    # aspect ratio within 1 px: the other side is what the long side's scale implies, ±1
    scale = max(im.size) / max(w, h)
    assert abs(im.size[0] - w * scale) <= 1 and abs(im.size[1] - h * scale) <= 1


def test_transparency_composited_on_white():
    im = Image.new("RGBA", (40, 40), (255, 0, 0, 0))
    out = decode(R.render(encode(im, "PNG")).jpeg)
    r, g, b = out.convert("RGB").getpixel((20, 20))
    assert min(r, g, b) > 245


def test_exif_orientation_applied():
    im = Image.new("RGB", (200, 100), (0, 0, 0))
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° CW on display
    out = R.render(encode(im, "JPEG", exif=exif.tobytes()))
    assert (out.width, out.height) == (100, 200)


def test_noisy_image_steps_quality_down_and_fits():
    import random
    rnd = random.Random(1)
    im = Image.frombytes("RGB", (1568, 1568), bytes(rnd.getrandbits(8) for _ in range(1568 * 1568 * 3)))
    out = R.render(encode(im, "PNG"))
    assert len(out.jpeg) <= R.MAX_BYTES


@pytest.mark.parametrize("src", [b"", b"not an image", b"\x89PNG\r\n\x1a\n" + b"\x00" * 20])
def test_undecodable(src):
    with pytest.raises(R.RenditionError, match="cannot be decoded"):
        R.render(src)


def test_pixel_limit(monkeypatch):
    monkeypatch.setattr(R, "MAX_PIXELS", 100)
    with pytest.raises(R.RenditionError, match="decode limit"):
        R.render(encode(Image.new("RGB", (20, 20)), "PNG"))


# ------------------------------------------------------------------------------------- description
def test_request_is_fixed_but_for_record_id_and_bytes():
    a = ID.request_arguments("image:1", b"\xff\xd8a", ["m1"])
    b = ID.request_arguments("image:2", b"\xff\xd8b", ["m1"])
    assert a["records"][0]["record_id"] == "image:1"
    img = a["records"][0]["messages"][0]["content"][1]["image"]
    assert img["format"] == "jpeg" and base64.b64decode(img["source"]["bytes"]) == b"\xff\xd8a"
    strip = lambda x: json.loads(json.dumps(x).replace("image:1", "X").replace("image:2", "X")  # noqa: E731
                                 .replace(img["source"]["bytes"], "B")
                                 .replace(b["records"][0]["messages"][0]["content"][1]["image"]["source"]["bytes"], "B"))
    assert strip(a) == strip(b)
    assert (a["use_batch"], a["temperature"], a["model_chain"]) == (False, 0, ["m1"])
    assert "model_chain" not in ID.request_arguments("image:1", b"x", [])
    assert ID.request_arguments("image:1", b"x", [], "r2-ab")["idempotency_token"] == "r2-ab"
    assert "thinking" not in a
    assert ID.request_arguments("image:1", b"x", ["anthropic.claude-sonnet-5-5"], None, "adaptive")["thinking"] == "adaptive"


def test_config_from_env_reads_the_image_model_and_thinking(monkeypatch):
    from ingestion.handlers import tick
    monkeypatch.setenv("INGESTION_CONFIG", json.dumps({"imageModelChain": ["anthropic.claude-sonnet-5-5"],
                                                       "imageThinking": "adaptive"}))
    cfg = tick.config_from_env()
    assert (cfg.image_model_chain, cfg.image_thinking) == (("anthropic.claude-sonnet-5-5",), "adaptive")
    monkeypatch.setenv("INGESTION_CONFIG", "{}")
    assert (tick.config_from_env().image_model_chain, tick.config_from_env().image_thinking) == ((), "")


@pytest.mark.parametrize("text,ok", [
    ('{"description": "A cat."}', True),
    ('```json\n{"description": "A cat.", "objects": ["cat"]}\n```', True),
    ('Here you go: {"description": "A cat."}', True),
    ('{"description": "  "}', False),
    ('{"objects": ["cat"]}', False),
    ("[1, 2]", False),
    ("no json", False),
])
def test_parse_response_line(text, ok):
    obj, why = ID.parse_response_line([{"record_id": "other"},
                                       {"record_id": "image:1", "status": "SUCCESS", "output_text": text}], "image:1")
    assert (obj is not None) is ok and (why == "") is ok


def test_parse_failed_record_and_missing():
    obj, why = ID.parse_response_line([{"record_id": "image:1", "status": "FAILED", "reason": "content_filtered"}],
                                      "image:1")
    assert obj is None and "content_filtered" in why
    assert ID.parse_response_line([], "image:1")[0] is None


def test_image_capable():
    assert ID.image_capable({"model": {"model_type": ["vision", "text"], "capabilities": {"image": {}}}})
    assert ID.image_capable({"model": {"model_type": ["text"], "capabilities": {"image": {"limits": {}}}}})
    assert not ID.image_capable({"model": {"model_type": ["text"], "capabilities": {}}})
