"""A failed turn must end visibly, not spin forever.

Observed: a multi-node DAG request sat on "Reading file..." for an hour. The agent had
already failed at 17:37:52 with `Model stopped generating due to maximum token limit`
(MaxTokensReachedException), but the handler only logged it and set `stream_done`. With
no terminal output the client kept displaying the last step indicator, so a dead turn
was indistinguishable from a slow one.
"""
import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

MAIN = (SRC / "main.py").read_text()


class TestErrorText:
    def test_output_limit_gets_an_actionable_message(self):
        from main import _stream_error_text
        msg = _stream_error_text(
            "Model stopped generating due to maximum token limit. The partial message "
            "has been added to the conversation history."
        )
        assert "output token limit" in msg
        # the remedy differs from a plain retry
        assert "continue" in msg and "smaller steps" in msg
        assert "nothing is lost" in msg

    def test_other_errors_report_what_happened(self):
        from main import _stream_error_text
        msg = _stream_error_text("ThrottlingException: rate exceeded")
        assert "This turn failed" in msg
        assert "ThrottlingException" in msg

    def test_an_empty_error_still_says_something(self):
        from main import _stream_error_text
        assert _stream_error_text(None).strip()
        assert _stream_error_text("").strip()

    def test_message_is_generic(self):
        """No tool names or domain nouns — this fires for any failure."""
        from main import _stream_error_text
        for probe in ("Model stopped generating due to maximum token limit", "boom"):
            msg = _stream_error_text(probe)
            for leak in ("plan_and_execute", "dispatch_tool", "shots.json",
                         "Diarize", "render tab", "media-processing"):
                assert leak not in msg


class TestErrorBranchYields:
    """Static guard: the error branch must produce output, not just log.

    A unit test cannot easily drive the streaming generator to failure, and this is the
    property that actually broke — so assert it on the source.
    """

    def test_the_stream_error_branch_yields_to_the_client(self):
        tree = ast.parse(MAIN)
        target = None
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                    and test.left.id == "tag"
                    and any(isinstance(c, ast.Constant) and c.value == "error"
                            for c in test.comparators)):
                target = node
                break
        assert target is not None, 'no `if tag == "error"` branch found'
        yields = [n for n in ast.walk(target) if isinstance(n, (ast.Yield, ast.YieldFrom))]
        assert yields, (
            "the stream-error branch does not yield — a failed turn would leave the "
            "client showing its last step indicator indefinitely"
        )

    def test_it_yields_both_a_step_and_an_explanation(self):
        assert '_step_event("error", "Turn failed")' in MAIN
        assert "yield _stream_error_text(value)" in MAIN
