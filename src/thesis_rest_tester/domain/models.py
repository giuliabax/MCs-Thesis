"""Core data models shared across workflow components."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TokenUsage(DomainModel):
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class RequirementItem(DomainModel):
    id: str
    source: str
    text: str
    role: str
    business_value: str | int | float | None = None
    constraints: list[str] = Field(default_factory=list)
    expected_behaviors: list[str] = Field(default_factory=list)


class OpenAPIOperation(DomainModel):
    operation_id: str | None = None
    method: str
    path: str
    summary: str | None = None
    description: str | None = None
    parameters: list[dict[str, Any]] = Field(default_factory=list)
    request_body_schema: dict[str, Any] | None = None
    response_codes: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    auth_required: bool | None = None

    @field_validator("method")
    @classmethod
    def normalize_method(cls, value: str) -> str:
        return value.upper()


class AgentOutput(DomainModel):
    agent_name: str
    raw_text: str
    parsed_json: dict[str, Any] | list[Any] | None = None
    token_usage: TokenUsage | None = None
    model: str | None = None


class TestStrategyItem(DomainModel):
    requirement_id: str
    requirement_summary: str
    api_endpoint: str
    http_method: str
    prompt: str
    test_type: Literal["happy_path", "edge_case", "negative", "stateful", "cleanup"]
    priority: Literal["high", "medium", "low"]
    auth_role: str | None = None
    setup_needed: list[str] = Field(default_factory=list)
    cleanup_strategy: str | None = None
    expected_status_codes: list[str] = Field(default_factory=list)
    rationale: str | None = None

    @field_validator("http_method")
    @classmethod
    def normalize_http_method(cls, value: str) -> str:
        return value.upper()


class WorkflowPlan(DomainModel):
    run_id: str
    project_name: str
    requirements_summary: dict[str, Any]
    api_summary: dict[str, Any]
    strategy_items: list[TestStrategyItem]
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    created_at: datetime
    sut_base_url: str | None = None
    requirement_coverage: dict[str, Any] | None = None


class MetricSnapshot(DomainModel):
    iteration: int = Field(ge=0)
    pass_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    execution_success_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    operation_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    status_code_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    seeded_bugs_detected: int | None = Field(default=None, ge=0)
    server_errors_count: int | None = Field(default=None, ge=0)
    token_usage: int | None = Field(default=None, ge=0)
    execution_time_seconds: float | None = Field(default=None, ge=0.0)
    estimated_cost_usd: float | None = Field(default=None, ge=0.0)
