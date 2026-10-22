"""Text deltas must reach the caller as they are generated, not in one final frame.

Measured before this change, on the real dev-portal route: a custom agent invoke
emitted 3 SSE frames with exactly ONE text frame — the model spent 2,731 ms
generating and the caller saw nothing until it finished. `stream_async` was
already yielding per-token deltas; `task_agent` appended them to a list and
yielded once at the end.

The load-bearing assertion here is NOT "streaming happened" but "streamed AND did
not also emit the joined text". Every consumer of this stream accumulates —
`dispatch_tool._consume_stream` does `"".join(chunks)`, the UI surfaces do
`+= event.text` — so a stray final `yield full_text` alongside the deltas shows
the answer TWICE instead of failing loudly.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

DELTAS = ["Hello", " streamed", " world"]
JOINED = "".join(DELTAS)


def _install_streaming_agent(monkeypatch, task_agent, deltas=DELTAS):
    """Replace ``Agent`` with a stub whose stream yields text deltas.

    Mirrors the shape Strands emits: ``{"data": "<text>"}`` per token. The real
    handler also reads dict-valued ``data`` (tool-use tracking) and ``result``
    events; omitting those is fine — they only add control frames.
    """
    class _StreamingAgent:
        def __init__(self, **_kwargs):
            pass

        def stream_async(self, _prompt):
            async def _gen():
                for d in deltas:
                    yield {"data": d}

            return _gen()

    monkeypatch.setattr(task_agent, "Agent", _StreamingAgent)
    monkeypatch.setattr(task_agent, "build_model", lambda **_kw: object())


@pytest.fixture
def runtime(monkeypatch):
    """task_agent with gateways stubbed and a delta-emitting Agent installed."""
    import task_agent

    async def _no_gateways(*_args, **_kwargs):
        pass

    monkeypatch.setattr(task_agent, "_ensure_gateways_loaded", _no_gateways)
    _install_streaming_agent(monkeypatch, task_agent)
    return task_agent


async def _drain(gen) -> list:
    return [e async for e in gen]


def _text_frames(events) -> list[str]:
    """The frames a consumer would treat as answer text.

    Mirrors how every real consumer classifies a frame: parse it, and anything
    that is a JSON *object* is a control frame (``step``, ``execution_summary``,
    ``output_ref``, ``error``). Everything else is answer text.
    """
    out = []
    for e in events:
        if not isinstance(e, str):
            continue
        try:
            parsed = json.loads(e)
        except (TypeError, ValueError):
            out.append(e)
            continue
        if not isinstance(parsed, dict):
            out.append(e)
    return out


@pytest.mark.asyncio
async def test_internal_invoke_streams_each_delta(runtime):
    """No principal → internal realm → each delta goes out as it arrives."""
    events = await _drain(runtime.invoke({"prompt": "hi"}, None))
    assert _text_frames(events) == DELTAS


@pytest.mark.asyncio
async def test_internal_invoke_does_not_also_emit_the_joined_answer(runtime):
    """The double-emit guard: deltas OR one joined frame, never both."""
    events = await _drain(runtime.invoke({"prompt": "hi"}, None))
    texts = _text_frames(events)
    assert JOINED not in texts, (
        "the joined answer was emitted alongside the deltas; every consumer "
        "accumulates, so the caller would see the answer twice"
    )
    assert "".join(texts) == JOINED


@pytest.mark.asyncio
async def test_external_invoke_streams_by_default(runtime):
    """Streaming is the default in every environment, external included.

    ``agent_id`` is deliberately absent so this is external (the redaction gate
    is ``isinstance(principal, dict)``) without being *agent-scoped* — which
    requires ``agent_id`` AND a marker — so no config resolution is needed.
    """
    payload = {"prompt": "hi", "principal": {"issuer": "https://i", "sub": "s"}}
    events = await _drain(runtime.invoke(payload, None))
    assert _text_frames(events) == DELTAS


@pytest.mark.asyncio
async def test_external_buffers_one_frame_when_the_flag_is_on(runtime, monkeypatch):
    """The escape hatch restores the old behavior: buffered, single frame."""
    monkeypatch.setattr(runtime, "EXTERNAL_OUTPUT_REDACTION", True)
    payload = {"prompt": "hi", "principal": {"issuer": "https://i", "sub": "s"}}
    events = await _drain(runtime.invoke(payload, None))
    assert _text_frames(events) == [JOINED]


@pytest.mark.asyncio
async def test_flag_on_redacts_a_secret_split_across_two_deltas(monkeypatch):
    """Why redaction and buffering are ONE decision, not two.

    The patterns match whole credentials. Split across deltas, `AKIA` and the
    16 following characters match nothing individually — so redaction is only
    sound on the joined text, which is exactly why the flag also disables
    streaming rather than redacting each delta.
    """
    import task_agent

    async def _no_gateways(*_args, **_kwargs):
        pass

    monkeypatch.setattr(task_agent, "_ensure_gateways_loaded", _no_gateways)
    _install_streaming_agent(
        monkeypatch, task_agent, deltas=["KEY=AKIA", "IOSFODNN7EXAMPLE"]
    )
    monkeypatch.setattr(task_agent, "EXTERNAL_OUTPUT_REDACTION", True)

    payload = {"prompt": "hi", "principal": {"issuer": "https://i", "sub": "s"}}
    events = await _drain(task_agent.invoke(payload, None))
    assert _text_frames(events) == ["KEY=[REDACTED]"]


@pytest.mark.asyncio
async def test_default_streams_that_secret_unredacted(monkeypatch):
    """Documents the deviation honestly rather than leaving it implicit.

    With the default (redaction off) the same split credential reaches the
    external caller verbatim. Recorded in the spec as a deliberate trade.
    """
    import task_agent

    async def _no_gateways(*_args, **_kwargs):
        pass

    monkeypatch.setattr(task_agent, "_ensure_gateways_loaded", _no_gateways)
    _install_streaming_agent(
        monkeypatch, task_agent, deltas=["KEY=AKIA", "IOSFODNN7EXAMPLE"]
    )

    payload = {"prompt": "hi", "principal": {"issuer": "https://i", "sub": "s"}}
    events = await _drain(task_agent.invoke(payload, None))
    assert _text_frames(events) == ["KEY=AKIA", "IOSFODNN7EXAMPLE"]


def test_consume_stream_joins_many_text_frames_identically():
    """The dispatcher's contract, which is why no consumer needs changing.

    ``dispatch_tool._consume_stream`` accumulates, so N small text frames must
    produce the same string as one large frame. Frames are double-encoded on the
    wire (AgentCore ``json.dumps``-es whatever the handler yields), so the fake
    lines below encode the text the same way.
    """
    from orchestration import dispatch_tool

    def _sse(payload_str: str) -> str:
        return f"data: {json.dumps(payload_str)}"

    many = [_sse(d) for d in DELTAS]
    one = [_sse(JOINED)]

    text_many, _, _ = dispatch_tool._consume_stream(iter(many))
    text_one, _, _ = dispatch_tool._consume_stream(iter(one))

    assert text_many == text_one == JOINED


@pytest.mark.asyncio
async def test_s3_output_ref_path_buffers_and_streams_nothing(monkeypatch):
    """A node invoke writes ONE object and emits an output_ref, never deltas.

    The plan chainer reads the S3 object, so streaming this path would leave it
    with nothing to read. ``ddb_table`` is blanked so only the S3 write happens.
    """
    import task_agent

    async def _no_gateways(*_args, **_kwargs):
        pass

    monkeypatch.setattr(task_agent, "_ensure_gateways_loaded", _no_gateways)
    _install_streaming_agent(monkeypatch, task_agent)

    puts = []

    class _FakeS3:
        def put_object(self, **kwargs):
            puts.append(kwargs)

    class _FakeBoto3:
        @staticmethod
        def client(service_name, **_kwargs):
            assert service_name == "s3", f"fake boto3 client expects 's3', got {service_name!r}"
            return _FakeS3()

    monkeypatch.setattr(task_agent, "boto3", _FakeBoto3)

    payload = {
        "prompt": "hi",
        "session_id": "sess-1",
        "node_id": "node-1",
        "s3_bucket": "bucket-1",
        "ddb_table": "",
    }
    events = await _drain(task_agent.invoke(payload, None))

    assert _text_frames(events) == [], "the S3 path must not stream text deltas"

    refs = []
    for e in events:
        try:
            parsed = json.loads(e)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict) and parsed.get("type") == "output_ref":
            refs.append(parsed)

    assert len(refs) == 1
    assert refs[0]["s3_key"] == "plans/sess-1/nodes/node-1/output"
    assert len(puts) == 1
    assert puts[0]["Body"] == JOINED.encode("utf-8")
    assert puts[0]["Bucket"] == "bucket-1"
    assert puts[0]["Key"] == "plans/sess-1/nodes/node-1/output"


@pytest.mark.asyncio
async def test_s3_write_failure_yields_one_joined_frame(monkeypatch):
    """S3 write failure falls back to yielding exactly one text frame, not deltas.

    The unguarded fallback in the except handler is safe only because the
    structural condition (_will_write_s3) locks the S3 path to buffered output.
    This test asserts the failure path yields the joined text exactly once, not
    zero times and not one-per-delta.
    """
    import task_agent

    async def _no_gateways(*_args, **_kwargs):
        pass

    monkeypatch.setattr(task_agent, "_ensure_gateways_loaded", _no_gateways)
    _install_streaming_agent(monkeypatch, task_agent)

    class _FakeS3:
        def put_object(self, **kwargs):
            raise Exception("S3 write failed")

    class _FakeBoto3:
        @staticmethod
        def client(service_name, **_kwargs):
            assert service_name == "s3"
            return _FakeS3()

    monkeypatch.setattr(task_agent, "boto3", _FakeBoto3)

    payload = {
        "prompt": "hi",
        "session_id": "sess-1",
        "node_id": "node-1",
        "s3_bucket": "bucket-1",
        "ddb_table": "",  # blank so only S3 write is exercised
    }
    events = await _drain(task_agent.invoke(payload, None))

    # The S3 write failed, so the fallback should yield the joined text exactly once
    texts = _text_frames(events)
    assert texts == [JOINED], (
        f"S3 write failure should yield exactly one joined text frame, got {texts}"
    )
    assert len(texts) == 1, "must not emit one-per-delta on failure"
