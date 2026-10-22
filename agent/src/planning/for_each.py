"""Compact "for each item" DAG nodes, expanded at the plan boundary.

Eight shots restyled the same way used to be eight spelled-out nodes: the Design Agent wrote the
same long prompt eight times (staging plan 81d9198e: 4,182 output tokens, 28.6s of design). A node
may instead carry ``for_each: {"var": "<name>", "items": [...]}`` and a TEMPLATED body. It is
expanded HERE, before validation, into ordinary sibling nodes, so the validator, the plan-state rows
and the Plan Chainer's ``nodes_json`` all see exactly the shape they always did — nothing
downstream knows the compact form existed.

Distinct from the runtime ``map`` kind (``dag_schema.NodeKind.MAP``), which fans out over a list
that only exists after an upstream node has run; ``for_each`` items are known at plan time.
"""
from __future__ import annotations

import json
import re

#: Same ceiling as a runtime map's default ``max_fanout``.
FOR_EACH_MAX_ITEMS = 50

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)((?:\.[A-Za-z0-9_]+)*)\s*\}\}")
_KEY_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


class ForEachError(ValueError):
    """A ``for_each`` node that cannot be expanded (reported whole, never half-expanded)."""


def _lookup(item, path: str):
    value = item
    for part in [p for p in path.split(".") if p]:
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise KeyError(path.lstrip("."))
    return value


def substitute(value, var: str, item):
    """Replace ``{{var}}`` / ``{{var.field}}`` in every string of ``value`` (recursively).

    A string that is EXACTLY one placeholder takes the item's value WITH its type (a number stays a
    number in a tool argument); a placeholder inside longer text is interpolated (non-strings as
    JSON). Placeholders naming any other variable are left alone — a runtime map template's own
    ``{{item_var}}`` survives untouched.
    """
    if isinstance(value, dict):
        return {k: substitute(v, var, item) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, var, item) for v in value]
    if not isinstance(value, str):
        return value
    whole = _PLACEHOLDER_RE.fullmatch(value.strip())
    if whole and whole.group(1) == var:
        return _lookup(item, whole.group(2))

    def one(m):
        if m.group(1) != var:
            return m.group(0)
        got = _lookup(item, m.group(2))
        return got if isinstance(got, str) else json.dumps(got)

    return _PLACEHOLDER_RE.sub(one, value)


def _item_key(item, index: int) -> str:
    raw = item.get("id") if isinstance(item, dict) else (item if isinstance(item, (str, int)) else None)
    key = _KEY_UNSAFE_RE.sub("_", str(raw)).strip("_") if raw not in (None, "") else ""
    return key or str(index + 1)


def has_for_each(nodes) -> bool:
    return any(isinstance(n, dict) and n.get("for_each") for n in (nodes or []))


def expand_for_each(nodes: list) -> list:
    """Expand every ``for_each`` node dict into one sibling per item. Pure; returns a new list.

    Child ids are ``<id>__<key>`` — the item's ``id`` field, else its 1-based position — the same
    ``__<suffix>`` convention as runtime map children and per-video guided nodes. A node that
    depended on the group id now depends on EVERY child; a child's own ``depends_on`` can name one
    specific sibling of another group through the placeholder (``analyze__{{shot.id}}``).
    ``output_key`` gets the same suffix so siblings never collide in the manifest.

    Raises :class:`ForEachError` on a malformed group, so the caller can route it to the design
    retry exactly like a validation error.
    """
    if not has_for_each(nodes):
        return list(nodes or [])
    groups: dict[str, list[str]] = {}
    out: list = []
    for n in nodes:
        spec = n.get("for_each") if isinstance(n, dict) else None
        if not spec:
            out.append(n)
            continue
        nid = n.get("id", "?")
        if not isinstance(spec, dict):
            raise ForEachError(f"node {nid!r}: for_each must be an object {{var, items}}")
        var, items = spec.get("var") or "item", spec.get("items")
        if not isinstance(var, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", var):
            raise ForEachError(f"node {nid!r}: for_each.var must be an identifier (got {var!r})")
        if not isinstance(items, list) or not items:
            raise ForEachError(f"node {nid!r}: for_each.items must be a non-empty list")
        if len(items) > FOR_EACH_MAX_ITEMS:
            raise ForEachError(
                f"node {nid!r}: for_each has {len(items)} items (max {FOR_EACH_MAX_ITEMS})")
        body = {k: v for k, v in n.items() if k != "for_each"}
        children, seen = [], set()
        for i, item in enumerate(items):
            key = _item_key(item, i)
            if key in seen:
                key = f"{key}_{i + 1}"
            seen.add(key)
            try:
                child = substitute(body, var, item)
            except KeyError as e:
                raise ForEachError(
                    f"node {nid!r}: item {i + 1} has no field {e.args[0]!r} named by a placeholder"
                ) from None
            child["id"] = f"{nid}__{key}"
            if child.get("output_key"):
                child["output_key"] = f"{child['output_key']}_{key}"
            children.append(child)
        groups[nid] = [c["id"] for c in children]
        out.extend(children)
    for n in out:
        deps = n.get("depends_on") if isinstance(n, dict) else None
        if deps:
            expanded: list = []
            for d in deps:
                for x in groups.get(d, [d]):
                    if x not in expanded:
                        expanded.append(x)
            n["depends_on"] = expanded
    return out
