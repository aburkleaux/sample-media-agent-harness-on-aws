"""Enable continuous (online) evaluation + monitoring on the main agent.

Creates an AgentCore online evaluation config that continuously samples live
traffic and scores it with built-in evaluators. Results land in a dedicated
CloudWatch log group + EMF metrics under the Bedrock-AgentCore/Evaluations
namespace (build dashboards/alarms from there).

    agent/.venv/bin/python -m tests.evaluation.online_eval_setup [--disable]

NOTE: this creates an IAM execution role (auto) and an ongoing, billable
evaluation job (LLM-judge on sampled sessions). Pause via:
    agentcore pause online-eval <config_name>
or ExecutionStatus DISABLED.
"""
import sys

CONFIG_NAME = "mediaworks_harness_main_online_eval"
AGENT_ID = "mediaworks_harness_main_agent-Fs0KI074Ow"
EVALUATORS = ["Builtin.GoalSuccessRate", "Builtin.Helpfulness", "Builtin.Correctness"]
SAMPLING_RATE = 100.0  # percent (0-100); dev is low-volume so evaluate all sampled sessions


def main():
    from bedrock_agentcore_starter_toolkit import Evaluation
    eval_client = Evaluation()

    if "--describe" in sys.argv:
        import boto3
        c = boto3.client("bedrock-agentcore-control", region_name=deployment_region())
        cid = "mediaworks_harness_main_online_eval-L2DFxa21jI"
        for op in ("get_online_evaluation_config",):
            try:
                import json as _j
                resp = getattr(c, op)(onlineEvaluationConfigId=cid)
                print(_j.dumps({k: v for k, v in resp.items() if k != "ResponseMetadata"}, indent=2, default=str))
            except Exception as e:
                print(f"{op}: {type(e).__name__}: {e}")
        return

    if "--disable" in sys.argv:
        # Best-effort disable/pause if a management method exists.
        for m in ("disable_online_config", "pause_online_config"):
            fn = getattr(eval_client, m, None)
            if fn:
                print(fn(config_name=CONFIG_NAME))
                return
        print("No disable method found; use `agentcore pause online-eval` or the console.")
        return

    config = eval_client.create_online_config(
        config_name=CONFIG_NAME,
        agent_id=AGENT_ID,
        sampling_rate=SAMPLING_RATE,
        evaluator_list=EVALUATORS,
        config_description="Continuous monitoring of the mediaworks-harness main agent (GoalSuccessRate/Helpfulness/Correctness).",
        auto_create_execution_role=True,
        enable_on_create=True,
    )
    print("Online evaluation configuration created:")
    for k in ("onlineEvaluationConfigId", "onlineEvaluationConfigArn", "status",
              "executionStatus", "outputConfig", "failureReason"):
        if isinstance(config, dict) and k in config:
            print(f"  {k}: {config[k]}")
    print(f"raw: {config}")


if __name__ == "__main__":
    main()
