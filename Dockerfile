# The bot, from Python 3.12 and uv: `docker compose up` (docker-compose.yml) runs it with Postgres and a data volume.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PACKS_DIR=/app/apps

WORKDIR /app

# Dependencies first, so a code change does not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY apps ./apps
COPY README.md ./
RUN uv sync --frozen --no-dev

# A user of its own; /data is where the local storage backend keeps files (a volume in docker-compose.yml).
RUN useradd --system --uid 10001 --home-dir /app app && mkdir /data && chown app /data
USER app
VOLUME /data

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

# Migrations run at start (the app's lifespan), as on Vercel.
CMD ["uvicorn", "assistant.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
