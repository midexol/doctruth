"""Orchestration: diff → changes → doc chunks → rules (+ snippets, + judge) → findings."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Optional

from . import changes as changes_mod
from .docs import DEFAULT_IGNORE, chunks_for, is_doc_file, is_ignored
from .gitio import GitError, Snapshot, diff_files, merge_base, resolve
from .judge import Judge
from .models import Change, DocChunk, Finding
from .rules import RuleEngine
from .snippets import run_snippets
from .symbols import is_code_file

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomllib = None


@dataclass
class Options:
    base: Optional[str] = None
    head: Optional[str] = None  # None = working tree
    min_confidence: float = 0.75
    ignore: list[str] = field(default_factory=list)
    run_snippets: bool = False
    all_snippets: bool = False
    llm: bool = False
    include_comments: bool = True
    judge: Optional[Judge] = None


@dataclass
class Result:
    findings: list[Finding]
    changes: list[Change]
    base: str
    head: str
    docs_scanned: int
    notes: list[str] = field(default_factory=list)


def load_config(repo: str, opts: Options) -> Options:
    if tomllib is None:
        return opts
    cfg: dict = {}
    path = os.path.join(repo, ".stale.toml")
    pyproject = os.path.join(repo, "pyproject.toml")
    try:
        if os.path.exists(path):
            with open(path, "rb") as fh:
                cfg = tomllib.load(fh)
        elif os.path.exists(pyproject):
            with open(pyproject, "rb") as fh:
                cfg = tomllib.load(fh).get("tool", {}).get("stale", {})
    except (OSError, ValueError):
        return opts
    opts.ignore = list(cfg.get("ignore", [])) + opts.ignore
    opts.min_confidence = float(cfg.get("min_confidence", opts.min_confidence))
    opts.run_snippets = opts.run_snippets or bool(cfg.get("run_snippets", False))
    opts.include_comments = bool(cfg.get("include_comments", opts.include_comments))
    return opts


def guess_base(repo: str, head: Optional[str]) -> str:
    for cand in ("origin/main", "origin/master", "main", "master"):
        try:
            resolve(repo, cand)
            mb = merge_base(repo, cand, head)
            if mb != resolve(repo, head or "HEAD") or head is None:
                return mb
        except GitError:
            continue
    return resolve(repo, (head or "HEAD") + "~1")


def collect_chunks(snap: Snapshot, ignore: list[str], include_comments: bool) -> list[DocChunk]:
    chunks: list[DocChunk] = []
    patterns = DEFAULT_IGNORE + ignore
    for path in snap.files():
        if is_ignored(path, patterns):
            continue
        if not (is_doc_file(path) or (include_comments and is_code_file(path))):
            continue
        text = snap.read(path)
        if text is not None:
            chunks.extend(chunks_for(path, text))
    return chunks


def check(repo: str, opts: Options) -> Result:
    repo = os.path.abspath(repo)
    opts = load_config(repo, opts)
    base = opts.base or guess_base(repo, opts.head)
    base_sha = resolve(repo, base)
    base_snap, head_snap = Snapshot(repo, base_sha), Snapshot(repo, opts.head)
    files = diff_files(repo, base_sha, opts.head)
    cs = changes_mod.compute(base_snap, head_snap, files)
    chunks = collect_chunks(head_snap, opts.ignore, opts.include_comments)
    findings = RuleEngine(cs.changes).run(chunks)
    notes: list[str] = []

    if opts.run_snippets:
        root = head_snap.materialize()
        try:
            names = None if opts.all_snippets else {c.old for c in cs.changes if c.old} | {c.param for c in cs.changes if c.param}
            snippet_findings = run_snippets(chunks, root, names)
        finally:
            if root != repo and "stale-head-" in os.path.basename(root):
                shutil.rmtree(root, ignore_errors=True)
        # a broken example usually also has a rule finding on the same line: keep the one with a fix
        taken = {(f.path, f.line) for f in findings}
        findings += [f for f in snippet_findings if (f.path, f.line) not in taken]

    if opts.llm or opts.judge:
        judge = opts.judge or Judge()
        findings = judge.verify(findings, chunks, cs.changes)
        findings += judge.discover(cs.changes, chunks, findings)
        if judge.errors:
            notes.append(f"LLM judge unavailable for {len(judge.errors)} call(s): {judge.errors[0]}")

    findings = [f for f in findings if f.confidence >= opts.min_confidence]
    findings.sort(key=lambda f: (f.path, f.line))
    return Result(findings, cs.changes, base_sha[:10], (opts.head or "working tree"),
                  len({c.path for c in chunks}), notes)
