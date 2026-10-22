import json

from planning.dag_schema import DAGNode, Plan


def test_external_inputs_round_trips():
    node = DAGNode(
        id="sg5", prompt="report", tools=["media-outputs___get_download_url"],
        depends_on=[], output_key="report", output_type="s3_uri",
        external_inputs=[{"plan": "plan-cm-analysis", "node": "sg2_diarize_speech"}],
    )
    d = node.to_dict()
    assert d["external_inputs"] == [{"plan": "plan-cm-analysis", "node": "sg2_diarize_speech"}]
    assert DAGNode.from_dict(d).external_inputs == node.external_inputs


def test_external_inputs_defaults_empty_and_omitted_when_empty():
    node = DAGNode(id="n", prompt="p", tools=[], depends_on=[],
                   output_key="k", output_type="text")
    assert node.external_inputs == []
    # Omitted when empty so existing TASK-node serialization is byte-for-byte unchanged.
    assert "external_inputs" not in node.to_dict()


def test_external_inputs_survive_orchestrator_to_chainer_wire():
    # plan_and_execute_tool persists nodes as json.dumps([n.to_dict() ...]); the
    # chainer parses that json back to dicts. external_inputs must survive intact so
    # the chainer's _external_injection_text can read node_def["external_inputs"].
    plan = Plan(plan_id="p", goal="moderate", nodes=[
        DAGNode(id="sg5", prompt="report", tools=[], depends_on=[],
                output_key="report", output_type="s3_uri",
                external_inputs=[
                    {"plan": "plan-content-moderation-analysis", "node": "sg2_diarize_speech"},
                    {"plan": "plan-content-moderation-analysis", "node": "sg3_enrich_shot_metadata"},
                ]),
    ])
    wire = json.dumps([n.to_dict() for n in plan.nodes])   # orchestrator write
    node_def = json.loads(wire)[0]                          # chainer read
    assert node_def["external_inputs"] == [
        {"plan": "plan-content-moderation-analysis", "node": "sg2_diarize_speech"},
        {"plan": "plan-content-moderation-analysis", "node": "sg3_enrich_shot_metadata"},
    ]
    # And the whole-plan JSON round-trip preserves it too.
    assert Plan.from_json(plan.to_json()).nodes[0].external_inputs == plan.nodes[0].external_inputs
