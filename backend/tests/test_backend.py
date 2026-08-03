import json
import threading

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessageChunk

from backend.app.a2ui import A2UIStream, validate_envelope
from backend.app.analysis import run_analysis
from backend.app.cancellation import (
    register_cancellation,
    unregister_cancellation,
)
from backend.app.database import connection, json_dumps, utcnow
from backend.app.main import app
from backend.app.mcp_chart import McpVisualizationClient
from backend.app.mcp_config import (
    McpConfigError,
    McpTransport,
    enabled_servers,
    load_servers,
)
from backend.app.model_client import (
    ANSWER_SYSTEM_PROMPT,
    VISUALIZATION_SYSTEM_PROMPT,
    OpenAICompatibleModel,
)
from backend.app.model_runtime import (
    ModelConfigurationError,
    Provider,
    answer_text,
    chat_model,
    configured_effort,
    current_provider,
    model_for_mode,
    reasoning_text,
    thinking_body,
)
from backend.app.semantic import semantic_prompt_context
from backend.app.sql_guard import SQLGuardError, guard_sql
from backend.app.text_to_sql import SYSTEM_PROMPT, GeneratedPlan


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_demo_and_mapping_flow(client: TestClient):
    response = client.get("/api/v1/ingestions")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] >= 6
    names = {item["source_name"] for item in payload["items"]}
    assert {
        "vendor-contracts.csv", "project-budgets.xlsx", "material-quality-targets.csv"
    }.issubset(names)
    downloadable = next(item for item in payload["items"] if item["source_name"] == "vendor-contracts.csv")
    assert downloadable["download_url"]
    download = client.get(downloadable["download_url"])
    assert download.status_code == 200
    assert b"contracted_cost_usd" in download.content
    review_batch = next(
        item for item in payload["items"] if item["source_name"] == "bluepeak-q2.xlsx"
    )
    mapping = client.get(f"/api/v1/ingestions/{review_batch['batch_id']}/mapping").json()
    profile = client.get(f"/api/v1/ingestions/{review_batch['batch_id']}/profile").json()
    preview = client.get(f"/api/v1/ingestions/{review_batch['batch_id']}/preview").json()
    assert profile["row_count"] == review_batch["record_count"]
    assert profile["columns"]
    assert all("Example value" not in column["sample_values"] for column in profile["columns"])
    assert any(column["inferred_type"] == "Decimal" for column in profile["columns"])
    assert len(preview["rows"]) == 20
    assert all("Example value" not in row.values() for row in preview["rows"])
    assert mapping["can_commit"] is True
    assert any(item["confidence"] < 0.8 for item in mapping["mappings"])
    committed = client.post(f"/api/v1/ingestions/{review_batch['batch_id']}/commit").json()
    assert committed["status"] == "READY"
    with connection() as conn:
        conn.execute(
            "UPDATE ingestion_batches SET status = 'NEEDS_REVIEW', "
            "current_stage = 'Review field mapping' WHERE batch_id = ?",
            [review_batch["batch_id"]],
        )


def test_sql_guard_accepts_analytics_and_rejects_unsafe_sql():
    guarded = guard_sql("SELECT vendor, count(*) FROM fact_test_results GROUP BY vendor")
    assert "fact_test_results" in guarded
    assert "\n" in guarded
    assert "  COUNT(*)" in guarded
    assert guarded.endswith("LIMIT 200")
    joined = guard_sql(
        "SELECT f.vendor, c.sla_days FROM fact_test_results f "
        "JOIN dim_vendor_contracts c USING (vendor)"
    )
    assert "dim_vendor_contracts" in joined
    semantic_view = guard_sql(
        "SELECT vendor, contract_tier, sum(cost_usd) FROM vw_laboratory_analysis "
        "GROUP BY vendor, contract_tier"
    )
    assert "vw_laboratory_analysis" in semantic_view
    with pytest.raises(SQLGuardError):
        guard_sql("DROP TABLE fact_test_results")
    with pytest.raises(SQLGuardError):
        guard_sql("SELECT * FROM read_csv('secret.csv')")
    with pytest.raises(SQLGuardError):
        guard_sql("SELECT * FROM internal_users")


def test_relationship_rules_publish_validated_join_view(client: TestClient):
    response = client.get("/api/v1/schema/relationships")
    assert response.status_code == 200
    layer = response.json()
    assert layer["view_name"] == "vw_laboratory_analysis"
    assert len(layer["rules"]) >= 2
    assert any(
        rule["left_field"] == "material"
        and rule["right_table"] == "dim_material_standards"
        and rule["right_field"] == "material"
        for rule in layer["rules"]
    )
    assert all(rule["matched_pct"] == 100 for rule in layer["rules"])
    assert all(rule["right_key_unique"] for rule in layer["rules"])
    assert layer["view"]["column_count"] > 11
    assert layer["view"]["preview"][0]["contract_tier"]
    assert layer["view"]["preview"][0]["owner"]

    payload = {
        "rules": [
            {
                key: rule[key]
                for key in (
                    "name", "left_table", "left_field", "right_table", "right_field",
                    "join_type", "relationship",
                )
            }
            for rule in layer["rules"]
        ]
    }
    published = client.put("/api/v1/schema/relationships", json=payload)
    assert published.status_code == 200
    assert published.json()["view"]["status"] == "PUBLISHED"

    invalid = payload["rules"][0] | {"relationship": "ONE_TO_ONE"}
    rejected = client.put("/api/v1/schema/relationships", json={"rules": [invalid]})
    assert rejected.status_code == 422
    assert "must be unique" in rejected.text
    assert len(client.get("/api/v1/schema/relationships").json()["rules"]) == len(payload["rules"])


