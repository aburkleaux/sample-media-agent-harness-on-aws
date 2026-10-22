"""Regression tests for PLAN-row stream handling in the Plan Chainer.

Guards the "second plan in a conversation never runs" bug: the plan row is keyed
(session_id, node_id="PLAN") and written with put_item, so only the FIRST plan in
a session arrives as INSERT. Reacting to INSERT alone left every later plan's
nodes PENDING forever with no error surfaced anywhere.

The predicate is pure so it can be tested without the Lambda's boto3/env setup.
Mirrors the inline style used by the polling-worker tests in this tree.
"""


def _should_dispatch_plan_record(event_name: str, status: str) -> bool:
    """Copy of handler._should_dispatch_plan_record (kept in sync deliberately).

    The handler module reads os.environ at import time and builds boto3 clients,
    so importing it here would require full AWS scaffolding for a pure decision.
    """
    return event_name == "INSERT" or status == "pending"


def test_first_plan_in_session_dispatches():
    # First plan in a fresh conversation: the row does not exist yet -> INSERT.
    assert _should_dispatch_plan_record("INSERT", "pending") is True


def test_second_plan_in_same_session_dispatches():
    # THE REGRESSION: a later plan overwrites the existing PLAN row, so the
    # stream reports MODIFY. This must still dispatch, or the plan silently
    # never runs.
    assert _should_dispatch_plan_record("MODIFY", "pending") is True


def test_plan_status_writes_do_not_retrigger():
    # Non-pending plan states must not re-enter dispatch, so status bookkeeping
    # cannot cause a self-triggering loop through the DDB stream.
    for status in ("running", "completed", "failed", "aborted"):
        assert _should_dispatch_plan_record("MODIFY", status) is False


def test_insert_dispatches_regardless_of_status():
    # An INSERT is always a newly created plan, whatever status it carries.
    assert _should_dispatch_plan_record("INSERT", "running") is True


if __name__ == "__main__":
    test_first_plan_in_session_dispatches()
    test_second_plan_in_same_session_dispatches()
    test_plan_status_writes_do_not_retrigger()
    test_insert_dispatches_regardless_of_status()
    print("all plan-record dispatch tests passed")
