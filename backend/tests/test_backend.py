import json
import threading

import pytest
from fastapi.testclient import TestClient

from backend.app.a2ui import A2UIStream, validate_envelope
from backend.app.analysis import run_analysis
from backend.app.cancellation import (
    register_cancellation,
    unregister_cancellation,
)
from backend.app.database import connection, json_dumps, utcnow
from backend.app.main import app
from backend.app.mcp_chart import AntVChartClient
from backend.app.model_client import (
    ANSWER_SYSTEM_PROMPT,
    VISUALIZATION_SYSTEM_PROMPT,
    OpenAICompatibleModel,
)
from backend.app.model_runtime import apply_thinking_mode, reasoning_from_delta
from backend.app.semantic import semantic_prompt_context
from backend.app.sql_guard import SQLGuardError, guard_sql
from backend.app.text_to_sql import SYSTEM_PROMPT, GeneratedPlan, ModelConfigurationError


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


def test_visualization_mcp_uses_on_demand_npx_stdio(client: TestClient):
    chart_client = AntVChartClient()
    assert chart_client.server.command == "npx"
    assert chart_client.server.args == ["-y", "@antv/mcp-server-chart"]

    visualization = client.get("/api/v1/health").json()["visualization_mcp"]
    assert visualization == {
        "status": "on-demand",
        "transport": "stdio",
        "command": ["npx", "-y", "@antv/mcp-server-chart"],
    }


def test_deepseek_thinking_mode_changes_provider_payload(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LLM_THINKING_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("LLM_NON_THINKING_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "max")

    thinking = apply_thinking_mode({"stream": True}, True)
    assert thinking["model"] == "deepseek-v4-pro"
    assert thinking["thinking"] == {"type": "enabled"}
    # The live API validates reasoning_effort at the top level and ignores the
    # same key nested inside `thinking`, so placement is load-bearing.
    assert thinking["reasoning_effort"] == "max"
    assert "reasoning_effort" not in thinking["thinking"]

    direct = apply_thinking_mode({"stream": True}, False)
    assert direct["model"] == "deepseek-v4-flash"
    assert direct["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in direct


def test_gemini_dialect_translates_the_same_toggle(monkeypatch):
    """Swapping providers must stay a configuration change."""
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("LLM_MODEL", "gemini-3-pro")
    monkeypatch.delenv("LLM_THINKING_MODEL", raising=False)
    monkeypatch.delenv("LLM_NON_THINKING_MODEL", raising=False)
    # `xhigh` has no Gemini equivalent and is translated, not rejected.
    monkeypatch.setenv("LLM_REASONING_EFFORT", "xhigh")

    thinking = apply_thinking_mode({"stream": True}, True)
    assert thinking["model"] == "gemini-3-pro"
    assert thinking["reasoning_effort"] == "high"
    assert "thinking" not in thinking

    direct = apply_thinking_mode({"stream": True}, False)
    assert direct["reasoning_effort"] == "none"
    assert direct["google"] == {"thinking_config": {"thinking_budget": 0}}


def test_unsupported_runtime_settings_fail_loudly(monkeypatch):
    """A silently dropped setting is how LLM_REASONING_EFFORT became a no-op."""
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "hgih")
    with pytest.raises(ValueError, match="LLM_REASONING_EFFORT"):
        apply_thinking_mode({"stream": True}, True)

    monkeypatch.setenv("LLM_REASONING_EFFORT", "high")
    monkeypatch.setenv("LLM_PROVIDER", "deepsekk")
    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        apply_thinking_mode({"stream": True}, True)


def test_reasoning_is_read_through_the_provider_dialect(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    assert reasoning_from_delta({"reasoning_content": "step"}) == "step"
    assert reasoning_from_delta({"reasoning": "step"}) == "step"
    assert reasoning_from_delta({"content": "answer"}) is None


def test_other_openai_compatible_providers_switch_models_without_deepseek_fields(
    monkeypatch,
):
    monkeypatch.setenv("LLM_PROVIDER", "kimi")
    monkeypatch.setenv("LLM_NON_THINKING_MODEL", "kimi-direct")
    payload = apply_thinking_mode({}, False)
    assert payload == {"model": "kimi-direct"}


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
        (True, ["reasoning", "tool_group", "content"]),
        (False, ["tool_group", "content"]),
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

    monkeypatch.setattr("backend.app.a2ui.AntVChartClient.render", fake_render)
    def fake_analysis(
        _question, event_sink, _cancellation_token, thinking_enabled
    ):
        observed_modes.append(thinking_enabled)
        event_sink({"type": "reasoning_delta", "delta": "按供应商聚合测试数量。"})
        event_sink({
            "type": "tool_result", "tool_call_id": "duckdb_query:1",
            "name": "DuckDB · execute_query", "status": "COMPLETED",
            "arguments": {"sql": "SELECT 1"}, "result": {"row_count": 1},
        })
        return {
            "answer": "Query completed.",
            "requires_clarification": False,
            "table": {"columns": ["vendor", "tests"], "rows": [{"vendor": "A", "tests": 2}], "row_count": 1, "truncated": False},
            "sql": "SELECT vendor, count(*) AS tests FROM fact_test_results GROUP BY vendor LIMIT 200",
            "visualization": {"status": "PENDING", "data": [{"vendor": "A", "tests": 2}]},
            "warnings": [],
        }

    monkeypatch.setattr("backend.app.a2ui.run_analysis", fake_analysis)

    async def fake_answer(self, question, analysis, thinking_enabled):
        observed_modes.append(thinking_enabled)
        yield {"type": "content_delta", "delta": "A has 2 tests."}

    monkeypatch.setattr("backend.app.a2ui.OpenAICompatibleModel.stream_answer", fake_answer)
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
    assert content_event["markdown"] == "A has 2 tests."
    with TestClient(app) as client:
        history = client.get(
            f"/api/v1/conversations/{conversation['conversation_id']}/messages"
        ).json()
    assert history["total"] == 2
    assert history["items"][-1]["status"] == "COMPLETED"
    assert history["items"][-1]["a2ui_surface_snapshot"]["status"] == "COMPLETED"
    assert observed_modes == [reasoning_enabled, reasoning_enabled, reasoning_enabled]


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