def test_model_prompts_own_result_formatting_policy():
    assert "strftime(date_expression, '%Y-%m-%d')" in SYSTEM_PROMPT
    assert "explicitly ROUND(..., 2)" in SYSTEM_PROMPT
    assert "Never return a 0-1 fraction" in SYSTEM_PROMPT
    assert "table.formats metadata" in ANSWER_SYSTEM_PROMPT
    assert "never multiply it by 100 again" in ANSWER_SYSTEM_PROMPT
    context = semantic_prompt_context()
    assert "Current published semantic layer" in context
    assert "vw_laboratory_analysis" in context
    assert "contract_tier" in context


def test_visualization_is_selected_from_runtime_tools_not_planner_rules():
    assert "generate_column_chart" not in SYSTEM_PROMPT
    assert "generate_bar_chart" not in SYSTEM_PROMPT
    assert "generate_line_chart" not in SYSTEM_PROMPT
    assert "dynamically discovered MCP tools" in VISUALIZATION_SYSTEM_PROMPT
    assert "selected tool's JSON Schema" in VISUALIZATION_SYSTEM_PROMPT


def test_visualization_mcp_comes_from_the_config_file(client: TestClient):
    """The shipped AntV server must be declared in mcp.json, not in code."""
    servers = enabled_servers()
    antv = next(server for server in servers if server.name == "antv-chart")
    assert antv.transport is McpTransport.STDIO
    assert [antv.command, *antv.args] == ["npx", "-y", "@antv/mcp-server-chart"]
    # The SSRF allowlist travels with the server declaration.
    assert antv.asset_hosts == ("mdn.alipayobjects.com",)

    assert McpVisualizationClient().servers == servers

    visualization = client.get("/api/v1/health").json()["visualization_mcp"]
    assert visualization["status"] == "on-demand"
    assert visualization["config_path"].endswith("mcp.json")
    assert visualization["servers"] == [server.describe() for server in load_servers()]


def test_mcp_config_is_parsed_validated_and_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("CHART_TOKEN", "s3cret")
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({
        "mcpServers": {
            # transport omitted: inferred from the entry shape.
            "local": {
                "command": "npx",
                "args": ["-y", "server", "--key=${CHART_TOKEN}"],
                "env": {"TOKEN": "${CHART_TOKEN}"},
                "assetHosts": ["Cdn.Example.COM"],
            },
            "remote": {"url": "https://tools.example.com/mcp", "headers": {"X-Key": "${CHART_TOKEN}"}},
            "parked": {"command": "noop", "enabled": False},
        }
    }))
    local, remote, parked = load_servers(config)

    assert local.transport is McpTransport.STDIO
    # Secrets stay in the environment; the committed file only references them.
    assert local.args == ["-y", "server", "--key=s3cret"]
    assert local.env == {"TOKEN": "s3cret"}
    assert local.asset_hosts == ("cdn.example.com",)
    assert remote.transport is McpTransport.HTTP
    assert remote.url == "https://tools.example.com/mcp"
    assert remote.headers == {"X-Key": "s3cret"}
    assert parked.enabled is False
    assert [server.name for server in enabled_servers(config)] == ["local", "remote"]
    # Header values may be tokens and must not reach the health endpoint.
    assert "s3cret" not in json.dumps(remote.describe())

    missing = tmp_path / "absent.json"
    assert load_servers(missing) == []

    for broken, expected in [
        ({"mcpServers": {"x": {"transport": "carrier-pigeon", "command": "a"}}}, "transport="),
        ({"mcpServers": {"x": {}}}, "requires a command"),
        ({"mcpServers": {"x": {"url": "https://a", "headers": {"k": 1}}}}, "map of strings"),
        ({"mcpServers": {"x": {"command": "a", "assetHosts": "nope"}}}, "list of hostnames"),
        ({"mcpServers": {"x": {"command": "a", "enabled": "yes"}}}, "true or false"),
        ({"servers": {}}, "'mcpServers'"),
    ]:
        config.write_text(json.dumps(broken))
        with pytest.raises(McpConfigError, match=expected):
            load_servers(config)

    config.write_text("{not json")
    with pytest.raises(McpConfigError, match="not valid JSON"):
        load_servers(config)


