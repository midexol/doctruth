# doctruth

**A linter for documentation that your code change just made untrue.**

You rename a function, drop a flag, or bump a default. Tests pass, the PR merges, and the README keeps
confidently describing code that no longer exists. `doctruth` reads your diff, works out what changed
*semantically*, and points at every doc line, comment, docstring and example that is now false, with a fix.

```
$ doctruth --run-snippets
doctruth · 9 change(s) in this diff that docs could depend on
  · `fetch_forecast` was renamed to `get_forecast`
  · default of `days` in `get_forecast` changed from 3 to 7
  · `--json` was renamed to `--output-json`
  ...

README.md
  ✗ README.md:11  [renamed] References `fetch_forecast`, which was renamed to `get_forecast`. (95%, rules)
      - forecast = client.fetch_forecast("Lagos")
      + forecast = client.get_forecast("Lagos")
  ! README.md:26  [default changed] Says `attempts` is three, but the default of `retry` changed from 3 to 5. (80%, rules)
      - Network calls go through `retry()`, which retries a failed call three times before giving up.
      + Network calls go through `retry()`, which retries a failed call five times before giving up.

docs/forecast.md
  ✗ docs/forecast.md:6  [broken example] This example no longer runs: AssertionError: expected 3 days, got 7 (100%, snippet)

15 stale reference(s) in 3 file(s), 5 in code examples.
```

## Try it in 30 seconds

```bash
pip install -e .
python demo/build_demo.py /tmp/weatherly     # a repo with a PR that quietly breaks its docs
cd /tmp/weatherly && doctruth --run-snippets
```

The demo plants 15 lies and several traps (a CHANGELOG, an upgrade note, plain-English words, a
`doctruth:ignore` line). `doctruth` catches every lie and springs no traps.

## What it catches

| Change in the diff | Example of what gets flagged |
|---|---|
| Function, method or class renamed (detected by body fingerprint, not just name) | `` `Client.fetch_forecast()` returns… `` |
| Symbol removed (and not defined anywhere else) | `` cleaned up by `normalize_city()` `` |
| Parameter removed or renamed | `get_current("Akure", units="metric")` |
| Default value changed, including numbers written as words | "retries a failed call **three** times" |
| CLI flag removed or renamed (argparse, click, commander) | `weatherly Lagos --json` |
| Environment variable removed or renamed | `export WEATHERLY_TOKEN=…` |
| File or module deleted, renamed or moved | `` live in `weatherly/legacy_cache.py` `` |
| An example in the docs no longer runs (`--run-snippets`) | `assert len(days) == 3` → AssertionError |
| A described *behaviour* is no longer true (`--llm`) | "returns `None` when the user does not exist" |

It scans Markdown, MDX, reStructuredText and plain-text docs, plus comments and docstrings in Python and
JS/TS. Code is understood with Python's `ast` and careful JS/TS patterns.

## The design rule: precision over recall

A docs bot that cries wolf gets muted in a week. So `doctruth` only counts a mention when it is unambiguous:
inside a code block or `` `inline code` ``, called like `name()`, accessed as `.name`, or shaped like an
identifier (`snake_case`, `camelCase`, `CONSTANT_CASE`, `--flag`). Rename a function called `run` and
"we run the tests nightly" is left alone.

On top of that:
- **History is allowed to mention old names.** CHANGELOG/HISTORY/NEWS files are skipped.
- **Upgrade notes are respected.** A line that already names the new symbol isn't flagged.
- **Every finding has a confidence**, and anything below `--min-confidence` (0.75) is hidden.
- **Opt out per line.** `doctruth:ignore` on a line (or the line above it) silences it; `doctruth:ignore-file` silences a file.

Measured on the real thing: swept across the last **300 commits of [psf/requests](https://github.com/psf/requests)**,
it produced **1 finding**, a quickstart traceback still pointing at `requests/models.py` after the move to `src/`.

## Usage

```bash
doctruth                                # working tree vs. merge-base with main (includes uncommitted edits)
doctruth --base origin/main --head HEAD # a specific range
doctruth --run-snippets                 # also execute Python examples that mention changed code
doctruth --llm                          # Claude verifies borderline hits and checks described behaviour
doctruth -f markdown                    # PR comment with diff suggestions
doctruth -f github                      # inline annotations in GitHub Actions
doctruth -f json                        # for your own tooling
```

Exit codes: `0` clean, `1` stale docs found (unless `--no-fail`), `2` usage or git error.

**Config.** Put this in `.doctruth.toml` or under `[tool.doctruth]` in `pyproject.toml`:

```toml
ignore = ["docs/archive/**"]
min_confidence = 0.8
run_snippets = true
include_comments = true
```

### Executable docs

`--run-snippets` runs fenced `python` and `pycon` blocks that mention something the diff changed (or
every block with `--all-snippets`), with the repo on `PYTHONPATH`. **This executes code from your
docs**, which is why it's opt-in. Put `<!-- doctruth:skip -->` above a block to exclude it.

### The LLM judge

Rules catch names; the judge catches claims. With `--llm` and `ANTHROPIC_API_KEY` set, it does two narrow jobs:

1. **Verify.** Borderline rule hits (55–90% confidence) are re-checked. A judge "ok" drops the finding.
2. **Discover.** For functions whose body changed, prose describing them is checked with one question: *was
   this true before the change and false after it?*

It only sees the old code, the new code and one passage. It never edits anything, and if the API is down
the run continues with rules alone. Set the model with `DOCTRUTH_MODEL` (default `claude-sonnet-5-5`).

## In CI

```yaml
# .github/workflows/doctruth.yml
on: pull_request
jobs:
  doctruth:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: midexol/doctruth@v0
        with:
          args: --run-snippets
```

Findings show up as annotations on the PR's *Files changed* tab, and a summary with suggested fixes is
added to the job summary.

## How it works

```
git diff ──► changes.py   semantic changes: rename (by AST fingerprint), removal, signature, default, flag, env, file
               │
docs at HEAD ► docs.py    chunks: paragraphs, code blocks, comments, docstrings
               │
               ▼
            rules.py      precise reference matching + suggested fix for each line
            snippets.py   run the examples (opt-in)
            judge.py      Claude checks borderline hits and behavioural claims (opt-in)
               │
               ▼
            report.py     text · json · markdown PR comment · GitHub annotations
```

```
doctruth/
  gitio.py      read any ref or the working tree, list changed files
  symbols.py    functions/classes/methods/params (Python ast, JS/TS), CLI flags, env vars
  changes.py    turn a diff into semantic changes
  docs.py       chunk Markdown/RST/comments/docstrings; ignore rules
  rules.py      the matcher: reference strength, per-change handlers, fixes
  snippets.py   executable docs
  judge.py      the optional LLM judge
  engine.py     the pipeline + config
  report.py     output formats
  cli.py        the `doctruth` command
demo/           a repo generator with planted drift
tests/          end-to-end tests on the demo, plus traps, JS, judge (with a fake model), CLI
```

## Roadmap

- **A GitHub App** that posts review comments with one-click `suggestion` blocks, and learns per-repo
  thresholds from 👍/👎 and dismissed findings.
- **More languages**: Go and Rust via Tree-sitter.
- **Docs outside the repo**: Notion, Confluence and API reference sites.
- **Semantic linking**: embeddings to find prose that describes behaviour without naming the symbol.
- **An eval set mined from git history**: commits that changed code and fixed docs later are free labelled drift.

## License

MIT
