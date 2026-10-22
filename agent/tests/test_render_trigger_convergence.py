"""Render-trigger unification (session 443eb166 duplicate-tabs class).

Both render entry points must converge on ONE deterministic manifest id:
  * the render-artifacts HOOK -> dispatch_render -> content-addressed manifest_id;
  * an EXPLICIT render (dispatch_tool target=render) -> render agent -> build_render_manifest
    with NO manifest_id.

Previously the explicit path minted str(uuid.uuid4()) on every build, so asking to render the
same artifact produced a NEW tab each time (5 identical face-people manifests in ~90s). The fix
makes the no-id fallback a content-hash of the chosen deliverable urls (the SAME scheme
dispatch_render uses), so an explicit render converges on the hook's tab (idempotent).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import render_agent as ra  # noqa: E402
from render.artifacts import _manifest_id_for  # noqa: E402


def _stub(monkeypatch):
    monkeypatch.setattr(ra, "_write_manifest_to_s3", lambda mj, sid, mid: f"artifacts/{sid}/render/{mid}.json")
    ra._RENDER_WRITES.clear()
    ra._RENDER_BUILD_COUNTS.clear()


def _mid(out):
    return out["s3_key"].split("/render/")[-1][:-len(".json")]


def test_explicit_render_no_id_is_deterministic_and_converges(monkeypatch):
    urls = ["https://cdn/x/EXEC/transcoded.mp4"]
    _stub(monkeypatch)
    out1 = json.loads(ra.build_render_manifest(candidate_urls=json.dumps(urls), manifest_id="", session_id="s"))
    _stub(monkeypatch)
    out2 = json.loads(ra.build_render_manifest(candidate_urls=json.dumps(urls), manifest_id="", session_id="s"))
    # deterministic across separate explicit renders (was two different uuids -> two tabs)
    assert _mid(out1) == _mid(out2)
    # converges on what the HOOK would key (dispatch_render uses _manifest_id_for on the same urls)
    assert _mid(out1) == _manifest_id_for(urls)
    # a content hash (32 hex, no dashes), not a uuid
    assert "-" not in _mid(out1) and len(_mid(out1)) == 32


def test_distinct_artifacts_get_distinct_ids(monkeypatch):
    _stub(monkeypatch)
    a = json.loads(ra.build_render_manifest(candidate_urls=json.dumps(["https://cdn/a/EXEC/transcoded.mp4"]),
                                            manifest_id="", session_id="s"))
    _stub(monkeypatch)
    b = json.loads(ra.build_render_manifest(candidate_urls=json.dumps(["https://cdn/b/EXEC/transcoded.mp4"]),
                                            manifest_id="", session_id="s"))
    assert _mid(a) != _mid(b)


def test_caller_supplied_id_still_wins(monkeypatch):
    # the hook always passes its deterministic id; that must be honored verbatim.
    _stub(monkeypatch)
    out = json.loads(ra.build_render_manifest(candidate_urls=json.dumps(["https://cdn/x/EXEC/transcoded.mp4"]),
                                              manifest_id="hook-key-123", session_id="s"))
    assert _mid(out) == "hook-key-123"


def test_resolve_manifest_id_no_uuid_fallback():
    # the tool now returns "" (not a uuid) when none passed, so build derives the content hash.
    f = getattr(ra._resolve_manifest_id, "func", None) or getattr(ra._resolve_manifest_id, "_tool_func", None) or ra._resolve_manifest_id
    assert f({}) == ""
    assert f({"manifest_id": "abc"}) == "abc"


def test_empty_manifest_id_never_writes_dot_json(monkeypatch):
    # D (session a314ed42/916301b7): an empty manifest_id wrote `.../render/.json`, which every
    # empty-id render then overwrote/collided on. _write_manifest_to_s3 must never key a manifest
    # empty — derive from the body's `id`, else a content hash.
    import render_agent as ra
    captured = {}

    class _S3:
        def put_object(self, Bucket, Key, Body, ContentType):
            captured["key"] = Key

    monkeypatch.setattr(ra, "_s3", _S3())
    monkeypatch.setattr(ra, "S3_BUCKET", "b")

    # Empty id + a manifest carrying its own id -> use that id.
    k1 = ra._write_manifest_to_s3('{"id": "abc123def"}', "sess", "")
    assert k1 and k1.endswith("/render/abc123def.json")
    assert "/render/.json" not in k1

    # Empty id + no body id -> deterministic content hash (same body -> same key), never `.json`.
    k2 = ra._write_manifest_to_s3('{"tracks": [], "template": "nle-timeline"}', "sess", "")
    k2b = ra._write_manifest_to_s3('{"tracks": [], "template": "nle-timeline"}', "sess", "")
    assert "/render/.json" not in k2 and k2.endswith(".json")
    assert k2 == k2b  # deterministic

    # A supplied id is honored verbatim.
    k3 = ra._write_manifest_to_s3('{"tracks": []}', "sess", "mid-1")
    assert k3.endswith("/render/mid-1.json")