def test_deepseek_thinking_body_matches_the_live_api(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LLM_THINKING_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("LLM_NON_THINKING_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "max")

    thinking = thinking_body(Provider.DEEPSEEK, True)
    assert thinking["thinking"] == {"type": "enabled"}
    # The live API validates reasoning_effort at the top level of the body and
    # ignores the same key nested inside `thinking`, so placement is load-bearing.
    assert thinking["reasoning_effort"] == "max"
    assert "reasoning_effort" not in thinking["thinking"]
    assert model_for_mode(True) == "deepseek-v4-pro"

    direct = thinking_body(Provider.DEEPSEEK, False)
    assert direct == {"thinking": {"type": "disabled"}}
    assert model_for_mode(False) == "deepseek-v4-flash"


def test_gemini_translates_the_same_toggle(monkeypatch):
    """Swapping providers must stay a configuration change."""
    # `xhigh` has no Gemini equivalent and is translated, not rejected.
    monkeypatch.setenv("LLM_REASONING_EFFORT", "xhigh")
    assert thinking_body(Provider.GEMINI, True) == {"reasoning_effort": "high"}
    assert thinking_body(Provider.GEMINI, False) == {
        "reasoning_effort": "none",
        "google": {"thinking_config": {"thinking_budget": 0}},
    }


def test_providers_without_a_verified_switch_send_nothing(monkeypatch):
    """Guessing a parameter name is how a setting ends up silently ignored."""
    monkeypatch.setenv("LLM_PROVIDER", "kimi")
    monkeypatch.setenv("LLM_NON_THINKING_MODEL", "kimi-direct")
    assert thinking_body(Provider.KIMI, False) == {}
    assert thinking_body(Provider.KIMI, True) == {}
    assert model_for_mode(False) == "kimi-direct"
    # A generic OpenAI endpoint omits the field rather than sending a value it
    # may reject.
    assert thinking_body(Provider.OPENAI, False) == {}
    assert thinking_body(Provider.OPENAI, True) == {"reasoning_effort": "high"}


def test_unsupported_runtime_settings_fail_loudly(monkeypatch):
    """A silently dropped setting is how LLM_REASONING_EFFORT became a no-op."""
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "hgih")
    with pytest.raises(ValueError, match="LLM_REASONING_EFFORT"):
        configured_effort()

    monkeypatch.setenv("LLM_REASONING_EFFORT", "high")
    monkeypatch.setenv("LLM_PROVIDER", "deepsekk")
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        current_provider()


def test_reasoning_and_text_are_read_from_langchain_content_blocks():
    """LangChain normalises provider reasoning into `reasoning` blocks."""
    chunk = AIMessageChunk(
        content=[
            {"type": "reasoning", "reasoning": "weighing joins"},
            {"type": "text", "text": "DeltaLab is highest."},
        ]
    )
    assert reasoning_text(chunk) == "weighing joins"
    assert answer_text(chunk) == "DeltaLab is highest."

    text_only = AIMessageChunk(content=[{"type": "text", "text": "hi"}])
    assert reasoning_text(text_only) == ""
    assert answer_text(text_only) == "hi"


def test_chat_model_ignores_proxy_environment(monkeypatch):
    """The OpenAI SDK trusts proxy env vars by default; model traffic must not."""
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1080")
    # Construction alone raises ImportError if the SOCKS proxy is picked up.
    model = chat_model(True)
    assert model.model_name == "deepseek-v4-flash"

    monkeypatch.delenv("LLM_API_KEY")
    monkeypatch.setenv("LLM_API_KEY", "")
    with pytest.raises(ModelConfigurationError):
        chat_model(True)


def test_reference_tables_support_contract_and_budget_analysis(client: TestClient):
    with connection() as conn:
        contract_rows = conn.execute("SELECT count(*) FROM dim_vendor_contracts").fetchone()[0]
        budget_rows = conn.execute("SELECT count(*) FROM dim_project_budgets").fetchone()[0]
        material_rows = conn.execute("SELECT count(*) FROM dim_material_standards").fetchone()[0]
        joined = conn.execute(
            """
            SELECT count(*)
            FROM fact_test_results f
            JOIN dim_vendor_contracts c USING (vendor)
            JOIN dim_project_budgets b USING (project)
            """
        ).fetchone()[0]
    assert contract_rows == 6
    assert budget_rows == 10
    assert material_rows == 5
    assert joined == 1000


def test_llm_plan_executes_guarded_query(monkeypatch):
    plan = GeneratedPlan(
        intent="project_cost",
        sql="SELECT project, round(sum(cost_usd), 2) AS total_cost FROM fact_test_results GROUP BY project ORDER BY total_cost DESC",
        formats={"project": "TEXT", "total_cost": "CURRENCY_USD"},
    )

    def fake_generate(*_args, **kwargs):
        kwargs["reasoning_sink"]("provider-native reasoning chunk")
        return plan

    monkeypatch.setattr("backend.app.analysis.TextToSQLPlanner.generate", fake_generate)
    events = []
    analysis = run_analysis("Which projects cost the most?", events.append)
    assert analysis["table"]["rows"]
    assert "LIMIT 200" in analysis["sql"]
    assert analysis["visualization"]["status"] == "PENDING"
    assert analysis["table"]["formats"]["total_cost"] == "CURRENCY_USD"
    totals = [row["total_cost"] for row in analysis["table"]["rows"]]
    assert all(value == round(value, 2) for value in totals)
    assert events[0] == {
        "type": "reasoning_delta",
        "delta": "provider-native reasoning chunk",
    }
    completed_tools = [
        event
        for event in events
        if event["type"] == "tool_result" and event["status"] == "COMPLETED"
    ]
    assert [event["name"] for event in completed_tools] == [
        "SQLGlot · validate_sql",
        "DuckDB · execute_query",
    ]
    assert completed_tools[-1]["result"]["row_count"] > 0


def test_a2ui_envelope_shape():
    envelope = {
        "version": "v0.9.1",
        "createSurface": {
            "surfaceId": "message:test",
            "catalogId": "https://mini-hackathon.local/a2ui/catalogs/analytics-chat/v1",
        },
    }
    validate_envelope(envelope)


def test_agent_events_preserve_interleaved_order():
    stream = A2UIStream("conversation", "question")
    stream._update_reasoning("先确认指标口径。", "RUNNING")
    stream._update_tool("query", "DuckDB · execute_query", "RUNNING", 1, arguments={"sql": "SELECT 1"})
    stream._update_content_delta("先给出部分结论。")
    stream._update_tool("chart", "AntV MCP · generate_column_chart", "RUNNING", 2)
    stream._update_content_delta("工具返回后继续正文。")

    assert [event["type"] for event in stream.model["events"]] == [
        "reasoning", "tool_group", "content", "tool_group", "content",
    ]


def test_reasoning_events_complete_at_output_boundaries():
    stream = A2UIStream("conversation", "question")
    stream._update_reasoning_delta("Plan the query.", "RUNNING")
    assert stream.model["events"][-1]["status"] == "RUNNING"

    stream._update_tool("query", "DuckDB · execute_query", "RUNNING", 1)
    first_reasoning = stream.model["events"][0]
    assert first_reasoning["status"] == "COMPLETED"

    stream._update_reasoning_delta("Interpret the result.", "RUNNING")
    assert stream.model["events"][-1]["status"] == "RUNNING"
    stream._update_content_delta("The result is ready.")
    second_reasoning = stream.model["events"][-2]
    assert second_reasoning["status"] == "COMPLETED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reasoning_enabled", "expected_event_types"),
    [
        # The agent streams its answer before the chart is rendered, so the
        # visualization tool call lands after the content rather than before it.
        (True, ["reasoning", "tool_group", "content", "tool_group"]),
        (False, ["tool_group", "content", "tool_group"]),
    ],
)
async def test_stream_is_ordered_and_completes(
    monkeypatch, reasoning_enabled, expected_event_types
):
    observed_modes = []

    async def fake_render(
        self, question, analysis, event_sink, thinking_enabled
    ):
        observed_modes.append(thinking_enabled)
        event_sink({
            "type": "tool_call", "tool_call_id": "model:chart",
            "name": "AntV MCP · generate_column_chart", "status": "RUNNING",
            "arguments": {"data": [{"category": "A", "value": 2}]},
        })
        event_sink({
            "type": "tool_result", "tool_call_id": "model:chart",
            "name": "AntV MCP · generate_column_chart", "status": "COMPLETED",
            "result": {"asset_url": "/api/v1/assets/chart.png"},
        })
        return {
            **analysis["visualization"], "status": "READY",
            "tool_name": "generate_column_chart", "title": "Tests",
            "asset_url": "/api/v1/assets/chart.png",
        }

    observed_history: list = []
    monkeypatch.setattr("backend.app.a2ui.McpVisualizationClient.render", fake_render)

    async def fake_stream_agent(
        _question, run, cancellation_token=None, thinking_enabled=True, history=None
    ):
        observed_modes.append(thinking_enabled)
        observed_history.append(history)
        # The agent emits reasoning, then the guarded query tool's events, then
        # the answer. A2UIStream must keep that order and suppress reasoning in
        # fast mode.
        yield {"type": "reasoning_delta", "delta": "按供应商聚合测试数量。"}
        yield {
            "type": "tool_result", "tool_call_id": "duckdb_query:1",
            "name": "DuckDB · execute_query", "status": "COMPLETED",
            "arguments": {"sql": "SELECT 1"}, "result": {"row_count": 1},
        }
        run.sql = "SELECT vendor, count(*) AS tests FROM fact_test_results GROUP BY vendor LIMIT 200"
        run.rows = [{"vendor": "A", "tests": 2}]
        run.columns = ["vendor", "tests"]
        yield {"type": "content_delta", "delta": "A has 2 tests."}

    monkeypatch.setattr("backend.app.a2ui.stream_agent", fake_stream_agent)
    with TestClient(app) as client:
        conversation = client.post("/api/v1/conversations").json()
        stream = A2UIStream(
            conversation["conversation_id"],
            "Compare vendors",
            reasoning_enabled=reasoning_enabled,
        )
        chunks = [chunk async for chunk in stream.events()]
    chunks = [chunk for chunk in chunks if chunk]
    ids = [int(chunk.splitlines()[0].rsplit(":", 1)[1]) for chunk in chunks]
    assert ids == list(range(1, len(ids) + 1))
    envelopes = [
        json.loads(next(line[6:] for line in chunk.splitlines() if line.startswith("data: ")))
        for chunk in chunks
    ]
    assert "createSurface" in envelopes[0]
    assert envelopes[-1]["updateDataModel"]["value"] == "COMPLETED"
    event_updates = [
        envelope["updateDataModel"]["value"]
        for envelope in envelopes
        if envelope.get("updateDataModel", {}).get("path") == "/events"
    ]
    final_events = event_updates[-1]
    assert [event["type"] for event in final_events] == expected_event_types
    tool_event = next(event for event in final_events if event["type"] == "tool_group")
    content_event = next(event for event in final_events if event["type"] == "content")
    assert tool_event["calls"][0]["arguments"] == {"sql": "SELECT 1"}
    # The first turn has no prior context, but history must still be threaded
    # through both model calls rather than dropped on the way.
    # First turn has no prior context, but history is still threaded through.
    assert observed_history == [[]]
    assert content_event["markdown"] == "A has 2 tests."
    with TestClient(app) as client:
        history = client.get(
            f"/api/v1/conversations/{conversation['conversation_id']}/messages"
        ).json()
    assert history["total"] == 2
    assert history["items"][-1]["status"] == "COMPLETED"
    assert history["items"][-1]["a2ui_surface_snapshot"]["status"] == "COMPLETED"
    # Two model call sites now: the agent (planning and answering in one loop)
    # and the visualization tool selection. Both must honour the toggle.
    assert observed_modes == [reasoning_enabled, reasoning_enabled]


