import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import polars as pl
from fastapi import BackgroundTasks, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .a2ui import A2UIStream, replay_envelopes
from .cancellation import cancel_active, register_cancellation, unregister_cancellation
from .config import ARTIFACT_DIR, CATALOG_ID, DEMO_SOURCE_DIR, FRONTEND_DIST
from .database import connection, init_schema, json_dumps, rows_as_dicts, utcnow
from .demo_data import default_mappings, initialize_demo
from .logging_config import configure_logging
from .logging_config import describe as describe_logging
from .mcp_chart import prime_tool_cache
from .mcp_config import config_path, load_servers
from .model_runtime import current_provider
from .profiling import preview_frame, profile_frame, profile_text
from .schemas import (
    A2UIActionRequest,
    Conversation,
    CreateMessageRequest,
    IngestionBatch,
    JoinRuleSet,
    MappingDraft,
    TextIngestionRequest,
)
from .semantic import (
    SemanticRuleError,
    get_semantic_layer,
    initialize_semantic_layer,
    replace_semantic_rules,
)
from .skills import discovered_skills, execute_enabled, skill_dir

logger = logging.getLogger("prism.app")


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    logger.info("starting up")
    init_schema()
    initialize_demo()
    initialize_semantic_layer()
    # Warms the MCP tool schemas so the first chart does not pay discovery, and
    # so the model can pick a tool before any server is started. Never fatal.
    app.state.mcp_discovery = await prime_tool_cache()
    logger.info(
        "ready provider=%s skills=%s mcp=%s",
        current_provider().value, discovered_skills(), app.state.mcp_discovery,
    )
    yield
    logger.info("shutting down")


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
            "provider": os.getenv("LLM_PROVIDER", "not-configured"),
            "configured": model_configured,
        },
        "logging": describe_logging(),
        "skills": {
            "directory": str(skill_dir()),
            "execute_enabled": execute_enabled(),
            "discovered": discovered_skills(),
        },
        "visualization_mcp": {
            "status": "on-demand",
            "config_path": str(config_path()),
            "discovery": getattr(app.state, "mcp_discovery", None),
            "servers": [server.describe() for server in load_servers()],
        },
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
    return {"items": [_with_download_url(item) for item in items], "total": len(items)}


@app.get("/api/v1/ingestions/{batch_id}", response_model=IngestionBatch)
def get_ingestion(batch_id: str):
    with connection() as conn:
        rows = rows_as_dicts(
            conn.execute("SELECT * FROM ingestion_batches WHERE batch_id = ?", [batch_id])
        )
    if not rows:
        raise HTTPException(404, "Ingestion batch not found")
    return _with_download_url(rows[0])


def _with_download_url(item: dict) -> dict:
    source_name = item["source_name"]
    target = DEMO_SOURCE_DIR / source_name
    return {
        **item,
        "download_url": f"/api/v1/demo/files/{source_name}" if target.is_file() else None,
    }


@app.get("/api/v1/demo/files/{file_name}")
def download_demo_file(file_name: str):
    if Path(file_name).name != file_name:
        raise HTTPException(400, "Invalid file name")
    target = DEMO_SOURCE_DIR / file_name
    if not target.is_file():
        raise HTTPException(404, "Demo source file not found")
    return FileResponse(target, filename=file_name)


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
                "reason": "Matched on a token in the source field name"
                if target
                else "No canonical field matched the source field name",
                "status": "SUGGESTED" if target else "IGNORED",
                "sample_before": samples,
                "sample_after": samples,
                "warnings": [] if target else ["Field will be ignored"],
            }
        )
    with connection() as conn:
        conn.execute(
            "INSERT INTO ingestion_batches VALUES (?, ?, ?, ?, 'NEEDS_REVIEW', ?, 'Review field mapping', ?, ?)",
            [batch_id, source_type, file.filename or "upload", vendor_hint, frame.height, now, now],
        )
        conn.execute(
            "INSERT INTO field_mappings VALUES (?, 1, ?)", [batch_id, json_dumps(mappings)]
        )
        conn.execute(
            "INSERT INTO ingestion_payloads VALUES (?, ?, ?)",
            [batch_id, json_dumps(profile_frame(frame)), json_dumps(preview_frame(frame))],
        )
    return get_ingestion(batch_id)


