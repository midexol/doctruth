"""`stale` command line."""
from __future__ import annotations

import argparse
import sys

from . import __version__, report
from .engine import Options, check
from .gitio import GitError

FORMATS = {"text": report.text, "json": report.as_json, "markdown": report.markdown, "github": report.github}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="stale",
        description="Find docs, comments and examples that your code change just made untrue.",
    )
    p.add_argument("--version", action="version", version=f"stale {__version__}")
    sub = p.add_subparsers(dest="cmd")

    c = sub.add_parser("check", help="check a diff for stale docs (default command)")
    c.add_argument("--repo", default=".", help="path to the git repository (default: .)")
    c.add_argument("--base", help="base ref to diff from (default: merge-base with main, else HEAD~1)")
    c.add_argument("--head", help="head ref (default: your working tree, including uncommitted edits)")
    c.add_argument("--format", "-f", choices=sorted(FORMATS), default="text")
    c.add_argument("--min-confidence", type=float, default=None,
                   help="hide findings below this confidence, 0-1 (default 0.75)")
    c.add_argument("--ignore", action="append", default=[], metavar="GLOB", help="skip matching files")
    c.add_argument("--run-snippets", action="store_true",
                   help="execute Python examples in docs that mention changed code (runs doc code!)")
    c.add_argument("--all-snippets", action="store_true", help="with --run-snippets, run every Python example")
    c.add_argument("--llm", action="store_true",
                   help="use Claude to verify borderline hits and catch changed claims (needs ANTHROPIC_API_KEY)")
    c.add_argument("--no-comments", action="store_true", help="only scan doc files, not code comments")
    c.add_argument("--no-fail", action="store_true", help="always exit 0, even with findings")
    c.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version"):
        argv.insert(0, "check")
    args = build_parser().parse_args(argv)
    if args.cmd != "check":
        build_parser().print_help()
        return 2
    opts = Options(base=args.base, head=args.head, ignore=args.ignore, run_snippets=args.run_snippets,
                   all_snippets=args.all_snippets, llm=args.llm, include_comments=not args.no_comments)
    if args.min_confidence is not None:
        opts.min_confidence = args.min_confidence
    try:
        result = check(args.repo, opts)
    except GitError as exc:
        print(f"stale: {exc}", file=sys.stderr)
        return 2
    if args.format == "text":
        color = {"always": True, "never": False}.get(args.color)
        print(report.text(result.findings, result.changes, color))
    else:
        print(FORMATS[args.format](result.findings, result.changes))
    for note in result.notes:
        print(f"stale: note: {note}", file=sys.stderr)
    return 0 if args.no_fail or not result.findings else 1


if __name__ == "__main__":
    sys.exit(main())