def test_cancel_streaming_message(client: TestClient):
    conversation = client.post("/api/v1/conversations").json()
    stream = A2UIStream(conversation["conversation_id"], "Compare vendors")
    stream._create_message()
    token = register_cancellation(conversation["conversation_id"], stream.message_id)
    interrupted = threading.Event()
    token.add_callback(interrupted.set)
    try:
        response = client.post(
            f"/api/v1/conversations/{conversation['conversation_id']}/messages/{stream.message_id}/cancel"
        )
        assert response.status_code == 202
        assert response.json()["status"] == "CANCELLED"
        assert interrupted.is_set()
        assert token.cancelled
    finally:
        unregister_cancellation(stream.message_id, token)


@pytest.mark.asyncio
async def test_unconfigured_model_fails_instead_of_mocking(monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(ModelConfigurationError):
        _ = [
            chunk
            async for chunk in OpenAICompatibleModel().stream_answer(
                "question", {"answer": "This must never be returned as a mock."}
            )
        ]


def test_legacy_database_drops_hardcoded_quality_score(tmp_path, monkeypatch):
    """init_schema must migrate databases created before quality_score was removed."""
    import duckdb

    from backend.app import database

    legacy_path = tmp_path / "legacy.duckdb"
    legacy = duckdb.connect(str(legacy_path))
    legacy.execute(
        """
        CREATE TABLE ingestion_batches (
            batch_id VARCHAR PRIMARY KEY, source_type VARCHAR NOT NULL,
            source_name VARCHAR NOT NULL, vendor_hint VARCHAR, status VARCHAR NOT NULL,
            record_count INTEGER NOT NULL, quality_score DOUBLE NOT NULL,
            current_stage VARCHAR, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL
        )
        """
    )
    legacy.execute(
        "INSERT INTO ingestion_batches VALUES "
        "('b1', 'CSV', 'legacy.csv', NULL, 'READY', 7, 86, 'Ready', now(), now())"
    )
    legacy.close()

    monkeypatch.setattr(database, "DATABASE_PATH", legacy_path)
    monkeypatch.setattr(database, "_database_connection", None)
    database.init_schema()

    with database.connection() as conn:
        columns = [row[0] for row in conn.execute("DESCRIBE ingestion_batches").fetchall()]
        assert "quality_score" not in columns
        # Existing rows survive the migration and new inserts match the new arity.
        conn.execute(
            "INSERT INTO ingestion_batches VALUES "
            "('b2', 'CSV', 'new.csv', NULL, 'READY', 3, 'Ready', now(), now())"
        )
        assert conn.execute("SELECT count(*) FROM ingestion_batches").fetchone()[0] == 2
        conn.close()
    database._database_connection = None


def test_analysis_payload_carries_no_question_independent_metrics():
    """The answer surface must not ship figures that ignore the user's question."""
    from backend.app.analysis import _empty_analysis

    payload = _empty_analysis("Which vendor?")
    assert "kpis" not in payload
    assert "insights" not in payload


def test_conversations_persist_and_can_be_deleted(client: TestClient):
    """The sidebar must survive a reload and let a conversation be removed."""
    created = client.post("/api/v1/conversations").json()
    conversation_id = created["conversation_id"]

    listed = client.get("/api/v1/conversations").json()
    entry = next(
        item for item in listed["items"] if item["conversation_id"] == conversation_id
    )
    assert entry["title"] == "New analysis"
    assert entry["question_count"] == 0

    # Simulate a completed turn the way A2UIStream persists one.
    with connection() as conn:
        now = utcnow()
        snapshot = {"events": [], "content": {"markdown": "done"}, "status": "COMPLETED"}
        conn.execute(
            "INSERT INTO messages VALUES (?, ?, 'USER', ?, 'COMPLETED', NULL, ?, ?)",
            ["u1", conversation_id, "Which vendor is cheapest?", now, now],
        )
        conn.execute(
            "INSERT INTO messages VALUES (?, ?, 'ASSISTANT', ?, 'COMPLETED', ?, ?, ?)",
            ["a1", conversation_id, "done", json_dumps(snapshot), now, now],
        )
        conn.execute(
            "INSERT INTO a2ui_events VALUES ('a1:1', 'a1', 1, '{}', ?)", [now]
        )
        conn.execute(
            "UPDATE conversations SET title = ? WHERE conversation_id = ?",
            ["Which vendor is cheapest?", conversation_id],
        )

    messages = client.get(f"/api/v1/conversations/{conversation_id}/messages").json()
    assistant = next(item for item in messages["items"] if item["role"] == "ASSISTANT")
    # A restored answer replays its snapshot instead of re-running the analysis.
    replay = assistant["a2ui_replay"]
    assert [next(iter(set(envelope) - {"version"})) for envelope in replay] == [
        "createSurface", "updateComponents", "updateDataModel",
    ]
    assert replay[2]["updateDataModel"]["value"] == snapshot
    assert replay[0]["createSurface"]["surfaceId"] == "message:a1"
    user = next(item for item in messages["items"] if item["role"] == "USER")
    assert user["a2ui_replay"] is None

    reloaded = next(
        item for item in client.get("/api/v1/conversations").json()["items"]
        if item["conversation_id"] == conversation_id
    )
    assert reloaded["title"] == "Which vendor is cheapest?"
    assert reloaded["question_count"] == 1

    assert client.delete(f"/api/v1/conversations/{conversation_id}").status_code == 204
    assert client.get(f"/api/v1/conversations/{conversation_id}/messages").status_code == 404
    assert all(
        item["conversation_id"] != conversation_id
        for item in client.get("/api/v1/conversations").json()["items"]
    )
    with connection() as conn:
        assert conn.execute(
            "SELECT count(*) FROM messages WHERE conversation_id = ?", [conversation_id]
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM a2ui_events WHERE message_id = 'a1'"
        ).fetchone()[0] == 0
    assert client.delete(f"/api/v1/conversations/{conversation_id}").status_code == 404


def test_conversation_history_is_sent_to_the_model(client: TestClient):
    """A follow-up has nothing to resolve against unless earlier turns are sent."""
    from backend.app.conversation import MAX_CHARS_PER_MESSAGE, recent_messages, with_history

    conversation_id = client.post("/api/v1/conversations").json()["conversation_id"]
    with connection() as conn:
        base = utcnow()
        rows = [
            ("u1", "USER", "Compare cost by vendor", "COMPLETED", 0),
            ("a1", "ASSISTANT", "DeltaLab is highest at $19,300.02.", "COMPLETED", 0),
            ("u2", "USER", "Now only Ceramic-C", "COMPLETED", 1),
            ("a2", "ASSISTANT", "", "STREAMING", 1),
        ]
        for message_id, role, content, status, offset in rows:
            conn.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, NULL, ?, NULL)",
                [message_id, conversation_id, role, content, status,
                 base.replace(microsecond=offset * 1000)],
            )

    history = recent_messages(conversation_id, "a2")
    # The in-flight turn and its own question are excluded; earlier turns are not.
    assert history == [
        {"role": "user", "content": "Compare cost by vendor"},
        {"role": "assistant", "content": "DeltaLab is highest at $19,300.02."},
    ]

    messages = with_history("SYSTEM", history, "Now only Ceramic-C")
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[0]["content"] == "SYSTEM"
    assert messages[-1]["content"] == "Now only Ceramic-C"

    # An empty conversation still produces a well-formed two-message prompt.
    assert with_history("SYSTEM", [], "Q") == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "Q"},
    ]

    with connection() as conn:
        conn.execute(
            "UPDATE messages SET content = ?, status = 'COMPLETED' WHERE message_id = 'a2'",
            ["x" * (MAX_CHARS_PER_MESSAGE + 500)],
        )
        conn.execute(
            "INSERT INTO messages VALUES ('u3', ?, 'USER', 'And by month?', 'COMPLETED', "
            "NULL, ?, NULL)",
            [conversation_id, utcnow()],
        )
        conn.execute(
            "INSERT INTO messages VALUES ('a3', ?, 'ASSISTANT', '', 'STREAMING', NULL, ?, NULL)",
            [conversation_id, utcnow()],
        )
    clipped = next(m for m in recent_messages(conversation_id, "a3") if m["content"].startswith("x"))
    assert len(clipped["content"]) == MAX_CHARS_PER_MESSAGE + 1  # includes the ellipsis

    client.delete(f"/api/v1/conversations/{conversation_id}")


