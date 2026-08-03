from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class IngestionBatch(BaseModel):
    batch_id: str
    source_type: Literal["CSV", "XLSX", "TEXT"]
    source_name: str
    vendor_hint: str | None = None
    status: Literal[
        "UPLOADED", "PROFILING", "MAPPING", "NEEDS_REVIEW", "COMMITTING", "READY", "FAILED"
    ]
    record_count: int = 0
    current_stage: str | None = None
    created_at: datetime
    updated_at: datetime
    download_url: str | None = None


class FieldMapping(BaseModel):
    source_field: str
    target_field: str | None
    confidence: float = Field(ge=0, le=1)
    transform: str = "IDENTITY"
    reason: str
    status: Literal["SUGGESTED", "CONFIRMED", "MODIFIED", "IGNORED"] = "SUGGESTED"
    sample_before: list[Any] = Field(default_factory=list)
    sample_after: list[Any] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class MappingDraft(BaseModel):
    batch_id: str
    version: int
    mappings: list[FieldMapping]
    missing_required_fields: list[str] = Field(default_factory=list)
    can_commit: bool = True


class JoinRuleInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    left_table: str
    left_field: str
    right_table: str
    right_field: str
    join_type: Literal["LEFT", "INNER"] = "LEFT"
    relationship: Literal["MANY_TO_ONE", "ONE_TO_ONE"] = "MANY_TO_ONE"


class JoinRuleSet(BaseModel):
    rules: list[JoinRuleInput] = Field(min_length=1, max_length=8)


class TextIngestionRequest(BaseModel):
    source_name: str
    content: str = Field(min_length=1, max_length=50_000)
    vendor_hint: str | None = None


class Conversation(BaseModel):
    conversation_id: str
    title: str
    created_at: datetime
    updated_at: datetime


class A2UIClientCapabilities(BaseModel):
    supportedCatalogIds: list[str]


class CreateMessageRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1_000)
    reasoningEnabled: bool = True
    filters: dict[str, Any] = Field(default_factory=dict)
    a2uiClientCapabilities: A2UIClientCapabilities


class A2UIActionRequest(BaseModel):
    surface_id: str
    action_id: str
    name: str
    context: dict[str, Any] = Field(default_factory=dict)
