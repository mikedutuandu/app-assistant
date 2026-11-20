"""Prints the seconds `import assistant.asgi` takes, best of 3 fresh interpreters.

Run it with `uv run python scripts/cold_start.py`. Importing builds the settings, so placeholder values fill in
any variable that is not set; nothing connects anywhere, so the time is the cost every cold start pays.
"""

import os
import subprocess
import sys

RUNS = 3
PLACEHOLDERS = {
    "OPENROUTER_API_KEY": "x",
    "DATABASE_URL": "postgresql://user:pass@localhost:5432/db",
    "S3_BUCKET": "x",
    "S3_ACCESS_KEY": "x",
    "S3_SECRET_KEY": "x",
    "PUBLIC_BASE_URL": "https://example.com",
}
SNIPPET = "import time; t = time.perf_counter(); import assistant.asgi; print(time.perf_counter() - t)"


def main() -> None:
    env = {**PLACEHOLDERS, **os.environ}
    runs = [
        subprocess.run([sys.executable, "-c", SNIPPET], check=False, capture_output=True, text=True, env=env)
        for _ in range(RUNS)
    ]
    for run in runs:
        if run.returncode != 0:
            sys.exit(run.stderr)
    best = min(float(run.stdout) for run in runs)
    print(f"import assistant.asgi: {best:.3f} s (best of {RUNS})")


if __name__ == "__main__":
    main()
