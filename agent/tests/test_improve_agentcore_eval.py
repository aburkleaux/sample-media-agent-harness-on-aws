"""AgentCore online-eval scores: harvest -> sessions.json -> Run.resources -> per-model rows.

The producer half runs against a fake ``logs`` client whose requests are validated by botocore's
own service model (a fake looser than the real API hides total breakage), and whose rows are the
shape the real ``stats ... by sid, evaluator`` query returned on staging: every value a STRING.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from improve import harvest_live  # noqa: E402
from improve.contract import EvaluatorResult, Outcome, Overall, Run, UtilityVector, Verdict  # noqa: E402
from improve.dto import scorecard_to_wire  # noqa: E402
from improve.observe.build_run import HarvestedAgentCoreEvalSource  # noqa: E402

PREFIX = "/aws/bedrock-agentcore/evaluations/results/mediaworks_harness_"
MAIN_GROUP = PREFIX + "main_online_eval-UZKOq97qDT"
COMPONENT_GROUP = PREFIX + "component_online_eval-AbCdEf1234"


def _validate(service, operation, params):
    import botocore.session
    from botocore.validate import ParamValidator
    shape = (botocore.session.get_session()
             .get_service_model(service).operation_model(operation).input_shape)
    report = ParamValidator().validate(params, shape)
    assert not report.has_errors(), report.generate_report()


class _Paginator:
    def __init__(self, owner):
        self._owner = owner

    def paginate(self, **params):
        _validate("logs", "DescribeLogGroups", params)
        names = [g for g in self._owner.groups if g.startswith(params["logGroupNamePrefix"])]
        # Two pages, so the paginator loop is exercised.
        return iter([{"logGroups": [{"logGroupName": n} for n in names[:1]]},
                     {"logGroups": [{"logGroupName": n} for n in names[1:]]}])


class _FakeLogs:
    def __init__(self, groups, rows):
        self.groups, self.rows, self.queries = groups, rows, []

    def get_paginator(self, name):
        assert name == "describe_log_groups"
        return _Paginator(self)

    def start_query(self, **params):
        _validate("logs", "StartQuery", params)
        self.queries.append(params)
        return {"queryId": "q1"}

    def get_query_results(self, **params):
        _validate("logs", "GetQueryResults", params)
        return {"status": "Complete",
                "results": [[{"field": k, "value": v} for k, v in r.items()] for r in self.rows]}


class _Aws:
    def __init__(self, logs):
        self._logs = logs

    def client(self, name):
        assert name == "logs"
        return self._logs


ROWS = [  # as Insights returns them: strings, one row per (session, evaluator)
    {"sid": "s1", "evaluator": "Builtin.GoalSuccessRate", "mean": "1", "n": "1"},
    {"sid": "s1", "evaluator": "Builtin.Helpfulness", "mean": "0.8311", "n": "9"},
    {"sid": "s1", "evaluator": "Builtin.ToolSelectionAccuracy", "mean": "0.5", "n": "4"},
    {"sid": "s2", "evaluator": "Builtin.Helpfulness", "mean": "0.5", "n": "2"},
    {"sid": "s2", "evaluator": "Builtin.Correctness", "mean": "", "n": "1"},   # no score: dropped
    {"evaluator": "Builtin.Correctness", "mean": "1", "n": "1"},              # no session: dropped
]


def test_the_harvest_reads_every_project_result_group_in_one_aggregated_query(monkeypatch):
    monkeypatch.setattr(harvest_live, "EVAL_RESULTS_PREFIX", PREFIX)
    monkeypatch.setattr(harvest_live.time, "sleep", lambda _s: None)
    logs = _FakeLogs([COMPONENT_GROUP, MAIN_GROUP,
                      "/aws/bedrock-agentcore/evaluations/results/other_project_eval-ZZZ"], ROWS)
    got = harvest_live.harvest_agentcore_eval(_Aws(logs), 1000, 2000)

    assert len(logs.queries) == 1
    q = logs.queries[0]
    assert q["logGroupNames"] == [COMPONENT_GROUP, MAIN_GROUP]     # sorted, other project excluded
    assert (q["startTime"], q["endTime"]) == (1000, 2000)
    assert "stats avg(score) as mean, count(*) as n by sid, evaluator" in q["queryString"]
    assert f"| limit {harvest_live._EVAL_ROW_CAP}" in q["queryString"]
    assert got == {
        "s1": {"Builtin.GoalSuccessRate": {"mean": 1.0, "n": 1},
               "Builtin.Helpfulness": {"mean": 0.8311, "n": 9},
               "Builtin.ToolSelectionAccuracy": {"mean": 0.5, "n": 4}},
        "s2": {"Builtin.Helpfulness": {"mean": 0.5, "n": 2}},
    }


def test_no_result_group_and_a_failed_read_are_unmeasured_not_empty(monkeypatch, capsys):
    monkeypatch.setattr(harvest_live, "EVAL_RESULTS_PREFIX", PREFIX)
    assert harvest_live.harvest_agentcore_eval(_Aws(_FakeLogs([], ROWS)), 0, 1) is None

    class _Broken(_FakeLogs):
        def start_query(self, **params):
            raise RuntimeError("AccessDeniedException")
    assert harvest_live.harvest_agentcore_eval(_Aws(_Broken([MAIN_GROUP], ROWS)), 0, 1) is None
    assert "AgentCore eval scores unread" in capsys.readouterr().out


def test_scores_land_beside_usage_and_only_for_sessions_that_have_them(tmp_path):
    spans = [{"sid": "s1", "@timestamp": "2026-10-16 01:00:00.000", "svc": "x", "name": "n"},
             {"sid": "s3", "@timestamp": "2026-10-16 01:00:00.000", "svc": "x", "name": "n"}]
    scores = harvest_live.agentcore_eval_by_session(ROWS)
    out = harvest_live.reconstruct_sessions(str(tmp_path), spans, [], agentcore_eval=scores)
    assert out["s1"]["agentcore_eval"]["Builtin.Helpfulness"] == {"mean": 0.8311, "n": 9}
    assert "agentcore_eval" not in out["s3"]          # not scored: no key, not {}
    assert "s2" not in out                            # scored but no spans: no run to attach to

    path = str(tmp_path / "sessions" / "sessions.json")
    source = HarvestedAgentCoreEvalSource([path])
    assert source.eval_for("s1")["Builtin.ToolSelectionAccuracy"] == {"mean": 0.5, "n": 4}
    assert source.eval_for("s3") is None


def test_the_reader_drops_an_out_of_range_or_non_numeric_score(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({"s": {"agentcore_eval": {
        "Builtin.Helpfulness": {"mean": 0.7, "n": 3},
        "Builtin.Correctness": {"mean": 7, "n": 1},
        "Builtin.GoalSuccessRate": {"mean": "1", "n": 1},
    }}}))
    assert HarvestedAgentCoreEvalSource([str(path)]).eval_for("s") == {
        "Builtin.Helpfulness": {"mean": 0.7, "n": 3}}


def _verdict(run_id, overall):
    results = [EvaluatorResult("dispatch_correct", "llm", Outcome.PASS, 0.9, ""),
               EvaluatorResult("results_delivered", "llm", Outcome.PASS, 0.9, "")]
    return Verdict(run_id, results, UtilityVector(), overall, "")


def test_each_model_row_carries_agentcore_means_over_the_runs_agentcore_scored():
    opus, sonnet = "global.anthropic.claude-opus-5", "global.anthropic.claude-sonnet-5"
    runs = [
        Run(run_id="a", resources={"by_model": {opus: {"tokens": 10}}, "agentcore_eval": {
            "Builtin.Helpfulness": {"mean": 1.0, "n": 3}, "Builtin.GoalSuccessRate": {"mean": 1.0, "n": 1}}}),
        Run(run_id="b", resources={"by_model": {opus: {"tokens": 10}}, "agentcore_eval": {
            "Builtin.Helpfulness": {"mean": 0.5, "n": 1}}}),
        Run(run_id="c", resources={"by_model": {opus: {"tokens": 10}}}),         # never scored
        Run(run_id="d", resources={"by_model": {sonnet: {"tokens": 10}}}),
    ]
    card = scorecard_to_wire(runs, [_verdict(r.run_id, Overall.PASS) for r in runs])
    rows = {r["model"]: r for r in card["byModelScores"]}
    assert rows[opus]["agentcore"] == {
        "Builtin.GoalSuccessRate": {"mean": 1.0, "measuredOf": 1, "total": 3},
        "Builtin.Helpfulness": {"mean": 0.75, "measuredOf": 2, "total": 3},   # per RUN, not per trace
    }
    assert rows[sonnet]["agentcore"] == {}                                      # absent, never 0


def test_harvest_survives_an_account_with_no_eval_groups(tmp_path, monkeypatch):
    """``harvest()`` wires the step in and keeps going when it reads nothing."""
    hours = 24
    calls = []
    monkeypatch.setattr(harvest_live, "harvest_spans", lambda *a, **k: ([], []))
    monkeypatch.setattr(harvest_live, "harvest_token_usage", lambda *a, **k: {})
    monkeypatch.setattr(harvest_live, "harvest_agentcore_eval",
                        lambda aws, s, e: calls.append((s, e)) or None)

    class _Sts:
        def get_caller_identity(self):
            return {"Account": "0"}

    class _Session:
        def client(self, name, region_name=None):
            return _Sts()
    monkeypatch.setattr(harvest_live.boto3, "Session", lambda *a, **k: _Session())
    harvest_live.harvest(out_dir=str(tmp_path), hours=hours, skip_ddb=True, skip_alarms=True)
    assert len(calls) == 1 and calls[0][1] - calls[0][0] == hours * 3600
    assert json.load(open(tmp_path / "sessions" / "sessions.json")) == {}
