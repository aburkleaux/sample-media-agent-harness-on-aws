"""LOCAL HARNESS — inter-node handoff (design doc §0/§3). Reproduces the recurring report/PDF
failure from session bd12a66c with NO LLM, NO gateway, NO deploy, so we iterate + validate locally.

Real failure: the author_summary node's PERSISTED output file holds the full 21 KB document, but
its structured `value` is a small "…delivered above" PLACEHOLDER. The chainer injects the small
`value` inline (its size is under the cap, so the size-triggered reference never fires), so
format_pdf receives only the placeholder — no content, no S3 URI — and cannot render.

DESIGN TARGET (§3 A1): the consumer ALWAYS receives a deterministic reference to the dependency's
persisted output file, regardless of the `value`'s size. These tests assert that target; they are
RED on the current size-triggered code and GREEN once A1 lands.
"""
from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("PLAN_STATE_TABLE", "test-plan-state")
os.environ.setdefault("JOB_REGISTRY_TABLE", "test-job-registry")
os.environ.setdefault("TASK_AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/task")

sys.path.insert(0, os.path.dirname(__file__))

import pytest  # noqa: E402
import handler  # noqa: E402

# The real bd12a66c shapes.
_PLACEHOLDER_VALUE = {
    "summary_document_text": (
        "Full document authored with 4 sections (Overview & Purpose; Available Offerings; How It "
        "Works: Architecture & Integration; References) as delivered in the assistant's markdown "
        "response above."
    )
}
_FULL_DOC = "# AWS MCP Servers: Overview, Offerings, and Technical Mechanics\n\n## 1. Overview\n" + ("MCP is an open protocol. " * 1500)  # ~21 KB, like the real file


class _FileS3:
    """S3 double: the per-node output FILE holds the full document (the durable truth)."""
    def __init__(self, files):
        self.files = files

    def get_object(self, Bucket, Key):
        if Key in self.files:
            import io
            return {"Body": io.BytesIO(self.files[Key].encode("utf-8"))}
        raise Exception("NoSuchKey")

    def list_objects_v2(self, Bucket, Prefix="", **kw):
        keys = [k for k in self.files if k.startswith(Prefix)]
        return {"Contents": [{"Key": k} for k in keys]} if keys else {}


@pytest.fixture(autouse=True)
def _bucket(monkeypatch):
    monkeypatch.setattr(handler, "SKILLS_S3_BUCKET", "skills-bkt")


def test_repro_bd12a66c_consumer_gets_reference_not_placeholder():
    session, plan = "61f9fcf0", "bd12a66c"
    output_key = f"plans/{session}/{plan}/author_summary/output"
    handler._s3 = _FileS3({output_key: _FULL_DOC})
    manifest = {"outputs": {"author_summary": {"status": "completed", "value": _PLACEHOLDER_VALUE}}}
    node = {"id": "format_pdf", "depends_on": ["author_summary"]}
    node_states = {"author_summary": {"result_s3_key": output_key}}

    text = handler._dependency_injection_text(node, manifest["outputs"], node_states, session, plan)

    # DESIGN TARGET (RED on current size-triggered code): format_pdf must get the deterministic
    # reference to the persisted file, so it can read the real 21 KB — not just the placeholder.
    assert output_key in text, "consumer was NOT given the persisted-file reference (the bug)"
    assert "read" in text.lower(), "consumer was not told to read the file for the full content"


def test_a2_gateway_prefix_resolves_named_deliverables():
    # Design §3 A2: a gateway producer's `value` carries an s3 PREFIX + `*_key` deliverable names,
    # not a filename. The consumer must receive the EXACT deliverable uris so it never invents
    # `final_transcript.json` / a wrong `audio.wav`. The framework resolves them via
    # result_resolution.canonical_result_refs and injects them.
    session, plan = "S", "P"
    output_key = f"plans/{session}/{plan}/diarize/output"
    handler._s3 = _FileS3({output_key: "{}"})
    gateway_value = {
        "output": {"s3uri": "s3://bkt/mw/speech-diarization/EXEC/"},
        "data": {
            "final_transcript_key": "mw/speech-diarization/EXEC/final_transcript.json",
            "speaker_manifest_key": "mw/speech-diarization/EXEC/speaker_manifest.json",
            "frames_to_shots_cfg": {"is_large_lambda": True},   # config noise, not a deliverable
        },
    }
    dep_outputs = {"diarize": {"status": "completed", "value": gateway_value}}
    node = {"id": "translate", "depends_on": ["diarize"]}
    node_states = {"diarize": {"result_s3_key": output_key}}

    text = handler._dependency_injection_text(node, dep_outputs, node_states, session, plan)

    assert "DELIVERABLE FILES" in text
    deliverables = text.split("DELIVERABLE FILES", 1)[1]
    assert "s3://bkt/mw/speech-diarization/EXEC/final_transcript.json" in deliverables   # exact uri, not guessed
    assert "s3://bkt/mw/speech-diarization/EXEC/speaker_manifest.json" in deliverables
    assert "frames_to_shots_cfg" not in deliverables                                     # config noise excluded
