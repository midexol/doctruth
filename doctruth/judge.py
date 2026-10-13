"""Optional LLM judge.

Rules catch names. The judge catches *claims*: "retries with exponential backoff", "returns
None when the user is missing". It does two jobs, both narrow on purpose:

  verify   – re-check borderline rule findings, dropping ones that aren't really stale
  discover – for functions whose body changed, ask whether prose describing them is still true

It only ever sees: old code, new code, one passage. One question: is this passage still true?
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Callable, Optional

from .models import Change, DocChunk, Finding
from .rules import _refs

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = os.environ.get("DOCTRUTH_MODEL", "claude-sonnet-5-5")

SYSTEM = """You audit documentation for drift. You get a code change (before and after) and one \
passage of documentation. Decide whether the passage makes a claim that was true before the change \
and is FALSE after it. Be strict: only answer "stale" when a specific statement in the passage is \
now wrong. Vague, still-true, or unrelated passages are "ok". Reply with JSON only:
{"verdict": "stale"|"ok", "confidence": 0.0-1.0, "claim": "<the false statement, verbatim>", \
"fix": "<the passage line rewritten to be true, or empty>"}"""

CallFn = Callable[[str, str], str]


def anthropic_call(system: str, user: str) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    body = json.dumps({
        "model": DEFAULT_MODEL, "max_tokens": 600, "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(API_URL, data=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def _parse(raw: str) -> Optional[dict]:
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        out = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return out if out.get("verdict") in ("stale", "ok") else None


class Judge:
    def __init__(self, call: CallFn = anthropic_call, max_calls: int = 40):
        self.call = call
        self.max_calls = max_calls
        self.calls = 0
        self.errors: list[str] = []

    def _ask(self, change: Change, passage: str, path: str) -> Optional[dict]:
        if self.calls >= self.max_calls:
            return None
        self.calls += 1
        user = (f"CHANGE: {change.describe()}\n\nBEFORE:\n```\n{(change.old_source or '(not available)')[:4000]}\n```\n\n"
                f"AFTER:\n```\n{(change.new_source or '(deleted)')[:4000]}\n```\n\n"
                f"DOCUMENTATION ({path}):\n\"\"\"\n{passage[:3000]}\n\"\"\"")
        try:
            return _parse(self.call(SYSTEM, user))
        except Exception as exc:  # network, auth, rate limit: never fail the run because of the judge
            self.errors.append(str(exc))
            return None

    def verify(self, findings: list[Finding], chunks: list[DocChunk], changes: list[Change],
               band: tuple[float, float] = (0.55, 0.9)) -> list[Finding]:
        by_old = {c.old: c for c in changes}
        kept = []
        for f in findings:
            if f.source != "rules" or not (band[0] <= f.confidence < band[1]) or f.old not in by_old:
                kept.append(f)
                continue
            chunk = next((c for c in chunks if c.path == f.path and c.line <= f.line < c.line + c.text.count("\n") + 1), None)
            verdict = self._ask(by_old[f.old], chunk.text if chunk else f.original_line or "", f.path)
            if verdict is None:
                kept.append(f)
            elif verdict["verdict"] == "stale":
                f.confidence = round(max(f.confidence, float(verdict.get("confidence", f.confidence))), 2)
                f.source = "rules+llm"
                kept.append(f)
            # verdict ok -> dropped: the judge vetoed a borderline rule hit
        return kept

    def discover(self, changes: list[Change], chunks: list[DocChunk], already: list[Finding]) -> list[Finding]:
        flagged = {(f.path, f.line) for f in already}
        out: list[Finding] = []
        for change in changes:
            if change.kind != "behaviour_changed":
                continue
            for chunk in chunks:
                if chunk.context == "code":
                    continue
                refs = [(o, s) for o, s in _refs(chunk, change.old, change.symbol_kind) if s >= 0.8]
                if not refs:
                    continue
                first = chunk.line_of(refs[0][0])
                if any((chunk.path, chunk.line_of(o)) in flagged for o, _ in refs):
                    continue
                verdict = self._ask(change, chunk.text, chunk.path)
                if not verdict or verdict["verdict"] != "stale":
                    continue
                claim = (verdict.get("claim") or "").strip()
                line = first
                if claim:
                    for i, l in enumerate(chunk.text.split("\n")):
                        if claim[:40] and claim[:40] in l:
                            line = chunk.line + i
                            break
                out.append(Finding(
                    path=chunk.path, line=line, kind="claim_outdated", source="llm",
                    message=f"`{change.old}` changed, and this no longer holds: \"{claim[:160]}\"" if claim
                    else f"`{change.old}` changed and this description may no longer hold.",
                    confidence=round(float(verdict.get("confidence", 0.7)), 2), old=change.old,
                    original_line=chunk.line_text(line), suggestion=(verdict.get("fix") or None),
                    evidence=f"{change.path}:{change.line}",
                ))
        return out
