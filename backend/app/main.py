import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import polars as pl
from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .a2ui import A2UIStream
from .config import ARTIFACT_DIR, CATALOG_ID, FRONTEND_DIST
from .database import connection, init_schema, json_dumps, rows_as_dicts, utcnow
from .demo_data import default_mappings, initialize_demo
from .schemas import (
    Conversation,
    CreateMessageRequest,
    IngestionBatch,
    MappingDraft,
    TextIngestionRequest,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_schema()
    initialize_demo()
    yield


app = FastAPI(title="Lab Insight Workspace", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/v1/health")
def health() -> dict:
    model_configured = bool(os.getenv("LLM_BASE_URL") and os.getenv("LLM_API_KEY"))
    return {
        "status": "ok",
        "version": "0.1.0",
        "database": {"status": "ready"},
        "model": {
            "provider": os.getenv("LLM_PROVIDER", "deterministic-demo"),
            "configured": model_configured,
        },
        "visualization_mcp": {"status": "optional", "url": "http://127.0.0.1:1122/mcp"},
    }


@app.post("/api/v1/demo/initialize")
def demo_initialize() -> dict:
    initialized, count, batch_ids = initialize_demo()
    return {"initialized": initialized, "record_count": count, "batch_ids": batch_ids}


@app.get("/api/v1/ingestions")
def list_ingestions() -> dict:
    with connection() as conn:
        items = rows_as_dicts(
            conn.execute("SELECT * FROM ingestion_batches ORDER BY created_at DESC")
        )
    return {"items": items, "total": len(items)}


@app.get("/api/v1/ingestions/{batch_id}", response_model=IngestionBatch)
def get_ingestion(batch_id: str):
    with connection() as conn:
        rows = rows_as_dicts(
            conn.execute("SELECT * FROM ingestion_batches WHERE batch_id = ?", [batch_id])
        )
    if not rows:
        raise HTTPException(404, "Ingestion batch not found")
    return rows[0]


@app.post("/api/v1/ingestions/files", status_code=202, response_model=IngestionBatch)
async def upload_file(file: UploadFile = File(...), vendor_hint: str | None = None):  # noqa: B008
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".csv", ".xlsx"}:
        raise HTTPException(415, "Only CSV and XLSX are supported")
    contents = await file.read()
    batch_id = str(uuid.uuid4())
    try:
        if suffix == ".csv":
            frame = pl.read_csv(contents)
        else:
            frame = pl.read_excel(contents)
    except Exception as exc:
        raise HTTPException(422, f"Unable to parse file: {type(exc).__name__}") from exc
    now = utcnow()
    source_type = "CSV" if suffix == ".csv" else "XLSX"
    mappings = []
    for name in frame.columns:
        target = next(
            (
                target
                for token, target in [
                    ("sample", "sample_id"),
                    ("specimen", "sample_id"),
                    ("test", "test_name"),
                    ("analysis", "test_name"),
                    ("result", "result"),
                    ("status", "result"),
                    ("cost", "cost_amount"),
                    ("amount", "cost_amount"),
                    ("date", "completed_date"),
                ]
                if token in name.lower()
            ),
            None,
        )
        samples = frame.get_column(name).drop_nulls().head(3).to_list()
        mappings.append(
            {
                "source_field": name,
                "target_field": target,
                "confidence": 0.9 if target else 0.35,
                "transform": "IDENTITY",
                "reason": "Matched from field name and sampled values"
                if target
                else "No confident canonical field match",
                "status": "SUGGESTED" if target else "IGNORED",
                "sample_before": samples,
                "sample_after": samples,
                "warnings": [] if target else ["Field will be ignored"],
            }
        )
    with connection() as conn:
        conn.execute(
            "INSERT INTO ingestion_batches VALUES (?, ?, ?, ?, 'NEEDS_REVIEW', ?, 86, 'Review field mapping', ?, ?)",
            [batch_id, source_type, file.filename or "upload", vendor_hint, frame.height, now, now],
        )
        conn.execute(
            "INSERT INTO field_mappings VALUES (?, 1, ?)", [batch_id, json_dumps(mappings)]
        )
    return get_ingestion(batch_id)


@app.post("/api/v1/ingestions/text", status_code=202, response_model=IngestionBatch)
def ingest_text(request: TextIngestionRequest):
    batch_id = str(uuid.uuid4())
    now = utcnow()
    count = max(1, len([line for line in request.content.splitlines() if line.strip()]))
    mappings = default_mappings(batch_id, "TEXT")
    with connection() as conn:
        conn.execute(
            "INSERT INTO ingestion_batches VALUES (?, 'TEXT', ?, ?, 'NEEDS_REVIEW', ?, 80, 'Review extracted fields', ?, ?)",
            [batch_id, request.source_name, request.vendor_hint, count, now, now],
        )
        conn.execute(
            "INSERT INTO field_mappings VALUES (?, 1, ?)", [batch_id, json_dumps(mappings)]
        )
    return get_ingestion(batch_id)


@app.get("/api/v1/ingestions/{batch_id}/mapping", response_model=MappingDraft)
def get_mapping(batch_id: str):
    with connection() as conn:
        row = conn.execute(
            "SELECT version, payload FROM field_mappings WHERE batch_id = ?", [batch_id]
        ).fetchone()
    if not row:
        raise HTTPException(404, "Mapping not found")
    return {
        "batch_id": batch_id,
        "version": row[0],
        "mappings": json.loads(row[1]),
        "missing_required_fields": [],
        "can_commit": True,
    }


@app.put("/api/v1/ingestions/{batch_id}/mapping", response_model=MappingDraft)
def update_mapping(batch_id: str, draft: MappingDraft):
    with connection() as conn:
        conn.execute(
            "UPDATE field_mappings SET version = ?, payload = ? WHERE batch_id = ?",
            [
                draft.version + 1,
                json_dumps([item.model_dump() for item in draft.mappings]),
                batch_id,
            ],
        )
    return get_mapping(batch_id)


@app.post("/api/v1/ingestions/{batch_id}/commit", response_model=IngestionBatch)
def commit_ingestion(batch_id: str):
    with connection() as conn:
        conn.execute(
            "UPDATE ingestion_batches SET status = 'READY', current_stage = 'Ready for analysis', updated_at = ? WHERE batch_id = ?",
            [utcnow(), batch_id],
        )
    return get_ingestion(batch_id)


@app.post("/api/v1/conversations", response_model=Conversation, status_code=201)
def create_conversation():
    conversation_id = str(uuid.uuid4())
    now = utcnow()
    with connection() as conn:
        conn.execute(
            "INSERT INTO conversations VALUES (?, 'New analysis', ?, ?)",
            [conversation_id, now, now],
        )
    return {
        "conversation_id": conversation_id,
        "title": "New analysis",
        "created_at": now,
        "updated_at": now,
    }


@app.post("/api/v1/conversations/{conversation_id}/messages/stream")
def stream_message(
    conversation_id: str,
    request: CreateMessageRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    if CATALOG_ID not in request.a2uiClientCapabilities.supportedCatalogIds:
        raise HTTPException(406, "A2UI_CATALOG_UNSUPPORTED")
    with connection() as conn:
        exists = conn.execute(
            "SELECT 1 FROM conversations WHERE conversation_id = ?", [conversation_id]
        ).fetchone()
    if not exists:
        raise HTTPException(404, "Conversation not found")
    resume_after = 0
    if last_event_id and ":" in last_event_id:
        try:
            resume_after = int(last_event_id.rsplit(":", 1)[1])
        except ValueError:
            raise HTTPException(400, "Invalid Last-Event-ID") from None
    with connection() as conn:
        existing = conn.execute(
            "SELECT message_id, question FROM stream_requests WHERE idempotency_key = ?",
            [idempotency_key],
        ).fetchone()
        if existing and existing[1] != request.question:
            raise HTTPException(409, "Idempotency-Key was already used for another question")
        message_id = existing[0] if existing else str(uuid.uuid4())
        if not existing:
            conn.execute(
                "INSERT INTO stream_requests VALUES (?, ?, ?, ?, ?)",
                [idempotency_key, conversation_id, message_id, request.question, utcnow()],
            )
        else:
            conn.execute(
                "DELETE FROM a2ui_events WHERE message_id = ? AND sequence > ?",
                [message_id, resume_after],
            )
            conn.execute(
                "UPDATE messages SET status = 'STREAMING', completed_at = NULL WHERE message_id = ?",
                [message_id],
            )
    stream = A2UIStream(
        conversation_id, request.question, message_id=message_id, resume_after=resume_after
    )
    return StreamingResponse(
        stream.events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/v1/analysis/suggestions")
def suggestions():
    return {
        "items": [
            {
                "id": "vendor",
                "label": "Compare vendors",
                "question": "Compare cost, pass rate and turnaround time by vendor",
                "category": "Performance",
            },
            {
                "id": "trend",
                "label": "Find cost anomalies",
                "question": "What cost trends or anomalies appeared in recent months?",
                "category": "Trend",
            },
            {
                "id": "material",
                "label": "Inspect quality risk",
                "question": "Which materials have the highest failure rate?",
                "category": "Quality",
            },
        ]
    }


@app.get("/api/v1/assets/{asset_name}")
def asset(asset_name: str):
    if Path(asset_name).name != asset_name:
        raise HTTPException(400, "Invalid asset name")
    target = ARTIFACT_DIR / asset_name
    if not target.is_file():
        raise HTTPException(404, "Asset not found")
    return FileResponse(target)


if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def frontend(path: str):
        candidate = FRONTEND_DIST / path
        return FileResponse(candidate if candidate.is_file() else FRONTEND_DIST / "index.html")
