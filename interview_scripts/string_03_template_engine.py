"""
Program 3 — Mini Jinja-like template engine.

Supports:
  * {{ var }}, {{ obj.attr }}, {{ a.b.c }}
  * {{ var | upper }}, {{ var | default:"N/A" }}, chained filters
  * {% if expr %} ... {% else %} ... {% endif %}
  * {% for item in iterable %} ... {% endfor %}  (with loop.index, loop.first)
  * Comments: {# ... #}
  * Auto-escaping of HTML in {{ var }} unless |safe filter is used

Implementation: tokenize -> parse to AST -> render against a context.

Demonstrates:
  * A small recursive-descent style parser
  * Visitor-pattern rendering
  * Pluggable filter registry
  * Safe attribute lookup with dotted paths
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------

TOKEN_RE = re.compile(
    r"(?P<comment>\{#.*?#\})"
    r"|\{%\s*(?P<block>.*?)\s*%\}"
    r"|\{\{\s*(?P<expr>.*?)\s*\}\}",
    re.DOTALL,
)


@dataclass
class Token:
    kind: str        # "text" | "expr" | "block" | "comment"
    value: str
    inner: str = ""  # the trimmed content for expr/block


def tokenize(source: str) -> list[Token]:
    tokens: list[Token] = []
    pos = 0
    for m in TOKEN_RE.finditer(source):
        if m.start() > pos:
            tokens.append(Token("text", source[pos : m.start()]))
        if m.group("comment") is not None:
            pass  # drop comments
        elif m.group("block") is not None:
            tokens.append(Token("block", m.group(0), m.group("block").strip()))
        elif m.group("expr") is not None:
            tokens.append(Token("expr", m.group(0), m.group("expr").strip()))
        pos = m.end()
    if pos < len(source):
        tokens.append(Token("text", source[pos:]))
    return tokens


# ---------------------------------------------------------------------------
# AST nodes
# ---------------------------------------------------------------------------

@dataclass
class Text:
    value: str

@dataclass
class Expr:
    raw: str  # e.g. "user.name | upper | default:'?'"

@dataclass
class If:
    cond: str
    then: list = field(default_factory=list)
    otherwise: list = field(default_factory=list)

@dataclass
class For:
    var: str
    iterable: str
    body: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class _NodeList(list):
    """A list of AST nodes that can carry the stop-word that ended parsing."""
    stopped: str = ""


def parse(tokens: list[Token]) -> _NodeList:
    """Build a list-of-nodes AST."""
    it = iter(tokens)
    return _parse_until(it, ())


def _parse_until(it, stop_words: tuple[str, ...]) -> _NodeList:
    out = _NodeList()
    for tok in it:
        if tok.kind == "text":
            out.append(Text(tok.value))
        elif tok.kind == "expr":
            out.append(Expr(tok.inner))
        elif tok.kind == "block":
            head = tok.inner.split(None, 1)[0]
            if head in stop_words:
                out.stopped = head
                return out
            if head == "if":
                cond = tok.inner[2:].strip()
                node = If(cond=cond)
                node.then = _parse_until(it, ("else", "endif"))
                if node.then.stopped == "else":
                    node.otherwise = _parse_until(it, ("endif",))
                out.append(node)
            elif head == "for":
                m = re.match(r"for\s+(\w+)\s+in\s+(.+)", tok.inner)
                if not m:
                    raise SyntaxError(f"Bad for-loop: {tok.inner!r}")
                node = For(var=m.group(1), iterable=m.group(2).strip())
                node.body = _parse_until(it, ("endfor",))
                out.append(node)
            else:
                raise SyntaxError(f"Unknown block: {tok.inner!r}")
    return out


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

FILTERS: dict[str, Callable[..., Any]] = {
    "upper": lambda v: str(v).upper(),
    "lower": lambda v: str(v).lower(),
    "title": lambda v: str(v).title(),
    "len":   lambda v: len(v),
    "default": lambda v, fallback="": v if v not in (None, "", []) else fallback,
    "safe":  lambda v: _Safe(str(v)),
    "join":  lambda v, sep=", ": sep.join(str(x) for x in v),
}


class _Safe(str):
    """Marker subclass: render without HTML-escaping."""


def register_filter(name: str, fn: Callable[..., Any]) -> None:
    FILTERS[name] = fn


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------

def _lookup(path: str, context: dict) -> Any:
    """Resolve dotted paths against a context dict, falling back to attrs."""
    parts = path.strip().split(".")
    cur: Any = context.get(parts[0], _MISSING)
    for p in parts[1:]:
        if cur is _MISSING:
            return _MISSING
        if isinstance(cur, dict):
            cur = cur.get(p, _MISSING)
        else:
            cur = getattr(cur, p, _MISSING)
    return cur


_MISSING = object()


def _eval_expression(raw: str, context: dict) -> Any:
    """Evaluate `path | filter | filter:arg` expressions."""
    parts = [p.strip() for p in raw.split("|")]
    value = _lookup(parts[0], context)
    if value is _MISSING:
        value = ""
    for f in parts[1:]:
        if ":" in f:
            name, arg = f.split(":", 1)
            arg_val = _eval_literal(arg.strip(), context)
            value = FILTERS[name.strip()](value, arg_val)
        else:
            value = FILTERS[f](value)
    return value


def _eval_literal(s: str, context: dict) -> Any:
    """Quoted strings are literals; bare names are looked up; numbers parsed."""
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return _lookup(s, context)


def _eval_condition(expr: str, context: dict) -> bool:
    """Tiny condition evaluator: supports ==, !=, and truthy on a single path."""
    for op in ("==", "!="):
        if op in expr:
            left, right = [s.strip() for s in expr.split(op, 1)]
            lv = _eval_literal(left, context)
            rv = _eval_literal(right, context)
            return (lv == rv) if op == "==" else (lv != rv)
    val = _lookup(expr.strip(), context)
    return bool(val) and val is not _MISSING


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

def render(nodes: Iterable, context: dict) -> str:
    out: list[str] = []
    for n in nodes:
        if isinstance(n, Text):
            out.append(n.value)
        elif isinstance(n, Expr):
            v = _eval_expression(n.raw, context)
            out.append(str(v) if isinstance(v, _Safe) else html.escape(str(v)))
        elif isinstance(n, If):
            branch = n.then if _eval_condition(n.cond, context) else n.otherwise
            out.append(render(branch, context))
        elif isinstance(n, For):
            iterable = _lookup(n.iterable, context) or []
            items = list(iterable)
            for i, item in enumerate(items):
                ctx = dict(context)
                ctx[n.var] = item
                ctx["loop"] = {"index": i, "index1": i + 1, "first": i == 0, "last": i == len(items) - 1}
                out.append(render(n.body, ctx))
    return "".join(out)


def render_template(source: str, context: dict) -> str:
    """One-shot helper: source + context -> rendered string."""
    return render(parse(tokenize(source)), context)


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    template = """\
<html>
<body>
  <h1>Hello, {{ user.name | title }}!</h1>
  {# greet differently for VIPs #}
  {% if user.tier == "gold" %}
    <p>Welcome back, valued customer.</p>
  {% else %}
    <p>Thanks for visiting.</p>
  {% endif %}
  <ul>
  {% for item in cart %}
    <li>{{ loop.index1 }}. {{ item.name }} — ${{ item.price }}{% if loop.last %} (last){% endif %}</li>
  {% endfor %}
  </ul>
  <p>Total items: {{ cart | len }}</p>
  <p>Notes: {{ note | default:"none" }}</p>
  <p>Raw HTML: {{ banner | safe }}</p>
</body>
</html>
"""
    ctx = {
        "user": {"name": "ada lovelace", "tier": "gold"},
        "cart": [
            {"name": "Notebook", "price": 12},
            {"name": "Pen", "price": 3},
            {"name": "Mug", "price": 9},
        ],
        "note": "",
        "banner": "<em>limited time!</em>",
    }
    print(render_template(template, ctx))
