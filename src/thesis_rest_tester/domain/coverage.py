"""Traceability models for comparing shared requirements with one OpenAPI contract."""

from __future__ import annotations

from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CoverageModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OperationReference(CoverageModel):
    method: str
    path: str
    operation_id: str | None = None

    @field_validator("method")
    @classmethod
    def normalize_method(cls, value: str) -> str:
        return value.upper()


class RequirementAPIMatch(CoverageModel):
    requirement_id: str
    status: Literal[
        "implemented",
        "partially_implemented",
        "not_implemented",
        "not_assessable",
    ]
    matched_operations: list[OperationReference] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    missing_behaviors: list[str] = Field(default_factory=list)
    rationale: str


class RequirementCoverageDraft(CoverageModel):
    matches: list[RequirementAPIMatch]


class ProjectRequirementCoverage(CoverageModel):
    project_name: str
    openapi_path: str
    assessment_basis: Literal["openapi_documentation"] = "openapi_documentation"
    requirements_total: int = Field(ge=0)
    status_counts: dict[str, int] = Field(default_factory=dict)
    matches: list[RequirementAPIMatch] = Field(default_factory=list)
    validation_warnings: list[str] = Field(default_factory=list)

    @classmethod
    def from_matches(
        cls,
        *,
        project_name: str,
        openapi_path: str,
        matches: list[RequirementAPIMatch],
        warnings: list[str] | None = None,
    ) -> ProjectRequirementCoverage:
        counts = Counter(match.status for match in matches)
        statuses = (
            "implemented",
            "partially_implemented",
            "not_implemented",
            "not_assessable",
        )
        return cls(
            project_name=project_name,
            openapi_path=openapi_path,
            requirements_total=len(matches),
            status_counts={status: counts.get(status, 0) for status in statuses},
            matches=matches,
            validation_warnings=warnings or [],
        )
