# Prism · Lab Insight Workspace

Prism is a local-first data product for turning heterogeneous supplier files and unstructured updates into a trusted canonical schema, natural-language analysis, charts, and explainable insights.

The shipped MVP includes reviewable source profiling and row previews, canonical field mapping, SQLGlot AST-based read-only query protection, persistent multi-turn A2UI conversations with cancellation, and agent-selected AntV MCP charts.

## Run the product

```bash
make setup
make demo
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) locally, or use the machine's LAN address from another device. The server listens on `0.0.0.0:8000`; the app initializes a deterministic 1,000-row laboratory dataset automatically and starts the local AntV MCP chart runtime.

For frontend and backend hot reload during development:

```bash
make dev
```

The Vite product is then available at [http://127.0.0.1:5173](http://127.0.0.1:5173).

## Configure DeepSeek or Kimi

Copy `.env.example` to `.env` and fill in the OpenAI-compatible provider URL, API key and model. The backend loads this file automatically. Both Text-to-SQL planning and the final evidence-based answer use the configured model; missing or failed model calls are shown as explicit errors and never replaced by mock answers. SQLGlot validation, DuckDB execution and tool execution remain controlled by the application.

## Verification

```bash
make test
```

The product contract and implementation rationale are documented in [`docs/Mini Hackathon.md`](docs/Mini%20Hackathon.md).
