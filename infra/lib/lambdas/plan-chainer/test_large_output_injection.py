"""Regression harness for the large-authored-document failure (AWS-MCP PDF report).

A synthesis/format node depends on an author node that produced a LARGE report. The chainer
injected the producer's output INLINE, capped at _MAX_INJECTED_CHARS (3000), so the consumer
(format_pdf) received only a ~3KB fragment — or a "(full report included above)" placeholder —
of a 16KB report and rendered an empty/structure-only PDF. The injection must instead hand the
consumer a REFERENCE to the COMPLETE stored output so it reads the full text by reference.
"""
from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("PLAN_STATE_TABLE", "test-plan-state")
os.environ.setdefault("JOB_REGISTRY_TABLE", "test-job-registry")
os.environ.setdefault("TASK_AGENT_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:1:runtime/task")

sys.path.insert(0, os.path.dirname(__file__))

import pytest  # noqa: E402
import handler  # noqa: E402


@pytest.fixture(autouse=True)
def _bucket(monkeypatch):
    monkeypatch.setattr(handler, "SKILLS_S3_BUCKET", "skills-bkt")


def _node(depends_on):
    return {"id": "format_pdf", "depends_on": depends_on}


def test_large_dependency_output_injected_by_reference():
    big = "X" * (handler._MAX_INJECTED_CHARS * 5)  # ~15KB report
    dep_outputs = {"author_report": {"status": "completed", "value": big}}
    node_states = {"author_report": {"result_s3_key": "plans/S/P/author_report/output"}}
    text = handler._dependency_injection_text(_node(["author_report"]), dep_outputs, node_states, "S", "P")
    # A reference to the FULL stored output is provided, with a read-by-reference instruction.
    assert "s3://skills-bkt/plans/S/P/author_report/output" in text
    assert "FULL OUTPUT (authoritative" in text
    assert "read_result_files" in text or "file_read" in text
    # The full 15KB is NOT dumped inline (only a truncated preview).
    assert text.count("X") <= handler._MAX_INJECTED_CHARS + 50


def test_small_dependency_output_also_gets_the_reference():
    # Design §3 A1: the handoff is UNIFORM — even a small value gets the deterministic file
    # reference (so a small *placeholder* value can never starve the consumer of the real file).
    small = "a concise section result"
    dep_outputs = {"research_overview": {"status": "completed", "value": small}}
    node_states = {"research_overview": {"result_s3_key": "plans/S/P/research_overview/output"}}
    text = handler._dependency_injection_text(_node(["research_overview"]), dep_outputs, node_states, "S", "P")
    assert small in text                                                    # preview present
    assert "s3://skills-bkt/plans/S/P/research_overview/output" in text      # + the reference


def test_placeholder_value_still_offers_the_reference_when_large():
    # Even if the producer emitted a placeholder, a large stored file is referenced so the
    # consumer can recover the real content. (Here the value itself is small placeholder text,
    # so the inline path is used — but the reference path is covered above with a large value.)
    dep_outputs = {"author_report": {"status": "completed",
                                     "value": "(full report included above in this response)"}}
    node_states = {"author_report": {"result_s3_key": "plans/S/P/author_report/output"}}
    text = handler._dependency_injection_text(_node(["author_report"]), dep_outputs, node_states, "S", "P")
    # small value -> inline, but the block is still present (regression that it isn't dropped)
    assert 'Output from step "author_report"' in text


def test_reproduces_824f9431_truncation_and_fix():
    """Faithful reproduction of the AWS-MCP report failure (session 824f9431): a ~16KB authored
    report reaches format_pdf. BEFORE the fix, the injection was inline-only + capped, so the
    consumer received a truncated fragment with NO reference to the full text -> empty/partial PDF.
    AFTER the fix, the consumer gets a reference to the COMPLETE stored output.
    """
    # A realistic large report body (~16KB, like the real author_report/output = 16730 chars).
    report = "# AWS MCP Servers: A Comprehensive Report\n\n" + (
        "The Model Context Protocol standardizes how agents discover and call AWS tools. " * 220)
    assert len(report) > 5 * handler._MAX_INJECTED_CHARS  # genuinely larger than the inline cap

    # --- BEFORE (reproduce the bug): the old path injected only the capped inline value ---
    filtered = handler._filter_for_injection(report)
    old_inline = filtered if isinstance(filtered, str) else __import__("json").dumps(filtered)
    assert len(old_inline) < len(report)                 # the report WAS truncated
    assert "s3://" not in old_inline                     # and no reference to the full text
    # -> format_pdf would have rendered only this fragment (the observed empty/structure-only PDF).

    # --- AFTER (the fix): the consumer is handed the full-output reference ---
    dep_outputs = {"author_report": {"status": "completed", "value": report}}
    node_states = {"author_report": {"result_s3_key": "plans/824f9431/PLAN/author_report/output"}}
    text = handler._dependency_injection_text(_node(["author_report"]), dep_outputs, node_states,
                                              "824f9431", "PLAN")
    assert "s3://skills-bkt/plans/824f9431/PLAN/author_report/output" in text
    assert "FULL OUTPUT (authoritative" in text
    # the full report is NOT dumped inline (only a bounded preview remains)
    assert len(text) < len(report)


def test_absent_result_s3_key_is_not_fabricated_into_an_authoritative_uri():
    # Absent != empty: when NO durable output was recorded, the chainer must not synthesise a
    # `plans/.../output` path and present it as authoritative — that is a URI to a file that was
    # never written, which the consumer is explicitly forbidden to reconstruct from (a dead end,
    # the P1 defect). The preview/content is still handed over, just not as an authoritative file.
    dep_outputs = {"research_overview": {"status": "completed", "value": "a short summary"}}
    node_states = {"research_overview": {}}  # no result_s3_key recorded
    text = handler._dependency_injection_text(_node(["research_overview"]), dep_outputs,
                                              node_states, "S", "P")
    assert "a short summary" in text                              # content still provided
    assert "FULL OUTPUT (authoritative" not in text              # but NOT as an authoritative file
    assert "plans/S/P/research_overview/output" not in text      # and NO fabricated path
    assert "No durable output file was recorded" in text
