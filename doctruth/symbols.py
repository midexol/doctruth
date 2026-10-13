"""Extract the things prose can talk about: functions, classes, methods, CLI flags, env vars.

Python is parsed properly with `ast`. JS/TS uses careful regexes, which is enough for the
symbols docs actually mention (exported functions, classes, arrow functions).
"""
from __future__ import annotations

import ast
import hashlib
import re
import warnings
from typing import Iterable

from .models import Param, Symbol

PY_EXT = (".py",)
JS_EXT = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
CODE_EXT = PY_EXT + JS_EXT

FLAG_RE = re.compile(
    r"""(?:add_argument|add_option|option|Option|\.option)\s*\(\s*(?:['"]-\w['"]\s*,\s*)?['"](--[A-Za-z0-9][\w-]*)['"]"""
)
ENV_RES = [
    re.compile(r"""os\.environ(?:\.get)?\s*[\[(]\s*['"]([A-Z][A-Z0-9_]{2,})['"]"""),
    re.compile(r"""os\.getenv\s*\(\s*['"]([A-Z][A-Z0-9_]{2,})['"]"""),
    re.compile(r"""process\.env\.([A-Z][A-Z0-9_]{2,})"""),
    re.compile(r"""process\.env\[\s*['"]([A-Z][A-Z0-9_]{2,})['"]"""),
    re.compile(r"""Deno\.env\.get\(\s*['"]([A-Z][A-Z0-9_]{2,})['"]"""),
]


def is_code_file(path: str) -> bool:
    return path.endswith(CODE_EXT)


def extract(path: str, source: str) -> list[Symbol]:
    syms: list[Symbol] = []
    if path.endswith(PY_EXT):
        syms.extend(_python_symbols(path, source))
    elif path.endswith(JS_EXT):
        syms.extend(_js_symbols(path, source))
    else:
        return syms
    syms.extend(_string_symbols(path, source))
    return syms


# ---------------------------------------------------------------- python

class _Anon(ast.NodeTransformer):
    """Normalise a function so renames hash identically."""

    def generic_visit(self, node):
        for attr in ("lineno", "col_offset", "end_lineno", "end_col_offset"):
            if hasattr(node, attr):
                setattr(node, attr, 0)
        return super().generic_visit(node)


def _body_hash(node: ast.AST) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        clone = ast.parse(ast.unparse(node))
    fn = clone.body[0]
    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        fn.name = "_"
        if fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(getattr(fn.body[0], "value", None), ast.Constant) \
                and isinstance(fn.body[0].value.value, str):
            fn.body = fn.body[1:] or [ast.Pass()]  # docstring edits shouldn't break rename detection
    _Anon().visit(clone)
    return hashlib.sha1(ast.dump(clone).encode()).hexdigest()[:16]


def _py_params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[Param]:
    a = fn.args
    params: list[Param] = []
    positional = a.posonlyargs + a.args
    defaults = [None] * (len(positional) - len(a.defaults)) + list(a.defaults)
    for arg, d in zip(positional, defaults):
        params.append(Param(arg.arg, ast.unparse(d) if d is not None else None))
    if a.vararg:
        params.append(Param("*" + a.vararg.arg))
    for arg, d in zip(a.kwonlyargs, a.kw_defaults):
        params.append(Param(arg.arg, ast.unparse(d) if d is not None else None))
    if a.kwarg:
        params.append(Param("**" + a.kwarg.arg))
    return [p for p in params if p.name not in ("self", "cls")]


def _python_symbols(path: str, source: str) -> list[Symbol]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # old files: invalid escape sequences etc.
            tree = ast.parse(source)
    except SyntaxError:
        return _js_like_fallback(path, source)
    out: list[Symbol] = []

    def visit(nodes: Iterable[ast.stmt], prefix: str, in_class: bool):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("__") and node.name.endswith("__"):
                    continue
                qual = f"{prefix}{node.name}"
                out.append(Symbol(
                    kind="method" if in_class else "function", name=node.name, qualname=qual, path=path,
                    line=node.lineno, params=_py_params(node), body_hash=_body_hash(node),
                    source=ast.get_source_segment(source, node),
                ))
            elif isinstance(node, ast.ClassDef):
                qual = f"{prefix}{node.name}"
                out.append(Symbol(kind="class", name=node.name, qualname=qual, path=path, line=node.lineno,
                                  body_hash=_body_hash(node), source=ast.get_source_segment(source, node)))
                visit(node.body, qual + ".", True)

    visit(tree.body, "", False)
    return out


# ---------------------------------------------------------------- javascript / typescript

JS_PATTERNS = [
    ("function", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\(([^)]*)\)", re.M)),
    ("function", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s+)?\(([^)]*)\)\s*(?::[^=]+)?=>", re.M)),
    ("function", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?function\b[^(]*\(([^)]*)\)", re.M)),
    ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)()", re.M)),
]


def _js_params(raw: str) -> list[Param]:
    params = []
    depth, cur = 0, ""
    for ch in raw + ",":
        if ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            depth -= 1
        if ch == "," and depth == 0:
            cur = cur.strip()
            if cur:
                name, _, default = cur.partition("=")
                name = name.split(":")[0].strip().lstrip(".")
                if name and re.match(r"^[A-Za-z_$][\w$]*$", name):
                    params.append(Param(name, default.strip() or None))
            cur = ""
        else:
            cur += ch
    return params


def _js_symbols(path: str, source: str) -> list[Symbol]:
    out: list[Symbol] = []
    seen: set[str] = set()
    for kind, rx in JS_PATTERNS:
        for m in rx.finditer(source):
            name = m.group(1)
            if name in seen:
                continue
            seen.add(name)
            line = source.count("\n", 0, m.start(1)) + 1
            body = _js_body(source, m.end())
            normalised = re.sub(r"\s+", " ", body)
            out.append(Symbol(kind=kind, name=name, qualname=name, path=path, line=line,
                              params=_js_params(m.group(2)) if kind == "function" else [],
                              body_hash=hashlib.sha1(normalised.encode()).hexdigest()[:16] if body else None,
                              source=source[m.start():m.end()] + body))
    return out


def _js_body(source: str, start: int) -> str:
    i = source.find("{", start)
    if i == -1 or i - start > 200:
        return ""
    depth = 0
    for j in range(i, len(source)):
        if source[j] == "{":
            depth += 1
        elif source[j] == "}":
            depth -= 1
            if depth == 0:
                return source[i:j + 1]
    return source[i:]


def _js_like_fallback(path: str, source: str) -> list[Symbol]:
    return []


# ---------------------------------------------------------------- flags and env vars (any language)

def _string_symbols(path: str, source: str) -> list[Symbol]:
    out: list[Symbol] = []
    seen: set[tuple[str, str]] = set()
    for m in FLAG_RE.finditer(source):
        key = ("flag", m.group(1))
        if key not in seen:
            seen.add(key)
            out.append(Symbol("flag", m.group(1), m.group(1), path, source.count("\n", 0, m.start()) + 1))
    for rx in ENV_RES:
        for m in rx.finditer(source):
            key = ("env", m.group(1))
            if key not in seen:
                seen.add(key)
                out.append(Symbol("env", m.group(1), m.group(1), path, source.count("\n", 0, m.start()) + 1))
    return out
