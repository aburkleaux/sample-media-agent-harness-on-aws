"""Safe boolean expression evaluator for declarative hook conditions.

Hook conditions are small boolean expressions authored by users in the Lab UI
and evaluated by the harness against a normalized event context (a nested dict
like ``{"event": "after_tool_result", "tool": {"role": "results", "name": ...},
"result": {"urls": [...], "mimes": [...]}, "job": {"status": ...}, ...}``).

Security is the whole point of this module: expressions run inside the agent
runtime, so we must NOT expose arbitrary Python. We parse with the stdlib
``ast`` in ``eval`` mode and walk the tree against a strict node allowlist,
then evaluate with a hand-written interpreter that:

  * resolves ``Name`` nodes ONLY from the top-level context dict,
  * navigates ``Attribute`` access ONLY through dict lookups (never real
    Python attribute access — so ``x.__class__`` can't escape),
  * rejects any ``Attribute`` whose name starts with ``_``,
  * allows ``Call`` ONLY for a tiny whitelist of pure helper functions
    (``matches``, ``len``) invoked as bare names.

Anything outside the allowlist (imports, lambdas, comprehensions, subscripts,
arbitrary calls, arithmetic operators, f-strings, walrus, etc.) raises
``ExpressionError`` at validation time.

Public API:
  * ``validate(expression)`` — raise ``ExpressionError`` if the expression uses
    disallowed syntax (used by the UI validate/dry-run endpoint).
  * ``evaluate(expression, context)`` — return a ``bool``. An empty/whitespace
    expression evaluates to ``True`` (a hook with no condition always matches).
"""

from __future__ import annotations

import ast
import re
from typing import Any

__all__ = ["ExpressionError", "validate", "evaluate"]


class ExpressionError(ValueError):
    """Raised when an expression is syntactically invalid or uses disallowed nodes."""


# Whitelisted helper functions callable from an expression (bare-name calls only).
_ALLOWED_FUNCS = {"matches", "len", "lower"}

# Allowed AST node types. Everything else is rejected during validation.
_ALLOWED_NODES: tuple[type, ...] = (
    ast.Expression,
    ast.BoolOp, ast.And, ast.Or,
    ast.UnaryOp, ast.Not,
    ast.Compare,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.In, ast.NotIn, ast.Is, ast.IsNot,
    ast.Name, ast.Load,
    ast.Attribute,
    ast.Constant,
    ast.List, ast.Tuple,
    ast.Call,
)


def _check_nodes(tree: ast.AST) -> None:
    """Walk the tree and reject any node outside the allowlist."""
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ExpressionError(
                f"disallowed syntax: {type(node).__name__}"
            )
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise ExpressionError(
                    "access to private/dunder attributes is not allowed"
                )
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
                raise ExpressionError(
                    "only whitelisted functions may be called: "
                    + ", ".join(sorted(_ALLOWED_FUNCS))
                )
            if getattr(node, "keywords", None):
                raise ExpressionError("keyword arguments are not allowed")


def _parse(expression: str) -> ast.Expression:
    try:
        return ast.parse(expression, mode="eval")
    except SyntaxError as exc:  # noqa: PERF203
        raise ExpressionError(f"invalid expression syntax: {exc.msg}") from exc


def validate(expression: str) -> None:
    """Validate an expression's syntax and node allowlist. Raise on failure.

    An empty/whitespace expression is valid (always-true condition).
    """
    if expression is None or expression.strip() == "":
        return
    tree = _parse(expression)
    _check_nodes(tree)


def _apply_cmp(op: ast.cmpop, left: Any, right: Any) -> bool:
    if isinstance(op, ast.Eq):
        return left == right
    if isinstance(op, ast.NotEq):
        return left != right
    if isinstance(op, ast.Is):
        return left is right
    if isinstance(op, ast.IsNot):
        return left is not right
    if isinstance(op, ast.In):
        try:
            return left in right  # type: ignore[operator]
        except TypeError:
            return False
    if isinstance(op, ast.NotIn):
        try:
            return left not in right  # type: ignore[operator]
        except TypeError:
            return False
    # Ordered comparisons: guard against incomparable types.
    try:
        if isinstance(op, ast.Lt):
            return left < right  # type: ignore[operator]
        if isinstance(op, ast.LtE):
            return left <= right  # type: ignore[operator]
        if isinstance(op, ast.Gt):
            return left > right  # type: ignore[operator]
        if isinstance(op, ast.GtE):
            return left >= right  # type: ignore[operator]
    except TypeError:
        return False
    raise ExpressionError(f"unsupported comparison: {type(op).__name__}")


def _eval(node: ast.AST, ctx: dict) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body, ctx)

    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result: Any = True
            for v in node.values:
                result = _eval(v, ctx)
                if not result:
                    return result
            return result
        # Or
        result = False
        for v in node.values:
            result = _eval(v, ctx)
            if result:
                return result
        return result

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval(node.operand, ctx)

    if isinstance(node, ast.Compare):
        left = _eval(node.left, ctx)
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval(comparator, ctx)
            if not _apply_cmp(op, left, right):
                return False
            left = right
        return True

    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.List):
        return [_eval(e, ctx) for e in node.elts]

    if isinstance(node, ast.Tuple):
        return tuple(_eval(e, ctx) for e in node.elts)

    if isinstance(node, ast.Name):
        # Names resolve ONLY from the top-level context dict.
        return ctx.get(node.id)

    if isinstance(node, ast.Attribute):
        base = _eval(node.value, ctx)
        if isinstance(base, dict):
            return base.get(node.attr)
        # Non-dict base → no attribute navigation (prevents Python attr escape).
        return None

    if isinstance(node, ast.Call):
        fn = node.func.id  # type: ignore[attr-defined]
        args = [_eval(a, ctx) for a in node.args]
        if fn == "matches":
            field = args[0] if args else None
            pattern = args[1] if len(args) > 1 else ""
            if field is None:
                return False
            try:
                return re.search(str(pattern), str(field)) is not None
            except re.error:
                return False
        if fn == "len":
            try:
                return len(args[0]) if args else 0
            except TypeError:
                return 0
        if fn == "lower":
            return str(args[0]).lower() if args else ""
        raise ExpressionError(f"unsupported function: {fn}")

    raise ExpressionError(f"unsupported expression node: {type(node).__name__}")


def evaluate(expression: str, context: dict) -> bool:
    """Evaluate a hook condition against a context dict, returning a bool.

    Empty/whitespace expression → ``True`` (no condition = always match).
    Raises ``ExpressionError`` for disallowed syntax (fail closed — callers
    should treat a raising hook as non-matching and log it).
    """
    if expression is None or expression.strip() == "":
        return True
    tree = _parse(expression)
    _check_nodes(tree)
    return bool(_eval(tree, context or {}))
