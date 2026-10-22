"""Guard against the class of bug that P0 was: a function-local ``import X`` makes ``X`` local for
the WHOLE function body, so any use of ``X`` BEFORE that import raises ``UnboundLocalError`` at
runtime — invisibly, because it is a compile-time scoping decision the whole test suite stays
green around.

The concrete instance: task_agent._execute_plan_node used ``boto3.client`` at line 1822 while a
redundant ``import boto3`` sat later in the same function (added by 686ab83), so every plan node's
durable S3 write raised UnboundLocalError and silently persisted nothing.

This test walks every function in agent/src and fails if a name is USED (loaded) before a
same-scope import that binds it. Nested functions/lambdas are their own scope and are not
descended into; function parameters are excluded (they are already bound).
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def _own_scope_nodes(fn: ast.AST) -> list[ast.AST]:
    """All AST nodes in ``fn``'s OWN scope — not descending into nested functions/lambdas."""
    out: list[ast.AST] = []

    def rec(nodes) -> None:
        for n in nodes:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue  # a nested scope binds names independently
            out.append(n)
            rec(list(ast.iter_child_nodes(n)))

    rec(list(fn.body))  # type: ignore[attr-defined]
    return out


def _param_names(fn: ast.AST) -> set[str]:
    a = fn.args  # type: ignore[attr-defined]
    names = {p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)}
    if a.vararg:
        names.add(a.vararg.arg)
    if a.kwarg:
        names.add(a.kwarg.arg)
    return names


def _violations_in_file(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    out: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        scope = _own_scope_nodes(fn)
        params = _param_names(fn)

        first_import: dict[str, int] = {}
        for n in scope:
            if isinstance(n, ast.Import):
                for al in n.names:
                    name = (al.asname or al.name).split(".")[0]
                    first_import[name] = min(first_import.get(name, n.lineno), n.lineno)
            elif isinstance(n, ast.ImportFrom):
                for al in n.names:
                    name = al.asname or al.name
                    first_import[name] = min(first_import.get(name, n.lineno), n.lineno)

        first_load: dict[str, int] = {}
        for n in scope:
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                first_load[n.id] = min(first_load.get(n.id, n.lineno), n.lineno)

        for name, imp_line in first_import.items():
            if name in params:
                continue
            used = first_load.get(name)
            if used is not None and used < imp_line:
                out.append(
                    f"{path.name}:{fn.name}: '{name}' used at line {used} before its same-scope "
                    f"import at line {imp_line} (UnboundLocalError risk)"
                )
    return out


def test_no_use_before_same_scope_import():
    violations: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        violations.extend(_violations_in_file(path))
    assert not violations, "use-before-same-scope-import found:\n" + "\n".join(violations)
