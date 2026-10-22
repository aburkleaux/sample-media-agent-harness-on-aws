"""Property 21: the URL_Render_Rule never renders a URL from a search result.

A Compact_Hit carries no URL of its own, but its ``text`` can QUOTE one -- a
scene's on-screen text may read ``https://example.com/promo.mp4``. ``extract_urls``
would find that in the tool result and, left alone, the ``render-artifacts`` default
rule (the URL_Render_Rule) would render a URL taken from a Hit. Task 7.8 closes this
by adding the search tools to ``NON_ARTIFACT_TOOLS`` and matching ``SEARCH_TOOL_NAME``
in ``is_non_artifact_tool`` -- so Moments (built from ledger Hits) and URL renders stay
disjoint and the same media is never rendered twice in a turn (Req 3.7, 3.25).

Property 21 (design.md): *for any* tool name from
``{search_content, <target>___search_content, SEARCH_TOOL_NAME, search_dispatch}`` and
any result text embedding ``https://`` media URLs (including ``artifacts/<uuid>/`` URLs
and presigned query strings), ``ctx_after_tool_result`` gives ``new_artifacts == []``,
firing the default engine on that context dispatches no ``render`` action, and
``result.urls`` still equals ``extract_urls(text)``. *For any* other tool name,
``new_artifacts`` equals the value the pre-change computation returns for the same input
(the search-tool exemption changes nothing for non-search tools).

**Validates: Requirements 3.7, 3.25**
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from hypothesis import given, settings, strategies as st  # noqa: E402

from hooks import provider  # noqa: E402
from hooks.engine import HookEngine  # noqa: E402
from hooks.defaults import default_hook_specs  # noqa: E402
from hooks.provider import ctx_after_tool_result, extract_urls  # noqa: E402

# Media extensions the render path recognises (provider._MIME_BY_EXT), so a quoted URL
# of this shape would be a renderable artifact for any non-exempt producer.
_MEDIA_EXTS = [
    ".mp4", ".mov", ".webm", ".mp3", ".wav", ".m4a", ".flac",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf",
]

_SEARCH_LITERALS = frozenset({"search_content", "search_dispatch"})

# Path/query segments restricted to characters _URL_RE won't terminate on (no space,
# quote, <>, ), ], }), so a generated URL is detected whole.
_SAFE = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_",
    min_size=1,
    max_size=8,
)
# Prose kept to lowercase letters + spaces: cannot start a JSON block ('{'/'[') and
# cannot form a URL (no ':' or '/'), so it never perturbs URL extraction.
_PROSE = st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", min_size=0, max_size=24)

_TARGETS = ["mediaworks-harness-search", "proj-search-gw", "usr-8ddf64af24be45c894a7a8c1f254c83f"]


@st.composite
def _media_urls(draw):
    host = draw(st.sampled_from(["cf.example.com", "v3b.fal.media", "d111abc.cloudfront.net"]))
    segs: list[str] = []
    if draw(st.booleans()):  # an artifacts/<uuid>/ URL
        segs.append("artifacts")
        segs.append(str(draw(st.uuids())))
        if draw(st.booleans()):
            segs.append(draw(st.sampled_from(["input", "output"])))
    segs.extend(draw(st.lists(_SAFE, min_size=0, max_size=3)))
    name = draw(_SAFE)
    ext = draw(st.sampled_from(_MEDIA_EXTS))
    url = f"https://{host}/" + "/".join(segs + [name + ext])
    if draw(st.booleans()):  # presigned query string
        cred = draw(_SAFE)
        sig = draw(_SAFE)
        url += (
            "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
            f"&X-Amz-Credential=AKIA{cred}&X-Amz-Expires=3600&X-Amz-Signature={sig}"
        )
    return url


@st.composite
def _result_texts(draw):
    urls = draw(st.lists(_media_urls(), min_size=1, max_size=4))
    parts: list[str] = []
    for u in urls:
        if draw(st.booleans()):
            parts.append(draw(_PROSE))
        parts.append(u)
    if draw(st.booleans()):
        parts.append(draw(_PROSE))
    # Space-joined so every URL is delimited and extracted whole.
    return " ".join(p for p in parts if p).strip()


@st.composite
def _search_tool_cases(draw):
    """(tool_name, search_tool_name_env) covering every name in Property 21's set."""
    target = draw(st.sampled_from(_TARGETS))
    custom = draw(
        st.from_regex(r"[a-z][a-z0-9_]{1,14}", fullmatch=True).filter(
            lambda s: s not in _SEARCH_LITERALS
        )
    )
    choice = draw(
        st.sampled_from([
            "search_content",
            "prefixed_search_content",
            "search_dispatch",
            "prefixed_search_dispatch",
            "env_custom",
            "prefixed_env_custom",
        ])
    )
    if choice == "search_content":
        return "search_content", None
    if choice == "prefixed_search_content":
        return f"{target}___search_content", None
    if choice == "search_dispatch":
        return "search_dispatch", None
    if choice == "prefixed_search_dispatch":
        return f"{target}___search_dispatch", None
    if choice == "env_custom":
        return custom, custom
    return f"{target}___{custom}", custom


