"""Executable docs: run the Python examples in your Markdown and report the ones that break.

This executes code from your docs, so it is opt-in (`--run-snippets`). Mark a block that
shouldn't run with `<!-- doctruth:skip -->` on the line above it.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

from .models import DocChunk, Finding

RUNNABLE = {"python", "py", "python3", "pycon"}


def runnable(chunk: DocChunk) -> bool:
    if chunk.context != "code" or not chunk.lang or chunk.lang.endswith(":skip"):
        return False
    if chunk.lang not in RUNNABLE:
        return False
    text = chunk.text.strip()
    return bool(text) and "..." not in text.replace("print(...)", "")


def _pycon_to_script(text: str) -> str:
    lines = []
    for l in text.split("\n"):
        if l.startswith(">>> ") or l.startswith("... "):
            lines.append(l[4:])
        elif l in (">>>", "..."):
            lines.append("")
    return "\n".join(lines)


def run_snippets(chunks: list[DocChunk], root: str, only_names: set[str] | None = None,
                 timeout: float = 15.0) -> list[Finding]:
    findings: list[Finding] = []
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (root, os.path.join(root, "src"), env.get("PYTHONPATH", "")) if p)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for chunk in chunks:
        if not runnable(chunk):
            continue
        if only_names is not None and not any(n in chunk.text for n in only_names):
            continue
        code = _pycon_to_script(chunk.text) if chunk.lang == "pycon" else chunk.text
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write(code)
            script = fh.name
        try:
            proc = subprocess.run([sys.executable, script], cwd=root, env=env, capture_output=True,
                                  text=True, timeout=timeout)
            ok, err = proc.returncode == 0, proc.stderr
        except subprocess.TimeoutExpired:
            ok, err = False, f"timed out after {timeout:.0f}s"
        finally:
            os.unlink(script)
        if ok:
            continue
        line, last = chunk.line, (err.strip().splitlines() or ["failed"])[-1]
        for tl in err.splitlines():  # map the traceback line back into the markdown file
            if script in tl and "line " in tl:
                try:
                    line = chunk.line + int(tl.split("line ")[1].split(",")[0]) - 1
                except ValueError:
                    pass
        findings.append(Finding(
            path=chunk.path, line=line, kind="snippet_failed", source="snippet", severity="error",
            message=f"This example no longer runs: {last}", confidence=1.0,
            original_line=chunk.line_text(line),
        ))
    return findings
