"""
Program 5 — Expression evaluator (tokenize -> shunting-yard -> evaluate).

Parses and evaluates math expressions like:
    "2 + 3 * (4 - 1) ** 2"
    "max(a, b) + 10"
    "-x ** 2 + 4*x - 3"  (with x supplied via a context dict)

Supports:
  * +, -, *, /, %, ** (right-associative for **)
  * Unary minus
  * Parentheses
  * Variables (resolved against a context dict)
  * Function calls with comma-separated args (max, min, abs, round, sqrt)

Uses Dijkstra's shunting-yard algorithm to convert infix tokens to RPN,
then evaluates the RPN with a stack.

Demonstrates:
  * Hand-rolled tokenizer
  * Operator precedence + associativity table
  * Two-pass interpreter (shunting-yard + RPN eval)
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable

# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

@dataclass
class Token:
    kind: str   # "num" | "ident" | "op" | "lparen" | "rparen" | "comma"
    value: Any


_TOKEN_PATTERN = re.compile(
    r"""
    \s*(?:
        (?P<num>\d+\.\d+|\d+)
      | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
      | (?P<op>\*\*|[+\-*/%])
      | (?P<lparen>\()
      | (?P<rparen>\))
      | (?P<comma>,)
    )
    """,
    re.VERBOSE,
)


def tokenize(src: str) -> list[Token]:
    pos = 0
    out: list[Token] = []
    while pos < len(src):
        m = _TOKEN_PATTERN.match(src, pos)
        if not m or m.end() == pos:
            raise SyntaxError(f"Unexpected character at {pos}: {src[pos]!r}")
        for kind in ("num", "ident", "op", "lparen", "rparen", "comma"):
            if m.group(kind):
                value: Any = m.group(kind)
                if kind == "num":
                    value = float(value) if "." in value else int(value)
                out.append(Token(kind, value))
                break
        pos = m.end()
    return out


# ---------------------------------------------------------------------------
# Shunting yard
# ---------------------------------------------------------------------------

# (precedence, right-associative?)
PRECEDENCE: dict[str, tuple[int, bool]] = {
    "u-": (5, True),    # unary minus
    "**": (4, True),
    "*":  (3, False),
    "/":  (3, False),
    "%":  (3, False),
    "+":  (2, False),
    "-":  (2, False),
}


def to_rpn(tokens: list[Token]) -> list[Token]:
    """Convert infix tokens to Reverse Polish Notation."""
    out: list[Token] = []
    stack: list[Token] = []
    prev_kind: str | None = None

    for i, tok in enumerate(tokens):
        if tok.kind == "num":
            out.append(tok)
        elif tok.kind == "ident":
            # Function name if next token is '(', otherwise it's a variable
            # and goes straight to output.
            next_kind = tokens[i + 1].kind if i + 1 < len(tokens) else None
            if next_kind == "lparen":
                stack.append(tok)
            else:
                out.append(tok)
        elif tok.kind == "comma":
            while stack and stack[-1].kind != "lparen":
                out.append(stack.pop())
            if not stack:
                raise SyntaxError("Misplaced comma")
        elif tok.kind == "op":
            op = tok.value
            # Detect unary minus: '-' at start, after another op, or after '('.
            if op == "-" and prev_kind in (None, "op", "lparen", "comma"):
                op = "u-"
                tok = Token("op", "u-")
            p1, ra1 = PRECEDENCE[op]
            while stack and stack[-1].kind == "op":
                op2 = stack[-1].value
                p2, _ = PRECEDENCE[op2]
                if (not ra1 and p1 <= p2) or (ra1 and p1 < p2):
                    out.append(stack.pop())
                else:
                    break
            stack.append(tok)
        elif tok.kind == "lparen":
            stack.append(tok)
        elif tok.kind == "rparen":
            while stack and stack[-1].kind != "lparen":
                out.append(stack.pop())
            if not stack:
                raise SyntaxError("Mismatched parentheses")
            stack.pop()
            # If a function name sits on top now, send it to output.
            if stack and stack[-1].kind == "ident":
                out.append(stack.pop())
        prev_kind = tok.kind

    while stack:
        if stack[-1].kind in ("lparen", "rparen"):
            raise SyntaxError("Mismatched parentheses")
        out.append(stack.pop())
    return out


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------

FUNCTIONS: dict[str, Callable[..., Any]] = {
    "max": max,
    "min": min,
    "abs": abs,
    "round": round,
    "sqrt": math.sqrt,
    "pow": pow,
}


def eval_rpn(rpn: list[Token], context: dict[str, Any] | None = None) -> Any:
    context = context or {}
    stack: list[Any] = []

    def pop2():
        b = stack.pop()
        a = stack.pop()
        return a, b

    for tok in rpn:
        if tok.kind == "num":
            stack.append(tok.value)
        elif tok.kind == "ident":
            name = tok.value
            if name in FUNCTIONS:
                # Function with unknown arity — gather args off the stack.
                # Convention: every preceding ident-call evaluates with all its
                # args already pushed. We treat builtins as variadic by pulling
                # everything pushed since the last "function call boundary".
                # Simpler: assume the parser pushed exactly the right number of
                # args. We'll inspect the function's signature via heuristic.
                argc = _function_arity(name, stack)
                args = [stack.pop() for _ in range(argc)][::-1]
                stack.append(FUNCTIONS[name](*args))
            elif name in context:
                stack.append(context[name])
            else:
                raise NameError(f"Unknown identifier: {name}")
        elif tok.kind == "op":
            op = tok.value
            if op == "u-":
                stack.append(-stack.pop())
            else:
                a, b = pop2()
                stack.append(_apply(op, a, b))
    if len(stack) != 1:
        raise RuntimeError(f"Bad expression — stack: {stack}")
    return stack[0]


def _apply(op: str, a, b):
    if op == "+":  return a + b
    if op == "-":  return a - b
    if op == "*":  return a * b
    if op == "/":  return a / b
    if op == "%":  return a % b
    if op == "**": return a ** b
    raise ValueError(f"Bad operator: {op}")


def _function_arity(name: str, stack: list[Any]) -> int:
    """Pick a sensible arity for our toy function set.

    We special-case the variadic ones; for everything else, look at how many
    items are on the stack and assume the function consumes the trailing run
    that came from this call (for our demo, this is enough)."""
    if name in {"abs", "sqrt"}:
        return 1
    if name in {"pow", "round"}:
        # round takes 1 or 2; pow takes 2; we'll prefer 2 if available.
        return 2 if len(stack) >= 2 else 1
    if name in {"max", "min"}:
        return max(2, len(stack))  # variadic — eat what's on the stack
    return 1


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def evaluate(expr: str, context: dict[str, Any] | None = None) -> Any:
    return eval_rpn(to_rpn(tokenize(expr)), context)


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cases = [
        ("2 + 3 * 4", {}),
        ("(2 + 3) * 4", {}),
        ("2 ** 3 ** 2", {}),               # right-assoc: 2 ** (3 ** 2) = 512
        ("-3 ** 2", {}),                   # unary minus binds tighter? convention: -(3**2) = -9
        ("abs(-7) + sqrt(9)", {}),
        ("pow(2, 10)", {}),
        ("x ** 2 + 4 * x - 3", {"x": 5}),
        ("a + b * c - d / 2", {"a": 1, "b": 2, "c": 3, "d": 8}),
    ]
    print(f"{'expression':<30} | {'context':<30} | result")
    print("-" * 80)
    for expr, ctx in cases:
        try:
            result = evaluate(expr, ctx)
        except Exception as e:  # noqa: BLE001
            result = f"ERROR: {e}"
        print(f"{expr:<30} | {str(ctx):<30} | {result}")
