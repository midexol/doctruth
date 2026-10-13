"""Deterministic matching of semantic changes against doc chunks.

Design rule: precision over recall. A reference only counts if it is unambiguous: inside a code
block or `inline code`, called like a function, accessed as an attribute, or an identifier
shaped enough (snake_case, camelCase, CONSTANT_CASE, --flag) that it can't be an English word.
"""
from __future__ import annotations

import re
from typing import Optional

from .models import Change, DocChunk, Finding

NUM_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
             "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
             "nineteen", "twenty"]
INLINE_CODE = re.compile(r"(`+)(.+?)\1")
IDENT_SHAPED = re.compile(r"(_|[a-z][A-Z]|\$|\d)")


def _ident_rx(name: str) -> re.Pattern:
    return re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")


def _flag_rx(name: str) -> re.Pattern:
    return re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])")


def _in_inline_code(text: str, offset: int) -> bool:
    ls = text.rfind("\n", 0, offset) + 1
    le = text.find("\n", offset)
    line = text[ls: le if le != -1 else len(text)]
    rel = offset - ls
    return any(m.start(2) <= rel < m.end(2) for m in INLINE_CODE.finditer(line))


def ref_strength(chunk: DocChunk, offset: int, name: str, kind: str) -> float:
    """How sure are we that the text at `offset` refers to the symbol `name`?"""
    text = chunk.text
    end = offset + len(name)
    code = chunk.context == "code"
    inline = _in_inline_code(text, offset)
    if kind == "flag":
        return 0.97
    if kind == "env":
        if code or inline or text[max(0, offset - 1):offset] == "$":
            return 0.95
        return 0.88 if "_" in name else 0.7
    if kind in ("file", "module"):
        return 0.95 if (code or inline or "/" in name or "." in name) else 0.7
    called = bool(re.match(r"\s*\(", text[end:end + 3]))
    attr = text[max(0, offset - 1):offset] == "."
    shaped = bool(IDENT_SHAPED.search(name))
    if code:
        return 0.95 if (called or attr or shaped or chunk.lang in ("python", "py", "js", "ts", "javascript", "typescript")) else 0.75
    if inline:
        return 0.95
    if called:
        return 0.9
    if attr and shaped:
        return 0.85
    if shaped:
        return 0.8 if chunk.context in ("prose",) else 0.85
    return 0.0  # a plain English-looking word in prose: never flag on this alone


def _refs(chunk: DocChunk, name: str, kind: str, min_strength: float = 0.01) -> list[tuple[int, float]]:
    rx = _flag_rx(name) if kind == "flag" else _ident_rx(name)
    out = []
    for m in rx.finditer(chunk.text):
        s = ref_strength(chunk, m.start(), name, kind)
        if s >= min_strength:
            out.append((m.start(), s))
    return out


def _replace_on_line(line: str, old: str, new: str, kind: str) -> str:
    rx = _flag_rx(old) if kind == "flag" else _ident_rx(old)
    return rx.sub(new, line)