def test_prompts_bound_history_to_context_not_figures():
    assert "Earlier turns of this conversation" in SYSTEM_PROMPT
    assert "Earlier turns of this conversation" in ANSWER_SYSTEM_PROMPT
    assert "never from an\nearlier turn" in ANSWER_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_visualization_skips_cleanly_when_no_server_is_configured():
    """An empty config disables charts visibly, without failing the analysis."""
    events: list[dict] = []
    analysis = {
        "table": {"columns": ["vendor"], "rows": [{"vendor": "A"}]},
        "visualization": {"status": "PENDING", "data": [{"vendor": "A"}]},
    }
    result = await McpVisualizationClient(servers=[]).render("q", analysis, events.append)

    assert result["status"] == "SKIPPED"
    assert result["asset_url"] is None
    discovery = [event for event in events if event["tool_call_id"] == "mcp:tools/list"]
    # The empty result is still reported, so this cannot be mistaken for a model
    # that simply chose not to draw a chart.
    assert [event["type"] for event in discovery] == ["tool_call", "tool_result"]
    assert discovery[-1]["result"] == {"tool_count": 0, "servers": {}}


def test_agent_query_tool_is_the_only_path_to_duckdb():
    """The pipeline used to guarantee ordering; now the tool must guarantee it."""
    from backend.app.agent import AnalysisRun, build_query_tool

    run = AnalysisRun()
    events: list[dict] = []
    query = build_query_tool(run, events.append, None)

    rejected = query.invoke({"sql": "DROP TABLE fact_test_results"})
    assert rejected.startswith("REJECTED:")
    # Rejection is returned, not raised, so the agent can repair and retry.
    assert run.sql is None and run.rows == []
    assert [e["status"] for e in events if e["tool_call_id"] == "sql_guard:1"] == [
        "RUNNING", "FAILED",
    ]

    assert query.invoke({"sql": "SELECT * FROM read_csv('secret.csv')"}).startswith("REJECTED:")
    assert query.invoke({"sql": "SELECT * FROM internal_users"}).startswith("REJECTED:")

    ok = query.invoke({"sql": "SELECT vendor, count(*) AS test_count FROM fact_test_results GROUP BY vendor"})
    assert json.loads(ok)
    # The guarded AST, not the model's raw string, is what ran and what is shown.
    assert run.sql.endswith("LIMIT 200")
    assert "test_count" in run.columns
    analysis = run.as_analysis()
    assert analysis["visualization"]["status"] == "PENDING"
    assert analysis["requires_clarification"] is False


