"""Output formats: human text, JSON, a Markdown PR comment, GitHub Actions annotations."""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

from .models import Change, Finding

LABELS = {
    "renamed": "renamed", "removed": "gone", "moved": "moved", "param_removed": "param gone",
    "param_renamed": "param renamed", "default_changed": "default changed", "file_removed": "file gone",
    "file_renamed": "file moved", "snippet_failed": "broken example", "claim_outdated": "claim outdated",
}


def _c(code: str, s: str, on: bool) -> str:
    return f"\033[{code}m{s}\033[0m" if on else s


def text(findings: list[Finding], changes: list[Change], color: bool | None = None) -> str:
    on = sys.stdout.isatty() and not os.environ.get("NO_COLOR") if color is None else color
    meaningful = [c for c in changes if c.kind != "behaviour_changed"]
    out = []
    if meaningful:
        out.append(_c("1", f"doctruth · {len(meaningful)} change(s) in this diff that docs could depend on", on))
        for c in meaningful:
            out.append(_c("2", f"  · {c.describe()}", on))
        out.append("")
    if not findings:
        out.append(_c("32", "✓ No stale docs found. Your docs and code agree.", on))
        return "\n".join(out)
    by_file: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        by_file[f.path].append(f)
    for path, fs in by_file.items():
        out.append(_c("1;4", path, on))
        for f in fs:
            tag = LABELS.get(f.kind, f.kind)
            sev = _c("31", "✗", on) if f.severity == "error" else _c("33", "!", on)
            out.append(f"  {sev} {path}:{f.line}  {_c('36', f'[{tag}]', on)} {f.message} "
                       + _c("2", f"({int(f.confidence * 100)}%, {f.source})", on))
            if f.original_line is not None:
                out.append(_c("31", f"      - {f.original_line.strip()}", on))
            if f.suggestion:
                out.append(_c("32", f"      + {f.suggestion.strip()}", on))
        out.append("")
    errors = sum(f.severity == "error" for f in findings)
    out.append(_c("1", f"{len(findings)} stale reference(s) in {len(by_file)} file(s)"
                     + (f", {errors} in code examples" if errors else "") + ".", on))
    return "\n".join(out)


def as_json(findings: list[Finding], changes: list[Change]) -> str:
    return json.dumps({
        "changes": [{"kind": c.kind, "description": c.describe(), "path": c.path, "line": c.line}
                    for c in changes if c.kind != "behaviour_changed"],
        "findings": [f.to_dict() for f in findings],
    }, indent=2)


def markdown(findings: list[Finding], changes: list[Change]) -> str:
    """A PR comment. Each finding with a fix gets a GitHub ```suggestion block."""
    if not findings:
        return "### 🧹 doctruth\nNo stale docs in this PR. Docs and code agree."
    by_file: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        by_file[f.path].append(f)
    lines = [f"### 🧹 doctruth found {len(findings)} doc reference(s) this PR made untrue", ""]
    for path, fs in by_file.items():
        lines.append(f"**`{path}`**")
        lines.append("")
        for f in fs:
            lines.append(f"- **L{f.line}** · _{LABELS.get(f.kind, f.kind)}_ · {f.message} "
                         f"<sub>{int(f.confidence * 100)}% · {f.source}</sub>")
            if f.suggestion:
                lines += ["", "  ```diff", f"  - {f.original_line}", f"  + {f.suggestion}", "  ```"]
        lines.append("")
    lines.append("<sub>Wrong call? Add `doctruth:ignore` on or above the line, and it won't be flagged again.</sub>")
    return "\n".join(lines)


def github(findings: list[Finding], changes: list[Change]) -> str:
    """GitHub Actions workflow commands: inline annotations on the PR's Files tab."""
    out = []
    for f in findings:
        level = "error" if f.severity == "error" else "warning"
        msg = f.message + (f"%0ASuggested: {f.suggestion.strip()}" if f.suggestion else "")
        msg = msg.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A").replace("%250A", "%0A")
        out.append(f"::{level} file={f.path},line={f.line},title=doctruth: {LABELS.get(f.kind, f.kind)}::{msg}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(markdown(findings, changes) + "\n")
    return "\n".join(out) if out else "doctruth: no stale docs found"