class _Ev:
    def __init__(self, name: str, text: str):
        self.tool_use = {"name": name, "toolUseId": "t1"}
        self.result = {"content": [{"text": text}]}


def _set_search_env(value):
    if value is None:
        os.environ.pop("SEARCH_TOOL_NAME", None)
    else:
        os.environ["SEARCH_TOOL_NAME"] = value


@settings(deadline=None, max_examples=300)
@given(case=_search_tool_cases(), text=_result_texts())
def test_search_tool_never_renders_a_quoted_url(case, text):
    name, env = case
    result = {"content": [{"text": text}]}
    saved = os.environ.get("SEARCH_TOOL_NAME")
    try:
        _set_search_env(env)
        provider.reset_dispatched_artifacts()
        ctx = ctx_after_tool_result(_Ev(name, text))

        # 1. No artifact is produced by a search tool, whatever its prose quotes.
        assert ctx["result"]["new_artifacts"] == [], (name, text)

        # 2. The default engine dispatches no render action on this context.
        matched = HookEngine(default_hook_specs()).matching("after_tool_result", ctx)
        assert [h for h in matched if h.action_type == "render"] == [], (name, matched)

        # 3. result.urls is still the full set extract_urls finds in the text,
        #    so other consumers (citations, telemetry) are unaffected.
        assert ctx["result"]["urls"] == extract_urls(result)
    finally:
        _set_search_env(saved)


@st.composite
def _other_tool_names(draw):
    name = draw(
        st.one_of(
            st.sampled_from([
                "artifact",
                "x___get_diarization_results",
                "usr-8ddf64af24be45c894a7a8c1f254c83f___run_model",
                "dispatch_tool",
                "plan_and_execute_tool",
                "mediaworks-harness-aws-mcp___aws___read_documentation",
                "get_download_url",
                "face-recognition___recognize_faces",
            ]),
            st.from_regex(r"[a-z][a-z0-9_]{1,20}", fullmatch=True),
            st.builds(
                lambda t, b: f"{t}___{b}",
                st.sampled_from(_TARGETS),
                st.from_regex(r"[a-z][a-z0-9_]{1,15}", fullmatch=True),
            ),
        )
    )
    # Exclude the search names: this branch is about every OTHER tool.
    bare = name.split("___", 1)[-1]
    if name in _SEARCH_LITERALS or bare in _SEARCH_LITERALS:
        name = f"not_{name}"
    return name


@settings(deadline=None, max_examples=300)
@given(name=_other_tool_names(), text=_result_texts())
def test_non_search_tool_new_artifacts_unchanged_by_the_exemption(name, text):
    """For a non-search tool the search-tool exemption must change nothing: new_artifacts
    equals what the pre-change computation (NON_ARTIFACT_TOOLS without the search tools,
    SEARCH_TOOL_NAME matching disabled) would have returned for the same input."""
    saved = os.environ.get("SEARCH_TOOL_NAME")
    try:
        # SEARCH_TOOL_NAME unset so the only difference between the two computations is
        # whether the search literals sit in NON_ARTIFACT_TOOLS.
        _set_search_env(None)

        provider.reset_dispatched_artifacts()
        actual = ctx_after_tool_result(_Ev(name, text))["result"]["new_artifacts"]

        original_set = provider.NON_ARTIFACT_TOOLS
        provider.NON_ARTIFACT_TOOLS = frozenset(original_set - _SEARCH_LITERALS)
        try:
            provider.reset_dispatched_artifacts()
            pre_change = ctx_after_tool_result(_Ev(name, text))["result"]["new_artifacts"]
        finally:
            provider.NON_ARTIFACT_TOOLS = original_set

        assert actual == pre_change, (name, actual, pre_change)
    finally:
        _set_search_env(saved)
        provider.reset_dispatched_artifacts()
