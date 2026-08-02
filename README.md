# Prism · Lab Insight Workspace

Prism is a local-first data product for turning heterogeneous supplier files and unstructured updates into a trusted canonical schema, natural-language analysis, charts, and explainable insights.

The shipped MVP includes reviewable source profiling and row previews, canonical field mapping, SQLGlot AST-based read-only query protection, persistent multi-turn A2UI conversations with cancellation, and agent-selected AntV MCP charts.

## Run the product

```bash
make setup
make demo
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). The app initializes a deterministic 1,000-row laboratory dataset automatically and starts the local AntV MCP chart runtime.

For frontend and backend hot reload during development:

```bash
make dev
```

The Vite product is then available at [http://127.0.0.1:5173](http://127.0.0.1:5173).

## Optional DeepSeek or Kimi model

The full product remains demonstrable without a model key. To use an OpenAI-compatible provider for the final narrative answer, copy `.env.example` to `.env`, fill in the provider URL, API key and model, then export the values before starting. Schema mapping, SQL execution, metric calculation and tool execution remain controlled by the application.

## Verification

```bash
make test
```

The product contract and implementation rationale are documented in [`docs/Mini Hackathon.md`](docs/Mini%20Hackathon.md).
