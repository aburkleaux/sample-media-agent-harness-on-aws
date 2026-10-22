"""The Render Agent must accept canonical s3:// artifact refs.

Job artifacts are stored as `s3://bucket/key`. The URL extractor matched only
http(s), so an s3 ref produced an error manifest:

    {"duration": 0, "tracks": [], "errors": [{"code": "NO_URL", ...}]}

which the renderer surfaced to the user as
"Could not display this render — Field \"duration\" must be >= 0.001, got 0".

Verified against that exact staging failure.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import render_agent as ra  # noqa: E402


class TestUrlExtraction:
    def test_extracts_s3_refs(self):
        urls = ra._extract_urls("Render this artifact: s3://bucket/key/clip.mp4")
        assert urls == ["s3://bucket/key/clip.mp4"]

    def test_still_extracts_http_urls(self):
        urls = ra._extract_urls("Render this artifact: https://x.test/a.mp4?sig=1")
        assert urls == ["https://x.test/a.mp4?sig=1"]

    def test_extracts_a_mixed_set(self):
        urls = ra._extract_urls("s3://b/a.wav\nhttps://x.test/b.wav")
        assert len(urls) == 2


class TestGatewayResolution:
    def test_non_s3_urls_pass_through_untouched(self, monkeypatch):
        import asyncio

        async def no_gateways():
            return None

        monkeypatch.setattr(ra, "_ensure_gateways_loaded", no_gateways)
        out = asyncio.run(ra._resolve_s3_urls_via_gateway(["https://x.test/a.mp4"]))
        assert out == ["https://x.test/a.mp4"]

    def test_empty_input_is_a_noop(self):
        import asyncio
        assert asyncio.run(ra._resolve_s3_urls_via_gateway([])) == []

    def test_resolution_helpers_exist(self):
        # Guards against the slice being dropped again by a revert.
        for name in ("_ensure_gateways_loaded", "_tool_arg_name",
                     "_resolve_one", "_resolve_s3_urls_via_gateway"):
            assert hasattr(ra, name), name

    def test_invoke_resolves_before_building_the_manifest(self):
        src = (Path(__file__).resolve().parents[1] / "src" / "render_agent.py").read_text()
        i = src.index("_split_render_request(prompt)")
        j = src.index("_build_media_manifest", i)
        # resolution must happen between extraction and manifest construction
        assert "_resolve_s3_urls_via_gateway" in src[i:j]


def test_resolution_helpers_have_no_undefined_globals():
    """Catch NameErrors that only surface at request time.

    The restored slice initially referenced `_gateway_initialized` and
    `_download_url_tools`, neither of which existed — the module compiled fine and
    only failed inside a live request with
    `NameError: name '_gateway_initialized' is not defined`, costing a deploy
    cycle. compile() cannot see this; resolve the names statically instead.
    """
    import ast
    from pathlib import Path as _P

    src = (_P(__file__).resolve().parents[1] / "src" / "render_agent.py").read_text()
    tree = ast.parse(src)

    module_level = set()
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            module_level |= {t.id for t in targets if isinstance(t, ast.Name)}
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            module_level |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            module_level.add(node.name)

    watched = {
        "_ensure_gateways_loaded", "_tool_arg_name", "_resolve_one",
        "_resolve_s3_urls_via_gateway", "_download_url_tools",
    }
    undefined = {}
    for fn in tree.body:
        if not (isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name in watched):
            continue
        bound = {a.arg for a in fn.args.args}
        for sub in ast.walk(fn):
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bound.add(sub.name)
                bound |= {a.arg for a in sub.args.args}
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                bound.add(sub.id)
            if isinstance(sub, ast.comprehension) and isinstance(sub.target, ast.Name):
                bound.add(sub.target.id)
        for sub in ast.walk(fn):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                if sub.id.startswith("_") and sub.id not in module_level and sub.id not in bound:
                    undefined.setdefault(fn.name, set()).add(sub.id)

    assert not undefined, f"undefined globals: {{k: sorted(v) for k, v in undefined.items()}}"


def test_mcp_manager_binding_supports_the_calls_made_on_it():
    """`mcp_manager` must be an MCPManager INSTANCE, not the module.

    The helpers call `.load_gateways_from_registry()`, `.connect_all()` and
    `._gateways`. Those are MCPManager members, not module-level functions, so
    binding the name to the module compiles cleanly and then fails at request time
    with AttributeError — the same shape as the `_gateway_initialized` NameError
    that already cost a deploy cycle.
    """
    import ast
    from pathlib import Path as _P

    src_dir = _P(__file__).resolve().parents[1] / "src"
    render_src = (src_dir / "render_agent.py").read_text()

    # the binding must be an MCPManager() call, not `import mcp_manager`
    assert "\nimport mcp_manager\n" not in render_src, "mcp_manager must not be bound to the module"
    assert "mcp_manager = MCPManager()" in render_src

    # every attribute accessed on it must exist on the class
    used = set(re.findall(r"mcp_manager\.([A-Za-z_]+)", render_src))
    cls = next(
        n for n in ast.parse((src_dir / "mcp_manager.py").read_text()).body
        if isinstance(n, ast.ClassDef) and n.name == "MCPManager"
    )
    members = {m.name for m in cls.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for m in cls.body:
        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name == "__init__":
            for sub in ast.walk(m):
                if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) and sub.value.id == "self":
                    members.add(sub.attr)

    missing = sorted(a for a in used if a not in members)
    assert not missing, f"mcp_manager attributes not on MCPManager: {missing}"


def test_gateway_registry_is_actually_constructed():
    """Resolution must not be a silent no-op.

    `_ensure_gateways_loaded` returns immediately when `_gateway_registry is
    None`. If nothing ever constructs it, s3:// refs pass through unresolved, the
    manifest comes back NO_URL, and NOTHING is logged as an error — the failure is
    indistinguishable from "the fix didn't work". Assert the module actually builds
    the registry when GATEWAY_REGISTRY_TABLE is set.
    """
    from pathlib import Path as _P
    src = (_P(__file__).resolve().parents[1] / "src" / "render_agent.py").read_text()
    assert "GATEWAY_REGISTRY_TABLE = os.environ" in src
    assert "_gateway_registry = GatewayRegistry(" in src, "registry is never constructed"
    # and it must be guarded on the env var rather than constructed unconditionally
    i = src.index("_gateway_registry = GatewayRegistry(")
    assert "if GATEWAY_REGISTRY_TABLE:" in src[:i]


def test_region_is_defined_before_the_registry_uses_it():
    from pathlib import Path as _P
    src = (_P(__file__).resolve().parents[1] / "src" / "render_agent.py").read_text()
    assert src.index("REGION = os.environ") < src.index("_gateway_registry = GatewayRegistry(")


def test_unresolved_s3_refs_produce_an_error_not_a_broken_track():
    """An s3:// src is unplayable in a media element.

    Observed on staging: with the extractor accepting s3:// but resolution
    no-opping, the builder emitted a track whose src was the raw ref — HTTP 200,
    one track, no errors, and absolutely nothing plays. A silent success is worse
    than a visible failure, so invoke must refuse to emit unplayable tracks.
    """
    from pathlib import Path as _P
    src = (_P(__file__).resolve().parents[1] / "src" / "render_agent.py").read_text()
    i = src.index("_resolve_s3_urls_via_gateway(artifact_urls)")
    j = src.index("_build_media_manifest", i)
    window = src[i:j]
    assert "UNRESOLVED_S3" in window, "no guard between resolution and manifest build"
    assert 'startswith("s3://")' in window
    # the guard must stop the turn rather than fall through to the builder
    assert "return" in window
