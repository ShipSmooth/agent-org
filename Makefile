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

test:
	uv run pytest

check: lint typecheck importcheck test
