"""When pruning leaves NO deliverable, render nothing — do not emit an empty tab.

This is the deterministic slice of "intent filtering": the case where a result carries output
that does not need rendering at all. It is worth being precise about why only this slice is
implemented.

`build_render_manifest` already prunes, declaratively and without an LLM:

  * SOURCE/INPUT urls are dropped — a source is a canvas for projections, never a track
  * `subsumes:` drops siblings a richer deliverable covers (facegroup.json over facemetadata)
  * `intermediate_node_ids` drops plumbing nodes (a transcode that only feeds other nodes)
  * `deliverable:` picks WHICH file per skill

Both of the list-pruners deliberately refuse to prune to nothing (`_apply_subsumes` "degrades to
a no-op if it would drop everything"; `intermediate_node_ids` fails open on drop-all). That is
the right default for an ambiguous prune. But the SOURCE drop is not ambiguous: if every
candidate was a source, there is definitively nothing to show, and the correct output is zero
manifests.

Today that produces the opposite. With `cands` empty, `_manifest_id_for([])` hits its
"no urls" branch and returns a RANDOM uuid, so the builder writes an empty manifest under a
non-convergent key. That is a spurious tab, and because the id is random it cannot even be
overwritten idempotently — every retry adds another.

Deliberately NOT done here: LLM/agent intent matching to decide which of several legitimate
outputs a user "meant". It is nondeterministic on structurally identical input — the same
property that made content-based view selection produce three different skills for three
identical shot-segmentation jobs — and there is no captured reproduction demanding it. The
declarative pruners above cover the cases the live sessions actually exhibited.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import render_agent  # noqa: E402

SRC = "https://cdn.cloudfront.net/artifacts/sess-1/input/1787-clip.mp4"
OUT = "https://cdn.cloudfront.net/artifacts/sess-1/output/plan-1/n1/shots.json"


def _call(cands, srcs, monkeypatch):
    """Run the builder with S3 writes captured rather than performed."""
    written: list = []
    monkeypatch.setattr(render_agent, "_write_manifest_to_s3",
                        lambda body, sid, mid: written.append((mid, body)) or f"artifacts/{sid}/render/{mid}.json",
                        raising=False)
    out = render_agent.build_render_manifest(
        json.dumps(cands), "", session_id="sess-1", source_urls=json.dumps(srcs))
    return out, written


def test_every_candidate_was_a_source_renders_nothing(monkeypatch):
    # The unambiguous case: the only "output" is the input the tool ran on.
    out, written = _call([SRC], [SRC], monkeypatch)
    assert not written, f"wrote an empty manifest anyway: {written!r}"
    # ...and it must SAY so, rather than returning something the caller reads as a manifest.
    assert "s3_key" not in out or "no" in out.lower(), out


def test_a_real_deliverable_alongside_a_source_still_renders(monkeypatch):
    # The guard must not fire whenever a source is merely present.
    out, written = _call([OUT, SRC], [SRC], monkeypatch)
    assert written, "a genuine deliverable must still render"


def test_no_candidates_at_all_renders_nothing(monkeypatch):
    out, written = _call([], [], monkeypatch)
    assert not written, f"wrote a manifest for zero candidates: {written!r}"


def test_the_empty_case_does_not_mint_a_random_id(monkeypatch):
    """Two identical no-op renders must not accumulate tabs.

    `_manifest_id_for([])` returns a fresh uuid, so before the guard each retry wrote a NEW
    key — an empty tab that could not even be overwritten idempotently.
    """
    _, w1 = _call([SRC], [SRC], monkeypatch)
    _, w2 = _call([SRC], [SRC], monkeypatch)
    assert not w1 and not w2
