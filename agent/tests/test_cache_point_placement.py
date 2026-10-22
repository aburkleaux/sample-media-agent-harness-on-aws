"""The tools cache point must sit after the BUILTIN tools, not after all of them.

Strands appends `cache_tools`' cache point to the end of `toolConfig.tools`. That puts
the federated gateway tools inside the cached prefix — the wrong boundary here, because
the harness's builtins are identical on every request while the federated set changes
whenever tool selection narrows to what a request needs.

With one cache point at the very end, changing a single federated tool invalidates the
whole prefix, so per-turn narrowing would replace cheap cache reads with a full cache
write every turn — costing MORE than not narrowing. Splitting at the boundary lets
caching and narrowing coexist.
"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from model.factory import (StablePrefixBedrockModel, build_model,  # noqa: E402
                           is_federated_tool_name)

BUILTIN = ["code_interpreter", "http_request", "file_read", "dispatch_tool"]
FEDERATED = ["media-processing___process_media", "media-processing___get_job_status",
             "face-recognition___recognize_faces"]


def spec(name):
    return {"name": name, "description": f"desc for {name}",
            "inputSchema": {"json": {"type": "object", "properties": {}}}}


def _seam(model):
    """The request-formatting method this strands version actually exposes.

    Not public API, and the name changed between releases: 1.33 has
    `_format_request`, later versions `format_request`. The first version of this test
    called the private name directly and passed locally while failing in CI, where a
    newer minor resolved.
    """
    from strands.models import BedrockModel as Base
    # Ask the BASE class which name this version provides. Asking the instance would
    # find our own override, which exists for both names by design.
    for name in StablePrefixBedrockModel.SEAM_METHODS:
        if hasattr(Base, name):
            return getattr(model, name)
    raise AssertionError(
        "no request-formatting seam found; strands renamed it again. Tried "
        f"{StablePrefixBedrockModel.SEAM_METHODS} — update SEAM_METHODS in model_factory."
    )


def emit(tool_names, **kw):
    m = build_model("us.anthropic.claude-sonnet-5", **kw)
    req = _seam(m)(
        messages=[{"role": "user", "content": [{"text": "hi"}]}],
        tool_specs=[spec(n) for n in tool_names],
        system_prompt_content=None,
    )
    return req.get("toolConfig", {}).get("tools", [])


def layout(entries):
    out = []
    for e in entries:
        if "cachePoint" in e:
            out.append("<CACHE>")
        else:
            out.append(e["toolSpec"]["name"])
    return out


class TestSeam:
    """The version coupling must fail loudly here, not deep inside a request."""

    def test_installed_strands_exposes_a_known_seam(self):
        from strands.models import BedrockModel as Base
        found = [n for n in StablePrefixBedrockModel.SEAM_METHODS if hasattr(Base, n)]
        assert found, (
            "strands renamed the request-formatting method again; tried "
            f"{StablePrefixBedrockModel.SEAM_METHODS}. Add the new name to SEAM_METHODS "
            "and give StablePrefixBedrockModel a matching override."
        )

    def test_the_subclass_overrides_whatever_exists(self):
        from strands.models import BedrockModel as Base
        for name in StablePrefixBedrockModel.SEAM_METHODS:
            if hasattr(Base, name):
                assert getattr(StablePrefixBedrockModel, name) is not getattr(Base, name), \
                    f"{name} exists on BedrockModel but is not overridden"

    def test_reposition_is_idempotent(self):
        """Safe if a version routes the public method through the private one.

        The seam now RELOCATES the tools cache point strands emits (it no longer invents
        one), so the input carries the trailing point strands would have appended.
        """
        m = build_model("us.anthropic.claude-sonnet-5")
        req = {"toolConfig": {"tools": [
            {"toolSpec": spec("code_interpreter")},
            {"toolSpec": spec("media-processing___process_media")},
            {"cachePoint": {"type": "default"}},
        ]}}
        once = m._apply_cache_boundary(req)
        twice = m._apply_cache_boundary(once)
        assert once["toolConfig"]["tools"] == twice["toolConfig"]["tools"]
        assert sum(1 for e in twice["toolConfig"]["tools"] if "cachePoint" in e) == 1


class TestNaming:
    def test_federation_separator_identifies_gateway_tools(self):
        assert is_federated_tool_name("media-processing___process_media")
        assert not is_federated_tool_name("code_interpreter")
        assert not is_federated_tool_name("")


class TestPlacement:
    def test_cache_point_sits_between_builtin_and_federated(self):
        got = layout(emit(BUILTIN + FEDERATED))
        assert "<CACHE>" in got
        i = got.index("<CACHE>")
        before, after = set(got[:i]), set(got[i + 1:])
        assert before == set(BUILTIN), f"builtins must precede the cache point: {got}"
        assert after == set(FEDERATED), f"federated tools must follow it: {got}"

    def test_exactly_one_cache_point(self):
        """The trailing one Strands adds must be removed, not duplicated."""
        got = layout(emit(BUILTIN + FEDERATED))
        assert got.count("<CACHE>") == 1, got

    def test_order_is_corrected_even_if_the_registry_interleaves(self):
        """A cache point only covers what precedes it; ordering cannot be assumed."""
        interleaved = ["media-processing___process_media", "code_interpreter",
                       "face-recognition___recognize_faces", "file_read"]
        got = layout(emit(interleaved))
        i = got.index("<CACHE>")
        assert set(got[:i]) == {"code_interpreter", "file_read"}
        assert set(got[i + 1:]) == {"media-processing___process_media",
                                    "face-recognition___recognize_faces"}

    def test_every_tool_survives_the_reorder(self):
        names = [n for n in layout(emit(BUILTIN + FEDERATED)) if n != "<CACHE>"]
        assert sorted(names) == sorted(BUILTIN + FEDERATED)

    def test_builtin_only_keeps_the_default_trailing_placement(self):
        got = layout(emit(BUILTIN))
        assert got[-1] == "<CACHE>", got
        assert got.count("<CACHE>") == 1

    def test_federated_only_gets_no_cache_point(self):
        """Caching a set that varies every turn would write on every turn."""
        got = layout(emit(FEDERATED))
        assert "<CACHE>" not in got, got

    def test_no_cache_point_when_caching_is_disabled(self):
        got = layout(emit(BUILTIN + FEDERATED, cache=False))
        assert "<CACHE>" not in got, got
        assert sorted(got) == sorted(BUILTIN + FEDERATED)
