"""Build a tiny git repo ("weatherly") with a realistic PR that quietly makes its docs lie.

    python demo/build_demo.py /tmp/weatherly
    cd /tmp/weatherly && stale --run-snippets

Commit 1 ships code + docs that agree. Commit 2 (the "PR") changes code only:
  - renames Client.fetch_forecast -> Client.get_forecast
  - changes the default of `days` from 3 to 7
  - removes the `units` parameter from get_current()
  - changes retry()'s default attempts from 3 to 5
  - removes the --legacy-mode flag and renames --json to --output-json
  - renames env var WEATHERLY_TOKEN -> WEATHERLY_API_KEY
  - deletes weatherly/legacy_cache.py and removes normalize_city()
Docs are untouched, so they now lie in a dozen places. Some lines are deliberate traps that
must NOT be flagged (CHANGELOG, an upgrade note, plain English words, a stale:ignore line).
"""
import os
import subprocess
import sys
import textwrap

V1 = {
    "weatherly/__init__.py": """
        from .client import Client, get_current, retry
        from .format import format_temp
        """,
    "weatherly/client.py": '''
        import os


        def retry(fn, attempts=3):
            """Call fn, retrying on failure."""
            last = None
            for _ in range(attempts):
                try:
                    return fn()
                except Exception as exc:  # noqa: BLE001
                    last = exc
            raise last


        def normalize_city(name):
            return name.strip().title()


        def get_current(city, units="metric"):
            temp = 21.5 if units == "metric" else 70.7
            return {"city": city, "temp": temp}


        class Client:
            def __init__(self, token=None):
                self.token = token or os.environ.get("WEATHERLY_TOKEN", "demo")

            def fetch_forecast(self, city, days=3):
                # One entry per day, starting today.
                return [{"city": city, "day": i, "temp": 20 + i} for i in range(days)]
        ''',
    "weatherly/format.py": '''
        def format_temp(value, unit="C"):
            return f"{value:.1f}°{unit}"
        ''',
    "weatherly/legacy_cache.py": '''
        CACHE = {}
        ''',
    "weatherly/cli.py": '''
        import argparse
        import json

        from .client import Client


        def main(argv=None):
            parser = argparse.ArgumentParser(prog="weatherly")
            parser.add_argument("city")
            parser.add_argument("--json", action="store_true", help="print raw JSON")
            parser.add_argument("--legacy-mode", action="store_true", help="use the v1 API")
            args = parser.parse_args(argv)
            data = Client().fetch_forecast(args.city)
            print(json.dumps(data) if args.json else data)
        ''',
    "README.md": '''
        # weatherly

        A tiny weather client. Set `WEATHERLY_TOKEN` before using it.

        ## Quickstart

        ```python
        from weatherly import Client

        client = Client()
        forecast = client.fetch_forecast("Lagos")
        print(forecast)
        ```

        `Client.fetch_forecast()` returns a three-day forecast by default; pass `days` for more.

        Current conditions come from `get_current(city, units="metric")`:

        ```python
        from weatherly import get_current
        print(get_current("Akure", units="metric"))
        ```

        ## Reliability

        Network calls go through `retry()`, which retries a failed call three times before giving up.
        We retry because the upstream API is flaky. The `Client` class is safe to share between threads.

        ## Formatting

        ```python
        from weatherly import format_temp
        assert format_temp(21.456) == "21.5°C"
        ```
        ''',
    "docs/cli.md": '''
        # Command line

        Run `weatherly Lagos --json` to get machine-readable output.

        For accounts created before 2023, add --legacy-mode.

        In CI, export WEATHERLY_TOKEN first:

        ```bash
        export WEATHERLY_TOKEN=xxxx
        weatherly Akure --json
        ```

        Cached responses live in `weatherly/legacy_cache.py`.
        ''',
    "docs/forecast.md": '''
        # Forecasts

        ```python
        from weatherly import Client
        days = Client().get_forecast("Lagos") if hasattr(Client, "get_forecast") else Client().fetch_forecast("Lagos")
        assert len(days) == 3, f"expected 3 days, got {len(days)}"
        ```

        City names are cleaned up by `normalize_city()` before lookup.
        <!-- stale:ignore -->
        Historical note: early builds called `fetch_forecast` from a cron job.
        ''',
    "UPGRADING.md": '''
        # Upgrading to 2.0

        - `fetch_forecast` was renamed to `get_forecast`.
        ''',
    "CHANGELOG.md": '''
        # Changelog

        ## 1.0
        - Added `Client.fetch_forecast()` and the `--legacy-mode` flag.
        ''',
}

V2_CHANGES = {
    "weatherly/client.py": '''
        import os


        def retry(fn, attempts=5):
            """Call fn, retrying on failure."""
            last = None
            for _ in range(attempts):
                try:
                    return fn()
                except Exception as exc:  # noqa: BLE001
                    last = exc
            raise last


        def get_current(city):
            return {"city": city, "temp": 21.5}


        class Client:
            def __init__(self, token=None):
                self.token = token or os.environ.get("WEATHERLY_API_KEY", "demo")

            def get_forecast(self, city, days=7):
                # One entry per day, starting today.
                return [{"city": city, "day": i, "temp": 20 + i} for i in range(days)]
        ''',
    "weatherly/cli.py": '''
        import argparse
        import json

        from .client import Client


        def main(argv=None):
            parser = argparse.ArgumentParser(prog="weatherly")
            parser.add_argument("city")
            parser.add_argument("--output-json", action="store_true", help="print raw JSON")
            args = parser.parse_args(argv)
            data = Client().get_forecast(args.city)
            print(json.dumps(data) if args.output_json else data)
        ''',
}
V2_DELETES = ["weatherly/legacy_cache.py"]


def write(root, files):
    for rel, body in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path) or root, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(textwrap.dedent(body).lstrip("\n"))


def git(root, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="demo", GIT_AUTHOR_EMAIL="demo@example.com",
               GIT_COMMITTER_NAME="demo", GIT_COMMITTER_EMAIL="demo@example.com")
    subprocess.run(["git", "-C", root, *args], check=True, capture_output=True, env=env)


def build(root, commit_pr=True):
    os.makedirs(root, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    write(root, V1)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "weatherly 1.0")
    git(root, "checkout", "-q", "-b", "pr/forecast-api-cleanup")
    write(root, V2_CHANGES)
    for rel in V2_DELETES:
        os.remove(os.path.join(root, rel))
    if commit_pr:
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "Clean up forecast API")
    return root


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "weatherly-demo"
    if os.path.exists(target) and os.listdir(target):
        sys.exit(f"{target} exists and is not empty")
    build(target)
    print(f"Demo repo ready at {target} (branch pr/forecast-api-cleanup, base main).")
    print(f"Try:  cd {target} && stale --run-snippets")
