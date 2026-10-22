"""A sub-agent runs on the turn's model unless told otherwise.

The user's model selection reached the orchestrator only. A sub-agent's model
arrived as an LLM-chosen `dispatch_tool` argument picked off the catalogue in
the system prompt, so a session set to Sonnet 5 dispatched its real work to
claude-sonnet-4-6 -- the user paid for a model they did not choose, and the
badge and the worker disagreed. Config the user sets must propagate by
construction, not depend on the model restating it.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobs.context import current_model_id, set_model_id  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_binding():
    set_model_id("")
    yield
    set_model_id("")


def test_unbound_returns_empty_not_raises():
    """Management calls and tests run with no turn bound; "" means "you choose"."""
    assert current_model_id() == ""


def test_binding_round_trips():
    set_model_id("us.anthropic.claude-sonnet-5")
    assert current_model_id() == "us.anthropic.claude-sonnet-5"


def test_none_and_empty_normalise_to_empty():
    set_model_id("")
    assert current_model_id() == ""
    set_model_id(None)  # type: ignore[arg-type]
    assert current_model_id() == ""


def _resolve(explicit, bound):
    """Mirror the precedence both dispatch sites implement."""
    set_model_id(bound)
    model_id = explicit
    if not model_id:
        model_id = current_model_id() or None
    return model_id


def test_explicit_choice_still_wins():
    """Naming a cheap model for a trivial subtask stays legitimate."""
    assert _resolve("us.anthropic.claude-haiku-4-5", "us.anthropic.claude-sonnet-5") == (
        "us.anthropic.claude-haiku-4-5"
    )


def test_silence_inherits_the_turn_model():
    """The regression: no model named must NOT mean "free choice"."""
    assert _resolve(None, "us.anthropic.claude-sonnet-5") == "us.anthropic.claude-sonnet-5"


def test_silence_with_no_binding_defers_to_callee():
    assert _resolve(None, "") is None


def test_both_dispatch_paths_implement_inheritance():
    """dispatch_tool and plan_and_execute_tool must not drift apart."""
    src = Path(__file__).resolve().parents[1] / "src"
    for name in ("dispatch_tool.py", "plan_and_execute_tool.py"):
        text = (src / "orchestration" / name).read_text(encoding="utf-8")
        assert "current_model_id" in text, f"{name} does not inherit the turn model"


def test_main_binds_the_resolved_model():
    """Inheritance is worthless if nothing binds the value."""
    text = (Path(__file__).resolve().parents[1] / "src" / "main.py").read_text(encoding="utf-8")
    assert "set_model_id(resolved_model_id)" in text
