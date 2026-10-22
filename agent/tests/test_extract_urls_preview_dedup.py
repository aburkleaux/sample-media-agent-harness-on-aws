"""extract_urls must not harvest preview/thumbnail URLs — they are downscaled copies of an
original that is already present, so including them renders each generated image twice
(session 9aabc7ce: a fal image .jpg/.png plus its .webp preview as two gallery tiles).
"""
import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from hooks.provider import extract_urls  # noqa: E402

CDN = "https://dexamplecdn002.cloudfront.net/artifacts/external"


def _text_result(payload: dict) -> dict:
    return {"content": [{"text": json.dumps(payload)}]}


def test_fal_preview_url_is_not_harvested_as_a_second_image():
    # Real fal run_model result shape (from session 9aabc7ce).
    payload = {
        "status": "completed",
        "result": {"images": [{"url": f"{CDN}/fd11547fea0817b8d8a129c0.jpg"}]},
        "media": [{"url": f"{CDN}/fd11547fea0817b8d8a129c0.jpg", "content_type": "image/jpeg"}],
        "previews": [{
            "original_url": f"{CDN}/fd11547fea0817b8d8a129c0.jpg",
            "preview_url": f"{CDN}/a531f2e4bde60b4ddcab825c.webp",
            "mime_type": "image/webp",
        }],
    }
    urls = extract_urls(_text_result(payload))
    assert f"{CDN}/fd11547fea0817b8d8a129c0.jpg" in urls, "the original image must be harvested"
    assert f"{CDN}/a531f2e4bde60b4ddcab825c.webp" not in urls, "the preview thumbnail must NOT be harvested"
    # Exactly one URL for one logical image (original only).
    assert urls == [f"{CDN}/fd11547fea0817b8d8a129c0.jpg"]


def test_preview_dropped_in_structured_json_block_too():
    result = {"content": [{"json": {
        "media": [{"url": f"{CDN}/718e596a816576187728af00.png"}],
        "previews": [{"original_url": f"{CDN}/718e596a816576187728af00.png",
                      "preview_url": f"{CDN}/7be168713816c982ce093eaf.webp"}],
    }}]}
    urls = extract_urls(result)
    assert f"{CDN}/718e596a816576187728af00.png" in urls
    assert f"{CDN}/7be168713816c982ce093eaf.webp" not in urls


def test_non_preview_urls_are_unaffected():
    payload = {"media": [{"url": f"{CDN}/a.png"}, {"url": f"{CDN}/b.png"}]}
    urls = extract_urls(_text_result(payload))
    assert set(urls) == {f"{CDN}/a.png", f"{CDN}/b.png"}


def test_plain_text_urls_still_extracted():
    r = {"content": [{"text": f"See {CDN}/plain.png for the result."}]}
    assert extract_urls(r) == [f"{CDN}/plain.png"]
