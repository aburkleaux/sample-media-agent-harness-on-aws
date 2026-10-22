"""A duration nobody measured must be published as an estimate, not as fact.

A 5.04s music track was rendered on a ten-minute timeline. The chain: track ends default to a
fixed 600s hint; peak extraction runs only for `.wav`; the container probe reads the MP4/MOV
`mvhd` box. The audio models return `.mp3`, which neither path can measure -- so 600s was
published as though it were the real length, and this looked like a music-generation bug for
days. The music was correct: 5.04s music, 5.0s final mux.

The agent already knew the end was a guess -- it tracked `_endIsHint` -- and then deliberately
stripped the flag before writing the manifest. Publishing it lets the browser, which CAN measure
an mp3 once loaded, replace the estimate.
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "render_agent.py"


def test_the_hint_flag_is_published_not_discarded():
    body = SRC.read_text()
    assert 'if t.pop("_endIsHint", False):' in body
    assert 't["endIsEstimate"] = True' in body
    assert 'manifest["durationIsEstimate"] = True' in body


def test_a_measured_end_is_never_marked_an_estimate():
    """Only tracks whose end came from the hint carry the flag."""
    body = SRC.read_text()
    block = body[body.index("Publish whether an end is measured"):]
    block = block[:block.index("has_video =")]
    # The flag is set inside the pop's truthy branch, so a measured end cannot acquire it.
    assert re.search(r'if t\.pop\("_endIsHint", False\):\s*\n\s*t\["endIsEstimate"\] = True', block)


def test_a_sidecar_duration_still_clears_the_estimate():
    """When a tool reports the real duration, hinted tracks are clamped to it first.

    That happens above this change and must keep happening -- otherwise a shots.json that states
    30.03s would be overridden by a browser measuring the proxy's full length.
    """
    body = SRC.read_text()
    clamp = body.index('if correlated["duration"]:')
    publish = body.index("Publish whether an end is measured")
    assert clamp < publish, "the clamp must run before the flag is published"
    assert 't["_endIsHint"] = False' in body[clamp:publish]
