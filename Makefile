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

# The database tests need a Postgres to talk to (`docker compose up -d`);
# they skip themselves, loudly, when there is none.
test:
	uv run pytest -rs --cov --cov-fail-under=85

check: lint typecheck importcheck test
