.PHONY: install lint typecheck test importcheck check

install:
	uv sync

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

typecheck:
	uv run mypy

importcheck:
	uv run lint-imports

# Phase 0 has no tests yet; pytest exit code 5 (no tests collected) is OK.
test:
	uv run pytest || [ $$? -eq 5 ]

check: lint typecheck importcheck test