def test_skill_tools_are_scoped_and_execute_is_configurable(monkeypatch, tmp_path):
    """Skills need file reads; the write and execute surface is a deliberate choice."""
    from backend.app import skills

    monkeypatch.setenv("SKILL_DIR", str(tmp_path))
    assert skills.discovered_skills() == []
    # No skills means no filesystem tools at all, rather than idle file access.
    assert skills.skill_middleware() == []

    skill = tmp_path / "vendor-scorecard"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: vendor-scorecard\ndescription: Ranks vendors.\n---\n\n# Rules\n"
    )
    assert skills.discovered_skills() == ["vendor-scorecard"]

    monkeypatch.setenv("SKILL_EXECUTE_ENABLED", "true")
    assert skills.execute_enabled() is True
    assert "execute" in skills.allowed_tool_names()
    filesystem, skills_mw = skills.skill_middleware()
    names = {tool.name for tool in filesystem.tools}
    assert "read_file" in names, "a skill body cannot be loaded without read_file"
    assert "execute" in names
    assert skills_mw.sources == ["./"]

    monkeypatch.setenv("SKILL_EXECUTE_ENABLED", "false")
    assert skills.execute_enabled() is False
    filesystem, _ = skills.skill_middleware()
    names = {tool.name for tool in filesystem.tools}
    assert "execute" not in names
    assert "read_file" in names


