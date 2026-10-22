"""Role is derived from the artifact's PATH, not from the `s3://` scheme.

Why this matters
----------------
`extract_source_uris` decides whether a url is a SOURCE (context for the render — draw the
boxes over the video they describe) or a DELIVERABLE (gets its own tab). Today the decision
requires the value to start with ``s3://``:

    if not (isinstance(uri, str) and uri.startswith("s3://")): return

That is a scheme test standing in for a role test, and it fails in exactly the direction the
system is moving. Artifact references are becoming canonical CloudFront https urls; the moment
an input arrives as ``https://<cdn>/artifacts/<sid>/input/clip.mp4`` it is no longer recognised
as a source, so it is classified as an OUTPUT and renders its own spurious tab. The local E2E
harness hit precisely this (case 2 had to be fed an ``s3://`` uri to behave).

Uploads now land under ``artifacts/<session>/input/`` (`ui/server/uploadKey.ts`), which makes
role a *structural* property of the location: ``/input/`` is a source, ``/output/`` is a
deliverable. This pins that:

  1. an https artifact url in a source-named field is a source (scheme-independence)
  2. an ``/input/`` url is a source even with NO source-named field (path-derived)
  3. an ``/output/`` url is NOT dragged in as a source by a source-ish field name
  4. the existing ``s3://`` behaviour is unchanged (no regression)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hooks.provider import extract_source_uris  # noqa: E402

CDN = "https://dexamplecdn002.cloudfront.net"
SESS = "sess-1"
INPUT_URL = f"{CDN}/artifacts/{SESS}/input/1787-clip.mp4"
OUTPUT_URL = f"{CDN}/artifacts/{SESS}/output/plan-1/node-1/result.mp4"


def _result(payload: dict) -> dict:
    """Shape a tool result the way `_content_dicts` walks it."""
    import json
    return {"content": [{"text": json.dumps(payload)}]}


def test_https_artifact_url_in_a_source_field_is_a_source():
    # Scheme-independence: the same payload with s3:// already works.
    got = extract_source_uris(_result({"input": {"s3uri": INPUT_URL}, "status": "SUCCEEDED"}))
    assert got == [INPUT_URL], got


def test_input_path_is_a_source_even_without_a_source_named_field():
    # Path-derived role. `media_url` carries no input/source/asset token, so under the old
    # rule this was a deliverable purely because of what the field was called.
    got = extract_source_uris(_result({"media_url": INPUT_URL}))
    assert got == [INPUT_URL], got


def test_a_field_name_outranks_the_path_for_a_chained_node():
    """An `/output/` url named as this tool's input IS a source — do not reject it on path.

    These are two DIFFERENT questions and conflating them loses real source context:
      * the FIELD NAME states the url's role *relative to this tool* ("this is what I ran on")
      * the PATH states its role *relative to the session* ("the user gave us this" vs
        "the pipeline made this")
    In a DAG, node B's input is node A's output, so a chained node's source media legitimately
    lives under `/output/`. Rendering wants the tool-relative answer, so the field name wins
    where it exists and the path is only a fallback for fields that say nothing.
    """
    got = extract_source_uris(_result({"source_url": OUTPUT_URL}))
    assert got == [OUTPUT_URL], got


def test_path_inference_promotes_only_input_never_output():
    # The fallback is deliberately one-directional: with no field-name signal, only `/input/`
    # is claimed. An `/output/` url in a plain field stays a deliverable and keeps its tab —
    # otherwise path inference would silently swallow the very artifacts we exist to render.
    got = extract_source_uris(_result({"media_url": OUTPUT_URL}))
    assert got == [], got


def test_s3_uri_behaviour_is_unchanged():
    s3 = f"s3://bucket/artifacts/1787-clip.mp4"
    got = extract_source_uris(_result({"input": {"s3uri": s3}}))
    assert got == [s3], got


def test_non_media_extension_still_rejected():
    # A JSON analysis file under /input/ is not source MEDIA to overlay.
    got = extract_source_uris(_result({"media_url": f"{CDN}/artifacts/{SESS}/input/notes.json"}))
    assert got == [], got


def test_unrelated_https_url_is_not_a_source():
    # Only OUR artifact urls are eligible; an arbitrary external https must not be
    # silently promoted to source context by a source-named field.
    got = extract_source_uris(_result({"input_url": "https://example.com/somebody/else.mp4"}))
    assert got == [], got


def test_plan_aggregate_still_wins():
    # An aggregate states roles outright; path inference must not override it.
    agg = {
        "schema": "plan-result/v1",
        "nodes": [{
            "node_id": "n1", "status": "completed",
            "artifacts": [
                {"url": INPUT_URL, "role": "source"},
                {"url": OUTPUT_URL, "role": "output"},
            ],
        }],
    }
    got = extract_source_uris(_result(agg))
    assert got == [INPUT_URL], got
