"""Non-artifact-producing tools (AWS docs etc.) must not render images their
prose merely QUOTES.

The bug: the render hook rendered images a documentation page CONTAINS as
though the docs tool produced them -- a 22x20 console warning glyph and a
full-size console screenshot both showed up in the render panel. There is no
structural signal to detect (fal.ai returns a markdown image tag in a text
block; AWS docs embed images the SAME way), and a size threshold fails because
doc pages contain legitimately large images. The fix declares such tools
non-producing while defaulting every other tool to producing.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from hooks import provider  # noqa: E402
from hooks.engine import HookEngine  # noqa: E402
from hooks.defaults import default_hook_specs  # noqa: E402


class _Ev:
    def __init__(self, name, text):
        self.tool_use = {"name": name, "toolUseId": "t1"}
        self.result = {"content": [{"text": text}]}


# The real observed shape: a markdown image tag inside a text block. This is
# byte-for-byte the shape fal.ai (a producer) returns, which is why detection
# cannot tell them apart and the tool must be declared instead.
_DOC_TEXT = (
    "The warning icon ![warning](https://docs.aws.amazon.com/images/warn.png) "
    "indicates a problem. See the console:\n"
    "![MediaTailor console](https://docs.aws.amazon.com/images/mediatailor-console.png)"
)


def test_docs_tool_yields_no_new_artifacts_and_does_not_render():
    """A gateway-prefixed AWS docs result quoting image URLs → no render."""
    provider.reset_dispatched_artifacts()
    eng = HookEngine(default_hook_specs())

    ev = _Ev("mediaworks-harness-aws-mcp___aws___read_documentation", _DOC_TEXT)
    ctx = provider.ctx_after_tool_result(ev)

    # urls/mimes stay populated -- other consumers read them; only new_artifacts
    # gates rendering.
    assert len(ctx["result"]["urls"]) == 2
    assert "image/png" in ctx["result"]["mimes"]

    assert ctx["result"]["new_artifacts"] == [], \
        "a docs page's quoted images are not artifacts it produced"
    assert eng.matching("after_tool_result", ctx) == [], \
        "the render hook must not fire on a docs tool"
    provider.reset_dispatched_artifacts()


def test_all_three_doc_tools_are_non_producing():
    for bare in ("aws___search_documentation", "aws___read_documentation",
                 "aws___retrieve_skill"):
        prefixed = f"mediaworks-harness-aws-mcp___{bare}"
        assert provider.is_non_artifact_tool(prefixed), prefixed
        assert provider.is_non_artifact_tool(bare), bare


def test_two_separator_prefix_case_prefixed_and_unprefixed():
    """The names themselves contain `___`, so the prefix must be split ONCE.

    Splitting on every separator (`split("___")[-1]`) yields `read_documentation`
    and matches nothing -- the exact bug this repo has already hit.
    """
    prefixed = "mediaworks-harness-aws-mcp___aws___read_documentation"
    unprefixed = "aws___read_documentation"

    # Prove the naive split would have failed, so the maxsplit=1 choice is not
    # incidental.
    assert prefixed.split("___")[-1] == "read_documentation"
    assert "read_documentation" not in provider.NON_ARTIFACT_TOOLS

    # The correct single-split classification catches both shapes.
    assert prefixed.split("___", 1)[-1] == "aws___read_documentation"
    assert provider.is_non_artifact_tool(prefixed)
    assert provider.is_non_artifact_tool(unprefixed)


def test_unknown_tool_with_image_url_still_renders_the_default():
    """DEFAULT IS ARTIFACT-PRODUCING. An unlisted tool renders.

    This is the case a tool-name allowlist got wrong: a user-registered MCP
    server (fal.ai) returning an image rendered nowhere because nobody had
    enumerated it. It must render without being declared.
    """
    provider.reset_dispatched_artifacts()
    eng = HookEngine(default_hook_specs())

    img = "https://v3b.fal.media/files/b/0aa50f49/8c4ABgOJ45Lp.jpg"
    ev = _Ev("usr-8ddf64af24be45c894a7a8c1f254c83f___run_model",
             f"Generated: {img}")
    ctx = provider.ctx_after_tool_result(ev)

    assert not provider.is_non_artifact_tool(ev.tool_use["name"]), \
        "an unlisted tool is a producer by default"
    assert ctx["result"]["new_artifacts"] == [img]
    assert len(eng.matching("after_tool_result", ctx)) == 1
    provider.reset_dispatched_artifacts()


def test_relay_behaviour_unchanged():
    """Relay exclusion is orthogonal and still works after adding the docs set."""
    assert provider.is_relay_tool("dispatch_tool") is True
    assert provider.is_relay_tool("plan_and_execute_tool") is False
    # A docs tool is not a relay, and a relay is not a docs tool -- the two
    # gates are independent.
    assert provider.is_relay_tool("aws___read_documentation") is False
    assert provider.is_non_artifact_tool("dispatch_tool") is False

    provider.reset_dispatched_artifacts()
    url = "https://cf/relay-check.wav"
    relayed = type("Ev", (), {
        "tool_use": {"name": "dispatch_tool", "toolUseId": "t1"},
        "result": {"content": [{"text": f"the sub-agent produced {url}"}]},
    })()
    ctx = provider.ctx_after_tool_result(relayed)
    assert ctx["result"]["new_artifacts"] == [], "relay still zeroed"
    provider.reset_dispatched_artifacts()


def test_read_only_annotation_marks_a_tool_non_artifact():
    """The protocol's own readOnlyHint must work for tools nobody hardcoded.

    A hand-maintained name list cannot cover a server registered tomorrow. MCP
    annotations can, so a read-only tool is non-artifact whether or not it
    appears in NON_ARTIFACT_TOOLS.
    """
    import mcp_manager
    from hooks.provider import is_non_artifact_tool

    novel = "usr-deadbeef___some_future_search_tool"
    assert not is_non_artifact_tool(novel)
    mcp_manager._READ_ONLY_TOOL_NAMES.add(novel)
    try:
        assert is_non_artifact_tool(novel)
    finally:
        mcp_manager._READ_ONLY_TOOL_NAMES.discard(novel)
    assert not is_non_artifact_tool(novel)


def test_artifact_producing_tool_is_unaffected_by_the_annotation_path():
    """fal's run_model is not read-only and must keep rendering."""
    from hooks.provider import is_non_artifact_tool

    assert not is_non_artifact_tool("usr-8ddf64af___run_model")