@pytest.mark.asyncio
async def test_tool_schemas_are_discovered_once_and_reused(monkeypatch):
    """Starting a stdio server costs seconds, so schemas are cached per process."""
    from backend.app import mcp_chart

    monkeypatch.setattr(mcp_chart, "_TOOL_CACHE", None)
    calls: list[int] = []

    async def fake_discover(servers):
        calls.append(len(servers))
        tool = mcp_chart.DiscoveredTool(
            server=servers[0],
            schema={"type": "function", "function": {"name": "generate_bar_chart"}},
        )
        return {"generate_bar_chart": tool}, {servers[0].name: {"tool_count": 1}}

    monkeypatch.setattr(mcp_chart, "discover_tools", fake_discover)
    servers = enabled_servers()

    tools, _, from_cache = await mcp_chart.cached_tools(servers)
    assert set(tools) == {"generate_bar_chart"} and from_cache is False
    # The allowlist still travels with the tool, now via its server.
    assert tools["generate_bar_chart"].asset_hosts == ("mdn.alipayobjects.com",)

    _, _, from_cache = await mcp_chart.cached_tools(servers)
    assert from_cache is True
    assert calls == [1], "discovery must not run again once cached"

    await mcp_chart.cached_tools(servers, refresh=True)
    assert calls == [1, 1], "refresh must re-discover"

    monkeypatch.setattr(mcp_chart, "_TOOL_CACHE", None)

    async def failing_discover(servers):
        return {}, {servers[0].name: {"error": "OSError: npx missing"}}

    monkeypatch.setattr(mcp_chart, "discover_tools", failing_discover)
    _, report, _ = await mcp_chart.cached_tools(servers)
    assert "error" in report[servers[0].name]
    # A transient failure must not be cached, or charts stay dead for the
    # lifetime of the process.
    assert mcp_chart._TOOL_CACHE is None
    assert "error" not in await mcp_chart.prime_tool_cache()  # returns the report, not a raise
    monkeypatch.setattr(mcp_chart, "_TOOL_CACHE", None)


