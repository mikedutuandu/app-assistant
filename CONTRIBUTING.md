# Contributing

Issues and pull requests are welcome. For a large change, open an issue first so we can agree on the shape.

## Set up

You need Python 3.12 with [uv](https://docs.astral.sh/uv/), Node 22, and Docker (for a test Postgres).

```bash
docker compose -f docker-compose.test.yml up -d    # Postgres on localhost:54329
uv sync
cp .env.example .env                                # then fill it in
```

## Before a pull request

```bash
TEST_DATABASE_URL=postgresql://test:test@localhost:54329/test uv run pytest
uv run ruff check . && uv run ruff format --check .
cd widget && npm ci && npm test
cd sdk/js && npm ci && npm test && npm run typecheck
```

CI runs the same on every pull request.

- **Tests first.** A fix comes with a test that failed before it.
- **The contract is a promise to apps.** A change to what an app sends or receives updates
  [docs/integration.md](docs/integration.md) and `tests/contract/scenarios.json` (which the JS package's tests
  reuse), and keeps contract version 1 working.
- **No secrets** in code, tests or docs: use obviously fake values in tests.
- One change per pull request, against `main`.

By contributing, you agree that your work is released under the [MIT License](LICENSE).
