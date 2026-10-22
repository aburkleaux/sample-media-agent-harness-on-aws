"""Harness-side Index_Document contract check (Req 5.13, 5.16), run before anything is submitted.

The scope mappings are vendored from hybrid-search's ``schema/default_{shot,scene,utterance,topic}.json`` as a
Python constant (the ingestion package ships ``.py`` only). ``tests/test_ingestion_contract.py`` compares them
with the real schema files and runs generated documents through the real ``validate_contract`` whenever a
hybrid-search checkout is present, so the two cannot drift silently.

The first invalid document in build order fails the asset with
``document build: contract error at #<n> (<doc_type>): <field>``, ``n`` counting from 1.
"""
from __future__ import annotations

from typing import Optional, Sequence

# doc_type -> every mapped column. ``vector`` is the ANN column in every scope; the backend computes it.
SCOPE_MAPPINGS: dict[str, frozenset] = {
    "shot": frozenset({"doc_id", "asset_id", "start_ms", "end_ms", "vector", "frame_s3_key", "scene_parent_id"}),
    "scene": frozenset({"doc_id", "asset_id", "start_ms", "end_ms", "text", "vector", "named_entities",
                        "segment_parent_id"}),
    "utterance": frozenset({"doc_id", "asset_id", "start_ms", "end_ms", "text", "vector", "scene_parent_id",
                            "topic_parent_id"}),
    "topic": frozenset({"doc_id", "asset_id", "start_ms", "end_ms", "text", "vector", "named_entities"}),
}
ANN_FIELD = "vector"
# Columns the backend derives or takes from the top-level keys; a caller must not set them in ``fields``.
_RESERVED_FIELDS = frozenset({"doc_id", "asset_id", "start_ms", "end_ms", ANN_FIELD})
_TOP_KEYS = frozenset({"asset_id", "doc_type", "start_ms", "end_ms", "content", "fields"})
_ITEM_KEYS = frozenset({"kind", "field", "value"})


class ContractError(ValueError):
    def __init__(self, position: int, doc_type: str, field: str):
        self.position, self.doc_type, self.field = position, doc_type, field
        super().__init__(f"contract error at #{position} ({doc_type}): {field}")


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def first_violation(doc: dict, asset_id: str, duration_ms: int) -> Optional[str]:
    """The first invalid field of one document, or ``None``. Pure."""
    for k in doc:
        if k not in _TOP_KEYS:
            return k
    doc_type = doc.get("doc_type")
    if doc_type not in SCOPE_MAPPINGS:
        return "doc_type"
    mappings = SCOPE_MAPPINGS[doc_type]
    if doc.get("asset_id") != asset_id or not asset_id:
        return "asset_id"
    s, e = doc.get("start_ms"), doc.get("end_ms")
    if not _is_int(s) or s < 0:
        return "start_ms"
    if not _is_int(e) or not (s < e <= duration_ms):
        return "end_ms"

    fields = doc.get("fields", None)
    if fields is not None:
        if not isinstance(fields, dict) or not fields:
            return "fields"
        for f, v in fields.items():
            if f == ANN_FIELD or f not in mappings or f in _RESERVED_FIELDS or f == "segment_parent_id":
                return f"fields.{f}"
            if not isinstance(v, str) or not v.strip():
                return f"fields.{f}"
    fields = fields or {}

    content = doc.get("content")
    if not isinstance(content, list) or not content:
        return "content"
    vectors = images = texts = 0
    for i, item in enumerate(content):
        if not isinstance(item, dict):
            return f"content[{i}]"
        for k in item:
            if k not in _ITEM_KEYS:
                return f"content[{i}].{k}"
        kind, field, value = item.get("kind"), item.get("field"), item.get("value")
        if kind not in ("text", "image"):
            return f"content[{i}].kind"
        if field not in mappings or field in _RESERVED_FIELDS - {ANN_FIELD}:
            return f"content[{i}].field"
        if not isinstance(value, str) or not value.strip():
            return f"content[{i}].value"
        if kind == "image":
            images += 1
            if field != ANN_FIELD or not value.startswith("s3://") or images > 1:
                return f"content[{i}]"
        else:
            if field == "text":
                texts += 1
        if field == ANN_FIELD:
            vectors += 1
            if vectors > 1:
                return f"content[{i}].field"
    if vectors != 1:
        return "content.vector"
    if doc_type == "shot":
        if texts or images != 1 or any(it.get("kind") == "text" for it in content):
            return "content"
        if not fields.get("frame_s3_key"):
            return "fields.frame_s3_key"
    elif texts != 1:
        return "content.text"
    return None


def check(docs: Sequence[dict], asset_id: str, duration_ms: int) -> None:
    """Raise :class:`ContractError` for the first invalid document in build order (Req 5.16)."""
    for n, doc in enumerate(docs, start=1):
        bad = first_violation(doc, asset_id, duration_ms)
        if bad is not None:
            dt = doc.get("doc_type") if isinstance(doc.get("doc_type"), str) else "?"
            raise ContractError(n, dt, bad)
