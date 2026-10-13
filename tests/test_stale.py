import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "demo"))
import build_demo  # noqa: E402

from stale import report  # noqa: E402
from stale.engine import Options, check  # noqa: E402
from stale.judge import Judge  # noqa: E402
from stale.symbols import extract  # noqa: E402


def git(root, *args):
    build_demo.git(root, *args)


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    return build_demo.build(str(tmp_path_factory.mktemp("wx")))


@pytest.fixture(scope="module")
def result(demo):
    return check(demo, Options(base="main", head="HEAD", run_snippets=True))


def hits(result, path, kind=None):
    return {(f.line, f.kind) for f in result.findings if f.path == path and (kind is None or f.kind == kind)}


def test_detects_semantic_changes(result):
    got = {(c.kind, c.old, c.new) for c in result.changes}
    assert ("renamed", "fetch_forecast", "get_forecast") in got
    assert ("renamed", "--json", "--output-json") in got
    assert ("removed", "--legacy-mode", None) in got
    assert ("renamed", "WEATHERLY_TOKEN", "WEATHERLY_API_KEY") in got
    assert ("removed", "normalize_city", None) in got
    assert ("file_removed", "weatherly/legacy_cache.py", None) in got
    defaults = {(c.param, c.old_value, c.new_value) for c in result.changes if c.kind == "default_changed"}
    assert defaults == {("attempts", "3", "5"), ("days", "3", "7")}
    assert any(c.kind == "param_removed" and c.param == "units" for c in result.changes)


def test_finds_every_planted_lie(result):
    readme = hits(result, "README.md")
    assert (3, "renamed") in readme                # `WEATHERLY_TOKEN`
    assert (11, "renamed") in readme               # client.fetch_forecast("Lagos") in a code block
    assert (15, "renamed") in readme and (15, "default_changed") in readme
    assert (21, "param_removed") in readme
    assert (26, "default_changed") in readme       # "retries a failed call three times"
    cli = hits(result, "docs/cli.md")
    assert {(3, "renamed"), (5, "removed"), (7, "renamed"), (10, "renamed"), (11, "renamed"), (14, "file_removed")} <= cli
    forecast = hits(result, "docs/forecast.md")
    assert (6, "snippet_failed") in forecast       # only an executed example can catch this one
    assert (9, "removed") in forecast


def test_respects_traps(result):
    paths = {f.path for f in result.findings}
    assert "CHANGELOG.md" not in paths            # history is allowed to mention old names
    assert "UPGRADING.md" not in paths            # line already names the new symbol
    assert (11, "renamed") not in hits(result, "docs/forecast.md")  # stale:ignore
    readme_lines = {f.line for f in result.findings if f.path == "README.md"}
    assert 27 not in readme_lines                 # "We retry because..." plain English, no claim
    assert all(f.path != "weatherly/format.py" for f in result.findings)


def test_suggestions_are_real_fixes(result):
    by = {(f.path, f.line, f.kind): f for f in result.findings}
    assert by[("README.md", 15, "default_changed")].suggestion.count("seven-day") == 1
    assert by[("README.md", 26, "default_changed")].suggestion.strip().endswith("five times before giving up.")
    assert by[("README.md", 21, "param_removed")].suggestion.strip() == 'print(get_current("Akure"))'
    assert by[("docs/cli.md", 3, "renamed")].suggestion.strip() == "Run `weatherly Lagos --output-json` to get machine-readable output."


def test_no_findings_when_docs_fixed(tmp_path):
    root = build_demo.build(str(tmp_path / "r"))
    for rel in ("README.md", "docs/cli.md", "docs/forecast.md"):
        os.remove(os.path.join(root, rel))
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "drop docs")
    res = check(root, Options(base="main", head="HEAD"))
    assert res.findings == []


def test_working_tree_mode_sees_uncommitted_changes(tmp_path):
    root = build_demo.build(str(tmp_path / "r"), commit_pr=False)
    res = check(root, Options(base="main"))  # head=None -> working tree
    assert any(f.kind == "renamed" and f.old == "fetch_forecast" for f in res.findings)


def test_plain_word_symbols_are_not_flagged(tmp_path):
    root = str(tmp_path / "r")
    build_demo.write(root, {"app.py": "def run(x):\n    return x\n", "README.md": "We run the tests nightly.\n\nCall `run()` to start.\n"})
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "one")
    build_demo.write(root, {"app.py": "def start(x):\n    return x\n"})
    git(root, "commit", "-qam", "two")
    res = check(root, Options(base="main~1", head="HEAD"))
    assert [(f.line, f.kind) for f in res.findings] == [(3, "renamed")]


def test_javascript_symbols():
    syms = extract("api.ts", "export async function fetchUser(id: string, retries = 3) { return id }\n"
                             "const parseOpts = (raw) => { return raw }\nexport class HttpClient {}\n"
                             "const k = process.env.API_KEY\n")
    names = {(s.kind, s.name) for s in syms}
    assert {("function", "fetchUser"), ("function", "parseOpts"), ("class", "HttpClient"), ("env", "API_KEY")} <= names
    f = next(s for s in syms if s.name == "fetchUser")
    assert [(p.name, p.default) for p in f.params] == [("id", None), ("retries", "3")]


def test_llm_judge_discovers_changed_claims_and_vetoes(tmp_path):
    root = str(tmp_path / "r")
    build_demo.write(root, {
        "lib.py": "def load_user(uid):\n    return None if uid < 0 else {'id': uid}\n",
        "README.md": "`load_user()` returns `None` when the user does not exist.\n",
    })
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "one")
    build_demo.write(root, {"lib.py": "def load_user(uid):\n    if uid < 0:\n        raise KeyError(uid)\n    return {'id': uid}\n"})
    git(root, "commit", "-qam", "raise instead")

    calls = []

    def fake(system, user):
        calls.append(user)
        return json.dumps({"verdict": "stale", "confidence": 0.86,
                           "claim": "`load_user()` returns `None` when the user does not exist.",
                           "fix": "`load_user()` raises `KeyError` when the user does not exist."})

    res = check(root, Options(base="main~1", head="HEAD", judge=Judge(call=fake)))
    assert len(calls) == 1 and "raise KeyError" in calls[0]
    [f] = res.findings
    assert f.kind == "claim_outdated" and f.source == "llm" and "KeyError" in f.suggestion

    res_ok = check(root, Options(base="main~1", head="HEAD",
                                 judge=Judge(call=lambda s, u: '{"verdict": "ok", "confidence": 0.9}')))
    assert res_ok.findings == []


def test_judge_failure_never_breaks_the_run(result, demo):
    def boom(system, user):
        raise ConnectionError("offline")
    res = check(demo, Options(base="main", head="HEAD", judge=Judge(call=boom)))
    assert len(res.findings) >= 13 and res.notes


def test_reporters(result):
    md = report.markdown(result.findings, result.changes)
    assert "```diff" in md and "stale:ignore" in md
    gh = report.github(result.findings, result.changes)
    assert "::error file=README.md,line=11" in gh
    data = json.loads(report.as_json(result.findings, result.changes))
    assert len(data["findings"]) == len(result.findings)


def test_cli_exit_codes(demo):
    run = lambda *a: subprocess.run([sys.executable, "-m", "stale", *a], cwd=demo, capture_output=True, text=True)
    assert run("--base", "main", "--head", "HEAD").returncode == 1
    assert run("--base", "main", "--head", "HEAD", "--no-fail").returncode == 0
    assert run("--base", "main", "--head", "main").returncode == 0
    assert run("--base", "nope").returncode == 2