def test_logs_rotate_and_stay_bounded(tmp_path, monkeypatch):
    """Old logs must be deleted, or a long-running box fills its disk."""
    import logging

    from backend.app import logging_config

    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_MAX_BYTES", "2048")
    monkeypatch.setenv("LOG_BACKUP_COUNT", "2")
    monkeypatch.setattr(logging_config, "_configured", False)
    previous = logging.getLogger().handlers[:]
    try:
        logging_config.configure_logging()
        described = logging_config.describe()
        # The bound the operator actually cares about.
        assert described["max_total_bytes"] == 2048 * 3

        logger = logging.getLogger("prism.test")
        for index in range(400):
            logger.info("filler line %s %s", index, "x" * 80)

        written = sorted(tmp_path.glob("app.log*"))
        assert len(written) == 3, "active file plus exactly backup_count older ones"
        total = sum(path.stat().st_size for path in written)
        assert total <= described["max_total_bytes"] * 1.1
        # Rotation deletes rather than accumulates.
        assert not (tmp_path / "app.log.3").exists()
    finally:
        logging.getLogger().handlers = previous
        logging_config._configured = False

    monkeypatch.setenv("LOG_MAX_BYTES", "0")
    with pytest.raises(ValueError, match="LOG_MAX_BYTES"):
        logging_config.max_bytes()


def test_registry_is_seeded_from_the_real_tables(client: TestClient):
    """The dropdown offered sample_id, cost_amount and lab_vendor, none of which
    exist, while omitting six columns that do. Seeding from DESCRIBE makes that
    class of drift unrepresentable."""
    registry = client.get("/api/v1/schema/registry").json()
    entities = {item["entity"]: item for item in registry["entities"]}
    assert set(entities) == {
        "fact_test_results", "dim_vendor_contracts",
        "dim_project_budgets", "dim_material_standards",
    }
    assert entities["fact_test_results"]["role"] == "FACT"
    assert entities["dim_vendor_contracts"]["role"] == "DIMENSION"

    offered = set(client.get("/api/v1/schema/canonical-fields").json()["items"])
    real = {
        column
        for table in client.get("/api/v1/schema/relationships").json()["tables"]
        for column in table["columns"]
    }
    assert offered == real, "a mapping target that does not exist is the original bug"

    cost = next(f for f in entities["fact_test_results"]["fields"] if f["field"] == "cost_usd")
    # A field carries meaning, not just a name and a type.
    assert cost["description"] and cost["unit"] == "USD"
    assert cost["result_format"] == "CURRENCY_USD"
    result = next(f for f in entities["fact_test_results"]["fields"] if f["field"] == "result")
    assert result["enum_values"] == ["PASS", "FAIL"]


def test_registry_field_meaning_is_editable_but_identity_is_not(client: TestClient):
    from backend.app.registry import registry_prompt_context

    patched = client.patch(
        "/api/v1/schema/registry/fact_test_results/turnaround_days",
        json={"description": "Working days from submission to report.", "unit": "working days"},
    )
    assert patched.status_code == 200
    assert patched.json()["unit"] == "working days"
    # The prompt reads the registry, so an edit reaches Text-to-SQL.
    assert "Working days from submission to report." in registry_prompt_context()

    rejected = client.patch(
        "/api/v1/schema/registry/fact_test_results/turnaround_days",
        json={"result_format": "FURLONGS"},
    )
    assert rejected.status_code == 422 and "result_format" in rejected.text

    unknown = client.patch("/api/v1/schema/registry/fact_test_results/nope", json={"unit": "x"})
    assert unknown.status_code == 422 and "Unknown field" in unknown.text

    client.patch(
        "/api/v1/schema/registry/fact_test_results/turnaround_days",
        json={"description": "Days between submission and completion.", "unit": "days"},
    )
