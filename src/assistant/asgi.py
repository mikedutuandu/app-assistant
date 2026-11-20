"""Vercel's entrypoint (`[tool.vercel] entrypoint` in pyproject.toml): the one FastAPI app."""

from assistant.main import create_app

app = create_app()