def test_list_still_covers_servers_that_omit_annotations():
    """Annotations are optional in the spec; absence must not mean "renders"."""
    from hooks.provider import is_non_artifact_tool

    assert is_non_artifact_tool("mediaworks-harness-aws-mcp___aws___read_documentation")


def test_readers_and_url_vendors_cannot_produce_artifacts():
    """A reader and a presigned-URL vendor hand back inputs, not results.

    Session 842bb417: three of eight render tabs were empty generic-preview
    manifests (tracks: 0) attributed to "Media Outputs" and "Read Result Files",
    and one played the untouched 11:58 source video, because read_result_files
    returns file CONTENT that quotes the source uris and get_download_url returns
    a presigned GET for an object someone else produced.
    """
    from hooks.provider import is_non_artifact_tool

    assert is_non_artifact_tool("read_result_files")
    assert is_non_artifact_tool("media-outputs___get_download_url")
    # Bare and federated spellings both resolve, and a real producer is untouched.
    assert is_non_artifact_tool("get_download_url")
    assert not is_non_artifact_tool("face-recognition___recognize_faces")
    assert not is_non_artifact_tool("media-processing___process_media")



def test_model_metadata_tools_are_non_producing():
    """A model gateway's schema/pricing/recommendation lookups quote EXAMPLE media, not
    deliverables. Sessions d92e6328 / 4681f2da: a fal example image (mountain-biker demo)
    landed in a Meridian gallery and a video model's example clip rendered as a stray tab.
    """
    from hooks.provider import is_non_artifact_tool

    for bare in ("get_model_schema", "recommend_model", "get_pricing"):
        assert is_non_artifact_tool(bare), bare
        assert is_non_artifact_tool(f"usr-8ddf64af24be45c894a7a8c1f254c83f___{bare}"), bare
    # The producers on the SAME gateway must stay producers.
    assert not is_non_artifact_tool("usr-8ddf64af24be45c894a7a8c1f254c83f___run_model")
    assert not is_non_artifact_tool("usr-8ddf64af24be45c894a7a8c1f254c83f___submit_job")


def test_get_model_schema_example_image_does_not_render():
    """The biker, reproduced: a `get_model_schema` result quoting an example image URL must
    yield NO new_artifacts (so the render hook opens no tab), while `urls` stays populated."""
    provider.reset_dispatched_artifacts()
    eng = HookEngine(default_hook_specs())

    example = "https://v3b.fal.media/files/b/example/mountain-biker-demo.jpg"
    ev = _Ev(
        "usr-8ddf64af24be45c894a7a8c1f254c83f___get_model_schema",
        f'{{"example_output": {{"images": [{{"url": "{example}"}}]}}, '
        f'"description": "trail/forest demo"}}',
    )
    ctx = provider.ctx_after_tool_result(ev)

    assert example in ctx["result"]["urls"], "url still surfaced for other consumers"
    assert ctx["result"]["new_artifacts"] == [], \
        "a model-schema example image is not a deliverable and must not render"
    assert eng.matching("after_tool_result", ctx) == [], \
        "the render hook must not fire on get_model_schema"
    provider.reset_dispatched_artifacts()


def test_project_tools_are_non_producing_and_their_uris_do_not_render():
    """The Project_Tools report on EXISTING assets; their replies quote source URIs. Staging rendered
    project_status replies as "Project Status — Video/Data" tabs (~90s in the render agent)."""
    from project_search.project_tools import TOOL_NAMES

    for bare in TOOL_NAMES:
        assert provider.is_non_artifact_tool(bare), bare

    provider.reset_dispatched_artifacts()
    eng = HookEngine(default_hook_specs())
    url = "https://ingest.s3.us-west-2.amazonaws.com/projects/p1/tidal.mp4"
    ev = _Ev("project_status", f'{{"matches": [{{"displayName": "tidal.mp4", "sourceUri": "{url}"}}]}}')
    ctx = provider.ctx_after_tool_result(ev)
    assert url in ctx["result"]["urls"]
    assert ctx["result"]["new_artifacts"] == []
    assert eng.matching("after_tool_result", ctx) == []
    provider.reset_dispatched_artifacts()
