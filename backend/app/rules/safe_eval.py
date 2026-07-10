"""A restricted boolean/arithmetic expression language for user-defined rules.

Deliberately NOT a general Python `eval`: the AST is walked first and any node
outside a small whitelist (no `Call`, no `Attribute`, no `Subscript`, no
comprehensions, no lambdas, no imports, no f-strings) is rejected before the
expression is ever compiled. That closes the standard eval-sandbox escape
routes (e.g. `().__class__.__bases__...`), every one of which needs a `Call`
or `Attribute` node to reach anything dangerous — so excluding both, plus
running with `__builtins__` stripped, leaves nothing reachable beyond the
caller-supplied variables and plain comparison/boolean/arithmetic operators.

Public surface:
- `RuleExpressionError`
- `validate_expression(expr, allowed_names) -> None`  (raises on anything unsafe)
- `compile_expression(expr, allowed_names) -> Callable[[dict], bool]`
"""
from __future__ import annotations

import ast
from typing import Callable

_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare,
    ast.Name, ast.Load, ast.Constant, ast.List, ast.Tuple,
    ast.And, ast.Or, ast.Not,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.USub, ast.UAdd,
)


class RuleExpressionError(ValueError):
    """A custom rule expression failed to parse or used a disallowed construct."""


def _parse(expr: str, allowed_names: set[str]) -> ast.Expression:
    if len(expr) > 500:
        raise RuleExpressionError("expression too long (500 char max)")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise RuleExpressionError(f"syntax error: {e.msg}") from e
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise RuleExpressionError(
                f"'{type(node).__name__}' is not allowed here — only comparisons, "
                "boolean logic (and/or/not), arithmetic, and 'in' checks over the "
                "listed fields are permitted (no function calls or attribute access)"
            )
        if isinstance(node, ast.Name) and node.id not in allowed_names:
            raise RuleExpressionError(
                f"unknown field '{node.id}' — available: {', '.join(sorted(allowed_names))}"
            )
    return tree


def validate_expression(expr: str, allowed_names: set[str]) -> None:
    """Raise RuleExpressionError if `expr` isn't a safe expression over
    `allowed_names`. Never evaluates anything."""
    _parse(expr, allowed_names)


def compile_expression(expr: str, allowed_names: set[str]) -> Callable[[dict], bool]:
    """Validate then compile `expr` into a callable `variables -> bool`.

    The returned callable still needs its own runtime error handling by the
    caller (a field that's `None` in one row, or a str/int comparison, can
    raise `TypeError` — that's a per-row concern, not a compile-time one)."""
    tree = _parse(expr, allowed_names)
    code = compile(tree, "<rule-expression>", "eval")

    def run(variables: dict) -> bool:
        return bool(eval(code, {"__builtins__": {}}, dict(variables)))  # noqa: S307 — AST pre-validated in _parse()

    return run
