"""A plan node that started a Step Functions job stays `processing` until the job ends.

Staging 82654e2f (plan-media-analysis-trim151) and 2f8bd1c4 (content moderation): every node that
started an M2C job was written `completed` while the job still ran ("SFN status=RUNNING -> node
processing", then "plan state updated: completed"). The `else: effective_status = status` that closes
the async/queue chain sat on the queue-REGISTRATION `if` (split out 2026-08-06), so a node with an SFN
ARN and no fal queue handle fell into it. The bridge never reopens a completed row ("already finished;
job … not applied"), the chainer waited for an event that never came, and the plans hung.

`_execute_plan_node` is too large to drive here, so — like its sibling tests — this pins the control
flow by source (via the AST, so formatting does not matter).
"""
import ast
import pathlib

SRC = (pathlib.Path(__file__).resolve().parents[1] / "src" / "task_agent.py").read_text()


def _plan_node_fn() -> ast.AsyncFunctionDef:
    tree = ast.parse(SRC)
    return next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_execute_plan_node")


def _if_on(fn, test_src: str) -> ast.If:
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and ast.unparse(node.test) == test_src:
            return node
    raise AssertionError(f"no `if {test_src}` in _execute_plan_node")


def _assigns_effective_status_from_status(stmts) -> bool:
    return any(isinstance(s, ast.Assign) and ast.unparse(s) == "effective_status = status" for s in stmts)


def test_the_sync_fallback_closes_the_async_chain():
    fn = _plan_node_fn()
    chain = _if_on(fn, "is_async_job")
    elif_queue = chain.orelse[0]
    assert isinstance(elif_queue, ast.If) and ast.unparse(elif_queue.test) == "queue_handle"
    # `if is_async_job: … elif queue_handle: … else: effective_status = status`
    assert _assigns_effective_status_from_status(elif_queue.orelse)


def test_the_queue_registration_if_has_no_status_fallback():
    fn = _plan_node_fn()
    registration = _if_on(fn, "queue_handle and JOB_REGISTRY_TABLE and actor_id")
    # Its `else` is what overwrote an SFN node's `processing` with `completed`.
    assert not _assigns_effective_status_from_status(registration.orelse)
    assert registration.orelse == []


def test_effective_status_is_assigned_exactly_once_per_branch_of_the_chain():
    fn = _plan_node_fn()
    chain = _if_on(fn, "is_async_job")
    sfn_src = "\n".join(ast.unparse(s) for s in chain.body)
    assert "effective_status = _node_status_for_adopted_job(adopted_sfn_status)" in sfn_src
    queue_src = "\n".join(ast.unparse(s) for s in chain.orelse[0].body)
    assert "_node_status_for_queue_job(" in queue_src
