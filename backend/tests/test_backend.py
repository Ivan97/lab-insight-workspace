import json

import pytest
from fastapi.testclient import TestClient

from backend.app.a2ui import A2UIStream, validate_envelope
from backend.app.main import app


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
    assert mapping["can_commit"] is True
    assert any(item["confidence"] < 0.8 for item in mapping["mappings"])
    committed = client.post(f"/api/v1/ingestions/{review_batch['batch_id']}/commit").json()
    assert committed["status"] == "READY"


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
