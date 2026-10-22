"""Unit tests for the invoker SSE parser (tests/evaluation/invoker.py)."""
import json

from tests.evaluation.invoker import parse_sse_stream


def _sse(*events: dict) -> str:
    return "\n".join(f"data: {json.dumps(e)}" for e in events)


def test_accumulates_text():
    body = _sse({"type": "text", "text": "Hello "}, {"type": "text", "text": "world"})
    out = parse_sse_stream(body)
    assert out.response_text == "Hello world"


def test_extracts_manifest_from_step():
    body = _sse(
        {"type": "step", "label": "↳ Rendered artifacts", "agent": "render",
         "render_manifest_key": "artifacts/x/render/abc.json"},
    )
    out = parse_sse_stream(body)
    assert out.manifest_keys == ["artifacts/x/render/abc.json"]
    assert len(out.steps) == 1


def test_extracts_output_ref_and_render_manifest():
    body = _sse(
        {"type": "output_ref", "s3_key": "artifacts/y/render/m.json"},
        {"type": "output_ref", "s3_key": "artifacts/y/data.csv"},
    )
    out = parse_sse_stream(body)
    assert "artifacts/y/render/m.json" in out.output_refs
    assert out.manifest_keys == ["artifacts/y/render/m.json"]  # only the render json


def test_skips_invalid_json_lines():
    body = "data: {not json}\n" + _sse({"type": "text", "text": "ok"})
    out = parse_sse_stream(body)
    assert out.response_text == "ok"


def test_dedups_manifest_keys():
    body = _sse(
        {"type": "step", "render_manifest_key": "k1"},
        {"type": "step", "render_manifest_key": "k1"},
    )
    assert parse_sse_stream(body).manifest_keys == ["k1"]


def test_handles_bytes_body():
    body = _sse({"type": "text", "text": "bytes"}).encode("utf-8")
    assert parse_sse_stream(body).response_text == "bytes"


def test_double_encoded_json_string():
    # Some producers emit data: "<json-string>"
    inner = json.dumps({"type": "text", "text": "nested"})
    body = f"data: {json.dumps(inner)}"
    assert parse_sse_stream(body).response_text == "nested"
