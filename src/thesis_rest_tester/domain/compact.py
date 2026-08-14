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


def compact_operations_for_writing(
    operations: list[OpenAPIOperation],
) -> list[dict[str, object]]:
    """Endpoint identity plus the request shape a test must actually satisfy.

    The matcher only needs to recognize an endpoint; a test writer has to populate it,
    so this projection additionally carries the parameters and the request-body fields.
    Property schemas are flattened to their type name: a writer needs to know that
    ``username`` is a string and is required, not the full JSON Schema around it.
    """

    projected: list[dict[str, object]] = []
    for operation in operations:
        entry: dict[str, object] = {
            "method": operation.method,
            "path": operation.path,
            "summary": _operation_summary(operation),
            "auth_required": operation.auth_required,
            "response_codes": operation.response_codes,
        }
        parameters = [
            {
                "name": parameter.get("name"),
                "in": parameter.get("in"),
                "required": bool(parameter.get("required", parameter.get("in") == "path")),
                "type": _parameter_type(parameter),
            }
            for parameter in operation.parameters
            if parameter.get("name")
        ]
        if parameters:
            entry["parameters"] = parameters
        body = body_field_spec(operation.request_body_schema)
        if body is not None:
            entry["request_body"] = body
        projected.append(entry)
    return projected


def body_field_spec(schema: dict[str, object] | None) -> dict[str, object] | None:
    """Flatten a request-body schema to required names and field types.

    Returns None when the schema is absent or is a bare ``$ref``: the reconstructed
    contracts do not carry the components section, so a reference cannot be resolved
    and no claim about the body's fields can honestly be made.
    """

    if not isinstance(schema, dict):
        return None
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return None
    required = schema.get("required")
    return {
        "required": [str(name) for name in required] if isinstance(required, list) else [],
        "properties": {
            str(name): _schema_type(value) for name, value in properties.items()
        },
    }


def _schema_type(value: object) -> str:
    """Name a field's type, or admit that it is unknown.

    A bare ``$ref`` must stay unknown rather than being guessed as an object. The
    reconstructed contracts carry no components section, so the reference cannot be
    resolved, and referenced schemas are frequently string enums: one project spells its
    report status inline as ``type: string, enum: [...]`` while another hides the same
    concept behind ``$ref: '#/components/schemas/ReportStatus'``. Calling that an object
    rejects the correct string value. This mirrors what ``body_field_spec`` already does
    one level up -- no claim is better than a wrong one.
    """

    if isinstance(value, dict):
        declared = value.get("type")
        if isinstance(declared, str):
            return declared
        if "properties" in value:
            return "object"
    return "unknown"


def _parameter_type(parameter: dict[str, object]) -> str:
    schema = parameter.get("schema")
    if isinstance(schema, dict):
        return _schema_type(schema)
    declared = parameter.get("type")
    return declared if isinstance(declared, str) else "unknown"


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
