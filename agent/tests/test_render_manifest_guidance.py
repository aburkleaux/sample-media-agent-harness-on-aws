"""Fix 2 guard: the sub-agent is told not to hand-write render manifests.

The harness renders tool results deterministically (hooks/actions.py, which already
coalesces multi-image results into ONE gallery). A sub-agent hand-writing its own
render-manifest JSON via code_interpreter/artifact only adds a redundant 'Generic'
data-file tab and wastes steps, so the default task prompt must warn against it.
"""
from task_agent import DEFAULT_TASK_SYSTEM_PROMPT


def test_default_task_prompt_forbids_hand_written_manifests():
    p = DEFAULT_TASK_SYSTEM_PROMPT.lower()
    assert "render-manifest" in p or "render manifest" in p
    assert "never write" in p
    # names the mechanism so the guidance is actionable
    assert "code_interpreter" in p and "artifact" in p


def test_default_task_prompt_states_render_is_automatic():
    assert "RENDERING IS AUTOMATIC" in DEFAULT_TASK_SYSTEM_PROMPT
