"""Vercel's entrypoint (`[tool.vercel] entrypoint = "app:app"`): a root-level file, so Vercel finds it without
guessing at the `src/` layout. It puts `src/` on the path when the package is not installed, then re-exports the app."""

import sys
from importlib.util import find_spec
from pathlib import Path

if find_spec("assistant") is None:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from assistant.asgi import app

__all__ = ["app"]