@app.post("/api/v1/ingestions/text", status_code=202, response_model=IngestionBatch)
def ingest_text(request: TextIngestionRequest):
    batch_id = str(uuid.uuid4())
    now = utcnow()
    count = max(1, len([line for line in request.content.splitlines() if line.strip()]))
    mappings = default_mappings(batch_id, "TEXT")
    profile, preview = profile_text(request.content)
    with connection() as conn:
        conn.execute(
            "INSERT INTO ingestion_batches VALUES (?, 'TEXT', ?, ?, 'NEEDS_REVIEW', ?, 'Review extracted fields', ?, ?)",
            [batch_id, request.source_name, request.vendor_hint, count, now, now],
        )
        conn.execute(
            "INSERT INTO field_mappings VALUES (?, 1, ?)", [batch_id, json_dumps(mappings)]
        )
        conn.execute(
            "INSERT INTO ingestion_payloads VALUES (?, ?, ?)",
            [batch_id, json_dumps(profile), json_dumps(preview)],
        )
    return get_ingestion(batch_id)


@app.get("/api/v1/ingestions/{batch_id}/profile")
def get_profile(batch_id: str):
    with connection() as conn:
        row = conn.execute(
            "SELECT profile FROM ingestion_payloads WHERE batch_id = ?", [batch_id]
        ).fetchone()
    if not row:
        raise HTTPException(404, "Profile not found")
    return json.loads(row[0])


@app.get("/api/v1/ingestions/{batch_id}/preview")
def get_preview(batch_id: str, limit: int = 20):
    if not 1 <= limit <= 100:
        raise HTTPException(422, "limit must be between 1 and 100")
    with connection() as conn:
        row = conn.execute(
            "SELECT preview FROM ingestion_payloads WHERE batch_id = ?", [batch_id]
        ).fetchone()
    if not row:
        raise HTTPException(404, "Preview not found")
    rows = json.loads(row[0])[:limit]
    return {"rows": rows, "row_count": len(rows), "limit": limit}


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


@app.get("/api/v1/schema/relationships")
def list_relationships():
    return get_semantic_layer()


@app.put("/api/v1/schema/relationships")
def publish_relationships(request: JoinRuleSet):
    try:
        return replace_semantic_rules([rule.model_dump() for rule in request.rules])
    except SemanticRuleError as exc:
        raise HTTPException(422, str(exc)) from exc


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


@app.get("/api/v1/conversations")
def list_conversations() -> dict:
    with connection() as conn:
        items = rows_as_dicts(
            conn.execute(
                """
                SELECT c.conversation_id, c.title, c.created_at, c.updated_at,
                       count(m.message_id) FILTER (WHERE m.role = 'USER') AS question_count
                FROM conversations c
                LEFT JOIN messages m USING (conversation_id)
                GROUP BY c.conversation_id, c.title, c.created_at, c.updated_at
                ORDER BY c.created_at
                """
            )
        )
    return {"items": items, "total": len(items)}


