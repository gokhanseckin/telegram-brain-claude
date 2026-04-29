# Developing

## Prerequisites

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) for dependency management
- Docker + Docker Compose for Postgres/Ollama

## Bootstrap

```bash
# Install all workspace dependencies into a single venv
uv sync --all-packages

# Install pre-commit hooks (one-time per clone) — blocks accidental commits of
# secrets, large files, Telethon session files, broken YAML/TOML/JSON, etc.
uv run pre-commit install

# Bring up Postgres (with pgvector + pg_trgm) and Ollama
docker compose -f docker-compose.dev.yml up -d

# Pull local models into the compose Ollama (first time only, ~6GB)
docker compose -f docker-compose.dev.yml exec ollama ollama pull qwen2.5:7b-instruct-q4_K_M
docker compose -f docker-compose.dev.yml exec ollama ollama pull bge-m3

# Run migrations
uv run alembic -c packages/common/alembic.ini upgrade head

# Copy env template
cp .env.example .env
# ... fill in credentials when you have them ...
```

## Day-to-day

```bash
make lint    # ruff check + mypy
make test    # pytest (excluding real_ollama)
make fmt     # ruff format

uv run pre-commit run --all-files   # run hooks across the whole repo
```

If detect-secrets ever flags a known-fake placeholder (e.g. dev DB password),
refresh the baseline:

```bash
uv run detect-secrets scan --baseline .secrets.baseline
```

## Running a service locally

```bash
uv run python -m services.ingestion.main
uv run python -m services.worker_understanding.main
# ...etc
```

## Testing philosophy

- Unit tests: pure logic, no DB. Always fast.
- Integration tests (`@pytest.mark.integration`): spin an ephemeral Postgres, run Alembic, exercise real SQL.
- Ollama is mocked by default. Opt in to `@pytest.mark.real_ollama` when iterating on the understanding prompt.
- Anthropic API is faked via `respx`.

## Worktrees

Phase 1 parallel development uses git worktrees, one per component:

```bash
git worktree add ../tbc-ingestion        -b feat/ingestion
git worktree add ../tbc-understanding    -b feat/understanding
git worktree add ../tbc-analytics        -b feat/analytics
git worktree add ../tbc-brief            -b feat/brief
git worktree add ../tbc-bot              -b feat/bot
git worktree add ../tbc-mcp              -b feat/mcp
git worktree add ../tbc-infra            -b feat/infra
```

Each component's agent owns exactly its own `services/*` directory plus tests. `packages/common` is read-only from Phase 1 agents; extensions flow back through PRs to `main`.
