"""M2C run_inference calls default to thinking "adaptive" (bundle alpha.20261014a's Sonnet 5.5 rejects "disabled")."""
from tool_args import default_inference_thinking

TOOL = "media-workflow-gateway-v6-staging___bedrock-inference___run_inference"
NEW = {"records", "model_chain", "thinking", "use_batch"}


def test_adds_adaptive_when_omitted_and_declared():
    assert default_inference_thinking(TOOL, {"records": []}, NEW) == {"records": [], "thinking": "adaptive"}
    assert default_inference_thinking("bedrock-inference___run_inference", {}, NEW) == {"thinking": "adaptive"}


def test_never_overrides_the_callers_choice():
    args = {"records": [], "thinking": "disabled"}
    assert default_inference_thinking(TOOL, args, NEW) is args


def test_leaves_an_older_schema_and_other_tools_alone():
    args = {"records": []}
    assert default_inference_thinking(TOOL, args, {"records", "model_chain"}) is args
    assert default_inference_thinking(TOOL, args, None) is args
    assert default_inference_thinking("media-processing___process_media", args, NEW | {"asset_s3uri"}) is args
    assert default_inference_thinking("bedrock-inference___get_model", args, NEW) is args


def test_the_agents_gateway_round_trip_sends_it(monkeypatch):
    import asyncio

    from jobs import strands_tool_wrap as stw

    captured = {}

    async def _fake_stream(self, tool_use, invocation_state, **kwargs):
        captured["input"] = tool_use["input"]
        yield {"done": True}

    monkeypatch.setattr(stw.MCPAgentTool, "stream", _fake_stream)

    class _Tool(stw._ResilientMCPMixin):
        mcp_client = object()
        tool_name = "bedrock-inference___run_inference"
        tool_spec = {"name": tool_name, "inputSchema": {"json": {
            "type": "object", "properties": {p: {"type": "string"} for p in NEW}}}}

    async def _go():
        return [ev async for ev in _Tool()._base_stream_once(
            {"toolUseId": "t1", "name": _Tool.tool_name, "input": {"records": []}}, {}, {})]

    asyncio.run(_go())
    assert captured["input"] == {"records": [], "thinking": "adaptive"}
