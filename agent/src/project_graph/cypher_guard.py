"""Read-only guard for agent-written Cypher (`graph_query`). Pure: no lance-graph import.

The Project boundary is NOT enforced here — the tools Lambda only ever loads the tables of the Project whose
`project_id` the Search_Gateway's Cedar policy already matched to the caller's Project_Token. This guard keeps a
model-written query inside what is safe and what lance-graph 0.5.4 can actually run:

- one `MATCH … [WHERE …] RETURN …` statement; no `;`, no write or procedure clauses;
- no OPTIONAL MATCH / WITH / UNION (lance-graph cannot execute them);
- every node pattern in MATCH is labelled and every relationship is typed, with labels/types from the schema
  (lance-graph cannot plan an unlabelled node);
- variable-length relationships are bounded (`*1..3` at most);
- the row count is capped: a missing LIMIT is added, a larger one is lowered.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

MAX_ROWS = 200
MAX_HOPS = 3
MAX_QUERY_CHARS = 4000

_FORBIDDEN = ("CREATE", "MERGE", "SET", "DELETE", "DETACH", "REMOVE", "CALL", "LOAD", "FOREACH", "UNWIND",
              "OPTIONAL", "WITH", "UNION", "DROP", "USING", "YIELD")
_STRING_RE = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
_MATCH_SEGMENT_RE = re.compile(r"^\s*MATCH\b(?P<pattern>.*?)(?=\bWHERE\b|\bRETURN\b)", re.IGNORECASE | re.DOTALL)
_NODE_RE = re.compile(r"\(([^()]*)\)")
_REL_RE = re.compile(r"\[([^\[\]]*)\]")
_LABEL_RE = re.compile(r"^\s*[A-Za-z_]\w*\s*:\s*([A-Za-z_]\w*)|^\s*:\s*([A-Za-z_]\w*)")
_VARLEN_RE = re.compile(r"\*\s*(\d*)\s*(?:\.\.\s*(\d*))?")
_LIMIT_RE = re.compile(r"\bLIMIT\s+(\d+)\s*$", re.IGNORECASE)


class GuardError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _blank_strings(q: str) -> str:
    """Replace string literal contents with spaces so keywords inside strings are not matched."""
    return _STRING_RE.sub(lambda m: m.group(0)[0] + " " * (len(m.group(0)) - 2) + m.group(0)[-1], q)


def check(query: object, labels: Iterable[str], relationships: Iterable[str], max_rows: int = MAX_ROWS) -> str:
    """The query to run (with a LIMIT ≤ ``max_rows``), or raise :class:`GuardError`."""
    if not isinstance(query, str) or not query.strip():
        raise GuardError("invalid_query", "cypher must be a non-empty string")
    q = query.strip()
    if len(q) > MAX_QUERY_CHARS:
        raise GuardError("invalid_query", f"cypher is longer than {MAX_QUERY_CHARS} characters")
    bare = _blank_strings(q)
    if ";" in bare:
        raise GuardError("invalid_query", "one statement only (no ';')")
    if "//" in bare or "/*" in bare:
        raise GuardError("invalid_query", "comments are not allowed")
    upper = bare.upper()
    for kw in _FORBIDDEN:
        if re.search(rf"\b{kw}\b", upper):
            raise GuardError("unsupported_clause", f"{kw} is not allowed: graph_query is read-only MATCH … RETURN")
    if not re.match(r"^\s*MATCH\b", upper):
        raise GuardError("invalid_query", "the query must start with MATCH")
    if len(re.findall(r"\bMATCH\b", upper)) != 1:
        raise GuardError("unsupported_clause", "use a single MATCH (comma-separated patterns are fine)")
    if not re.search(r"\bRETURN\b", upper):
        raise GuardError("invalid_query", "the query must RETURN something")

    seg = _MATCH_SEGMENT_RE.match(bare)
    if not seg:
        raise GuardError("invalid_query", "could not read the MATCH pattern")
    pattern = seg.group("pattern")
    label_set, rel_set = set(labels), set(relationships)
    for node in _NODE_RE.findall(pattern):
        m = _LABEL_RE.match(node)
        label = (m.group(1) or m.group(2)) if m else None
        if not label:
            raise GuardError("unlabelled_node", f"every node needs a label, e.g. (p:Person): ({node.strip()})")
        if label not in label_set:
            raise GuardError("unknown_label", f"unknown label {label!r}; see graph_schema")
    for rel in _REL_RE.findall(pattern):
        m = _LABEL_RE.match(rel)
        rtype = (m.group(1) or m.group(2)) if m else None
        if not rtype:
            raise GuardError("untyped_relationship", f"every relationship needs a type, e.g. [:APPEARS_IN]: [{rel.strip()}]")
        if rtype not in rel_set:
            raise GuardError("unknown_relationship", f"unknown relationship {rtype!r}; see graph_schema")
        vl = _VARLEN_RE.search(rel)
        if vl:
            hi = vl.group(2) if vl.group(2) is not None else vl.group(1)
            if not hi or int(hi) > MAX_HOPS:
                raise GuardError("unbounded_path", f"variable-length paths must be bounded to at most {MAX_HOPS} hops (*1..{MAX_HOPS})")

    lim = _LIMIT_RE.search(q)
    if lim:
        if int(lim.group(1)) > max_rows:
            q = q[: lim.start(1)] + str(max_rows)
    elif re.search(r"\bLIMIT\b", upper):
        raise GuardError("invalid_query", "LIMIT must be a number at the end of the query")
    else:
        q = f"{q} LIMIT {max_rows}"
    return q


def referenced(query: str) -> tuple[list[str], list[str]]:
    """``(labels, relationship_types)`` the MATCH pattern names, in order (for a query `check` accepted).

    lance-graph 0.5.4 fails with "Table has no data" when ANY dataset it is given is empty, so `graph_query` hands
    it only these tables — and answers "no rows" itself when one of them is empty (a MATCH needs every one).
    """
    seg = _MATCH_SEGMENT_RE.match(_blank_strings(query))
    pattern = seg.group("pattern") if seg else ""
    labels, rels = [], []
    for node in _NODE_RE.findall(pattern):
        m = _LABEL_RE.match(node)
        if m and (m.group(1) or m.group(2)) not in labels:
            labels.append(m.group(1) or m.group(2))
    for rel in _REL_RE.findall(pattern):
        m = _LABEL_RE.match(rel)
        if m and (m.group(1) or m.group(2)) not in rels:
            rels.append(m.group(1) or m.group(2))
    return labels, rels


def guard_or_none(query: object, labels: Iterable[str], relationships: Iterable[str]) -> Optional[GuardError]:
    try:
        check(query, labels, relationships)
    except GuardError as e:
        return e
    return None
