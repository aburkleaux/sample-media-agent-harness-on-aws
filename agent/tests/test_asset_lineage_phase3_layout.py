"""asset-lineage Phase 3 (a): session outputs under ``projects/<pid>/sessions/<sid>/output/`` (design §9.1).

Covers, per consumer:

- writers: main / task agent / crop_video / orchestrator guidance pick the project root only with the flag
  AND a trusted project (main: ``project_access == "ok"``; task agent: the forwarded producing project);
- the ``artifact`` tool: a model-passed new-layout key is not doubled, ``adopt`` relocates a file written
  under the legacy session prefix, ``download`` finds an older output under the legacy root;
- readers (flag-independent): foreign-session filters, own-key recognition, URL↔key parsing, the sandbox
  URL presigner, turn-output detection, the malformed-id guard, image-search scope, render keys,
  adopted-output lookup;
- ingestion: an s3-location import of ``projects/<pid>/`` skips ``sessions/``; ``remove`` deletes a
  new-layout output of the asset's own Project and never one of another Project.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from projects import storage_layout as sl  # noqa: E402

PID = "p" + "a" * 32
OTHER_PID = "p" + "b" * 32
SID = "4f9d0d02-1111-4222-8333-444455556666"
OTHER_SID = "7e1f0a0b-1111-4222-8333-444455556666"
NEW_OUT = f"projects/{PID}/sessions/{SID}/output/"
OLD_OUT = f"artifacts/{SID}/output/"


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv(sl.LAYOUT_ENV, "project")


@pytest.fixture
def bound(monkeypatch):
    """A request context with session SID; returns a setter for the main-turn project context."""
    from jobs import context as jc
    from project_search import context as psc

    toks = jc.set_request_context(actor_id="actor-1", session_id=SID)

    def set_turn(pid=PID, access="ok"):
        tc = psc.TurnContext.parse({"active_project": {"id": pid, "collection_id": pid, "name": "P"},
                                    "project_access": access})
        psc.current_turn_context.set(tc)

    yield set_turn
    psc.current_turn_context.set(None)
    jc.reset_request_context(toks)


# ------------------------------------------------------------------ trusted project id
def test_trusted_project_requires_ok_access(bound):
    from jobs import context as jc

    bound(access="ok")
    assert jc.trusted_project_id() == PID
    bound(access="unavailable")
    assert jc.trusted_project_id() is None
    # current_producing_project_id keeps its broader (registration) meaning.
    assert jc.current_producing_project_id() == PID


def test_trusted_project_falls_back_to_the_forwarded_one(bound):
    from jobs import context as jc
    from project_search import context as psc

    psc.current_turn_context.set(None)
    tok = jc.set_producing_project_id(PID)
    try:
        assert jc.trusted_project_id() == PID
    finally:
        jc.reset_producing_project_id(tok)
    tok = jc.set_producing_project_id("personal")
    try:
        assert jc.trusted_project_id() is None, "only a project id is trusted"
    finally:
        jc.reset_producing_project_id(tok)


# ------------------------------------------------------------------ writers
def test_main_output_prefix(bound, flag_on, monkeypatch):
    import main

    bound(access="ok")
    assert main._artifact_output_prefix() == NEW_OUT
    bound(access="unavailable")
    assert main._artifact_output_prefix() == OLD_OUT
    monkeypatch.delenv(sl.LAYOUT_ENV)
    bound(access="ok")
    assert main._artifact_output_prefix() == OLD_OUT


def test_orchestrator_guidance_names_the_project_prefix(flag_on):
    from prompts.orchestrator import build_session_output_guidance

    text = build_session_output_guidance(SID, "bkt", "artifacts/", "us-west-2", "", project_id=PID)
    assert f"s3://bkt/{NEW_OUT}<filename>" in text and f"`{NEW_OUT}`" in text
    assert OLD_OUT not in text
    assert f"s3://bkt/{OLD_OUT}" in build_session_output_guidance(SID, "bkt", "artifacts/", project_id=None)


def test_task_agent_guidance_reads_the_forwarded_project_from_the_payload(flag_on):
    import task_agent

    prompt = task_agent._resolve_system_prompt({"session_id": SID, "producing_project_id": PID})
    assert f"{NEW_OUT}<filename>" in prompt
    prompt = task_agent._resolve_system_prompt({"session_id": SID})
    assert f"{OLD_OUT}<filename>" in prompt and "projects/" not in prompt.split("S3 CONTEXT", 1)[1][:400]


def test_task_agent_prefix_from_the_bound_producing_project(bound, flag_on):
    import task_agent
    from jobs import context as jc
    from project_search import context as psc

    psc.current_turn_context.set(None)
    tok = jc.set_producing_project_id(PID)
    try:
        assert task_agent._artifact_output_prefix() == NEW_OUT
    finally:
        jc.reset_producing_project_id(tok)
    assert task_agent._artifact_output_prefix() == OLD_OUT


def test_crop_video_output_prefix(bound, flag_on):
    from tools import crop_video

    bound(access="ok")
    assert crop_video._output_prefix() == NEW_OUT


# ------------------------------------------------------------------ the artifact tool
class _FakeS3:
    def __init__(self, keys=()):
        self.objects = {k: {"ContentType": "video/mp4"} for k in keys}
        self.copies = []

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise RuntimeError("404")
        return self.objects[Key]

    def copy_object(self, Bucket, Key, CopySource, **kw):
        self.copies.append((CopySource["Key"], Key))
        self.objects[Key] = {"ContentType": kw.get("ContentType", "")}

    def generate_presigned_url(self, *a, **k):
        return "https://presigned"


def _artifact(mod, **kw):
    fn = mod.artifact
    return json.loads(fn._tool_func(**kw) if hasattr(fn, "_tool_func") else fn(**kw))


@pytest.mark.parametrize("modname", ["main", "task_agent"])
def test_adopt_relocates_a_legacy_session_write_into_the_project_root(modname, bound, flag_on, monkeypatch):
    mod = __import__(modname)
    bound(access="ok")
    tok = None
    if modname == "task_agent":
        from jobs import context as jc
        tok = jc.set_producing_project_id(PID)
    s3 = _FakeS3([OLD_OUT + "clip.mp4"])
    monkeypatch.setattr(mod, "s3_client" if modname == "main" else "_s3", s3)
    monkeypatch.setattr(mod, "S3_BUCKET", "bkt")
    monkeypatch.setattr(mod, "ARTIFACTS_DOMAIN", "cdn.example")
    try:
        out = _artifact(mod, operation="adopt", key="clip.mp4")
        assert out["key"] == NEW_OUT + "clip.mp4"
        assert s3.copies == [(OLD_OUT + "clip.mp4", NEW_OUT + "clip.mp4")]
        # A key the model already prefixed with the new root is not doubled.
        out = _artifact(mod, operation="download", key=NEW_OUT + "clip.mp4")
        assert out["key"] == NEW_OUT + "clip.mp4"
        # download falls back to an output this session wrote before the layout.
        s3.objects[OLD_OUT + "older.mp4"] = {}
        out = _artifact(mod, operation="download", key="older.mp4")
        assert out["key"] == OLD_OUT + "older.mp4"
        assert "error" in _artifact(mod, operation="download", key="missing.mp4")
    finally:
        if tok is not None:
            from jobs import context as jc
            jc.reset_producing_project_id(tok)


# ------------------------------------------------------------------ readers
def test_foreign_session_filter_reads_both_roots():
    from hooks.actions import _url_is_foreign_session

    assert _url_is_foreign_session(f"https://cdn/projects/{PID}/sessions/{OTHER_SID}/output/a.mp4", SID)
    assert not _url_is_foreign_session(f"https://cdn/{NEW_OUT}a.mp4", SID)
    assert _url_is_foreign_session(f"https://cdn/artifacts/{OTHER_SID}/output/a.mp4", SID)
    assert not _url_is_foreign_session("https://cdn/artifacts/external/0123456789abcdef01234567.mp4", SID)


def test_provider_recognises_new_layout_outputs():
    from hooks import provider

    assert provider._is_own_media_ref(f"https://cdn.example/{NEW_OUT}clip.mp4")
    assert not provider._is_own_media_ref(f"https://cdn.example/projects/{PID}/video:x/clip.mp4")
    assert provider._is_probe_answer_json(f"https://cdn.example/{NEW_OUT}metadata.json")


def test_own_artifact_key_accepts_project_session_outputs_only(monkeypatch):
    from media import artifact_fetch as af

    monkeypatch.setenv("ARTIFACTS_DOMAIN", "cdn.example")
    key = NEW_OUT + "clip.mp4"
    assert af.own_artifact_key(key) == key
    assert af.own_artifact_key(f"https://cdn.example/{key}") == key
    assert af.own_artifact_key(f"/api/artifact?key={key}") == key
    assert af.own_artifact_key(f"projects/{PID}/video:x/source.mp4") is None, "asset bytes stay asset-id-gated"
    assert af.own_artifact_key(f"projects/{PID}/sessions/{SID}/render/m.json") is None
    assert af.own_artifact_key(f"https://cdn.example/{NEW_OUT}../../x") is None
    assert af.own_artifact_key(f"https://cdn.example/{OLD_OUT}clip.mp4") == OLD_OUT + "clip.mp4"


def test_sandbox_code_urls_under_the_new_root_are_presigned(monkeypatch):
    from media import artifact_fetch as af

    monkeypatch.setenv("ARTIFACTS_DOMAIN", "cdn.example")
    monkeypatch.setenv("SKILLS_S3_BUCKET", "bkt")
    seen = []
    monkeypatch.setattr(af, "presign_artifact_url", lambda u, e=0: seen.append(u) or "https://signed")
    code = f"requests.get('https://cdn.example/{NEW_OUT}clip.mp4')"
    out, changed = af.presign_artifact_urls_in_text(code)
    assert changed and seen == [f"https://cdn.example/{NEW_OUT}clip.mp4"] and "https://signed" in out
    # A project ASSET url is not an artifact and is left alone.
    out, changed = af.presign_artifact_urls_in_text(f"'https://cdn.example/projects/{PID}/video:x/a.mp4'")
    assert not changed


def test_turn_outputs_own_key_and_other_session(monkeypatch, bound):
    from media import turn_outputs as to

    monkeypatch.setattr(to, "_own_bucket", lambda: "bkt")
    assert to._is_own_output_key(f"s3://bkt/{NEW_OUT}clip.mp4")
    assert not to._is_own_output_key(f"s3://bkt/projects/{PID}/sessions/{SID}/render/m.json")
    assert to._under_other_session(f"s3://bkt/projects/{PID}/sessions/{OTHER_SID}/output/x.mp4")
    assert not to._under_other_session(f"s3://bkt/{NEW_OUT}x.mp4")
    assert to._under_other_session(f"s3://bkt/artifacts/{OTHER_SID}/output/x.mp4")


def test_malformed_id_guard_covers_the_new_root():
    from media.source_media import _ARTIFACT_SEG_RE

    m = _ARTIFACT_SEG_RE.search(f"s3://bkt/projects/{PID}/sessions/{SID}x/output/a.mp4")
    assert m and m.group(1) == SID + "x"


def test_a_session_output_image_is_searched_by_its_asset_not_its_location():
    # A session output is registered IN PLACE as a project asset; as a query image it goes by its asset_id
    # (search_image_asset_id -> its Search_Rendition). Its own location is an original, which M2C's search
    # role cannot read (staging 2026-10-15: AccessDenied), so the pre-check refuses it before M2C.
    from project_search.call_checks import CallScope, check_image_uri

    scope = CallScope(ingest_bucket="bkt", proxy_bucket="px", session_id=SID, project_id=PID,
                      frame_keys=frozenset)
    assert not check_image_uri(f"s3://bkt/{NEW_OUT}frame.jpg", scope)
    assert not check_image_uri(f"s3://bkt/projects/{OTHER_PID}/sessions/{SID}/output/frame.jpg", scope)
    assert check_image_uri(f"s3://bkt/projects/{PID}/image:1/rendition.jpg", scope)


def test_render_key_from_new_layout_paths():
    import render_agent

    urls = [f"https://cdn/{NEW_OUT}plan-1/node-a/reel.mp4", f"https://cdn/{NEW_OUT}plan-1/node-a/shots.json"]
    assert render_agent.render_key_from_artifact_paths(urls) == "plan-1--node-a"


def test_adopted_output_keys_cover_both_roots_and_manifest_projects():
    from render import artifacts as ra

    keys = ra._adopted_output_keys(SID, "e" * 8 + "-0000-4000-8000-" + "0" * 12, "clip.mp4", [PID])
    assert keys[0].startswith(OLD_OUT) and keys[1].startswith(NEW_OUT)
    manifest = {"tracks": [{"src": f"https://cdn/{NEW_OUT}x.mp4"},
                           {"src": f"https://cdn/projects/{OTHER_PID}/sessions/{OTHER_SID}/output/y.mp4"}]}
    assert ra._manifest_project_ids(manifest, SID) == [PID]


# ------------------------------------------------------------------ ingestion
def test_registrar_treats_project_sessions_as_managed():
    from ingestion.registrar import _is_managed_key

    pfx = f"projects/{PID}/"
    assert _is_managed_key(f"{pfx}sessions/{SID}/output/a.mp4", pfx)
    assert _is_managed_key(f"{pfx}graph/_meta.json", pfx)
    assert not _is_managed_key(f"{pfx}incoming/a.mp4", pfx)


def test_remove_owns_a_new_layout_output_of_its_own_project_only():
    from ingestion.handlers.asset_service import AssetService

    svc = AssetService.__new__(AssetService)
    svc._ingest_bucket = "bkt"
    row = {"source": "session", "sourceUri": f"s3://bkt/{NEW_OUT}clip.mp4"}
    assert svc._owned_keys(PID, "video:x", row) == [NEW_OUT + "clip.mp4"]
    assert svc._owned_keys(OTHER_PID, "video:x", row) == [], "never another Project's bytes"
    row = {"source": "session", "sourceUri": f"s3://bkt/projects/{PID}/sessions/{SID}/render/m.json"}
    assert svc._owned_keys(PID, "video:x", row) == []
    assert svc._owned_keys(PID, "video:x", {"source": "session", "sourceUri": f"s3://bkt/{OLD_OUT}a.mp4"}) == [
        OLD_OUT + "a.mp4"]


from test_ingestion_remove import BUCKET as _RM_BUCKET, PID as _RM_PID, _call, ctx  # noqa: E402,F401


def test_s3_location_import_of_a_project_prefix_skips_session_outputs(ctx):  # noqa: F811
    service, _store, s3, _t = ctx
    sess = f"projects/{_RM_PID}/sessions/{SID}/output/clip.mp4"
    plain = f"projects/{_RM_PID}/incoming/source.mp4"
    for k in (sess, plain):
        s3.put_object(Bucket=_RM_BUCKET, Key=k, Body=b"v" * 10, ContentType="video/mp4")
    reg = _call(service, "register_s3_location", {"location": f"s3://{_RM_BUCKET}/projects/{_RM_PID}/"})
    uris = [r["sourceUri"] for r in reg["value"]["registered"]]
    assert uris == [f"s3://{_RM_BUCKET}/{plain}"], "a session output is never re-registered as an s3 source"


# ------------------------------------------------------------------ review fixes
class _ListS3(_FakeS3):
    def list_objects_v2(self, Bucket, Prefix, MaxKeys=1000):
        return {"Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)]}


@pytest.mark.parametrize("modname", ["main", "task_agent"])
def test_list_shows_both_roots_for_a_session_spanning_the_flip(modname, bound, flag_on, monkeypatch):
    mod = __import__(modname)
    bound(access="ok")
    tok = None
    if modname == "task_agent":
        from jobs import context as jc
        tok = jc.set_producing_project_id(PID)
    s3 = _ListS3([OLD_OUT + "before.mp4", NEW_OUT + "after.mp4", f"artifacts/{OTHER_SID}/output/x.mp4"])
    monkeypatch.setattr(mod, "s3_client" if modname == "main" else "_s3", s3)
    monkeypatch.setattr(mod, "S3_BUCKET", "bkt")
    try:
        keys = _artifact(mod, operation="list")["keys"]
        assert set(keys) == {OLD_OUT + "before.mp4", NEW_OUT + "after.mp4"}, keys
    finally:
        if tok is not None:
            jc.reset_producing_project_id(tok)


def test_dispatch_forwards_only_a_trusted_project(bound, monkeypatch):
    """A main turn whose project access is not `ok` writes legacy; its sub-agents must not write
    under (or get sandbox credentials for) that project either."""
    from orchestration import dispatch_tool as dt
    from orchestration import dispatch_credentials as dc

    monkeypatch.setattr(dc, "dispatch_credentials", lambda *a, **k: {})
    bound(access="ok")
    payload: dict = {}
    dt._forward_request_context(payload)
    assert payload.get("producing_project_id") == PID
    bound(access="unavailable")
    payload = {}
    dt._forward_request_context(payload)
    assert "producing_project_id" not in payload


def test_plan_row_and_job_stamp_use_the_trusted_project():
    import inspect
    from orchestration import plan_and_execute_tool as pe
    from jobs import strands_tool_wrap as stw

    for mod in (pe, stw):
        src = inspect.getsource(mod)
        assert "producing_project_id=jobs_context.trusted_project_id()" in src, mod.__name__
        assert "producing_project_id=jobs_context.current_producing_project_id()" not in src, mod.__name__


def test_guidance_tells_the_model_to_build_clients_per_cell(flag_on):
    import task_agent
    from prompts.orchestrator import build_session_output_guidance

    g1 = task_agent._s3_context_guidance(SID, PID)
    g2 = build_session_output_guidance(SID, "bkt", "artifacts/", "us-west-2", "", project_id=PID)
    for g in (g1, g2):
        assert "SAME code_interpreter cell" in g and NEW_OUT in g
