"""Translate use case (#3, session 96198138): a diarize node's structured output is a POINTER
whose prefix key is `output_s3_prefix` (not `s3uri`). The chainer's inliner only recognized
`s3uri`, so it never listed the prefix, the transcript was never inlined, and the translate node
(no read tool) got a bare pointer and refused. The inliner must recognize an s3:// prefix under
ANY key and inline the transcript dialogue (which fits the input window).
"""
from __future__ import annotations

import io
import json
import os
import sys

os.environ.setdefault("PLAN_STATE_TABLE", "t")
os.environ.setdefault("JOB_REGISTRY_TABLE", "t")
os.environ.setdefault("TASK_AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/task")
sys.path.insert(0, os.path.dirname(__file__))
import handler  # noqa: E402


class _StubS3:
    def __init__(self, objs):
        self.objs = objs  # {key: text}

    def list_objects_v2(self, Bucket, Prefix="", **k):
        keys = [x for x in self.objs if x.startswith(Prefix)]
        return {"Contents": [{"Key": x} for x in keys]} if keys else {}

    def head_object(self, Bucket, Key):
        return {"ContentLength": len(self.objs.get(Key, ""))}

    def get_object(self, Bucket, Key, **k):
        return {"Body": io.BytesIO(self.objs[Key].encode("utf-8"))}


_TRANSCRIPT = json.dumps({
    "language": "en", "num_sentences": 2,
    "sentences": [
        {"text": "So these guys just disappeared?", "start": 26.978, "end": 29.318},
        {"text": "That's what the report says.", "start": 30.0, "end": 31.5},
    ],
})


def test_diarize_output_prefix_inlines_transcript_dialogue():
    tkey = "micro-workflow/speech-diarization/EXEC/dynamic/transcripts/transcript_0.json"
    handler._s3 = _StubS3({tkey: _TRANSCRIPT})
    # Real diarize structured value: a pointer keyed output_s3_prefix (NOT s3uri).
    value = {"job_id": "arn:aws:states:...:EXEC", "status": "SUCCEEDED",
             "output_s3_prefix": "s3://m2cv6-proxy/micro-workflow/speech-diarization/EXEC/"}
    out, inlined = handler._inline_primary_text_artifact(value)
    assert inlined is True, "diarize output_s3_prefix transcript was NOT inlined (the translate bug)"
    assert "So these guys just disappeared?" in json.dumps(out)
