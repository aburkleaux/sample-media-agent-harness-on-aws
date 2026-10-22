from planning.dag_schema import DAGNode, Plan
from planning.validator import validate_plan


def _node(**kw):
    base = dict(id="n", prompt="p", tools=[], depends_on=[],
                output_key="k", output_type="text")
    base.update(kw)
    return DAGNode(**base)


def test_external_input_is_not_treated_as_missing_dependency():
    # A cross-plan producer is NOT in this plan's node set. It must NOT be
    # rejected as invalid_dependency (that check only covers depends_on).
    plan = Plan(plan_id="p", goal="g", nodes=[
        _node(id="sg5",
              external_inputs=[{"plan": "plan-cm-analysis", "node": "sg2_diarize_speech"}]),
    ])
    errors = validate_plan(plan, available_tools=set())
    assert not [e for e in errors if e.error_type == "invalid_dependency"]


def test_malformed_external_input_is_rejected():
    plan = Plan(plan_id="p", goal="g", nodes=[
        _node(id="sg5", external_inputs=[{"plan": "plan-cm-analysis"}]),  # missing "node"
    ])
    errors = validate_plan(plan, available_tools=set())
    assert [e for e in errors if e.error_type == "invalid_external_input"]
