"""The optimization runner must target the live deployment, not stale constants.

This script could not have worked. It pinned `REGION = "us-east-1"` while the agent's
log groups are in us-west-2, pinned an agent id (`-Fs0KI074Ow`) that no longer exists
(the deployed runtime is `-iwK54XE4sd`), and carried a verbatim COPY of the system
prompt that had drifted from `agent/src/main.py`. Each of those fails silently: you
get an empty span collection, or advice about a prompt that is not deployed.
"""
import ast
import os
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SRC = (HERE / "optimize_run.py").read_text()


def test_region_comes_from_the_deployment_config():
    # Not a literal, and not a generic AWS_REGION: this environment exports
    # us-east-1, which is how the original defect stayed invisible.
    assert "deployment_region()" in SRC
    assert 'os.environ.get("AWS_REGION")' not in SRC
    assert "us-east-1" not in SRC and "us-west-2" not in SRC


def test_agent_id_is_discovered_not_pinned():
    assert "def _discover_main_agent_id" in SRC
    # the stale id must not reappear
    assert "Fs0KI074Ow" not in SRC


def test_system_prompt_is_read_from_source():
    assert "_current_system_prompt" in SRC
    assert "build_orchestrator_system_prompt" in SRC


def test_no_verbatim_prompt_copy_remains():
    """A second copy of the prompt is the thing that rotted."""
    # the old copy's distinctive wording must be gone
    assert "DEFAULT for any goal that involves research" not in SRC


def test_the_prompt_it_reads_matches_the_live_one():
    tree = ast.parse((ROOT / "agent" / "src" / "prompts" / "orchestrator.py").read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "build_orchestrator_system_prompt")
    live = "".join(n.value for n in ast.walk(fn)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str))
    assert len(live) > 1000
    # a rule added during this work must be visible to the optimizer
    assert "RETRIEVE AND READ" in live


def test_sessions_are_supplied_not_baked_in():
    assert "OPTIMIZE_SESSIONS" in SRC
    # the stale harvested session ids must be gone
    assert "eval-harvested-skill-selection-in-one-sentence" not in SRC
