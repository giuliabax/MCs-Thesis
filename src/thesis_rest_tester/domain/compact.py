"""Compact projections of the intermediate planning representations.

A local model has a small context window, so downstream agents consume these
token-lean projections instead of the full analysis objects. Only the fields each
agent needs for its task are kept; per-endpoint detail (full schema, parameters) is
looked up on demand from the original operations when an agent works on a single
endpoint. The deterministic post-processing still runs on the full objects, so
compaction only shrinks the LLM prompt.
"""

from __future__ import annotations

from thesis_rest_tester.domain.models import OpenAPIOperation, RequirementItem
from thesis_rest_tester.domain.schemas import APIAnalysis

# When an operation has no summary, fall back to a truncated description so the
# matcher keeps a semantic hook without pulling in the full prose.
_SUMMARY_FALLBACK_CHARS = 160


def compact_requirements(requirements: list[RequirementItem]) -> list[dict[str, object]]:
    """Requirement identity plus the semantics needed to match and plan against it."""

    return [{"id": item.id, "text": item.text, "role": item.role} for item in requirements]


def _operation_summary(operation: OpenAPIOperation) -> str | None:
    if operation.summary:
        return operation.summary
    if operation.description:
        return operation.description[:_SUMMARY_FALLBACK_CHARS]
    return None


def compact_operations_for_matching(
    operations: list[OpenAPIOperation],
) -> list[dict[str, object]]:
    """Endpoint identity + a short semantic hook + auth, enough to map requirements."""

    return [
        {
            "method": operation.method,
            "path": operation.path,
            "operation_id": operation.operation_id,
            "summary": _operation_summary(operation),
            "auth_required": operation.auth_required,
        }
        for operation in operations
    ]


def compact_api_analysis(api_analysis: APIAnalysis) -> dict[str, object]:
    """API shape for the planner: endpoints, auth, and dependency edges only.

    Drops the verbose per-operation notes and free-text risks; the planner reasons
    from the endpoint list, auth requirements, and dependency edges (for stateful
    tests), plus the requirement it is covering.
    """

    return {
        "summary": api_analysis.summary,
        "operations": [
            {
                "method": operation.method,
                "path": operation.path,
                "auth_required": operation.auth_required,
            }
            for operation in api_analysis.operations
        ],
        "authentication_notes": api_analysis.authentication_notes,
        "dependency_edges": [
            edge.model_dump(mode="json") for edge in api_analysis.dependency_edges
        ],
    }
