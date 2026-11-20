# app-assistant

A private Python service: a chat bot any web app adds with one script tag. FastAPI and LangGraph, Postgres for state, hosted on Railway.

```bash
uv sync
uv run pytest
uv run uvicorn assistant.main:create_app --factory
```

Copy `.env.example` to `.env` and fill it in before running the server.