class RuleEngine:
    def __init__(self, changes: list[Change]):
        self.changes = changes

    def run(self, chunks: list[DocChunk]) -> list[Finding]:
        findings: list[Finding] = []
        for change in self.changes:
            handler = getattr(self, f"_on_{change.kind}", None)
            if handler is None:
                continue
            for chunk in chunks:
                findings.extend(handler(change, chunk))
        return _dedupe([f for f in findings if f.line not in _ignored_for(chunks, f)])

    # ---------------------------------------------------------- handlers

    def _finding(self, chunk: DocChunk, offset: int, change: Change, kind: str, message: str,
                 confidence: float, suggestion_from: Optional[tuple[str, str]] = None) -> Finding:
        line = chunk.line_of(offset)
        original = chunk.line_text(line)
        suggestion = None
        if suggestion_from:
            candidate = _replace_on_line(original, *suggestion_from, change.symbol_kind)
            suggestion = candidate if candidate != original else None
        return Finding(
            path=chunk.path, line=line, kind=kind, message=message, confidence=round(confidence, 2),
            severity="error" if chunk.context == "code" else "warning", old=change.old, new=change.new,
            original_line=original, suggestion=suggestion,
            evidence=f"{change.path}:{change.line}" if change.line else change.path,
        )

    def _on_renamed(self, change: Change, chunk: DocChunk) -> list[Finding]:
        out = []
        new_lines = {chunk.line_of(o) for o, s in _refs(chunk, change.new, change.symbol_kind) if s > 0} if change.new else set()
        for off, s in _refs(chunk, change.old, change.symbol_kind):
            if s <= 0:
                continue
            if chunk.line_of(off) in new_lines:
                continue  # line already names the new symbol in code form ("`X` was renamed to `Y`")
            out.append(self._finding(chunk, off, change, "renamed",
                                     f"References `{change.old}`, which was renamed to `{change.new}`.", s,
                                     (change.old, change.new)))
        return out

    def _on_removed(self, change: Change, chunk: DocChunk) -> list[Finding]:
        out = []
        noun = {"flag": "flag", "env": "environment variable"}.get(change.symbol_kind, change.symbol_kind)
        for off, s in _refs(chunk, change.old, change.symbol_kind):
            if s <= 0:
                continue
            out.append(self._finding(chunk, off, change, "removed",
                                     f"References {noun} `{change.old}`, which no longer exists.", s))
        return out

    def _on_moved(self, change: Change, chunk: DocChunk) -> list[Finding]:
        out = []
        for off, s in _refs(chunk, change.old, "module"):
            line_text = chunk.line_text(chunk.line_of(off))
            if change.param and not _ident_rx(change.param).search(line_text):
                continue  # a moved function only matters where the line also names it
            if change.new and change.new in line_text:
                continue
            what = f"`{change.param}` now lives in `{change.new}`" if change.param else f"module `{change.old}` is now `{change.new}`"
            f = self._finding(chunk, off, change, "moved", f"Imports from `{change.old}`, but {what}.", s)
            f.suggestion = line_text.replace(change.old, change.new) if change.new else None
            out.append(f)
        return out

    def _call_kwargs(self, chunk: DocChunk, func: str, param: str) -> list[int]:
        hits = []
        for m in re.finditer(rf"(?<![\w$]){re.escape(func)}\s*\(", chunk.text):
            depth, i = 1, m.end()
            while i < len(chunk.text) and depth:
                depth += {"(": 1, ")": -1}.get(chunk.text[i], 0)
                i += 1
            call = chunk.text[m.end():i]
            for k in re.finditer(rf"(?<![\w$]){re.escape(param)}\s*[=:](?!=)", call):
                hits.append(m.end() + k.start())
        return hits

    def _prose_param_hits(self, chunk: DocChunk, change: Change) -> list[tuple[int, float]]:
        if chunk.context == "code" or not _refs(chunk, change.old, "function", 0.01):
            return []
        hits = []
        for m in _ident_rx(change.param).finditer(chunk.text):
            if _in_inline_code(chunk.text, m.start()):
                hits.append((m.start(), 0.85))
        return hits

    def _on_param_removed(self, change: Change, chunk: DocChunk) -> list[Finding]:
        out = []
        for off in self._call_kwargs(chunk, change.old, change.param):
            f = self._finding(chunk, off, change, "param_removed",
                              f"Passes `{change.param}=` to `{change.old}()`, but that parameter was removed.", 0.95)
            fixed = re.sub(rf",\s*{re.escape(change.param)}\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^,)]+)", "", f.original_line, count=1)
            if fixed == f.original_line:
                fixed = re.sub(rf"(?<=\(){re.escape(change.param)}\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^,)]+),?\s*", "", f.original_line, count=1)
            f.suggestion = fixed if fixed != f.original_line else None
            out.append(f)
        for off, s in self._prose_param_hits(chunk, change):
            out.append(self._finding(chunk, off, change, "param_removed",
                                     f"Describes parameter `{change.param}` of `{change.old}`, which was removed.", s))
        return out

    def _on_param_renamed(self, change: Change, chunk: DocChunk) -> list[Finding]:
        out = []
        for off in self._call_kwargs(chunk, change.old, change.param):
            f = self._finding(chunk, off, change, "param_renamed",
                              f"Passes `{change.param}=` to `{change.old}()`, but it is now `{change.new_value}`.", 0.95)
            f.suggestion = re.sub(rf"(?<![\w$]){re.escape(change.param)}(\s*[=:])", rf"{change.new_value}\1", f.original_line)
            out.append(f)
        for off, s in self._prose_param_hits(chunk, change):
            f = self._finding(chunk, off, change, "param_renamed",
                              f"Mentions parameter `{change.param}` of `{change.old}`; it is now `{change.new_value}`.", s)
            f.suggestion = f.original_line.replace(f"`{change.param}`", f"`{change.new_value}`")
            out.append(f)
        return out

    def _on_default_changed(self, change: Change, chunk: DocChunk) -> list[Finding]:
        if chunk.context == "code":
            return []  # examples stay valid when a default moves; prose describing it doesn't
        old_forms, new_forms = _value_forms(change.old_value), _value_forms(change.new_value)
        if not old_forms:
            return []
        text = chunk.text
        func_refs = [s for _, s in _refs(chunk, change.old, change.symbol_kind)]
        func_named = bool(_ident_rx(change.old).search(text))
        param_inline = any(_in_inline_code(text, m.start()) for m in _ident_rx(change.param).finditer(text))
        param_word = bool(re.search(rf"\b{re.escape(change.param)}\b", text, re.I))
        if not (func_named or param_inline):
            return []
        out = []
        for form, word in old_forms:
            for m in re.finditer(rf"(?<![\w.]){re.escape(form)}(?![\w.])", text, re.I if word else 0):
                line_text = chunk.line_text(chunk.line_of(m.start()))
                if any(re.search(rf"(?<![\w.]){re.escape(nf)}(?![\w.])", line_text, re.I) for nf, _ in new_forms):
                    continue  # already mentions the new value
                this_line = chunk.line_of(m.start())
                func_on_line = max((s for o, s in _refs(chunk, change.old, change.symbol_kind)
                                    if chunk.line_of(o) == this_line), default=0.0)
                conf = 0.6
                if (func_named or param_inline) and param_word:
                    conf = 0.9 if (max(func_refs, default=0) >= 0.8 or param_inline) else 0.8
                elif param_inline:
                    conf = 0.85
                elif func_on_line >= 0.8:
                    conf = 0.8  # "`retry()` retries three times" – function and value in one sentence
                f = self._finding(chunk, m.start(), change, "default_changed",
                                  f"Says `{change.param}` is {m.group(0)}, but the default of `{change.old}` changed "
                                  f"from {change.old_value} to {change.new_value}.", conf)
                new_text = _render_like(m.group(0), change.new_value, word)
                lt_start = text.rfind("\n", 0, m.start()) + 1
                rel = m.start() - lt_start
                f.suggestion = line_text[:rel] + new_text + line_text[rel + len(m.group(0)):]
                out.append(f)
        return out

    def _on_file_removed(self, change: Change, chunk: DocChunk) -> list[Finding]:
        return self._file_refs(change, chunk, f"Points to `{change.old}`, which was deleted.")

    def _on_file_renamed(self, change: Change, chunk: DocChunk) -> list[Finding]:
        return self._file_refs(change, chunk, f"Points to `{change.old}`, which moved to `{change.new}`.")

    def _file_refs(self, change: Change, chunk: DocChunk, message: str) -> list[Finding]:
        out = []
        for m in re.finditer(rf"(?<![\w/.-])(?:\./)?{re.escape(change.old)}(?![\w/-])", chunk.text):
            f = self._finding(chunk, m.start(), change, change.kind, message, 0.95)
            if change.new:
                f.suggestion = f.original_line.replace(change.old, change.new)
            out.append(f)
        if change.param and change.kind == "file_removed":  # distinctive basename, e.g. `legacy_auth.py`
            for m in re.finditer(rf"(?<![\w/.-]){re.escape(change.param)}(?![\w-])", chunk.text):
                if _in_inline_code(chunk.text, m.start()) or chunk.context == "code":
                    out.append(self._finding(chunk, m.start(), change, change.kind, message, 0.85))
        return out


