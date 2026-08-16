"""Compact projections of the intermediate planning representations.

A local model has a small context window, so downstream agents consume these
token-lean projections instead of the full analysis objects. Only the fields each
agent needs for its task are kept; per-endpoint detail (full schema, parameters) is
looked up on demand from the original operations when an agent works on a single
endpoint. The deterministic post-processing still runs on the full objects, so
compaction only shrinks the LLM prompt.
"""

from __future__ import annotations

import re

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


# A value that is entirely a placeholder: its type is decided at run time by a capture
# or by the unique-value generator, so nothing can be concluded about it statically.
_PLACEHOLDER_ONLY = re.compile(r"^\s*\{\{\s*[A-Za-z_][A-Za-z0-9_]*\s*\}\}\s*$")


def body_problems(body: object, spec: dict[str, object]) -> list[str]:
    """Ways in which a request body fails the fields its operation documents.

    Shared by the Generator, which uses it to reject a test before it is written, and by
    the Evaluator, which uses it to answer the opposite question: when the service
    rejected a body, had that body in fact satisfied the contract? A body that conforms
    and is rejected anyway is a disagreement between an application and its own
    documentation, not a fault in the test -- and attributing it to the generator would
    send the feedback loop rewriting tests that were already correct.
    """

    required = [str(name) for name in spec.get("required", []) or []]
    properties: dict[str, str] = spec.get("properties", {}) or {}  # type: ignore[assignment]

    if body is None:
        return [f"no body sent, but {', '.join(required)} required"] if required else []
    if not isinstance(body, dict):
        return ["body must be a JSON object"]

    problems = [f"missing required field '{name}'" for name in required if name not in body]
    problems.extend(
        f"field '{name}' is not documented for this operation"
        for name in body
        if name not in properties
    )
    problems.extend(
        problem
        for name, value in body.items()
        if name in properties
        for problem in _field_type_problem(name, value, properties[name])
    )
    return problems


def _field_type_problem(name: str, value: object, declared: str) -> list[str]:
    """Flag a literal value whose type contradicts the contract."""

    if isinstance(value, str) and _PLACEHOLDER_ONLY.match(value):
        return []
    expected: dict[str, tuple[type, ...]] = {
        "string": (str,),
        "integer": (int,),
        "number": (int, float),
        "boolean": (bool,),
        "array": (list,),
        "object": (dict,),
    }
    allowed = expected.get(declared)
    if allowed is None or value is None:
        return []
    # bool is an int subclass; an integer field must not silently accept True.
    if declared in {"integer", "number"} and isinstance(value, bool):
        return [f"field '{name}' should be {declared}, got boolean"]
    if isinstance(value, allowed):
        return []
    # A string carrying a placeholder still renders to a string, which is fine for a
    # string field but says nothing useful for a numeric one.
    if isinstance(value, str) and "{{" in value and declared != "string":
        return []
    return [f"field '{name}' should be {declared}, got {type(value).__name__}"]
