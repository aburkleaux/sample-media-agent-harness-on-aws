"""Property test for the no-URL invariant (design Property 12).

Covers the two turn-end objects that are stored and shipped to the SPA:

- the CitationBlock, ``TurnOutcome.citations_wire()`` (``turn_end``, Task 6.3);
- the Moments manifest, ``render.moments.build_moments_manifest(...)`` (Task 7.1).

Property 12 (design): *for any* ``TurnOutcome`` and ``MomentSelection`` built from Hits whose
``text``/``title``/``named_entities``/``highlights``/``frame_s3_key`` may quote ``https://`` URLs,
``s3://`` URIs and query strings, and from registry Display_Names:

1. in ``json.dumps(outcome.citations_wire())`` no string except the answer-derived ``text`` and
   ``display_name`` contains ``"://"``;
2. ``build_moments_manifest(...)`` writes no ``src``/``url``/``galleryClips``/``peaks``/``durationHint``
   key;
3. in that manifest no string outside ``text``/``highlight_terms``/``display_name``/``citation``
   contains ``"://"``;
4. no string in either object contains ``X-Amz-Signature``, ``X-Amz-Credential`` or an
   ``https://<host>/projects/`` URL.

How the generators keep the test honest (non-vacuous), and three deliberate reading decisions:

- **URLs are injected where they would actually land.** Hit ``text`` and ``named_entities`` carry
  benign ``https://``/``s3://`` URLs with query strings; these flow verbatim into a Moment's
  ``text`` (manifest, exempt). ``highlights`` carry URLs inside ``<em>`` spans, which flow into
  ``highlight_terms`` (manifest, exempt). Citation ``text``/``display_name`` and the registry
  Display_Name carry URLs too (exempt everywhere a name or answer text appears). So ``"://"`` really
  does occur in the generated objects — the assertions prove it occurs *only* in the exempt fields.

- **The three forbidden markers are injected into the one field that never reaches either object:
  the Hit ``title``.** A Moment and a CitationRecord take their name from the registry, never from
  the Hit's ``title`` (``to_moment`` reads ``ResolvedAsset.display_name``; ``verify`` reads the same),
  so a ``title`` is the one place the property permits the scariest leak markers to live. Clause 4 is
  therefore non-vacuous: every generated ``title`` carries a presigned-URL marker, and we prove none
  of them survive into the wire or the manifest.

- **``frame_s3_key`` and every id are generated as clean identifiers (no ``"://"``).** The
  ``AssetReference`` contract states ``frame_s3_key`` "never a URL" (it is a bucket key the SPA mints
  from), and ingestion guarantees that. The clause-1/clause-3 ``"://"`` check targets *leaked URLs*;
  echoing a malformed key with ``"://"`` back into ``ref`` would be a false positive, not a real
  leak, so the generator keeps keys and ids well-formed and the test proves the implementation adds
  no URL of its own there.

- **``asset_display_name`` is read as a Display_Name.** The wire carries the registry Display_Name
  under both ``display_name`` (as cited) and ``asset_display_name`` (canonical); clause 1 names the
  concept "display_name", so both keys are treated as exempt. No URL-leak detection is lost: a
  display name is never a presigned URL, and the ``ref``/id/key fields that *would* leak one stay in
  the enforced set.

**Validates: Requirements 2.12, 3.26**
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from project_search.assets import ResolvedAsset  # noqa: E402
from project_search.session_hits import SessionHit  # noqa: E402
from project_search.turn_end import TurnOutcome  # noqa: E402
from project_search.verify import AssetReference, CitationRecord  # noqa: E402
from render.moments import (  # noqa: E402
    MomentSelection,
    build_moments_manifest,
    select_moments,
)

ACTIVE_PID = "pX"
OTHER_PIDS = ["pY", "pZ"]
ALL_PIDS = [ACTIVE_PID, *OTHER_PIDS]

# asset_id pool: the `kind:` prefix is a single colon, never "://".
ASSET_IDS = ["video:a1", "image:a2", "audio:a3", "video:a4"]
_KIND_OF = {"video": "video", "image": "image", "audio": "audio"}
STATUSES = ["indexed", "failed", "queued", "analyzing", "indexing", "removed"]

# Fields whose string values the invariant allows to contain "://".
WIRE_ALLOWED = frozenset({"text", "display_name", "asset_display_name"})
MANIFEST_ALLOWED = frozenset({"text", "highlight_terms", "display_name", "citation"})
# Keys clause 2 forbids anywhere in the manifest.
FORBIDDEN_MANIFEST_KEYS = frozenset({"src", "url", "galleryClips", "peaks", "durationHint"})

# Benign URL atoms: contain "://" and a query string, but never a forbidden marker and never a
# "/projects/" path after a host (so clause 4 is clean by construction).
_BENIGN_URLS = [
    "https://example.com/a?x=1",
    "s3://my-bucket/path/to/key?versionId=abc",
    "http://host.test/p?q=2&r=3",
    "https://cdn.example.org/img.png",
    "s3://bucket/key",
]
# Presigned/CDN leak markers. These must only ever be placed in a Hit `title`, the one field that
# never reaches the wire or the manifest.
_LEAK_MARKERS = [
    "https://d111.cloudfront.net/projects/pX/clip.mp4?X-Amz-Signature=deadbeef",
    "X-Amz-Credential=AKIAEXAMPLE/20200101/us-west-2/s3/aws4_request",
    "X-Amz-Signature=0123456789abcdef",
    "https://cdn.example.net/projects/pY/thumb.jpg",
]
# A URL-ish, name-ish atom allowed in Display_Names and answer text (exempt fields).
_NAME_ATOMS = ["clip.mp4", "My Great Video", "weird://name.mov", "https://cdn/x name", "audio-01"]

_PROJECTS_URL_RE = re.compile(r'https?://[^/\s"\\]+/projects/')
# Small, safe filler alphabet that can never spell a forbidden marker.
_filler = st.text(alphabet="abcdefghij KLMNOP0123", max_size=10)
_url_atom = st.sampled_from(_BENIGN_URLS)
_name_atom = st.sampled_from(_NAME_ATOMS)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
@st.composite
def _url_text(draw) -> str:
    """Answer/Hit text that embeds a benign "://" URL between safe filler."""
    return f"{draw(_filler)} {draw(_url_atom)} {draw(_filler)}"


@st.composite
def _named_entities(draw):
    return draw(
        st.one_of(
            _url_atom,
            st.lists(_url_atom, min_size=1, max_size=3),
            st.dictionaries(st.sampled_from(["k1", "k2"]), _url_atom, min_size=1, max_size=2),
        )
    )


@st.composite
def _highlights(draw) -> str:
    """`highlights` markup whose <em> spans extract a "://" term plus a plain term."""
    return f"a <em>{draw(_url_atom)}</em> and <em>plain term</em> b"


@st.composite
def _poison_title(draw) -> str:
    """A Hit title that always carries a forbidden leak marker (and a benign URL)."""
    return f"title {draw(st.sampled_from(_LEAK_MARKERS))} {draw(_url_atom)}"


@st.composite
def _hit(draw) -> dict:
    aid = draw(st.sampled_from(ASSET_IDS))
    start = draw(st.integers(min_value=0, max_value=100_000))
    end = start + draw(st.integers(min_value=1, max_value=5_000))
    score = draw(
        st.one_of(
            st.integers(min_value=0, max_value=1000),
            st.floats(min_value=0, max_value=1000, allow_nan=False, allow_infinity=False),
        )
    )
    hit = {
        "asset_id": aid,
        "start_ms": start,
        "end_ms": end,
        "final_score": score,
        "title": draw(_poison_title()),  # markers live here only; never reaches an output object
    }
    if draw(st.booleans()):
        hit["source_index"] = "shot"
        # frame_s3_key is a clean bucket key (AssetReference contract: never a URL).
        hit["frame_s3_key"] = f"projects/{ACTIVE_PID}/frames/{aid.split(':')[1]}/{start:06d}.jpg"
    else:
        hit["source_index"] = draw(st.sampled_from(["scene", "utterance", "topic"]))
        hit["text"] = draw(_url_text())  # a "://" URL, carried into Moment text (exempt)
        if draw(st.booleans()):
            hit["named_entities"] = draw(_named_entities())
        if draw(st.booleans()):
            hit["highlights"] = draw(_highlights())
    return hit


@st.composite
def _session_hits(draw, *, prefix: str, turn_seq: int):
    hits = draw(st.lists(_hit(), min_size=0, max_size=6))
    out = []
    for i, hit in enumerate(hits):
        out.append(
            SessionHit(
                hit=hit,
                project_id=draw(st.sampled_from(ALL_PIDS)),
                turn_id=f"{prefix}-t{i}",
                tool_use_id=f"{prefix}-tu{i}",
                index=i,
                recency=(turn_seq, 0, -(i + 1)),
            )
        )
    return out


@st.composite
def _assets_map(draw, used_ids):
    """None (registry unreadable), or a map over a subset of used ids, every status.

    Display_Names may carry "://" (an exempt field everywhere a name appears).
    """
    if draw(st.integers(min_value=0, max_value=9)) == 0:
        return None
    out = {}
    for aid in used_ids:
        if draw(st.integers(min_value=0, max_value=9)) == 0:
            continue  # omit -> this id does not resolve, so its Hits are ineligible
        out[aid] = ResolvedAsset(
            asset_id=aid,
            kind=_KIND_OF[aid.split(":")[0]],
            display_name=draw(_name_atom),
            status=draw(st.sampled_from(STATUSES)),
        )
    return out


@st.composite
def _citations(draw, session_hits):
    """A mix of verified records (aligned to a session Hit) and unverified records."""
    recs: list[CitationRecord] = []
    for i in range(draw(st.integers(min_value=0, max_value=4))):
        if session_hits and draw(st.booleans()):
            sh = draw(st.sampled_from(session_hits))
            hit = sh.hit if isinstance(sh.hit, dict) else {}
            aid = hit.get("asset_id") if isinstance(hit.get("asset_id"), str) else "video:a1"
            frame = (
                hit.get("frame_s3_key")
                if hit.get("source_index") == "shot" and isinstance(hit.get("frame_s3_key"), str)
                else None
            )
            recs.append(
                CitationRecord(
                    key=f"k{i}",
                    text=draw(_url_text()),         # answer-derived (exempt)
                    display_name=draw(_name_atom),  # as cited (exempt)
                    start_ms=0,
                    end_ms=1000,
                    verified=True,
                    reason=None,
                    ref=AssetReference(project_id=ACTIVE_PID, asset_id=aid, frame_s3_key=frame),
                    kind=draw(st.sampled_from(["video", "image", "audio"])),
                    asset_display_name=draw(_name_atom),  # registry name (exempt)
                    hit_recency=sh.recency,
                )
            )
        else:
            recs.append(
                CitationRecord(
                    key=f"k{i}",
                    text=draw(_url_text()),
                    display_name=draw(_name_atom),
                    start_ms=0,
                    end_ms=1000,
                    verified=False,
                    reason=draw(st.sampled_from(["no_matching_hit", "registry_unavailable"])),
                )
            )
    return recs


@st.composite
def outcomes(draw) -> TurnOutcome:
    s_hits = draw(_session_hits(prefix="s", turn_seq=1))
    t_hits = draw(_session_hits(prefix="t", turn_seq=2))
    used = sorted(
        {
            sh.hit["asset_id"]
            for sh in (*s_hits, *t_hits)
            if isinstance(sh.hit, dict) and isinstance(sh.hit.get("asset_id"), str)
        }
    )
    assets = draw(_assets_map(used))
    citations = draw(_citations(s_hits))
    return TurnOutcome(
        turn_id="turn",
        project_id=ACTIVE_PID,
        citations=citations,
        session_hits=s_hits,
        turn_hits=t_hits,
        assets=assets,
        truncated=draw(st.booleans()),
    )


# ---------------------------------------------------------------------------
# Assertions (independent of the modules under test)
# ---------------------------------------------------------------------------
def _assert_no_url(node, allowed: bool, allowed_keys: frozenset) -> None:
    """No string reachable under a non-allowed key may contain "://"."""
    if isinstance(node, str):
        if not allowed:
            assert "://" not in node, f"leaked URL in a non-exempt field: {node!r}"
    elif isinstance(node, dict):
        for key, value in node.items():
            _assert_no_url(value, key in allowed_keys, allowed_keys)
    elif isinstance(node, list):
        for item in node:
            _assert_no_url(item, allowed, allowed_keys)


def _assert_no_markers(dumped: str) -> None:
    """Clause 4: the forbidden presigned/CDN markers appear nowhere."""
    assert "X-Amz-Signature" not in dumped
    assert "X-Amz-Credential" not in dumped
    assert _PROJECTS_URL_RE.search(dumped) is None, "a CDN /projects/ URL leaked"


def _all_keys(node) -> set:
    keys: set = set()
    if isinstance(node, dict):
        for key, value in node.items():
            keys.add(key)
            keys |= _all_keys(value)
    elif isinstance(node, list):
        for item in node:
            keys |= _all_keys(item)
    return keys


def _check_wire(outcome: TurnOutcome) -> dict:
    wire = outcome.citations_wire()
    _assert_no_url(wire, allowed=False, allowed_keys=WIRE_ALLOWED)
    _assert_no_markers(json.dumps(wire, ensure_ascii=False))
    return wire


def _check_manifest(manifest: dict) -> None:
    assert FORBIDDEN_MANIFEST_KEYS.isdisjoint(_all_keys(manifest)), (
        f"manifest wrote a forbidden key: {FORBIDDEN_MANIFEST_KEYS & _all_keys(manifest)}"
    )
    _assert_no_url(manifest, allowed=False, allowed_keys=MANIFEST_ALLOWED)
    _assert_no_markers(json.dumps(manifest, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Property 12
# ---------------------------------------------------------------------------
@settings(max_examples=300)
@given(outcomes())
def test_property12_no_url_in_citation_block_or_manifest(outcome: TurnOutcome):
    _check_wire(outcome)

    selection = select_moments(outcome)
    if selection is None:
        return  # no Moments this turn; the wire invariant still had to hold
    assert isinstance(selection, MomentSelection)
    manifest = build_moments_manifest(
        selection, session_id="sess", turn_id="turn", created_at="2026-01-01T00:00:00Z"
    )
    _check_manifest(manifest)


# ---------------------------------------------------------------------------
# Concrete examples pinning the specific clauses (and proving non-vacuity).
# ---------------------------------------------------------------------------
def _sh(hit, *, project_id=ACTIVE_PID, recency):
    return SessionHit(
        hit=hit, project_id=project_id, turn_id="turn", tool_use_id="tu", index=0, recency=recency
    )


def test_cited_scene_moment_keeps_urls_only_in_exempt_fields():
    hit = {
        "asset_id": "video:a1",
        "start_ms": 0,
        "end_ms": 1500,
        "final_score": 0.9,
        "source_index": "scene",
        "text": "see https://example.com/a?x=1 and s3://bucket/key?v=2",
        "highlights": "a <em>https://cdn.example.org/img.png</em> b",
        # The Hit title carries all three leak shapes; it must never surface.
        "title": "ignore X-Amz-Signature=abc https://d1.cloudfront.net/projects/pX/x.mp4"
        " X-Amz-Credential=AKIA/y",
    }
    sh = _sh(hit, recency=(1, 0, -1))
    assets = {"video:a1": ResolvedAsset("video:a1", "video", "weird://name.mp4", "indexed")}
    rec = CitationRecord(
        key="k0",
        text="per https://example.com/a?x=1",
        display_name="weird://name.mp4",
        start_ms=0,
        end_ms=1000,
        verified=True,
        reason=None,
        ref=AssetReference(project_id=ACTIVE_PID, asset_id="video:a1"),
        kind="video",
        asset_display_name="weird://name.mp4",
        hit_recency=(1, 0, -1),
    )
    outcome = TurnOutcome(
        turn_id="turn",
        project_id=ACTIVE_PID,
        citations=[rec],
        session_hits=[sh],
        turn_hits=[sh],
        assets=assets,
        truncated=False,
    )

    wire = _check_wire(outcome)
    selection = select_moments(outcome)
    assert selection is not None and selection.basis == "cited"
    manifest = build_moments_manifest(
        selection, session_id="sess", turn_id="turn", created_at="2026-01-01T00:00:00Z"
    )
    _check_manifest(manifest)

    # Non-vacuity: "://" really is present (in exempt fields), and the markers really were dropped.
    assert "://" in json.dumps(wire)
    assert "://" in json.dumps(manifest)
    assert "X-Amz-Signature" not in json.dumps(manifest)
    assert "cloudfront" not in json.dumps(manifest)


def test_searched_shot_moment_frame_key_is_clean_and_present():
    hit = {
        "asset_id": "image:a2",
        "start_ms": 0,
        "end_ms": 1000,
        "final_score": 7,
        "source_index": "shot",
        "frame_s3_key": "projects/pX/frames/a2/000000.jpg",
        "title": "t https://cdn.example.net/projects/pY/thumb.jpg?X-Amz-Signature=zz",
    }
    sh = _sh(hit, recency=(2, 0, -1))
    assets = {"image:a2": ResolvedAsset("image:a2", "image", "photo.png", "failed")}
    outcome = TurnOutcome(
        turn_id="turn",
        project_id=ACTIVE_PID,
        citations=[],  # no verified citation -> searched basis
        session_hits=[],
        turn_hits=[sh],
        assets=assets,
        truncated=False,
    )

    _check_wire(outcome)
    selection = select_moments(outcome)
    assert selection is not None and selection.basis == "searched"
    manifest = build_moments_manifest(
        selection, session_id="sess", turn_id="turn", created_at="2026-01-01T00:00:00Z"
    )
    _check_manifest(manifest)

    # The frame key is present in the manifest ref and is a clean bucket key.
    item = manifest["galleryMoments"]["items"][0]
    assert item["ref"]["frame_s3_key"] == "projects/pX/frames/a2/000000.jpg"
    assert "://" not in item["ref"]["frame_s3_key"]


def test_registry_unavailable_wire_has_no_url_and_no_manifest():
    rec = CitationRecord(
        key="k0",
        text="see https://example.com/a?x=1",
        display_name="weird://name",
        start_ms=0,
        end_ms=1000,
        verified=False,
        reason="registry_unavailable",
    )
    outcome = TurnOutcome(
        turn_id="turn",
        project_id=ACTIVE_PID,
        citations=[rec],
        session_hits=[],
        turn_hits=[],
        assets=None,  # registry unreadable -> no Moments
        truncated=False,
    )
    _check_wire(outcome)
    assert select_moments(outcome) is None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
