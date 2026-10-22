"""Phase 0 of dynamic (runtime) fan-out — the collection extractor.

A ``map`` node fans out over a list that only exists after its producer node runs
(e.g. the speakers a diarization job discovered). To turn that producer output into
N child nodes, the Plan Chainer must resolve a *path* against the producer's output
value and get back a Python list. This module is that resolver, and nothing more.

It is intentionally:
  * pure and dependency-free (no boto3, no I/O), so the same code can live in the
    Lambda layer the Plan Chainer imports AND be unit-tested in the agent suite;
  * small — one explode point (``[*]``) per path. Nested fan-out (a list of lists)
    is deliberately out of scope for v1; a path with two ``[*]`` is rejected rather
    than guessed at.

Grammar (see docs/dynamic-fanout-design.md §3.4):
  ""            the value itself must already be a list
  a.b.c         navigate dict keys; the final value must be a list
  a[*]          navigate to the list ``a``; iterate its elements as items
  a[*].b.c      iterate ``a``; extract ``b.c`` from each element as the item

Every failure raises ``FanoutExtractError`` with an actionable message so a map
node fails loudly (and the plan reports why) instead of silently fanning out over
nothing.
"""
from __future__ import annotations

from typing import Any

__all__ = ["FanoutExtractError", "extract_collection", "validate_path", "find_collection_in_text"]

_EXPLODE = "[*]"


class FanoutExtractError(ValueError):
    """A fan-out ``over.path`` could not be resolved to a list."""


def _navigate(value: Any, dotted: str, *, _origin: str) -> Any:
    """Walk a ``.``-separated sequence of dict keys. No list indexing here."""
    current = value
    walked: list[str] = []
    for key in dotted.split("."):
        if not key:
            raise FanoutExtractError(
                f"path {_origin!r} has an empty segment (check for '..' or a leading/trailing '.')"
            )
        if not isinstance(current, dict):
            where = ".".join(walked) or "<root>"
            raise FanoutExtractError(
                f"path {_origin!r}: expected a dict at {where!r} to read key {key!r}, "
                f"got {type(current).__name__}"
            )
        if key not in current:
            where = ".".join(walked) or "<root>"
            available = sorted(current.keys())
            raise FanoutExtractError(
                f"path {_origin!r}: key {key!r} not found under {where!r}; "
                f"available keys: {available}"
            )
        current = current[key]
        walked.append(key)
    return current


def validate_path(path: str) -> None:
    """Raise ``FanoutExtractError`` if ``path`` is not a supported grammar.

    Static check for the validator (Phase 1) — does not need a value. Enforces the
    "exactly one explode point" rule and rejects empty segments.
    """
    if path is None:
        raise FanoutExtractError("path must be a string, got None")
    if path.count(_EXPLODE) > 1:
        raise FanoutExtractError(
            f"path {path!r} has more than one '[*]'; nested fan-out is not supported (v1)"
        )
    if not path:
        return
    if _EXPLODE in path:
        left, _, right = path.partition(_EXPLODE)
        left = left.rstrip(".")
        right = right.lstrip(".")
        for part, label in ((left, "before '[*]'"), (right, "after '[*]'")):
            if part:
                for seg in part.split("."):
                    if not seg:
                        raise FanoutExtractError(
                            f"path {path!r} has an empty segment {label}"
                        )
    else:
        for seg in path.split("."):
            if not seg:
                raise FanoutExtractError(f"path {path!r} has an empty segment")


def extract_collection(value: Any, path: str) -> list:
    """Resolve ``path`` against ``value`` and return the fan-out list.

    See the module docstring for the grammar. Raises ``FanoutExtractError`` on any
    unresolved / non-list result; returns ``[]`` for a legitimately empty
    collection (the caller decides skip-vs-fail via the map node's ``on_empty``).
    """
    validate_path(path)

    if not path:
        if isinstance(value, list):
            return list(value)
        raise FanoutExtractError(
            f"empty path requires the value itself to be a list, got {type(value).__name__}"
        )

    if _EXPLODE in path:
        left, _, right = path.partition(_EXPLODE)
        left = left.rstrip(".")
        right = right.lstrip(".")
        collection = _navigate(value, left, _origin=path) if left else value
        if not isinstance(collection, list):
            where = left or "<root>"
            raise FanoutExtractError(
                f"path {path!r}: expected a list at {where!r} to iterate, "
                f"got {type(collection).__name__}"
            )
        if not right:
            return list(collection)
        return [_navigate(element, right, _origin=path) for element in collection]

    collection = _navigate(value, path, _origin=path)
    if not isinstance(collection, list):
        raise FanoutExtractError(
            f"path {path!r}: expected a list at the end of the path, "
            f"got {type(collection).__name__}"
        )
    return list(collection)


def _iter_json_values(text: str) -> list:
    """Yield every balanced, parseable top-level JSON object/array embedded in ``text``.

    A sub-agent producer often wraps its JSON in prose ("I'll start by …{…}") or a
    ```json fence, or emits more than one JSON object (an intermediate + the final).
    This scans for balanced ``{...}`` / ``[...]`` spans — STRING-AWARE so braces
    inside string values don't miscount — and returns each span that ``json.loads``
    accepts, in document order. Pure/dependency-free so it unit-tests in the agent
    suite and runs in the Plan Chainer's Lambda layer.
    """
    import json as _json

    out: list = []
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch in "{[":
            depth = 0
            in_str = False
            esc = False
            j = i
            while j < n:
                c = text[j]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                else:
                    if c == '"':
                        in_str = True
                    elif c in "{[":
                        depth += 1
                    elif c in "}]":
                        depth -= 1
                        if depth == 0:
                            span = text[i:j + 1]
                            try:
                                out.append(_json.loads(span))
                            except Exception:
                                pass
                            break
                j += 1
            i = j + 1
        else:
            i += 1
    return out


def find_collection_in_text(text: str, path: str) -> list:
    """Tolerantly recover the fan-out list from a raw producer result string.

    Fallback for when the producer's captured ``value`` is missing/wrong (e.g. the
    node-value extraction grabbed a different object, or the model prepended prose
    so a strict ``json.loads`` fails). Scans ``text`` for embedded JSON values and
    returns the first for which ``extract_collection(value, path)`` yields a list —
    i.e. the object that actually carries the requested collection. Raises
    ``FanoutExtractError`` if no embedded JSON value satisfies ``path``.
    """
    validate_path(path)
    last: Exception | None = None
    for value in _iter_json_values(text):
        try:
            return extract_collection(value, path)
        except FanoutExtractError as e:
            last = e
    raise FanoutExtractError(
        f"no embedded JSON value with a list at path {path!r} was found in the producer "
        f"output text" + (f" (last: {last})" if last else "")
    )
