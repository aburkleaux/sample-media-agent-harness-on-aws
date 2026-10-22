"""P4 MaxTokens resilience: a plan node truncated at the output-token cap
(stop_reason == "max_tokens") must be recorded FAILED with actionable guidance to build
the document out-of-band (compile_pdf/report failure class), not silently completed with
half a document. P2's error-aware retry then re-dispatches it with that guidance.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from task_agent import _maxtokens_failure_output  # noqa: E402


def test_no_failure_when_not_truncated():
    assert _maxtokens_failure_output("end_turn", ["done"]) is None
    assert _maxtokens_failure_output("tool_use", ["x"]) is None
    assert _maxtokens_failure_output("", ["x"]) is None


def test_truncation_produces_actionable_out_of_band_guidance():
    msg = _maxtokens_failure_output("max_tokens", ["# Report\npart one ", "part two"])
    assert msg is not None
    assert msg.lower().startswith("error:")
    # steers to text output in SECTIONS (platform auto-continues + assembles), and warns AGAINST
    # embedding the whole document in a tool call (the mid-tool-use truncation that can't recover)
    assert "section" in msg.lower()
    assert "code_interpreter" in msg.lower()   # mentioned as what NOT to do
    assert "tool call" in msg.lower()
    # forbids the placeholder shortcut that shipped an empty PDF (author emitted
    # "(full report included above)" instead of the real text on retry)
    assert "placeholder" in msg.lower()
    assert "included above" in msg.lower() or "in this response" in msg.lower()
    # preserves a preview of what was generated
    assert "part one part two" in msg


def test_truncation_preview_is_bounded():
    big = ["x" * 5000]
    msg = _maxtokens_failure_output("max_tokens", big)
    assert msg is not None
    # the preview run is capped at 1500 chars (guidance prose may contain stray 'x's)
    assert "x" * 1501 not in msg
    assert "x" * 1500 in msg


def test_maxtokens_output_with_empty_preview():
    # the exception path passes [] (partial content is lost inside the helper on raise) — the
    # guidance must still be actionable and must not crash on an empty chunk list
    msg = _maxtokens_failure_output("max_tokens", [])
    assert msg is not None
    assert "section" in msg.lower()
    assert "tool call" in msg.lower()   # steers away from embedding the doc in a tool call


def test_exception_path_detects_maxtokens():
    from task_agent import _is_maxtokens_error
    # the real Strands message when a node truncates mid-generation / mid-tool-use
    assert _is_maxtokens_error("Model stopped generating due to maximum token limit") is True
    assert _is_maxtokens_error("hit max_tokens") is True
    assert _is_maxtokens_error("exceeded max tokens") is True
    # unrelated errors are not misclassified
    assert _is_maxtokens_error("ValidationException: bad input") is False
    assert _is_maxtokens_error("") is False
    assert _is_maxtokens_error(None) is False


def test_continuation_decision_is_bounded():
    from task_agent import _should_continue, MAX_NODE_CONTINUATIONS
    # continue while truncated and under budget
    assert _should_continue("max_tokens", 0) is True
    assert _should_continue("max_tokens", MAX_NODE_CONTINUATIONS - 1) is True
    # stop at the budget (prevents a runaway continuation loop)
    assert _should_continue("max_tokens", MAX_NODE_CONTINUATIONS) is False
    # never continue a clean stop
    assert _should_continue("end_turn", 0) is False
    assert _should_continue("tool_use", 0) is False
    assert _should_continue("", 0) is False


# ---- entry->exit tests of the continuation loop itself (not just the decision) ----

import asyncio


class _Result:
    def __init__(self, stop_reason):
        self.stop_reason = stop_reason


class _FakeAgent:
    """Scripts a sequence of streams. Each script is (list_of_text_chunks, stop_reason).
    Records the prompt received on each stream_async call so we can assert the continue nudge."""
    def __init__(self, scripts):
        self.scripts = scripts
        self.call_prompts = []
        self._i = 0

    async def stream_async(self, prompt):
        self.call_prompts.append(prompt)
        chunks, stop = self.scripts[self._i]
        self._i += 1
        for c in chunks:
            yield {"data": c}
        yield {"result": _Result(stop)}


def test_loop_single_pass_no_continuation():
    from task_agent import _generate_with_continuation
    a = _FakeAgent([(["hello ", "world"], "end_turn")])
    out, stop, cont = asyncio.run(_generate_with_continuation(a, "do it", "n1"))
    assert out == "hello world"
    assert stop == "end_turn"
    assert cont == 0
    assert len(a.call_prompts) == 1


def test_loop_completes_after_truncation():
    from task_agent import _generate_with_continuation
    # truncate once, then finish on the continuation → the two pieces must be concatenated
    a = _FakeAgent([(["part1 "], "max_tokens"), (["part2"], "end_turn")])
    out, stop, cont = asyncio.run(_generate_with_continuation(a, "do it", "n1"))
    assert out == "part1 part2"
    assert stop == "end_turn"
    assert cont == 1
    assert len(a.call_prompts) == 2
    assert "Continue EXACTLY" in a.call_prompts[1]   # the continue nudge was sent, not the original prompt


def test_loop_caps_when_always_truncated():
    from task_agent import _generate_with_continuation, MAX_NODE_CONTINUATIONS
    scripts = [([f"c{i} "], "max_tokens") for i in range(MAX_NODE_CONTINUATIONS + 5)]
    a = _FakeAgent(scripts)
    out, stop, cont = asyncio.run(_generate_with_continuation(a, "do it", "n1"))
    assert stop == "max_tokens"                        # still truncated → caller routes out-of-band
    assert cont == MAX_NODE_CONTINUATIONS              # capped: no runaway loop
    assert len(a.call_prompts) == MAX_NODE_CONTINUATIONS + 1   # initial + N continuations
    # all partial content up to the cap is preserved in the concatenation
    assert out.startswith("c0 c1 ")


def test_loop_assembles_multi_part_report_within_raised_budget():
    """The core of the large-text fix: a report that needs several turns must ASSEMBLE across
    continuations and complete — not fail. Truncate 4x then finish. With the raised budget (6)
    this succeeds with the full text; under the old budget (3) it would have hit the cap and
    been recorded FAILED (the sg3_synthesize_report failure class)."""
    from task_agent import _generate_with_continuation, MAX_NODE_CONTINUATIONS
    assert MAX_NODE_CONTINUATIONS >= 5   # headroom for a multi-section report
    scripts = [([f"sec{i} "], "max_tokens") for i in range(4)] + [(["done"], "end_turn")]
    a = _FakeAgent(scripts)
    out, stop, cont = asyncio.run(_generate_with_continuation(a, "write the report", "sg3"))
    assert stop == "end_turn"                         # completed cleanly -> node SUCCEEDS
    assert cont == 4                                   # four continuations used
    assert out == "sec0 sec1 sec2 sec3 done"           # full report assembled across turns


def test_maxtokens_message_steers_artifact_recovery_to_by_reference():
    """For a PDF/artifact node that truncated, the guidance must say: emit the doc as TEXT and
    let a SEPARATE downstream step read the stored text BY REFERENCE to render — never inline."""
    low = _maxtokens_failure_output("max_tokens", ["body"]).lower()
    assert "by reference" in low
    assert "downstream" in low
    assert "never receive it inline" in low


def test_text_node_emits_plain_prose_not_json_envelope():
    # research->PDF (36c99c2d): a text/author node must NOT be told to wrap the whole document in a
    # {"text": ...} JSON block (that guarantees the output-token cap mid-emission). It emits markdown.
    from task_agent import _output_format_for
    txt = _output_format_for("text")
    low = txt.lower()
    assert "normal response text" in low or "markdown" in low
    assert "speech_stem_uri" not in txt            # no structured JSON example for a text node
    assert "do not wrap" in low                     # explicitly forbids the JSON envelope
    assert "reference" in low                       # stored + read downstream by reference
    assert "placeholder" in low                     # still forbids the placeholder shortcut


def test_structured_node_still_emits_json_block():
    from task_agent import _output_format_for
    for ot in ("s3_uri", "json", "", None):
        fmt = _output_format_for(ot)
        assert "```json" in fmt and "downstream" in fmt.lower()
