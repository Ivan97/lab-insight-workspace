import json

import pytest
from fastapi.testclient import TestClient

from backend.app.a2ui import A2UIStream, validate_envelope
from backend.app.analysis import run_analysis
from backend.app.main import app
from backend.app.model_client import OpenAICompatibleModel
from backend.app.sql_guard import SQLGuardError, guard_sql
from backend.app.text_to_sql import GeneratedPlan, ModelConfigurationError


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_demo_and_mapping_flow(client: TestClient):
    response = client.get("/api/v1/ingestions")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] >= 3
    review_batch = next(item for item in payload["items"] if item["source_type"] == "XLSX")
    mapping = client.get(f"/api/v1/ingestions/{review_batch['batch_id']}/mapping").json()
    profile = client.get(f"/api/v1/ingestions/{review_batch['batch_id']}/profile").json()
    preview = client.get(f"/api/v1/ingestions/{review_batch['batch_id']}/preview").json()
    assert profile["row_count"] == review_batch["record_count"]
    assert profile["columns"]
    assert preview["rows"]
    assert mapping["can_commit"] is True
    assert any(item["confidence"] < 0.8 for item in mapping["mappings"])
    committed = client.post(f"/api/v1/ingestions/{review_batch['batch_id']}/commit").json()
    assert committed["status"] == "READY"


def test_sql_guard_accepts_analytics_and_rejects_unsafe_sql():
    guarded = guard_sql("SELECT vendor, count(*) FROM fact_test_results GROUP BY vendor")
    assert "fact_test_results" in guarded
    with pytest.raises(SQLGuardError):
        guard_sql("DROP TABLE fact_test_results")
    with pytest.raises(SQLGuardError):
        guard_sql("SELECT * FROM read_csv('secret.csv')")
    with pytest.raises(SQLGuardError):
        guard_sql("SELECT * FROM internal_users")


def test_llm_plan_executes_guarded_query(monkeypatch):
    plan = GeneratedPlan(
        intent="project_cost",
        sql="SELECT project, round(sum(cost_usd), 2) AS total_cost FROM fact_test_results GROUP BY project ORDER BY total_cost DESC",
        title="Total cost by project",
        tool_name="generate_column_chart",
        x_field="project",
        y_field="total_cost",
        rationale="Compare project totals.",
    )
    monkeypatch.setattr("backend.app.analysis.TextToSQLPlanner.generate", lambda *_args, **_kwargs: plan)
    analysis = run_analysis("Which projects cost the most?")
    assert analysis["table"]["rows"]
    assert "LIMIT 200" in analysis["sql"]
    assert analysis["visualization"]["x_field"] == "project"
    totals = [row["total_cost"] for row in analysis["table"]["rows"]]
    assert all(value == round(value, 2) for value in totals)


def test_a2ui_envelope_shape():
    envelope = {
        "version": "v0.9.1",
        "createSurface": {
            "surfaceId": "message:test",
            "catalogId": "https://mini-hackathon.local/a2ui/catalogs/analytics-chat/v1",
        },
    }
    validate_envelope(envelope)


@pytest.mark.asyncio
async def test_stream_is_ordered_and_completes(monkeypatch):
    async def fake_render(self, visualization):
        return {**visualization, "status": "SKIPPED", "asset_url": None}

    monkeypatch.setattr("backend.app.a2ui.AntVChartClient.render", fake_render)
    monkeypatch.setattr(
        "backend.app.a2ui.run_analysis",
        lambda _question: {
            "answer": "Query completed.",
            "requires_clarification": False,
            "kpis": [],
            "table": {"columns": ["vendor", "tests"], "rows": [{"vendor": "A", "tests": 2}], "row_count": 1, "truncated": False},
            "insights": [],
            "sql": "SELECT vendor, count(*) AS tests FROM fact_test_results GROUP BY vendor LIMIT 200",
            "visualization": {"status": "PENDING", "tool_name": "generate_column_chart", "title": "Tests", "rationale": "Comparison", "x_field": "vendor", "y_field": "tests", "data": [{"vendor": "A", "tests": 2}]},
            "warnings": [],
        },
    )

    async def fake_answer(self, question, analysis):
        yield "A has 2 tests."

    monkeypatch.setattr("backend.app.a2ui.OpenAICompatibleModel.stream_answer", fake_answer)
    with TestClient(app) as client:
        conversation = client.post("/api/v1/conversations").json()
        stream = A2UIStream(conversation["conversation_id"], "Compare vendors")
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
    with TestClient(app) as client:
        history = client.get(
            f"/api/v1/conversations/{conversation['conversation_id']}/messages"
        ).json()
    assert history["total"] == 2
    assert history["items"][-1]["status"] == "COMPLETED"
    assert history["items"][-1]["a2ui_surface_snapshot"]["status"] == "COMPLETED"


def test_cancel_streaming_message(client: TestClient):
    conversation = client.post("/api/v1/conversations").json()
    stream = A2UIStream(conversation["conversation_id"], "Compare vendors")
    stream._create_message()
    response = client.post(
        f"/api/v1/conversations/{conversation['conversation_id']}/messages/{stream.message_id}/cancel"
    )
    assert response.status_code == 202
    assert response.json()["status"] == "CANCELLED"


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
