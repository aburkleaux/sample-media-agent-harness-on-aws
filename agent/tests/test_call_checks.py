"""Property tests for the Search_Tool argument and image-path checks
(media-harness-search-qa-rendering design Component 2, Correctness Properties 3 and 4).

This module hosts the property tests for ``project_search.call_checks``. Property 3 (argument
checks, Req 1.19) lives in the clearly separated ``Property 3`` section below. Property 4
(image-path acceptance, Req 1.20/1.21) is a sibling task and appends its own section; the two
sections share only the import block and the generic generators above the Property 3 marker.

### Property 3: Argument checks

For any arguments dict where ``search_term`` and ``search_image_s3uri`` are absent or any JSON value
(including ``""``), ``k`` is absent or any JSON value (including ``true``, ``0``, ``51``, ``10.0``,
``"10"``), and ``asset_kinds`` is absent or any JSON value (including ``[]``, duplicates, unknown
kinds, a bare string), ``check_args`` returns ``None`` exactly when the Req 1.19 oracle accepts the
arguments. When it rejects them, it returns the message of the first violated rule in the documented
order. A rejected call returns category ``other`` with that message (asserted through ``precheck``).

**Validates: Requirements 1.19**

This is a property-based test (Hypothesis). The expected decision and message are recomputed from the
requirement text by an in-test oracle, independent of ``call_checks.py``.
"""
from __future__ import annotations

import os
import sys

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

# Add agent/src to path so imports resolve (mirrors sibling test modules).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from project_search import errors  # noqa: E402
from project_search.call_checks import CallScope, check_args, precheck  # noqa: E402

_ABSENT = object()

# ---------------------------------------------------------------------------
# Generic JSON-value generators shared by every property in this file.
# ---------------------------------------------------------------------------

# Scalars usable as argument contents. NaN/Infinity are excluded; they are not valid JSON and never
# reach these checks. Strings include "" via st.text(min_size=0, ...).
_json_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-5, max_value=100),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(min_size=0, max_size=12),
)
_json_values = st.recursive(
    _json_scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=3),
        st.dictionaries(st.text(max_size=4), children, max_size=3),
    ),
    max_leaves=5,
)


def _opt(strat):
    """A slot that is either absent (key omitted) or a drawn value."""
    return st.one_of(st.just(_ABSENT), strat)


# ===========================================================================
# Property 3: Argument checks (Req 1.19)
# ===========================================================================

# The exact contract messages, re-stated from Req 1.19 / the design (the oracle's output).
_MSG_ONEOF = "exactly one of search_term and search_image_s3uri must be a non-empty string"
_MSG_K = "k must be an integer from 1 to 50"
_MSG_KINDS = "asset_kinds must be a non-empty list of distinct values from video, image, audio"

# Allowed asset kinds, written from the requirement text (independent of compact.ASSET_KINDS).
_ORACLE_KINDS = ("video", "image", "audio")


def _oracle_check_args(args: dict):
    """Req 1.19, re-stated independently of ``call_checks``.

    Returns the message of the FIRST violated rule, in the documented order
    (one-of search_term/search_image_s3uri → k → asset_kinds), or ``None`` when valid.
    """
    term = args.get("search_term")
    image = args.get("search_image_s3uri")
    term_ok = isinstance(term, str) and term != ""
    image_ok = isinstance(image, str) and image != ""
    # Both or neither non-empty string → reject.
    if term_ok == image_ok:
        return _MSG_ONEOF

    if "k" in args:
        k = args["k"]
        if isinstance(k, bool) or not isinstance(k, int) or not (1 <= k <= 50):
            return _MSG_K

    if "asset_kinds" in args:
        kinds = args["asset_kinds"]
        # Short-circuit matches the contract: a non-list / empty / unknown-member list is rejected
        # before set() is evaluated, so an unhashable member never raises (a dict/list member is
        # already "not in _ORACLE_KINDS").
        if (
            not isinstance(kinds, list)
            or len(kinds) == 0
            or any(kind not in _ORACLE_KINDS for kind in kinds)
            or len(set(kinds)) != len(kinds)
        ):
            return _MSG_KINDS

    return None


# search_term / search_image_s3uri: absent, or any JSON value, biased toward strings and "".
_term_like = st.one_of(st.text(min_size=0, max_size=12), st.just(""), _json_values)

# k: absent, or any JSON value, with the Req-named boundary values pinned into the sample.
_k_like = st.one_of(
    st.sampled_from([True, False, 0, 1, 10, 50, 51, -1, 1000, 10.0, "10", None, [], {}]),
    st.integers(min_value=-5, max_value=60),
    _json_values,
)

