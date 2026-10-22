"""``ingestion.contract`` — the Harness-side Index_Document check (Req 5.13, 5.16).

The drift tests run only where a hybrid-search checkout sits at the repo root (``hybrid-search-on-aws/``, a
gitignored dev clone): they compare the vendored mappings with the real schema files and pass every generated
document through the real ``validate_contract``.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import pathlib
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from ingestion import contract  # noqa: E402
from test_ingestion_documents import ASSET, build_fixture  # noqa: E402

HS = pathlib.Path(__file__).resolve().parents[2] / "hybrid-search-on-aws"


def _shot():
    return {"asset_id": "video:x", "doc_type": "shot", "start_ms": 0, "end_ms": 5,
            "content": [{"kind": "image", "field": "vector", "value": "s3://b/f.jpg"}],
            "fields": {"frame_s3_key": "f.jpg"}}


def _scene():
    return {"asset_id": "video:x", "doc_type": "scene", "start_ms": 0, "end_ms": 5,
            "content": [{"kind": "text", "field": "text", "value": "t"}, {"kind": "text", "field": "vector",
                                                                            "value": "t"}]}


def test_valid_docs_pass():
    contract.check([_shot(), _scene()], "video:x", 5)


@pytest.mark.parametrize("mutate,field", [
    (lambda d: d.update(collection_id="c"), "collection_id"),
    (lambda d: d.update(refresh=False), "refresh"),
    (lambda d: d.update(doc_type="segment"), "doc_type"),
    (lambda d: d.update(asset_id="video:y"), "asset_id"),
    (lambda d: d.update(start_ms=5), "end_ms"),
    (lambda d: d.update(start_ms=-1), "start_ms"),
    (lambda d: d.update(end_ms=6), "end_ms"),
    (lambda d: d.update(start_ms=True), "start_ms"),
    (lambda d: d.update(fields={"vector": "x"}), "fields.vector"),
    (lambda d: d.update(fields={"bogus": "x"}), "fields.bogus"),
    (lambda d: d.update(fields={"segment_parent_id": "x"}), "fields.segment_parent_id"),
    (lambda d: d["content"][0].update(kind="video"), "content[0].kind"),
    (lambda d: d["content"][0].update(field="bogus"), "content[0].field"),
    (lambda d: d["content"][0].update(value=" "), "content[0].value"),
    (lambda d: d["content"][0].update(extract_entities=True), "content[0].extract_entities"),
    (lambda d: d["content"].append({"kind": "image", "field": "vector", "value": "s3://b/x.jpg"}),
     "content[2].field"),
    (lambda d: d["content"].append({"kind": "image", "field": "text", "value": "s3://b/x.jpg"}), "content[2]"),
    (lambda d: d["content"].pop(0), "content.text"),
    (lambda d: d["content"].pop(1), "content.vector"),
])
def test_scene_violations(mutate, field):
    d = _scene()
    mutate(d)
    with pytest.raises(contract.ContractError) as e:
        contract.check([_shot(), d], "video:x", 5)
    assert e.value.position == 2 and e.value.field == field
    assert str(e.value) == f"contract error at #2 ({d.get('doc_type')}): {field}"


@pytest.mark.parametrize("mutate,field", [
    (lambda d: d["content"][0].update(value="https://x/f.jpg"), "content[0]"),
    (lambda d: d.update(fields={}), "fields"),
    (lambda d: d.update(fields={"scene_parent_id": "scene:0:5"}), "fields.frame_s3_key"),
    (lambda d: d["content"].append({"kind": "text", "field": "frame_s3_key", "value": "t"}), "content"),
])
def test_shot_violations(mutate, field):
    d = _shot()
    mutate(d)
    with pytest.raises(contract.ContractError) as e:
        contract.check([d], "video:x", 5)
    assert (e.value.position, e.value.field) == (1, field)


# ------------------------------------------------------------------------------------- drift guards
needs_hs = pytest.mark.skipif(not (HS / "lib" / "contracts.py").exists(), reason="no hybrid-search checkout")


@needs_hs
def test_vendored_mappings_match_hybrid_search_schemas():
    for scope, cols in contract.SCOPE_MAPPINGS.items():
        raw = json.loads((HS / "schema" / f"default_{scope}.json").read_text())
        assert raw["doc_type"] == scope
        assert frozenset(raw["mappings"]) == cols, scope
        assert next(k for k, v in raw["mappings"].items() if v.get("ann")) == contract.ANN_FIELD


@needs_hs
def test_generated_documents_pass_real_validate_contract():
    spec = importlib.util.spec_from_file_location("hs_contracts", HS / "lib" / "contracts.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class Schema:
        def __init__(self, scope):
            raw = json.loads((HS / "schema" / f"default_{scope}.json").read_text())
            self.doc_type, self._mappings = raw["doc_type"], raw["mappings"]
            self.ann_field = next(k for k, v in raw["mappings"].items() if v.get("ann"))
            self.required = [k for k, v in raw["mappings"].items() if v.get("required")]

    res, _ = build_fixture()
    for d in res.documents:
        sch = Schema(d["doc_type"])
        norm = mod.validate_contract(copy.deepcopy(d), sch, "p0123456789abcdef")
        row = {"asset_id": norm["asset_id"], "start_ms": norm["start_ms"], "end_ms": norm["end_ms"],
               "doc_id": norm["doc_id"], sch.ann_field: [0.0], **norm["fields"]}
        row.update({it["field"]: it["value"] for it in norm["content"] if it["field"] != sch.ann_field})
        assert not [f for f in sch.required if row.get(f) in (None, "", [])], d
    assert res.documents and res.documents[0]["asset_id"] == ASSET
