# Prism · Lab Insight Workspace

Prism is a local-first data product for turning heterogeneous supplier files and unstructured updates into a trusted canonical schema, natural-language analysis, charts, and explainable insights.

The shipped MVP includes reviewable source profiling and row previews, canonical field mapping, SQLGlot AST-based read-only query protection, persistent multi-turn A2UI conversations with cancellation, and agent-selected AntV MCP charts.

## Run the product

```bash
make setup
make demo
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) locally, or use the machine's LAN address from another device. The server listens on `0.0.0.0:8000` and initializes a deterministic 1,000-row laboratory dataset automatically.

## Configure MCP tool servers

Tool servers are declared in [`mcp.json`](mcp.json), using the same `mcpServers` shape as Claude Desktop and Cursor, so an entry can be pasted between them. The shipped configuration starts AntV on demand over stdio; nothing is hardcoded in the application, and adding a second server is an edit to this file.

```json
{
  "mcpServers": {
    "antv-chart": {
      "command": "npx",
      "args": ["-y", "@antv/mcp-server-chart"],
      "assetHosts": ["mdn.alipayobjects.com"]
    }
  }
}
```

Tool schemas are discovered once per process and cached, warmed at startup. Starting a stdio server costs seconds, so the model chooses a tool from the cached schemas first and only the server owning the chosen tool is started -- a question that needs no chart starts nothing. Discovery failures are not cached, and never block startup: charts degrade, the app serves. `GET /api/v1/health` reports the warm-up result.

`transport` is inferred from the entry shape (`command` for stdio, `url` for streamable HTTP) and may also be set explicitly. Set `enabled` to `false` to keep an entry without starting it, and override the file location with `MCP_CONFIG_PATH`.

The file is committed, so secrets belong in `.env` and are referenced as `${VAR}` inside `env`, `args`, `url` or `headers`. `assetHosts` is an SSRF boundary: only those hosts may serve images a tool returns, and every image is cached and re-served from this origin. A malformed entry fails loudly rather than silently disabling a server. `GET /api/v1/health` reports the resolved configuration.

For frontend and backend hot reload during development:

```bash
make dev
```

The Vite product is then available at [http://127.0.0.1:5173](http://127.0.0.1:5173).

## Configure DeepSeek or Kimi

Copy `.env.example` to `.env` and fill in the OpenAI-compatible provider URL, API key and model. The backend loads this file automatically. Both Text-to-SQL planning and the final evidence-based answer use the configured model; missing or failed model calls are shown as explicit errors and never replaced by mock answers. SQLGlot validation, DuckDB execution and tool execution remain controlled by the application.

`LLM_THINKING_MODEL` and `LLM_NON_THINKING_MODEL` can select different models for the composer toggle. For DeepSeek V4, both may use the same model: the backend sends the provider-native `thinking.type=enabled/disabled` parameter for Text-to-SQL, visualization tool selection and final answer generation.

## Verification

```bash
make test
```

The product contract and implementation rationale are documented in [`docs/Mini Hackathon.md`](docs/Mini%20Hackathon.md).
