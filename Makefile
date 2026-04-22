.PHONY: help sync lint fmt test test-all compose-up compose-down migrate clean

help:
	@echo "Targets:"
	@echo "  sync         Install all workspace deps (uv sync --all-packages)"
	@echo "  lint         ruff check + mypy"
	@echo "  fmt          ruff format"
	@echo "  test         pytest (excludes real_ollama)"
	@echo "  test-all     pytest (all markers)"
	@echo "  compose-up   Bring up postgres + ollama for local dev"
	@echo "  compose-down Tear down dev stack"
	@echo "  migrate      alembic upgrade head"

sync:
	uv sync --all-packages

lint:
	uv run ruff check .
	uv run mypy .

fmt:
	uv run ruff format .
	uv run ruff check --fix .

test:
	uv run pytest -m "not real_ollama"

test-all:
	uv run pytest

compose-up:
	docker compose -f docker-compose.dev.yml up -d

compose-down:
	docker compose -f docker-compose.dev.yml down

migrate:
	uv run alembic -c packages/common/alembic.ini upgrade head

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	rm -rf .mypy_cache .ruff_cache .pytest_cache
