"""GPT-family models reject images inside toolResult; the factory hoists them to sibling blocks (model/factory.py)."""
from __future__ import annotations

import os
import sys

from strands.models import BedrockModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from model import factory  # noqa: E402
from model.factory import (  # noqa: E402
    ToolResultImageHoistBedrockModel, build_model, hoist_tool_result_images, rejects_tool_result_images,
)

IMG = {"image": {"format": "webp", "source": {"bytes": b"RIFF...."}}}


def _tool_msg(*results):
    return {"role": "user", "content": list(results)}


def _tr(tid, *content):
    return {"toolResult": {"toolUseId": tid, "status": "success", "content": list(content)}}


def test_family_gate():
    assert rejects_tool_result_images("global.openai.gpt-6.1-sol")
    assert rejects_tool_result_images("openai.gpt-5.5")
    assert not rejects_tool_result_images("global.anthropic.claude-opus-5-5")
    assert not rejects_tool_result_images("")


def test_hoists_images_after_all_tool_results_and_keeps_text():
    msgs = [{"role": "user", "content": [{"text": "hi"}]},
            {"role": "assistant", "content": [{"toolUse": {"toolUseId": "a", "name": "run_model", "input": {}}}]},
            _tool_msg(_tr("a", {"text": '{"status":"completed"}'}, IMG), _tr("b", {"text": "plain"}))]
    out = hoist_tool_result_images(msgs)
    assert out[:2] == msgs[:2]
    blocks = out[2]["content"]
    assert [list(b)[0] for b in blocks] == ["toolResult", "toolResult", "text", "image"]
    a = blocks[0]["toolResult"]
    assert a["toolUseId"] == "a" and a["status"] == "success"
    assert not any("image" in c for c in a["content"])
    assert a["content"][0] == {"text": '{"status":"completed"}'} and "attached after" in a["content"][1]["text"]
    assert blocks[1] == _tr("b", {"text": "plain"})  # untouched
    assert "tool result a" in blocks[2]["text"] and blocks[3] == IMG


def test_image_only_result_gets_a_placeholder_text():
    out = hoist_tool_result_images([_tool_msg(_tr("a", IMG, IMG))])
    tr = out[0]["content"][0]["toolResult"]
    assert tr["content"] == [{"text": "[2 images returned by this tool are attached after the tool results]"}]
    assert sum("image" in b for b in out[0]["content"]) == 2


def test_no_images_is_identity():
    msgs = [_tool_msg(_tr("a", {"text": "x"})), {"role": "assistant", "content": [{"text": "y"}]}]
    out = hoist_tool_result_images(msgs)
    assert out == msgs and out[0] is msgs[0]


def test_input_is_not_mutated():
    msg = _tool_msg(_tr("a", {"text": "x"}, IMG))
    snapshot = repr(msg)
    hoist_tool_result_images([msg])
    assert repr(msg) == snapshot


def test_seam_exists_on_installed_strands():
    assert hasattr(BedrockModel, ToolResultImageHoistBedrockModel.SEAM_METHOD), (
        "strands renamed _format_bedrock_messages: GPT tool-result images will fail again — re-hook the seam")


def test_format_request_hoists_for_gpt():
    m = build_model("global.openai.gpt-6.1-sol")
    assert isinstance(m, ToolResultImageHoistBedrockModel)
    req = m._format_bedrock_messages([_tool_msg(_tr("a", {"text": "x"}, IMG))])
    tr = req[0]["content"][0]["toolResult"]
    assert not any("image" in c for c in tr["content"])
    assert any("image" in b for b in req[0]["content"])


def test_claude_models_unchanged():
    m = build_model("global.anthropic.claude-opus-5-5")
    assert not isinstance(m, ToolResultImageHoistBedrockModel)
    req = m._format_bedrock_messages([_tool_msg(_tr("a", {"text": "x"}, IMG))])
    assert any("image" in c for c in req[0]["content"][0]["toolResult"]["content"])


def test_uncached_fallback_keeps_the_hoist(monkeypatch):
    calls = []
    orig = ToolResultImageHoistBedrockModel.__init__

    def flaky(self, **kw):
        calls.append(kw)
        if len(calls) == 1:
            raise TypeError("unexpected keyword")
        orig(self, **kw)

    monkeypatch.setattr(ToolResultImageHoistBedrockModel, "__init__", flaky)
    m = build_model("global.openai.gpt-6.1-sol")
    assert isinstance(m, ToolResultImageHoistBedrockModel) and len(calls) == 2
    assert factory.__all__.count("hoist_tool_result_images") == 1
