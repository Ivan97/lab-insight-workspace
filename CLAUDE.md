# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
make setup   # uv sync --extra dev + npm install in frontend/
make demo    # build frontend, then serve API + static bundle on 0.0.0.0:8000
make dev     # uvicorn --reload on :8000 + vite on :5173 (proxies /api to :8000)
make test    # ruff → pytest → oxlint → tsc/vite build (all four must pass)
```

Individual steps:

```bash
uvx ruff check backend main.py
uv run pytest -q
uv run pytest -q -k "test_sql_guard_accepts"                    # single test by name
uv run pytest backend/tests/test_backend.py::test_a2ui_envelope_shape
npm --prefix frontend run lint          # oxlint
npm --prefix frontend run build         # tsc -b && vite build
```

`make demo` serves the built bundle, so **frontend changes are invisible until `npm --prefix frontend run build` reruns**. Use `make dev` while iterating on UI.

DuckDB allows one writer per database file. A server started from this checkout holds `data/app.duckdb`, which makes `pytest` fail with lock errors — stop it first. (A server running from a `.claude/worktrees/*` copy uses that worktree's own `data/`, so it does not conflict.)

Copy `.env.example` to `.env` for model configuration; `backend/app/config.py` loads it at import.

## Architecture

Single FastAPI process (`backend/app/main.py`) that owns the API, one DuckDB file, and — in `demo` mode — the static React bundle. There is no separate service.

### The question pipeline

`POST /api/v1/conversations/{id}/messages/stream` is the only interesting path:

```
main.stream_message
  └─ a2ui.A2UIStream          persists messages, emits A2UI v0.9.1 envelopes over SSE
      └─ agent.stream_agent   LangChain create_agent, one tool, bounded loop
          └─ execute_query    sql_guard.guard_sql → DuckDB
      └─ mcp_chart.render     picks & runs one MCP charting tool, if rows exist
```

`agent.py` replaced a fixed plan → guard → execute pipeline. Because an agent picks its own call order, **the SQL guard moved inside the query tool**: `build_query_tool` is the only code path from the model to DuckDB. Any new database-touching tool must route through `guard_sql` or that invariant breaks silently.

Rejections and query errors are *returned as strings* to the model rather than raised, so the agent repairs and retries on its own. `MAX_ITERATIONS` bounds the loop.

### Streaming, resume, and replay

Every envelope is persisted to `a2ui_events` with a monotonic sequence, and the finished data model is snapshotted into `messages.a2ui_surface_snapshot`. This gives two distinct recovery paths that must stay in sync:

- **Resume mid-stream**: client resends the same `Idempotency-Key` with `Last-Event-ID`; the server truncates events after that sequence and continues.
- **Replay after reload**: `list_messages` returns `a2ui_replay` envelopes rebuilt from the snapshot by `replay_envelopes`. This never re-runs analysis. Snapshots already reflect the reasoning mode the question was asked in, so the frontend replays them verbatim (`A2UIReplay`) instead of re-filtering.

`CATALOG_ID` is duplicated in `backend/app/config.py` and `frontend/src/a2ui/catalog.tsx` and must match — a mismatch is rejected with HTTP 406.

### Schema is data, not code

Two DuckDB-backed layers feed the system prompt. Both exist because the schema had previously drifted across the CREATE TABLEs, a hardcoded React array, and the prompt.

- **`registry.py`** — canonical field meanings, seeded *from the physical tables* so it cannot invent a column. `update_field` accepts only `display_name`, `description`, `result_format`, `unit`; identifiers and types are deliberately not editable. The mapping dropdown reads `/api/v1/schema/canonical-fields`, never a literal.
- **`semantic.py`** — user-reviewed join rules that are compiled into `CREATE OR REPLACE VIEW vw_laboratory_analysis`. Rules are validated against real data (match rate, key uniqueness) before publishing.

The agent's system prompt is `SYSTEM_PROMPT + semantic_prompt_context() + registry_prompt_context()`. Changing what the model knows about the schema means editing the registry or the join rules, not the prompt string.

`sql_guard.ALLOWED_TABLES` must stay in sync with `semantic.ANALYTICAL_TABLES` plus `VIEW_NAME`; they are separate literals in separate modules.

### Provider dialects

`model_runtime.py` isolates the one thing LangChain cannot abstract: how each provider spells "think". Two behaviours are verified against the live DeepSeek API and easy to break —

- `thinking` travels in `extra_body`, but `reasoning_effort` is validated at the **top level** of the body and silently ignored when nested inside `thinking`.
- Both HTTP clients are pinned to `trust_env=False` so a proxy in the environment cannot capture model traffic.

Unknown `LLM_PROVIDER` / `LLM_REASONING_EFFORT` values raise at request time by design: providers ignore fields they do not recognise, so a typo used to look configured while doing nothing. An effort level a provider lacks is translated to its nearest supported one rather than dropped.

### MCP tool servers

Declared in `mcp.json` (same shape as Claude Desktop / Cursor), never hardcoded. Schemas are discovered once per process and cached; the model chooses a tool from the *cached* schemas, and only the server owning that tool is started — a question needing no chart starts nothing. Failed discovery is not cached, and never blocks startup.

`assetHosts` is an SSRF boundary: only those hosts may serve tool images, and every image is downloaded, content-type sniffed, size-capped, and re-served from `/api/v1/assets/`.

### Agent Skills

`skill/*/SKILL.md` folders, discovered by deepagents' `SkillsMiddleware`. `FilesystemMiddleware` is mounted alongside because reading a skill body needs file tools; it is rooted at `SKILL_DIR`. The `execute` tool is on by default and **is not sandboxed** — rooting bounds file paths, not shell commands. `SKILL_EXECUTE_ENABLED=false` drops it. If no skill exists on disk, no filesystem tools are mounted at all.

### Legacy pipeline

`analysis.py` and `text_to_sql.py` are the pre-agent implementation. `run_analysis` is now exercised only by tests — **live question answering goes through `agent.py`**. They are not dead code: `mcp_chart.py` still uses `model_client.OpenAICompatibleModel` (for MCP tool selection), which imports from `text_to_sql`. Check the call graph before editing either.

## Conventions

- Conventional commits, lowercase description (`feat:`, `fix:`, `refactor:`, `perf:`).
- Failures surface as explicit errors; never substitute mock answers for a failed model or query call.
- `contracts/openapi.yaml` is the intended API contract source; JSON fields are `snake_case` end to end, with no frontend renaming.
- `docs/Mini Hackathon.md` (Chinese) is the original design doc and product rationale. Several of its technology decisions have since been superseded by the code — notably "no LangGraph" and the hand-rolled `ModelClient`, both now replaced by LangChain. Treat the code as authoritative.
