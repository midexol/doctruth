"""Turn a git diff into semantic changes: renames, removals, signature and default changes."""
from __future__ import annotations

import difflib
import posixpath
import re
from typing import Optional

from .gitio import FileChange, Snapshot
from .models import Change, Symbol
from .symbols import extract, is_code_file

CALLABLE = ("function", "method", "class")


def module_of(path: str) -> Optional[str]:
    if not path.endswith(".py"):
        return None
    mod = path[:-3].replace("/", ".")
    for prefix in ("src.", "lib."):
        if mod.startswith(prefix):
            mod = mod[len(prefix):]
    return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod


def _syms(snap: Snapshot, path: Optional[str]) -> list[Symbol]:
    if not path or not is_code_file(path):
        return []
    src = snap.read(path)
    return extract(path, src) if src is not None else []


def _similar(a: Symbol, b: Symbol) -> float:
    if not a.source or not b.source:
        return 0.0
    sa = re.sub(rf"\b{re.escape(a.name)}\b", "_", a.source)
    sb = re.sub(rf"\b{re.escape(b.name)}\b", "_", b.source)
    return difflib.SequenceMatcher(None, sa, sb).ratio()


class ChangeSet:
    def __init__(self, changes: list[Change], old_symbols: list[Symbol], new_symbols: list[Symbol]):
        self.changes = changes
        self.old_symbols = old_symbols
        self.new_symbols = new_symbols


def compute(base: Snapshot, head: Snapshot, files: list[FileChange]) -> ChangeSet:
    renamed_paths = {f.old_path: f.new_path for f in files if f.status == "R"}
    old_syms: list[Symbol] = []
    new_syms: list[Symbol] = []
    for f in files:
        old_syms += _syms(base, f.old_path)
        new_syms += _syms(head, f.new_path)

    # Everything defined anywhere at head: a symbol "removed" from one file but alive elsewhere isn't
    # gone. Parsing the whole tree is the expensive step, so only do it if something might be gone.
    def _bucket(s: Symbol) -> tuple[str, str]:
        return (s.kind if s.kind in ("flag", "env") else "callable", s.name)

    maybe_gone = {_bucket(s) for s in old_syms} - {_bucket(s) for s in new_syms}
    head_names: set[tuple[str, str]] = set()
    if maybe_gone:
        changed_new = {f.new_path for f in files if f.new_path}
        head_names = {_bucket(s) for s in new_syms}
        for p in head.files():
            if p not in changed_new and is_code_file(p):
                src = head.read(p)
                if src and any(name in src for _, name in maybe_gone):  # cheap text filter before parsing
                    head_names |= {_bucket(s) for s in extract(p, src)}

    changes: list[Change] = []

    # --------------------------------------------------- callables
    def eff_path(p: str) -> str:
        return renamed_paths.get(p, p)

    old_c = {(s.kind, eff_path(s.path), s.qualname): s for s in old_syms if s.kind in CALLABLE}
    new_c = {(s.kind, s.path, s.qualname): s for s in new_syms if s.kind in CALLABLE}
    removed = [s for k, s in old_c.items() if k not in new_c]
    added = [s for k, s in new_c.items() if k not in old_c]
    pairs: list[tuple[Symbol, Symbol]] = [(old_c[k], new_c[k]) for k in old_c if k in new_c]
    used: set[int] = set()

    for r in removed:
        match: Optional[Symbol] = None
        # 1) identical body anywhere -> rename or move
        for i, a in enumerate(added):
            if i not in used and a.kind == r.kind and r.body_hash and a.body_hash == r.body_hash:
                match = a
                used.add(i)
                break
        # 2) same file, same params, very similar body -> rename with edits
        if match is None:
            best, best_i = 0.0, -1
            for i, a in enumerate(added):
                if i in used or a.kind != r.kind or a.path != eff_path(r.path):
                    continue
                same_params = [p.name for p in a.params] == [p.name for p in r.params]
                score = _similar(r, a) + (0.1 if same_params else 0.0)
                if score > best:
                    best, best_i = score, i
            if best >= 0.8:
                match = added[best_i]
                used.add(best_i)
        if match is not None:
            if match.name != r.name:
                changes.append(Change("renamed", r.kind, r.name, match.name, match.path, match.line,
                                      old_source=r.source, new_source=match.source))
            elif module_of(r.path) and module_of(match.path) and module_of(r.path) != module_of(match.path):
                changes.append(Change("moved", r.kind, module_of(r.path), module_of(match.path), match.path,
                                      match.line, param=r.name))
            pairs.append((r, match))
        elif ("callable", r.name) not in head_names:
            changes.append(Change("removed", r.kind, r.name, None, r.path, r.line, old_source=r.source))

    for old, new in pairs:
        changes.extend(_signature_changes(old, new))

    # --------------------------------------------------- flags & env vars
    for kind in ("flag", "env"):
        by_file_old: dict[str, set[str]] = {}
        by_file_new: dict[str, set[str]] = {}
        for s in old_syms:
            if s.kind == kind:
                by_file_old.setdefault(eff_path(s.path), set()).add(s.name)
        for s in new_syms:
            if s.kind == kind:
                by_file_new.setdefault(s.path, set()).add(s.name)
        for path, olds in by_file_old.items():
            news = by_file_new.get(path, set())
            gone = sorted(n for n in olds - news if (kind, n) not in head_names)
            fresh = sorted(news - olds)
            for old_name, new_name in _pair_names(gone, fresh):
                if new_name:
                    changes.append(Change("renamed", kind, old_name, new_name, path))
                else:
                    changes.append(Change("removed", kind, old_name, None, path))

    # --------------------------------------------------- files
    head_basenames = {posixpath.basename(p) for p in head.files()}
    for f in files:
        if f.status == "D" and f.old_path:
            changes.append(Change("file_removed", "file", f.old_path, None, f.old_path,
                                  param=posixpath.basename(f.old_path) if posixpath.basename(f.old_path) not in head_basenames else None))
        elif f.status == "R" and f.old_path and f.new_path:
            changes.append(Change("file_renamed", "file", f.old_path, f.new_path, f.new_path))
            om, nm = module_of(f.old_path), module_of(f.new_path)
            if om and nm and om != nm:
                changes.append(Change("moved", "module", om, nm, f.new_path))

    return ChangeSet(changes, old_syms, new_syms)


