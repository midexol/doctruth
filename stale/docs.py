"""Split documentation and code comments into chunks that can be checked for stale claims."""
from __future__ import annotations

import ast
import fnmatch
import posixpath
import re
import warnings
from typing import Iterable

from .models import DocChunk

DOC_EXT = (".md", ".mdx", ".markdown", ".rst", ".txt", ".adoc")
DOC_NAMES = ("README", "CONTRIBUTING", "USAGE", "INSTALL")

DEFAULT_IGNORE = [
    "CHANGELOG*", "**/CHANGELOG*", "HISTORY*", "**/HISTORY*", "NEWS*", "RELEASE*",
    "**/node_modules/**", "node_modules/**", "**/vendor/**", "vendor/**", "dist/**", "build/**",
    "**/*.lock", "**/package-lock.json", "*.min.js", "**/*.min.js", ".git/**",
]

FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([\w+-]*)")
LIST_START = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def is_ignored(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def is_doc_file(path: str) -> bool:
    base = posixpath.basename(path)
    return path.lower().endswith(DOC_EXT) or base.upper() in DOC_NAMES


def _ignored_lines(lines: list[str], start: int) -> set[int]:
    out: set[int] = set()
    for i, l in enumerate(lines):
        if "stale:ignore" in l and "stale:ignore-file" not in l:
            out.add(start + i)
            out.add(start + i + 1)
    return out


def chunk_markdown(path: str, text: str) -> list[DocChunk]:
    if "stale:ignore-file" in text:
        return []
    lines = text.split("\n")
    all_ignored = _ignored_lines(lines, 1)
    chunks: list[DocChunk] = []
    buf: list[str] = []
    buf_start = 0
    i = 0

    def flush():
        nonlocal buf
        if buf and any(l.strip() for l in buf):
            chunks.append(DocChunk(path, buf_start, "\n".join(buf), "prose", ignored_lines=all_ignored))
        buf = []

    while i < len(lines):
        line = lines[i]
        fence = FENCE_RE.match(line)
        if fence:
            flush()
            marker, lang = fence.group(2), (fence.group(3) or "").lower() or None
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith(marker[0] * len(marker)):
                j += 1
            body = lines[i + 1:j]
            skip = any("stale:skip" in l for l in lines[max(0, i - 2):i])
            chunk = DocChunk(path, i + 2, "\n".join(body), "code", lang=lang, ignored_lines=all_ignored)
            if skip:
                chunk.lang = (lang or "") + ":skip"
            chunks.append(chunk)
            i = j + 1
            continue
        if not line.strip() or line.lstrip().startswith("#") or LIST_START.match(line):
            flush()
            if line.strip():
                buf, buf_start = [line], i + 1
                if line.lstrip().startswith("#"):
                    flush()
            i += 1
            continue
        if not buf:
            buf_start = i + 1
        buf.append(line)
        i += 1
    flush()
    return chunks


def chunk_python_comments(path: str, text: str) -> list[DocChunk]:
    if "stale:ignore-file" in text:
        return []
    lines = text.split("\n")
    ignored = _ignored_lines(lines, 1)
    chunks: list[DocChunk] = []
    block: list[str] = []
    start = 0
    for i, l in enumerate(lines + [""]):
        s = l.strip()
        if s.startswith("#") and not s.startswith("#!"):
            if not block:
                start = i + 1
            block.append(l)
        else:
            idx = l.find("  # ")  # trailing comment
            if block:
                chunks.append(DocChunk(path, start, "\n".join(block), "comment", ignored_lines=ignored))
                block = []
            if idx != -1 and l[:idx].strip():
                chunks.append(DocChunk(path, i + 1, l, "comment", ignored_lines=ignored))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except SyntaxError:
        return chunks
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                seg = ast.get_source_segment(text, first) or ""
                chunks.append(DocChunk(path, first.lineno, seg, "docstring", ignored_lines=ignored))
    return chunks


JS_COMMENT = re.compile(r"/\*[\s\S]*?\*/|//[^\n]*")


def chunk_js_comments(path: str, text: str) -> list[DocChunk]:
    if "stale:ignore-file" in text:
        return []
    ignored = _ignored_lines(text.split("\n"), 1)
    chunks: list[DocChunk] = []
    for m in JS_COMMENT.finditer(text):
        line = text.count("\n", 0, m.start()) + 1
        prev = text[text.rfind("\n", 0, m.start()) + 1:m.start()]
        if "://" in prev[-6:] or prev.count('"') % 2 or prev.count("'") % 2:
            continue  # inside a string literal (e.g. a URL)
        chunks.append(DocChunk(path, line, m.group(0), "comment", ignored_lines=ignored))
    return chunks


def chunks_for(path: str, text: str) -> list[DocChunk]:
    if is_doc_file(path):
        return chunk_markdown(path, text)
    if path.endswith(".py"):
        return chunk_python_comments(path, text)
    if path.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")):
        return chunk_js_comments(path, text)
    return []
