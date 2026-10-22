"""Reading many result files must cost one call, not one call per file.

A four-job fan-out previously spent 7 presign calls plus 4 fetches — 11 model
round-trips to read 4 JSON files — because every file needed its own presign and
its own fetch. These tests pin the batching, the caps that protect the context
window, and the schema-derived presign argument (nothing about the federated
tool is hardcoded).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tools import result_files as rf  # noqa: E402


class _Spec:
    """Minimal stand-in for a Strands tool object."""

    def __init__(self, name, schema, wrap_json=False):
        self.tool_name = name
        inner = schema
        self.tool_spec = {"inputSchema": {"json": inner} if wrap_json else inner}


class TestArgNameFromSchema:
    def test_required_property_wins(self):
        t = _Spec("media-outputs___get_download_url", {
            "type": "object",
            "properties": {"expiry_seconds": {"type": "integer"},
                           "s3uri_or_key": {"type": "string"}},
            "required": ["s3uri_or_key"]})
        assert rf._arg_name(t) == "s3uri_or_key"

    def test_handles_the_json_wrapped_schema_shape(self):
        t = _Spec("x___get_download_url", {
            "type": "object",
            "properties": {"ref": {"type": "string"}},
            "required": ["ref"]}, wrap_json=True)
        assert rf._arg_name(t) == "ref"

    def test_falls_back_to_a_string_property(self):
        t = _Spec("x___get_download_url", {
            "type": "object", "properties": {"key": {"type": "string"}}})
        assert rf._arg_name(t) == "key"

    def test_no_properties_means_unknown(self):
        assert rf._arg_name(_Spec("x", {"type": "object"})) is None
        assert rf._arg_name(_Spec("x", {})) is None


class TestPresignScraping:
    class _Client:
        def __init__(self, payload):
            self.payload = payload
            self.calls = []

        def call_tool_sync(self, use_id, name, args):
            self.calls.append((name, args))
            return self.payload

    class _Gw:
        def __init__(self, client):
            self._client = client

    def _resolve(self, payload, uri="s3://b/micro-workflow/x/shots.json"):
        client = self._Client(payload)
        gw = self._Gw(client)
        t = _Spec("m___get_download_url", {"properties": {"s3uri_or_key": {"type": "string"}},
                                           "required": ["s3uri_or_key"]})
        return rf._presign_one(gw, t, "s3uri_or_key", uri), client

    def test_extracts_the_presigned_url(self):
        url, client = self._resolve({"url": "https://bucket.s3.amazonaws.com/x/shots.json?sig=1"})
        assert url == "https://bucket.s3.amazonaws.com/x/shots.json?sig=1"
        assert client.calls[0][1] == {"s3uri_or_key": "s3://b/micro-workflow/x/shots.json"}

    def test_ignores_diagnostic_links_in_an_error_payload(self):
        """A validation error embeds errors.pydantic.dev; returning it would
        silently produce an unusable URL."""
        url, _ = self._resolve({"detail": "see https://errors.pydantic.dev/2.12/v/missing"})
        assert url is None

    def test_prefers_the_url_carrying_the_filename(self):
        url, _ = self._resolve({"other": "https://cdn/unrelated.bin",
                                "url": "https://cdn/x/shots.json?sig=2"})
        assert url == "https://cdn/x/shots.json?sig=2"

    def test_presign_failure_is_not_fatal(self):
        class Boom:
            def call_tool_sync(self, *a, **k):
                raise RuntimeError("gateway down")
        t = _Spec("m___get_download_url", {"properties": {"k": {"type": "string"}},
                                           "required": ["k"]})
        assert rf._presign_one(self._Gw(Boom()), t, "k", "s3://b/k.json") is None


class TestReadResultFiles:
    def _stub_fetch(self, monkeypatch, bodies):
        """Serve bodies by URL, recording how many fetches happened."""
        calls = []

        from security.url_guard import FetchResult

        def fake_fetch(url, *, max_bytes, timeout=None, truncate=False, **_kw):
            calls.append(url)
            if url not in bodies:
                raise RuntimeError("404")
            data = bodies[url]
            return FetchResult(status=200, headers={}, body=data[:max_bytes], url=url,
                               truncated=len(data) > max_bytes)

        # Every fetch goes through the shared SSRF guard (security.url_guard.fetch).
        monkeypatch.setattr(rf.url_guard, "fetch", fake_fetch)
        return calls

    def test_reads_every_file_in_one_call(self, monkeypatch):
        bodies = {f"https://cdn/f{i}.json": json.dumps({"i": i}).encode() for i in range(4)}
        calls = self._stub_fetch(monkeypatch, bodies)
        out = json.loads(rf.read_result_files(list(bodies.keys())))
        assert out["count"] == 4
        assert [f["json"]["i"] for f in out["files"]] == [0, 1, 2, 3]
        assert len(calls) == 4

    def test_order_matches_the_input(self, monkeypatch):
        bodies = {"https://cdn/b.json": b'{"n":"b"}', "https://cdn/a.json": b'{"n":"a"}'}
        self._stub_fetch(monkeypatch, bodies)
        out = json.loads(rf.read_result_files(["https://cdn/b.json", "https://cdn/a.json"]))
        assert [f["ref"] for f in out["files"]] == ["https://cdn/b.json", "https://cdn/a.json"]

    def test_non_json_comes_back_as_text(self, monkeypatch):
        self._stub_fetch(monkeypatch, {"https://cdn/a.srt": b"1\n00:00:01 --> 00:00:02\nhi"})
        out = json.loads(rf.read_result_files(["https://cdn/a.srt"]))
        f = out["files"][0]
        assert f["ok"] and "json" not in f and "hi" in f["text"]

    def test_oversized_file_is_truncated_not_dropped(self, monkeypatch):
        self._stub_fetch(monkeypatch, {"https://cdn/big.json": b"x" * 5000})
        out = json.loads(rf.read_result_files(["https://cdn/big.json"], max_bytes_each=1000))
        f = out["files"][0]
        assert f["ok"] and f["truncated"] and f["bytes"] == 1000

    def test_one_failure_does_not_lose_the_others(self, monkeypatch):
        self._stub_fetch(monkeypatch, {"https://cdn/ok.json": b'{"a":1}'})
        out = json.loads(rf.read_result_files(["https://cdn/ok.json", "https://cdn/gone.json"]))
        assert out["files"][0]["ok"] is True
        assert out["files"][1]["ok"] is False and "error" in out["files"][1]

    def test_unresolvable_s3_ref_is_reported_per_ref(self, monkeypatch):
        # No gateway presign AND direct read also unavailable -> reported per ref (not a crash).
        monkeypatch.setattr(rf, "_presign_tools", lambda: [])
        monkeypatch.setattr(rf, "_read_s3_direct",
                            lambda uri, limit: {"ok": False, "error": "could not resolve to a fetchable URL"})
        out = json.loads(rf.read_result_files(["s3://b/micro-workflow/x/shots.json"]))
        f = out["files"][0]
        assert f["ok"] is False and "resolve" in f["error"]

    def test_s3_ref_falls_back_to_direct_read_when_no_gateway(self, monkeypatch):
        # The regression: no download-url gateway connected in this runtime, so presign yields
        # nothing — the file must still be read directly from S3 (the runtime role can), not
        # reported 'unfetchable'. Wiring check: _read_s3_direct is used for the unpresigned ref.
        monkeypatch.setattr(rf, "_presign_tools", lambda: [])
        monkeypatch.setattr(rf, "_read_s3_direct",
                            lambda uri, limit: {"ok": True, "bytes": 12, "truncated": False,
                                                "json": {"n_shots": 7}})
        out = json.loads(rf.read_result_files(["s3://proxy/micro-workflow/x/shots.json"]))
        assert out["files"][0]["json"]["n_shots"] == 7

    def test_s3_refs_are_resolved_then_fetched(self, monkeypatch):
        monkeypatch.setattr(rf, "_presign_many",
                            lambda uris: {u: "https://cdn/shots.json" for u in uris})
        self._stub_fetch(monkeypatch, {"https://cdn/shots.json": b'{"n_shots":15}'})
        out = json.loads(rf.read_result_files(["s3://b/micro-workflow/x/shots.json"]))
        assert out["files"][0]["json"]["n_shots"] == 15

    def test_ref_count_is_capped_and_reported(self, monkeypatch):
        bodies = {f"https://cdn/f{i}.json": b'{"i":0}' for i in range(rf.MAX_REFS + 3)}
        self._stub_fetch(monkeypatch, bodies)
        out = json.loads(rf.read_result_files(list(bodies.keys())))
        assert out["count"] == rf.MAX_REFS
        assert "note" in out

    def test_total_budget_protects_the_context_window(self, monkeypatch):
        big = b"y" * rf.MAX_BYTES_PER_FILE
        refs = [f"https://cdn/f{i}.bin" for i in range(6)]
        self._stub_fetch(monkeypatch, {r: big for r in refs})
        out = json.loads(rf.read_result_files(refs))
        kept = [f for f in out["files"] if f["ok"]]
        omitted = [f for f in out["files"] if not f["ok"]]
        assert kept and omitted, "expected the budget to stop before the last file"
        assert sum(f["bytes"] for f in kept) <= rf.MAX_TOTAL_BYTES
        assert "budget" in omitted[0]["error"]

    def test_empty_and_string_inputs_are_tolerated(self, monkeypatch):
        assert json.loads(rf.read_result_files([]))["count"] == 0
        assert json.loads(rf.read_result_files(["", "  "]))["count"] == 0
        self._stub_fetch(monkeypatch, {"https://cdn/a.json": b'{"a":1}'})
        out = json.loads(rf.read_result_files("https://cdn/a.json"))
        assert out["count"] == 1 and out["files"][0]["json"] == {"a": 1}


class TestRegistration:
    """A tool the model cannot see is a tool that changes nothing.

    Being in the catalogue is NOT enough: main.py binds only the names in
    `_added_builtins`. The first deployed build registered the tool in
    `_all_builtin_tools` but not in that set, so the agent never saw it and went
    on presigning and fetching one file per round-trip — the exact cost this
    tool exists to remove. Assert availability, not mere registration.
    """

    def _main_src(self):
        return (Path(__file__).resolve().parents[1] / "src" / "main.py").read_text()

    def test_present_in_the_catalogue(self):
        assert '"read_result_files": read_result_files' in self._main_src()

    def test_actually_bound_to_the_agent(self):
        src = self._main_src()
        start = src.index("_added_builtins.update({")
        added = src[start:src.index("})", start)]
        assert '"read_result_files"' in added, "in the catalogue but never bound"

    def test_available_to_dispatched_sub_agents(self):
        task = (Path(__file__).resolve().parents[1] / "src" / "task_agent.py").read_text()
        assert '"read_result_files": read_result_files' in task