def _name_similarity(a: str, b: str) -> float:
    ta = set(re.split(r"[-_]+", a.strip("-").lower())) - {""}
    tb = set(re.split(r"[-_]+", b.strip("-").lower())) - {""}
    overlap = len(ta & tb) / max(1, min(len(ta), len(tb)))
    return max(overlap, difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio())


def _pair_names(gone: list[str], fresh: list[str]) -> list[tuple[str, Optional[str]]]:
    """Pair removed names with added ones that look like renames (--json -> --output-json)."""
    if len(gone) == 1 and len(fresh) == 1:
        return [(gone[0], fresh[0])]
    scored = sorted(((_name_similarity(g, f), g, f) for g in gone for f in fresh), reverse=True)
    paired: dict[str, str] = {}
    used: set[str] = set()
    for score, g, f in scored:
        if score >= 0.6 and g not in paired and f not in used:
            paired[g] = f
            used.add(f)
    return [(g, paired.get(g)) for g in gone]


def _signature_changes(old: Symbol, new: Symbol) -> list[Change]:
    out: list[Change] = []
    label = new.name
    if old.kind not in ("function", "method"):
        if old.body_hash != new.body_hash:
            pass  # class bodies change constantly; methods are tracked individually
        return out
    old_p = {p.name: p for p in old.params}
    new_p = {p.name: p for p in new.params}
    gone = [p for p in old.params if p.name not in new_p]
    fresh = [p for p in new.params if p.name not in old_p]
    if len(gone) == 1 and len(fresh) == 1 and len(old.params) == len(new.params):
        out.append(Change("param_renamed", new.kind, label, label, new.path, new.line,
                          param=gone[0].name, old_value=gone[0].name, new_value=fresh[0].name))
    else:
        for p in gone:
            if p.name.startswith("*"):
                continue
            out.append(Change("param_removed", new.kind, label, label, new.path, new.line, param=p.name))
    for name, op in old_p.items():
        np_ = new_p.get(name)
        if np_ and op.default is not None and np_.default is not None and op.default != np_.default:
            out.append(Change("default_changed", new.kind, label, label, new.path, new.line, param=name,
                              old_value=op.default, new_value=np_.default))
    if old.body_hash != new.body_hash:
        out.append(Change("behaviour_changed", new.kind, label, label, new.path, new.line,
                          old_source=old.source, new_source=new.source))
    return out
