"""Executable test-case model: the structured form a generated test takes.

The Test Writer agent does not emit Python. It emits instances of these models, which
are validated against the schema and reconciled against the project's OpenAPI contract
before deterministic code renders them as pytest modules. This keeps the generation
stage inside the same trust boundary as the planning stage: the model proposes a test,
deterministic code decides whether it is admissible and how it is written.

Two placeholder forms may appear inside string fields and are resolved by the renderer,
never by the model:

``{{unique}}``
    Replaced by a value unique to the test invocation, so that repeated runs and
    parallel tests cannot collide on the same resource.
``{{capture_name}}``
    Replaced by a value captured from an earlier step's response, which is how a step
    consumes an identifier or a token produced by the step before it.
"""

from __future__ import annotations

import keyword
import re
from typing import Any, Literal

from pydantic import Field, field_validator

from thesis_rest_tester.domain.models import DomainModel

# A capture name doubles as a Python local, so it must be a plain identifier.
_CAPTURE_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")


class RequestSpec(DomainModel):
    """One HTTP call, expressed against the contract rather than against a URL."""

    method: str
    path: str
    query: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    json_body: dict[str, Any] | list[Any] | None = None

    @field_validator("method")
    @classmethod
    def normalize_method(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith("/"):
            raise ValueError("path must be relative to the SUT base URL and start with '/'")
        return value


class Capture(DomainModel):
    """Bind a value from a response so a later step can use it."""

    name: str
    source: Literal["json", "header"] = "json"
    # Dotted path into the JSON body ("data.id"), or the header name.
    expression: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not _CAPTURE_NAME.fullmatch(value) or keyword.iskeyword(value):
            raise ValueError(
                "capture name must be a lowercase Python identifier and not a keyword"
            )
        return value


class Assertion(DomainModel):
    """A check on a response, beyond its status code."""

    kind: Literal["json_field", "header", "body_contains"]
    # Dotted JSON path or header name; unused by body_contains.
    target: str | None = None
    # ``not_empty`` covers the commonest check on a collection endpoint. Without it the
    # model reaches for JavaScript: an observed test asserted `data.length` greater than
    # zero, which is not a JSON path and silently resolves to None.
    operator: Literal[
        "exists", "not_empty", "equals", "not_equals", "contains", "greater_than"
    ] = "exists"
    value: Any = None


class TestStep(DomainModel):
    """One request together with what is expected and extracted from its response."""

    description: str
    request: RequestSpec
    expect_status: list[int] = Field(default_factory=list)
    captures: list[Capture] = Field(default_factory=list)
    assertions: list[Assertion] = Field(default_factory=list)

    @field_validator("expect_status", mode="before")
    @classmethod
    def normalize_expect_status(cls, value: Any) -> Any:
        """Accept a bare code or strings; models emit `200` and `"200"` freely."""

        if isinstance(value, (int, str)):
            value = [value]
        if isinstance(value, list):
            return [int(str(item).strip()) for item in value if str(item).strip()]
        return value


class ExecutableTestCase(DomainModel):
    """A single generated test, ready to be rendered as one pytest function."""

    name: str
    requirement_id: str
    test_type: Literal["happy_path", "edge_case", "negative", "stateful", "cleanup"]
    description: str
    # Steps that establish preconditions; failures here are setup errors, not test
    # failures, and the renderer reports them as such.
    setup: list[TestStep] = Field(default_factory=list)
    # The steps under test. At least one is required: a test that asserts nothing
    # about the service is not a test.
    steps: list[TestStep] = Field(min_length=1)
    # Teardown steps, rendered inside a finally block so they run even on failure.
    cleanup: list[TestStep] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith("test_"):
            value = f"test_{value}"
        value = re.sub(r"[^0-9a-zA-Z_]+", "_", value).strip("_").lower()
        if not value.startswith("test_"):
            value = f"test_{value}"
        if keyword.iskeyword(value) or not value.isidentifier():
            raise ValueError(f"cannot derive a valid test function name from {value!r}")
        return value

    @property
    def all_steps(self) -> list[TestStep]:
        return [*self.setup, *self.steps, *self.cleanup]

    def undefined_placeholders(self) -> list[str]:
        """Placeholders referenced before anything captures them.

        A step that interpolates a name no earlier step produced would render to a
        NameError at import time, so this is checked before rendering rather than
        discovered when the suite is executed.
        """

        available: set[str] = set()
        missing: list[str] = []
        for step in self.all_steps:
            for name in _placeholders_in(step.request):
                if name != "unique" and name not in available:
                    missing.append(name)
            available.update(capture.name for capture in step.captures)
        return sorted(dict.fromkeys(missing))


def _placeholders_in(request: RequestSpec) -> list[str]:
    fragments = [request.path, *request.query.values(), *request.headers.values()]
    if request.json_body is not None:
        fragments.append(_stringify(request.json_body))
    return [name for fragment in fragments for name in _PLACEHOLDER.findall(fragment)]


def _stringify(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_stringify(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_stringify(item) for item in value)
    return str(value)