@app.delete("/api/v1/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: str) -> None:
    with connection() as conn:
        exists = conn.execute(
            "SELECT 1 FROM conversations WHERE conversation_id = ?", [conversation_id]
        ).fetchone()
        if not exists:
            raise HTTPException(404, "Conversation not found")
        # a2ui_events is keyed by message, so collect the ids before the
        # messages themselves are removed.
        message_ids = [
            row[0]
            for row in conn.execute(
                "SELECT message_id FROM messages WHERE conversation_id = ?", [conversation_id]
            ).fetchall()
        ]
        for message_id in message_ids:
            cancel_active(conversation_id, message_id)
            conn.execute("DELETE FROM a2ui_events WHERE message_id = ?", [message_id])
        conn.execute("DELETE FROM stream_requests WHERE conversation_id = ?", [conversation_id])
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", [conversation_id])
        conn.execute("DELETE FROM conversations WHERE conversation_id = ?", [conversation_id])


@app.get("/api/v1/conversations/{conversation_id}/messages")
def list_messages(conversation_id: str):
    with connection() as conn:
        exists = conn.execute(
            "SELECT 1 FROM conversations WHERE conversation_id = ?", [conversation_id]
        ).fetchone()
        if not exists:
            raise HTTPException(404, "Conversation not found")
        items = rows_as_dicts(
            conn.execute(
                """
                SELECT message_id, role, content, status, a2ui_surface_snapshot,
                       created_at, completed_at
                FROM messages WHERE conversation_id = ?
                ORDER BY created_at, CASE role WHEN 'USER' THEN 0 ELSE 1 END, message_id
                """,
                [conversation_id],
            )
        )
    for item in items:
        snapshot = item.get("a2ui_surface_snapshot")
        if isinstance(snapshot, str):
            snapshot = json.loads(snapshot)
            item["a2ui_surface_snapshot"] = snapshot
        item["a2ui_replay"] = (
            replay_envelopes(item["message_id"], snapshot)
            if item["role"] == "ASSISTANT" and isinstance(snapshot, dict)
            else None
        )
    return {"items": items, "total": len(items)}


@app.post("/api/v1/conversations/{conversation_id}/messages/{message_id}/cancel", status_code=202)
def cancel_message(
    conversation_id: str, message_id: str, background_tasks: BackgroundTasks
):
    if cancel_active(conversation_id, message_id):
        background_tasks.add_task(
            _persist_cancelled_message, conversation_id, message_id
        )
        return {"message_id": message_id, "status": "CANCELLED"}
    return _persist_cancelled_message(conversation_id, message_id)


def _persist_cancelled_message(conversation_id: str, message_id: str) -> dict:
    with connection() as conn:
        row = conn.execute(
            "SELECT status FROM messages WHERE conversation_id = ? AND message_id = ?",
            [conversation_id, message_id],
        ).fetchone()
        if not row:
            raise HTTPException(404, "Message not found")
        if row[0] == "STREAMING":
            conn.execute(
                "UPDATE messages SET status = 'CANCELLED', completed_at = ? WHERE message_id = ?",
                [utcnow(), message_id],
            )
    return {"message_id": message_id, "status": "CANCELLED" if row[0] == "STREAMING" else row[0]}


@app.post("/api/v1/conversations/{conversation_id}/a2ui/actions", status_code=202)
def handle_a2ui_action(conversation_id: str, request: A2UIActionRequest):
    expected_prefix = "message:"
    if not request.surface_id.startswith(expected_prefix):
        raise HTTPException(422, "Unknown A2UI surface")
    message_id = request.surface_id.removeprefix(expected_prefix)
    with connection() as conn:
        exists = conn.execute(
            "SELECT 1 FROM messages WHERE conversation_id = ? AND message_id = ?",
            [conversation_id, message_id],
        ).fetchone()
    if not exists:
        raise HTTPException(404, "A2UI surface not found")
    return {"accepted": True, "action_id": request.action_id, "name": request.name}


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
        message_id = existing[0] if existing else idempotency_key
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
    cancellation_token = register_cancellation(conversation_id, message_id)
    stream = A2UIStream(
        conversation_id,
        request.question,
        message_id=message_id,
        resume_after=resume_after,
        reasoning_enabled=request.reasoningEnabled,
        cancellation_token=cancellation_token,
    )

    async def stream_events():
        try:
            async for event in stream.events():
                yield event
        finally:
            cancellation_token.cancel()
            unregister_cancellation(message_id, cancellation_token)

    return StreamingResponse(
        stream_events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Message-ID": message_id,
        },
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
            {
                "id": "contract",
                "label": "Check contract performance",
                "question": "Which vendors exceed contracted cost or SLA targets?",
                "category": "Contract",
            },
            {
                "id": "budget",
                "label": "Review budget burn",
                "question": "Compare actual spend with approved budget by project",
                "category": "Budget",
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
        target = candidate if candidate.is_file() else FRONTEND_DIST / "index.html"
        headers = (
            {"Cache-Control": "no-store, no-cache, must-revalidate"}
            if target.name == "index.html"
            else None
        )
        return FileResponse(target, headers=headers)
