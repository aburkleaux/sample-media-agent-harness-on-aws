"""The orchestrator must not fabricate that an artifact is 'already rendered'.

Observed (session e97db0cb): asked to show a finished video, the agent replied it was
"already rendered and playing in the panel above" — it cannot see the user's panel, so this
was an unverifiable claim, and being wrong forced the user to ask again ("I don't see it").
These pin that the rule to (re)render on ask — and to never assert a render happened — is
present and stays generic (no model/tool/filename leaks that would rot).
"""
import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))


def _prompt() -> str:
    """The orchestrator prompt's literal text, read from source (no live gateway needed)."""
    tree = ast.parse((SRC / "prompts" / "orchestrator.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "build_orchestrator_system_prompt":
            return " ".join(
                n.value for n in ast.walk(node)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            )
    raise AssertionError("build_orchestrator_system_prompt not found")


def test_prompt_forbids_claiming_already_rendered():
    p = _prompt().lower()
    assert "cannot see the user's render panel" in p
    assert "already rendered" in p           # the exact anti-pattern is named
    assert "playing above" in p


def test_prompt_requires_rerender_on_ask():
    p = _prompt().lower()
    # asking to see / not seeing a result must trigger an actual (re)render this turn
    assert "re)render request" in p or "(re)render request" in p
    assert "dispatch_tool(target='render')" in p


def test_render_claim_rule_stays_generic():
    """No model ids, tool names, or filenames — the rule must hold for any artifact."""
    p = _prompt()
    for leak in ["flux", "minimax", "z-image", "h3-max", "probe.json", "combine_final", "fal-ai"]:
        assert leak.lower() not in p.lower(), f"render-claim rule leaked {leak!r}"