# asset_kinds: absent, or any JSON value, with empty / duplicate / unknown / bare-string cases pinned.
_kinds_like = st.one_of(
    st.sampled_from(
        [
            [],
            ["video"],
            ["image"],
            ["audio"],
            ["video", "image", "audio"],
            ["video", "video"],
            ["audio", "audio"],
            ["image", "bogus"],
            ["x"],
            ["video", "image"],
            "video",
            None,
            [1],
            [["video"]],
            {},
        ]
    ),
    st.lists(st.sampled_from(["video", "image", "audio", "x", "bogus", ""]), max_size=5),
    _json_values,
)


@st.composite
def _args(draw):
    args: dict = {}
    term = draw(_opt(_term_like))
    if term is not _ABSENT:
        args["search_term"] = term
    image = draw(_opt(_term_like))
    if image is not _ABSENT:
        args["search_image_s3uri"] = image
    k = draw(_opt(_k_like))
    if k is not _ABSENT:
        args["k"] = k
    kinds = draw(_opt(_kinds_like))
    if kinds is not _ABSENT:
        args["asset_kinds"] = kinds
    return args


def _trivial_scope() -> CallScope:
    """A scope whose contents never matter: for invalid args ``precheck`` rejects on ``check_args``
    before the image-path check runs, so the buckets are left empty and ``frame_keys`` empty."""
    return CallScope(
        session_id="s",
        project_id="p",
        ingest_bucket="",
        proxy_bucket="",
        frame_keys=lambda: frozenset(),
    )


# Feature: media-harness-search-qa-rendering, Property 3: Argument checks
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.data_too_large])
@given(_args())
# Both / neither non-empty string → the one-of message.
@example({})
@example({"search_term": "cats", "search_image_s3uri": "s3://b/k"})
@example({"search_term": "", "search_image_s3uri": ""})
@example({"search_term": ""})
@example({"search_image_s3uri": 123})
# Valid one-of, exercising k boundaries.
@example({"search_term": "cats", "k": 0})
@example({"search_term": "cats", "k": 1})
@example({"search_term": "cats", "k": 50})
@example({"search_term": "cats", "k": 51})
@example({"search_term": "cats", "k": True})
@example({"search_term": "cats", "k": 10.0})
@example({"search_term": "cats", "k": "10"})
# Valid one-of, exercising asset_kinds boundaries.
@example({"search_term": "cats", "asset_kinds": []})
@example({"search_term": "cats", "asset_kinds": ["video", "video"]})
@example({"search_term": "cats", "asset_kinds": ["bogus"]})
@example({"search_term": "cats", "asset_kinds": "video"})
@example({"search_term": "cats", "asset_kinds": ["video", "image", "audio"]})
# Ordering: when both k and asset_kinds are bad, the k message wins (checked before asset_kinds).
@example({"search_term": "cats", "k": 99, "asset_kinds": []})
def test_property3_argument_checks(args):
    expected = _oracle_check_args(args)

    # check_args agrees with the oracle on both the decision and the first-violated message.
    assert check_args(args) == expected

    # A rejected call surfaces category `other` with that same message, through precheck.
    if expected is not None:
        assert precheck(args, _trivial_scope()) == (errors.OTHER, expected)


# ===========================================================================
# Property 4: Image-path acceptance (Req 1.20, 1.21)
# ===========================================================================
#
# For any ``search_image_s3uri`` string and ``CallScope`` (non-empty or empty ``session_id``,
# ``project_id``, ``ingest_bucket`` and ``proxy_bucket``, and a ``frame_keys`` set that mixes keys
# returned under the Active_Project with keys returned only under other Projects),
# ``check_image_uri(uri, scope)`` equals the oracle ``_oracle_is_source(uri, scope)``, re-stated from
# Req 1.20: no ``%``, ``?`` or ``#``; the literal ``s3://`` scheme; a bucket and a key separated by
# ``/``; no empty or ``..`` key segment; and source (a), (b) or (c) with a non-empty remainder after
# the prefix. The generator draws at least 100 cases per run and always includes, through
# ``@example``, each case Req 1.21 names. When the check fails, a call carrying only that URI returns
# ``IMAGE_SOURCE_TEXT`` with category ``other`` (asserted through ``precheck``).
#
# **Validates: Requirements 1.20, 1.21**
#
# The oracle is written independently of ``call_checks.py``, directly from the requirement text.

from project_search.call_checks import IMAGE_SOURCE_TEXT, check_image_uri  # noqa: E402


