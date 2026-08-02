.PHONY: setup build test dev demo

setup:
	uv sync --extra dev
	npm --prefix frontend install

build:
	npm --prefix frontend run build

test:
	uvx ruff check backend main.py
	uv run pytest -q
	npm --prefix frontend run lint
	npm --prefix frontend run build

dev:
	@uv run uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000 & api_pid=$$!; \
	  trap 'kill $$api_pid 2>/dev/null || true' INT TERM EXIT; \
	  npm --prefix frontend run dev -- --host 0.0.0.0

demo: build
	@uv run uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
