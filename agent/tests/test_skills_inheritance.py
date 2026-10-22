"""A sub-agent runs with the turn's skills unless told otherwise.

The actor's skills reached the orchestrator's per-request skills plugin only. A
sub-agent's skills arrived as an LLM-filled ``dispatch_tool`` argument the model
left empty -- observed as ``Dispatching to task-agent: 1 tools, 0 skills`` while
that same turn narrated using the aws-research skill. Skills the actor has must
propagate by construction, not depend on the model re-serializing whole skill
bodies. This mirrors ``test_model_inheritance.py`` for the model case.

By-reference (forward names, sub-agent loads) is infeasible: the deployed Task
Agent builds ``Skill`` objects from ``payload["skills"]`` dicts and has no
per-actor name->definition resolver on the internal dispatch path, so the
orchestrator forwards resolved definitions instead. These tests pin the
precedence and assert BOTH dispatch sites implement inheritance so they cannot
drift.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobs.context import current_active_skills, set_active_skills  # noqa: E402

_SKILL = {"name": "aws-research", "description": "d", "instructions": "i"}
_OTHER = {"name": "explicit-skill", "description": "d2", "instructions": "i2"}


@pytest.fixture(autouse=True)
def _clear_binding():
    set_active_skills([])
    yield
    set_active_skills([])


def test_unbound_returns_empty_not_raises():
    """Management calls and tests run with no turn bound; [] means "inherit nothing"."""
    assert current_active_skills() == []


def test_binding_round_trips():
    set_active_skills([_SKILL])
    assert current_active_skills() == [_SKILL]


def test_none_and_empty_normalise_to_empty():
    set_active_skills([])
    assert current_active_skills() == []
    set_active_skills(None)  # type: ignore[arg-type]
    assert current_active_skills() == []


def _resolve(explicit, bound):
    """Mirror the precedence both dispatch sites implement."""
    set_active_skills(bound)
    skills = explicit
    if not skills:
        skills = current_active_skills() or []
    return skills


def test_explicit_choice_still_wins():
    """A caller that names its own skills is not overridden by the turn's set."""
    assert _resolve([_OTHER], [_SKILL]) == [_OTHER]


def test_silence_inherits_the_turn_skills():
    """The regression: no skills named must NOT mean "send none"."""
    assert _resolve([], [_SKILL]) == [_SKILL]


def test_silence_with_no_binding_sends_none():
    assert _resolve([], []) == []


def test_both_dispatch_paths_implement_inheritance():
    """dispatch_tool and plan_and_execute_tool must not drift apart."""
    src = Path(__file__).resolve().parents[1] / "src"
    for name in ("dispatch_tool.py", "plan_and_execute_tool.py"):
        text = (src / "orchestration" / name).read_text(encoding="utf-8")
        assert "current_active_skills" in text, f"{name} does not inherit the turn skills"


def test_main_binds_the_active_skills():
    """Inheritance is worthless if nothing binds the value."""
    text = (Path(__file__).resolve().parents[1] / "src" / "main.py").read_text(encoding="utf-8")
    assert "set_active_skills(" in text