def _oracle_is_source(uri, scope: CallScope) -> bool:
    """Req 1.20, re-stated independently of ``call_checks``.

    ``True`` exactly when ``uri`` names a Search_Image_Source for ``scope``.
    """
    if not isinstance(uri, str):
        return False
    if "%" in uri or "?" in uri or "#" in uri:
        return False
    if not uri.startswith("s3://"):
        return False

    bucket, sep, key = uri[5:].partition("/")
    if sep != "/":
        return False
    if any(seg == "" or seg == ".." for seg in key.split("/")):
        return False

    ingest = scope.ingest_bucket
    proxy = scope.proxy_bucket

    # (b) the Search_Rendition of an asset of the active project: exactly projects/<pid>/<asset>/rendition.jpg
    if ingest and bucket == ingest and scope.project_id:
        segs = key.split("/")
        if (len(segs) == 4 and segs[0] == "projects" and segs[1] == scope.project_id and segs[2]
                and segs[3] == "rendition.jpg"):
            return True

    # (c) a frame of a Hit returned this session under the turn's Active_Project
    if proxy and bucket == proxy:
        if key in scope.frame_keys():
            return True

    return False


def _scope(
    session_id: str,
    project_id: str,
    ingest_bucket: str,
    proxy_bucket: str,
    active_frame_keys,
) -> CallScope:
    """A CallScope whose ``frame_keys()`` returns exactly the Active_Project's frame keys.

    ``active_frame_keys`` is the set of keys returned under THIS turn's Active_Project; keys returned
    only under other Projects are deliberately absent, so a URI built from one must be rejected.
    """
    keys = frozenset(active_frame_keys)
    return CallScope(
        session_id=session_id,
        project_id=project_id,
        ingest_bucket=ingest_bucket,
        proxy_bucket=proxy_bucket,
        frame_keys=lambda k=keys: k,
    )


# Adversarial literal URIs that never depend on the drawn scope.
_LITERAL_URIS = [
    "",
    "s3://",
    "s3://bucket",
    "s3://bucket/",
    "s3:///key",
    "S3://ingest-bkt/projects/proj1/x.jpg",         # upper-case scheme
    "s3://ingest-bkt/projects/proj1/%2e%2e/x.jpg",  # percent-encoding
    "s3://ingest-bkt/projects/proj1/x.jpg?v=1",     # query
    "s3://ingest-bkt/projects/proj1/x.jpg#frag",    # fragment
    "s3://ingest-bkt/projects/proj1/../x.jpg",      # .. segment
    "s3://ingest-bkt//double/slash.jpg",            # empty segment
    "s3://ingest-bkt-evil/projects/proj1/x.jpg",    # lookalike bucket
    "https://ingest-bkt/projects/proj1/x.jpg",      # wrong scheme
    "ingest-bkt/projects/proj1/x.jpg",              # no scheme
]

_FRAME_KEY_POOL = ["frames/a.jpg", "frames/b.png", "proxy/f/1.jpg", "projects/proj1/img/f.jpg"]
_OTHER_KEY_POOL = ["frames/c.jpg", "frames/d.png", "proxy/f/2.jpg"]


@st.composite
def _image_case(draw):
    """Draw a ``(uri, scope)`` pair spanning the Req 1.20 acceptance branches and the Req 1.21 traps.

    ``ingest_bucket`` and ``proxy_bucket`` may be empty (disabling their sources) or equal (one bucket
    serving two sources). The URI is assembled to land near a branch so acceptances are exercised, with
    adversarial literals and free text mixed in; the oracle decides the truth either way.
    """
    ingest = draw(st.sampled_from(["ingest-bkt", "shared", ""]))
    proxy = draw(st.sampled_from(["proxy-bkt", "shared", ""]))
    session = draw(st.sampled_from(["sess1", "sess2", ""]))
    project = draw(st.sampled_from(["proj1", "proj2", ""]))

    active_keys = frozenset(draw(st.sets(st.sampled_from(_FRAME_KEY_POOL), max_size=3)))
    other_keys = frozenset(draw(st.sets(st.sampled_from(_OTHER_KEY_POOL), max_size=2)))
    scope = _scope(session, project, ingest, proxy, active_keys)

    # Bucket slot for template cases: the real buckets, lookalikes, empties, and unrelated names.
    bucket = draw(
        st.sampled_from(
            [ingest, proxy, "ingest-bkt", "proxy-bkt", "ingest-bkt-evil", "shared", "other", ""]
        )
    )
    # Tail after a matched prefix: present / empty / dotted / nested.
    tail = draw(st.sampled_from(["x.jpg", "a/b.jpg", "", "..", "sub/..", "p/q/r.png", ".", "img1/rendition.jpg",
                                 "img1/a/rendition.jpg", "rendition.jpg", "img1/rendition.jpeg", "/rendition.jpg"]))
    kind = draw(
        st.sampled_from(
            ["attached", "project", "frame_in", "frame_out", "literal", "freeform"]
        )
    )

    if kind == "attached":
        uri = f"s3://{bucket}/artifacts/{session}/input/{tail}"
    elif kind == "project":
        uri = f"s3://{bucket}/projects/{project}/{tail}"
    elif kind == "frame_in":
        fk = draw(st.sampled_from(sorted(active_keys))) if active_keys else "frames/a.jpg"
        uri = f"s3://{bucket}/{fk}"
    elif kind == "frame_out":
        fk = draw(st.sampled_from(sorted(other_keys))) if other_keys else "frames/c.jpg"
        uri = f"s3://{bucket}/{fk}"
    elif kind == "literal":
        uri = draw(st.sampled_from(_LITERAL_URIS))
    else:
        uri = draw(st.text(min_size=0, max_size=24))

    return uri, scope


