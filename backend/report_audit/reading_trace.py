"""Execution metadata for a saved report answer."""

from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, computed_field

ModelCallPhase = Literal["initial", "after_tools", "rewrite", "timeout_retry", "coverage_repair"]
OutputRejection = Literal["invalid_format", "unverified_output", "missing_groups", "missing_medication_evidence"]
ModelSkipReason = Literal["emergency", "pending_confirmation", "no_evidence", "evidence_purpose"]
FallbackReason = Literal["provider_error", "provider_timeout", "model_call_timeout", "agent_timeout", "output_rejected",
                         "invalid_tool_requests", "tool_budget_exhausted", "tool_failed", "configuration_error",
                         "internal_error"]


class ReadingModelCall(BaseModel):
    phase: ModelCallPhase
    status: Literal["returned", "failed", "timeout"]
    elapsed_ms: int = 0
    http_status: int | None = None
    rejection: OutputRejection | None = None
    missing_groups: list[list[int]] = Field(default_factory=list)


class ReadingToolExecution(BaseModel):
    name: Literal["lookup_report", "search_lab_knowledge", "check_lab_relationships", "unknown"]
    status: Literal["completed", "empty", "invalid_request", "failed"]
    result: str


class ReadingWorkRecord(BaseModel):
    request_id: str = Field(default_factory=lambda: uuid4().hex)
    model_calls: list[ReadingModelCall] = Field(default_factory=list)
    tool_rounds: list[list[ReadingToolExecution]] = Field(default_factory=list)
    fallback_reason: FallbackReason | None = None
    skip_reason: ModelSkipReason | None = None

    @computed_field
    @property
    def model_call_count(self) -> int:
        return len(self.model_calls)
