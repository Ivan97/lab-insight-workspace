.PHONY: setup build test dev demo

setup:
	uv sync --extra dev
	npm --prefix frontend install
	npm --prefix runtime install

build:
	npm --prefix frontend run build

test:
	uvx ruff check backend main.py
	uv run pytest -q
	npm --prefix frontend run lint
	npm --prefix frontend run build

dev:
	@./runtime/node_modules/.bin/mcp-server-chart --transport streamable & chart_pid=$$!; \
	  uv run uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000 & api_pid=$$!; \
	  trap 'kill $$chart_pid $$api_pid 2>/dev/null || true' INT TERM EXIT; \
	  npm --prefix frontend run dev -- --host 127.0.0.1

demo: build
	@./runtime/node_modules/.bin/mcp-server-chart --transport streamable & chart_pid=$$!; \
	  trap 'kill $$chart_pid 2>/dev/null || true' INT TERM EXIT; \
	  uv run uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
