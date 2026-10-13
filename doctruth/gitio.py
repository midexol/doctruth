"""Thin git layer. Reads file contents at a ref, or from the working tree when head is None."""
from __future__ import annotations

import io
import os
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from typing import Optional


class GitError(RuntimeError):
    pass


def _git(repo: str, *args: str, binary: bool = False):
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=not binary)
    if proc.returncode != 0:
        err = proc.stderr if not binary else proc.stderr.decode(errors="replace")
        raise GitError(f"git {' '.join(args)} failed: {err.strip()}")
    return proc.stdout


@dataclass
class FileChange:
    status: str  # A, M, D, R
    old_path: Optional[str]
    new_path: Optional[str]


class Snapshot:
    """A readable view of the repo at one ref (or the working tree)."""

    def __init__(self, repo: str, ref: Optional[str]):
        self.repo = repo
        self.ref = ref  # None == working tree
        self._cache: dict[str, Optional[str]] = {}
        self._files: Optional[list[str]] = None

    def files(self) -> list[str]:
        if self._files is None:
            if self.ref is None:
                out = _git(self.repo, "ls-files", "--cached", "--others", "--exclude-standard")
                self._files = [f for f in out.splitlines() if os.path.isfile(os.path.join(self.repo, f))]
            else:
                out = _git(self.repo, "ls-tree", "-r", "--name-only", self.ref)
                self._files = out.splitlines()
        return self._files

    def read(self, path: str) -> Optional[str]:
        if path in self._cache:
            return self._cache[path]
        content: Optional[str]
        try:
            if self.ref is None:
                with open(os.path.join(self.repo, path), encoding="utf-8") as fh:
                    content = fh.read()
            else:
                raw = _git(self.repo, "show", f"{self.ref}:{path}", binary=True)
                content = raw.decode("utf-8")
        except (GitError, OSError, UnicodeDecodeError):
            content = None
        self._cache[path] = content
        return content

    def materialize(self) -> str:
        """Return a directory containing this snapshot's files (for running snippets)."""
        if self.ref is None:
            return os.path.abspath(self.repo)
        target = tempfile.mkdtemp(prefix="doctruth-head-")
        data = _git(self.repo, "archive", "--format=tar", self.ref, binary=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            try:
                tar.extractall(target, filter="data")
            except TypeError:  # Python without tarfile extraction filters
                tar.extractall(target)
        return target


def resolve(repo: str, ref: str) -> str:
    return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def merge_base(repo: str, a: str, b: Optional[str]) -> str:
    return _git(repo, "merge-base", a, b or "HEAD").strip()


def diff_files(repo: str, base: str, head: Optional[str]) -> list[FileChange]:
    args = ["diff", "--name-status", "-M", "--no-color", base]
    if head is not None:
        args.append(head)
    out = _git(repo, *args)
    changes: list[FileChange] = []
    for line in out.splitlines():
        parts = line.split("\t")
        status = parts[0][0]
        if status == "R":
            changes.append(FileChange("R", parts[1], parts[2]))
        elif status == "D":
            changes.append(FileChange("D", parts[1], None))
        elif status == "A":
            changes.append(FileChange("A", None, parts[1]))
        else:
            changes.append(FileChange("M", parts[1], parts[1]))
    if head is None:  # untracked files are additions too
        for f in _git(repo, "ls-files", "--others", "--exclude-standard").splitlines():
            changes.append(FileChange("A", None, f))
    return changes
