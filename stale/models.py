"""Core data types shared across stale's pipeline: symbols, changes, doc chunks, findings."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class Param:
    name: str
    default: Optional[str] = None  # source repr of the default, None if no default


@dataclass
class Symbol:
    """Something code defines that prose can refer to."""

    kind: str  # function | method | class | flag | env
    name: str  # short name, e.g. "fetch_user", "--verbose", "API_TOKEN"
    qualname: str  # e.g. "Client.fetch_user"
    path: str
    line: int
    params: list[Param] = field(default_factory=list)
    body_hash: Optional[str] = None  # name-independent fingerprint of the body
    source: Optional[str] = None  # full source segment, used by the LLM judge

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.kind, self.path, self.qualname)


@dataclass
class Change:
    """A semantic change between base and head that can make prose false."""

    kind: str  # removed | renamed | moved | param_removed | param_renamed | default_changed | behaviour_changed | file_removed | file_renamed
    symbol_kind: str
    old: str  # the name prose would use for the old thing
    new: Optional[str] = None
    path: str = ""
    line: int = 0
    param: Optional[str] = None
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    old_source: Optional[str] = None
    new_source: Optional[str] = None

    def describe(self) -> str:
        k = self.kind
        if k == "renamed":
            return f"`{self.old}` was renamed to `{self.new}`"
        if k == "moved":
            return f"`{self.old}` moved to `{self.new}`"
        if k == "removed":
            return f"`{self.old}` was removed"
        if k == "param_removed":
            return f"parameter `{self.param}` was removed from `{self.old}`"
        if k == "param_renamed":
            return f"parameter `{self.param}` of `{self.old}` is now `{self.new_value}`"
        if k == "default_changed":
            return f"default of `{self.param}` in `{self.old}` changed from {self.old_value} to {self.new_value}"
        if k == "behaviour_changed":
            return f"the body of `{self.old}` changed"
        if k == "file_removed":
            return f"file `{self.old}` was deleted"
        if k == "file_renamed":
            return f"file `{self.old}` was renamed to `{self.new}`"
        return f"{k}: {self.old}"


@dataclass
class DocChunk:
    """A unit of prose: a paragraph, a fenced code block, a comment block or a docstring."""

    path: str
    line: int  # 1-based line of the first line of the chunk
    text: str
    context: str  # prose | code | comment | docstring
    lang: Optional[str] = None
    ignored_lines: set[int] = field(default_factory=set)  # absolute line numbers marked stale:ignore

    def line_of(self, offset: int) -> int:
        return self.line + self.text.count("\n", 0, offset)

    def line_text(self, abs_line: int) -> str:
        lines = self.text.split("\n")
        idx = abs_line - self.line
        return lines[idx] if 0 <= idx < len(lines) else ""


@dataclass
class Finding:
    path: str
    line: int
    kind: str
    message: str
    confidence: float
    severity: str = "warning"  # warning | error
    old: Optional[str] = None
    new: Optional[str] = None
    original_line: Optional[str] = None
    suggestion: Optional[str] = None  # full replacement line, if we can propose one
    evidence: Optional[str] = None  # where in the diff this came from
    source: str = "rules"  # rules | snippet | llm

    def to_dict(self) -> dict:
        return asdict(self)
