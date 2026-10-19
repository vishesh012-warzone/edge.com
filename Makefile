.PHONY: check test lint format

check: lint test

lint:
	ruff check .

test:
	pytest

format:
	ruff format .