def _value_forms(value: Optional[str]) -> list[tuple[str, bool]]:
    """Ways prose might spell a default value: 3 -> ["3", "three"], 'json' -> ["json"]."""
    if value is None:
        return []
    v = value.strip()
    if re.fullmatch(r"-?\d+(\.\d+)?", v):
        forms = [(v, False)]
        if v.isdigit() and int(v) < len(NUM_WORDS):
            forms.append((NUM_WORDS[int(v)], True))
        return forms
    if v in ("True", "False", "true", "false"):
        return [(f"`{v}`", False)]
    m = re.fullmatch(r"""(['"])(.+)\1""", v)
    if m and len(m.group(2)) >= 2:
        return [(m.group(2), False)]
    return []


def _render_like(found: str, new_value: str, word: bool) -> str:
    v = new_value.strip().strip("'\"")
    if word and v.isdigit() and int(v) < len(NUM_WORDS):
        w = NUM_WORDS[int(v)]
        return w.capitalize() if found[:1].isupper() else w
    return v


def _ignored_for(chunks: list[DocChunk], f: Finding) -> set[int]:
    for c in chunks:
        if c.path == f.path:
            return c.ignored_lines
    return set()


def _dedupe(findings: list[Finding]) -> list[Finding]:
    best: dict[tuple, Finding] = {}
    for f in findings:
        key = (f.path, f.line, f.kind, f.old)
        if key not in best or f.confidence > best[key].confidence:
            best[key] = f
    return sorted(best.values(), key=lambda f: (f.path, f.line))