# A fixed scope for the Req 1.21 named @examples: ingest and proxy distinct and non-empty, one frame
# key returned under the Active_Project (proj1), none under any other.
_EX = _scope(
    session_id="sess1",
    project_id="proj1",
    ingest_bucket="ingest-bkt",
    proxy_bucket="proxy-bkt",
    active_frame_keys={"frames/active.jpg"},
)


# Feature: media-harness-search-qa-rendering, Property 4: Image-path acceptance
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.data_too_large])
@given(_image_case())
# --- Accepted: one example of each Search_Image_Source (a), (b), (c). ---
@example(("s3://ingest-bkt/projects/proj1/image:1/rendition.jpg", _EX))  # (b) an asset's Search_Rendition
@example(("s3://proxy-bkt/frames/active.jpg", _EX))                   # (c) this session's hit frame
# --- Rejected: an original (M2C's search role cannot read it) and the retired (a) chat-input path. ---
@example(("s3://ingest-bkt/projects/proj1/image.png", _EX))           # an original, not a rendition
@example(("s3://ingest-bkt/projects/proj1/image:1/shot.jpg", _EX))    # an asset's original file
@example(("s3://ingest-bkt/projects/proj1/a/b/rendition.jpg", _EX))   # rendition name, wrong depth
@example(("s3://ingest-bkt/artifacts/sess1/input/photo.jpg", _EX))     # retired: chat input path
# --- Rejected: every case named by Req 1.21. ---
@example(("s3://ingest-bkt/artifacts/sess2/input/x.jpg", _EX))         # another session's input key
@example(("s3://ingest-bkt/artifacts/sess1/thumb/x.jpg", _EX))        # same session, outside input/
@example(("s3://ingest-bkt/projects/proj2/x.jpg", _EX))               # another Project's prefix
@example(("s3://ingest-bkt/projects/proj1", _EX))                     # projects/<id> no trailing /
@example(("s3://proxy-bkt/frames/never.jpg", _EX))                    # frame key never returned
@example(("s3://proxy-bkt/frames/other.jpg", _EX))                    # frame returned only elsewhere
@example(("s3://ingest-bkt/projects/proj1/../x.jpg", _EX))            # .. segment
@example(("s3://ingest-bkt/projects/proj1/%2e%2e.jpg", _EX))         # percent-encoding
@example(("S3://ingest-bkt/projects/proj1/x.jpg", _EX))               # upper-case S3:// scheme
@example(("s3://ingest-bkt-evil/projects/proj1/x.jpg", _EX))         # lookalike bucket name
@example(("s3://ingest-bkt/", _EX))                                   # bare bucket URI
def test_property4_image_path_acceptance(case):
    uri, scope = case
    expected = _oracle_is_source(uri, scope)

    # check_image_uri agrees with the oracle on every URI/scope.
    assert check_image_uri(uri, scope) == expected

    # Tie-in through precheck: a non-empty-string search_image_s3uri with no search_term passes
    # check_args, so precheck's decision is exactly the image-path check's. A failed check surfaces
    # IMAGE_SOURCE_TEXT with category `other`; an accepted one yields None.
    if uri != "":
        result = precheck({"search_image_s3uri": uri}, scope)
        if expected:
            assert result is None
        else:
            assert result == (errors.OTHER, IMAGE_SOURCE_TEXT)
