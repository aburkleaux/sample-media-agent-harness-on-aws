"""Guards a Python-specific failure the whole test suite missed.

A render of media with NO json sidecars — a plain video, the commonest case — crashed
with `cannot access local variable 'fetched' where it is not associated with a value`.
`fetched` was assigned only inside `if json_urls:` while the media branch passed it as
`prefetched` unconditionally. The user saw "Could not display this render / Field
duration must be >= 0.001, got 0", which named neither the real cause nor the file.

Every existing test happened to include a sidecar, so 1146 tests passed with the bug
live. Two guards here: an end-to-end render with no sidecars, and a static check that
no local in `invoke` is read on a path where it may be unassigned.
"""
import ast
import asyncio
import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))


class TestMediaWithoutSidecars:
    def _render(self, monkeypatch, urls):
        import render_agent as ra
        monkeypatch.setattr(ra, "media_probe", lambda u: '{"mime_type": "video/mp4"}')
        monkeypatch.setattr(ra, "peak_extractor", lambda u, n: '{"peaks": []}')
        monkeypatch.setattr(ra, "_container_duration", lambda u, timeout=4.0: 30.03)
        monkeypatch.setattr(ra, "_fetch_json", lambda u, timeout=5.0: None)

        async def run():
            out = []
            async for ch in ra._render_deterministic({"prompt": "Render: " + " ".join(urls)}, None):
                out.append(ch)
            return out
        for ch in asyncio.run(run()):
            try:
                obj = json.loads(ch)
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("duration") is not None:
                return obj
        return None

    def test_a_plain_video_renders(self, monkeypatch):
        m = self._render(monkeypatch, ["https://x/proxy/transcoded.mp4"])
        assert m is not None, "no manifest produced"
        assert not m.get("errors"), f"render failed: {m.get('errors')}"
        assert m["duration"] == 30.03
        assert len(m["tracks"]) == 1

    def test_plain_audio_renders(self, monkeypatch):
        m = self._render(monkeypatch, ["https://x/audio_16k_mono.wav"])
        assert m is not None and not m.get("errors")


class TestErrorManifestIsDisplayable:
    def test_error_manifest_passes_schema_validation(self):
        """The renderer validates before reading `errors`, so a zero duration hid it."""
        import render_agent as ra
        m = ra._build_error_manifest("UNRESOLVED_S3", "could not resolve", source_url="s3://b/k")
        assert m["duration"] >= 0.001, "a rejected manifest cannot show its own error"
        assert m["errors"][0]["code"] == "UNRESOLVED_S3"
        assert "could not resolve" in m["errors"][0]["message"]


class TestNoConditionallyAssignedLocals:
    """Static guard: a local read on a path where it may be unassigned.

    This class of bug survives unit tests whenever the tests happen to take the branch
    that assigns the variable — which is exactly what happened here.
    """

    def _invoke_fn(self) -> ast.FunctionDef:
        tree = ast.parse((SRC / "render_agent.py").read_text())
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_render_deterministic":
                return node
        raise AssertionError("_render_deterministic not found")

    def test_locals_used_in_calls_are_assigned_unconditionally(self):
        fn = self._invoke_fn()
        certain: set[str] = set()      # assigned on every path that reaches later code
        maybe: set[str] = set()        # assigned only on some paths

        def collect(body, conditional: bool) -> None:
            for stmt in body:
                if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                    targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                    for t in targets:
                        if isinstance(t, ast.Name):
                            (maybe if conditional else certain).add(t.id)
                # A `try` or `with` body runs in order, so assignments there are as
                # reliable as top-level ones. `if`/`else`, loop bodies (may not run)
                # and except handlers are not.
                branchy = isinstance(stmt, (ast.If, ast.For, ast.While, ast.AsyncFor))
                collect(getattr(stmt, "body", []) or [], conditional or branchy)
                collect(getattr(stmt, "orelse", []) or [], True)
                collect(getattr(stmt, "finalbody", []) or [], conditional)
                for h in getattr(stmt, "handlers", []) or []:
                    collect(h.body, True)

        collect(fn.body, False)
        risky = maybe - certain
        read_as_kwarg = {
            kw.value.id
            for call in ast.walk(fn) if isinstance(call, ast.Call)
            for kw in call.keywords if isinstance(kw.value, ast.Name)
        }
        offenders = sorted(risky & read_as_kwarg)
        assert not offenders, (
            "these locals are only assigned on some paths yet passed to a call: "
            f"{offenders} — initialise them before the branch"
        )
