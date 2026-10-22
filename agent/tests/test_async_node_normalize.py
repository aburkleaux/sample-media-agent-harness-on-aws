"""A submit_job node missing its trio is REPAIRED, not rejected.

The Design Agent reliably emits an image->video node with only ``submit_job`` and
``output_type: text`` — a shape ``validate_async_job_nodes`` (rightly) rejects, but which
the retry loop kept reproducing, so a correct anime+stitch DAG failed validation twice and
fell back to un-tracked dispatch (sessions f0598806, 59c46b2c). ``normalize_async_job_nodes``
repairs that structural, catalog-derivable defect deterministically: it grants the sibling
status/result verbs of the SAME gateway target from the live catalog and coerces the output
type — so the plan then validates and runs as a DAG. Gateway-agnostic (matched on the verb
grammar + the submit tool's own target), no hardcoded tool names.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from planning.dag_schema import DAGNode, Plan, normalize_async_job_nodes, validate_async_job_nodes

FAL = "usr-8ddf64af24be45c894a7a8c1f254c83f"
CATALOG = {
    f"{FAL}___submit_job", f"{FAL}___check_job", f"{FAL}___get_job_result",
    f"{FAL}___run_model", f"{FAL}___get_model_schema",
    # a second gateway, to prove target-scoped resolution picks the right siblings
    "audio-stem___separate_audio_stems", "audio-stem___get_job_status",
}


def _node(**kw):
    base = dict(id="video_A", prompt="animate", tools=[f"{FAL}___submit_job"],
                depends_on=[], output_key="video_a", output_type="text")
    base.update(kw)
    return DAGNode(**base)


def _plan(node):
    return Plan(plan_id="p", goal="g", nodes=[node])


def test_submit_only_text_node_is_the_shape_the_validator_rejects():
    # Baseline: the un-repaired node produces exactly the two errors seen in prod.
    errs = validate_async_job_nodes(_plan(_node()))
    kinds = {e.error_type for e in errs}
    assert kinds == {"async_node_output_type", "async_node_missing_tools"}


def test_normalize_grants_same_gateway_siblings_and_coerces_output_type():
    node = _node()
    notes = normalize_async_job_nodes(_plan(node), CATALOG)
    # Siblings resolved from the SAME fal target, not the audio-stem gateway.
    assert f"{FAL}___check_job" in node.tools
    assert f"{FAL}___get_job_result" in node.tools
    assert node.output_type == "json"
    assert notes  # human-readable repair notes for logging
    # And the plan now passes the async contract with no errors.
    assert validate_async_job_nodes(_plan(node)) == []


def test_normalize_is_idempotent():
    node = _node()
    normalize_async_job_nodes(_plan(node), CATALOG)
    tools_after_first = list(node.tools)
    normalize_async_job_nodes(_plan(node), CATALOG)
    assert node.tools == tools_after_first  # no duplicate verbs added


def test_run_model_node_is_now_a_producer_not_left_bare():
    # BEHAVIOR CHANGE (issue 2): a run_model node used to be left untouched (not a submit verb).
    # It is now recognized as a handle producer and granted resolver verbs, because run_model
    # degrades to a queue handle under load. output_type stays as declared (run_model usually
    # returns a final result).
    node = _node(id="edit", tools=[f"{FAL}___run_model"], output_type="text")
    notes = normalize_async_job_nodes(_plan(node), CATALOG)
    assert notes  # verbs were granted
    assert node.output_type == "text"  # NOT coerced (run_model, not submit_job)
    assert {t.rpartition("___")[2] for t in node.tools} >= {"run_model", "check_job", "get_job_result"}


def test_output_type_is_coerced_even_when_no_sibling_exists_in_catalog():
    # A gateway that genuinely lacks status/result verbs: output_type is still fixed
    # (catalog-independent), and the validator still reports the un-repairable gap.
    node = _node(tools=["lonely-gw___submit_job"])
    normalize_async_job_nodes(_plan(node), {"lonely-gw___submit_job"})
    assert node.output_type == "json"
    remaining = {e.error_type for e in validate_async_job_nodes(_plan(node))}
    assert remaining == {"async_node_missing_tools"}


def test_already_complete_node_is_unchanged():
    node = _node(tools=[f"{FAL}___submit_job", f"{FAL}___check_job", f"{FAL}___get_job_result",
                        f"{FAL}___get_model_schema"], output_type="s3_uri")
    notes = normalize_async_job_nodes(_plan(node), CATALOG)
    assert notes == []
    assert validate_async_job_nodes(_plan(node)) == []


# --- run_model is also a handle producer (issue 2, session 154b5b07) --------------------------

def test_run_model_edit_node_is_granted_resolver_verbs():
    # A flux edit node with only [run_model, get_model_schema] could not resolve a handle when
    # fal's queue backed up, so the downstream video node got no image URL. Grant the trio's
    # resolver verbs from the same gateway so a handle CAN be resolved.
    node = _node(id="edit_a", tools=[f"{FAL}___run_model", f"{FAL}___get_model_schema"],
                 output_type="text", output_key="scene_a_edit_url")
    notes = normalize_async_job_nodes(_plan(node), CATALOG)
    assert f"{FAL}___check_job" in node.tools
    assert f"{FAL}___get_job_result" in node.tools
    assert notes


def test_run_model_output_type_is_NOT_coerced():
    # run_model usually returns a final image URL — a `text` output_type is legitimate, so it
    # must be left alone (only always-async submit_job is coerced).
    node = _node(id="edit_a", tools=[f"{FAL}___run_model"], output_type="text")
    normalize_async_job_nodes(_plan(node), CATALOG)
    assert node.output_type == "text"


def test_run_model_node_that_already_has_resolvers_is_unchanged():
    node = _node(id="edit_a", tools=[f"{FAL}___run_model", f"{FAL}___check_job",
                                     f"{FAL}___get_job_result", f"{FAL}___get_model_schema"], output_type="text")
    notes = normalize_async_job_nodes(_plan(node), CATALOG)
    assert notes == []


def test_non_producer_node_still_untouched():
    # A pure analysis/tool node with neither submit_job nor run_model is not a handle producer.
    node = _node(id="x", tools=["shot-segmentation___segment_shots"], output_type="json")
    notes = normalize_async_job_nodes(_plan(node), CATALOG)
    assert notes == []
    assert node.tools == ["shot-segmentation___segment_shots"]


# --- the schema lookup the skill tells a generation node to make (session e2fb2692) ----------------------------
def test_generation_node_is_granted_its_gateways_schema_lookup():
    # The e2fb2692 shape: run_model + check_job + get_job_result, no get_model_schema -- yet media-references
    # tells the node to call get_model_schema before setting a tuning param, so two nodes stalled on it.
    node = _node(id="xi_a", tools=[f"{FAL}___run_model", f"{FAL}___check_job", f"{FAL}___get_job_result"],
                 output_type="json")
    notes = normalize_async_job_nodes(_plan(node), CATALOG)
    assert f"{FAL}___get_model_schema" in node.tools
    assert any("schema" in n for n in notes)


def test_schema_lookup_is_not_invented_when_the_gateway_has_none():
    node = _node(id="stems", tools=["audio-stem___separate_audio_stems"], output_type="json")
    catalog = {"audio-stem___separate_audio_stems", "audio-stem___get_job_status"}
    normalize_async_job_nodes(_plan(node), catalog)
    assert not any(t.endswith("get_model_schema") for t in node.tools)
