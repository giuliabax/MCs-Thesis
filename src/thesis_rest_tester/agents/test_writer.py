"""Test Writer agent: one strategy item in, one executable test case out."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from thesis_rest_tester.agents.base import AgentResponseError, BaseAgent
from thesis_rest_tester.artifacts.writer import ArtifactWriter
from thesis_rest_tester.domain.compact import (
    body_field_spec,
    body_problems,
    compact_operations_for_writing,
)
from thesis_rest_tester.domain.executable import ExecutableTestCase, TestStep
from thesis_rest_tester.domain.models import AgentOutput, OpenAPIOperation, TestStrategyItem
from thesis_rest_tester.llm.base import LLMClient

_logger = logging.getLogger(__name__)


class TestWriterAgent(BaseAgent[ExecutableTestCase]):
    """Translate a single test-strategy item into a validated executable test case.

    One call per item, rather than one per project: the strategy item is designed to be
    a self-contained instruction for one test, a small prompt is markedly more reliable
    on a local model, and a failure costs one test rather than a whole project.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        prompt_path: str | Path,
        artifact_writer: ArtifactWriter,
        temperature: float | None = None,
        max_tokens: int | None = None,
        think: bool = False,
    ) -> None:
        super().__init__(
            name="test_writer",
            prompt_path=prompt_path,
            llm_client=llm_client,
            artifact_writer=artifact_writer,
            response_adapter=TypeAdapter(ExecutableTestCase),
            raw_artifact_name="test_case.raw.txt",
            temperature=temperature,
            max_tokens=max_tokens,
            think=think,
        )

    def run(
        self,
        item: TestStrategyItem,
        operations: list[OpenAPIOperation],
        *,
        artifact_stem: str,
    ) -> tuple[ExecutableTestCase, AgentOutput]:
        self._raw_artifact_name = f"{artifact_stem}.raw.txt"
        payload = {
            "strategy_item": item.model_dump(mode="json"),
            "available_operations": compact_operations_for_writing(operations),
        }
        case, output = self.call_and_validate(
            "Write one executable test for this strategy item, using only the operations "
            "listed. Return only a strict JSON object.\n\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        case = self._reconcile(case, item, operations)
        return case, output.model_copy(update={"parsed_json": case.model_dump(mode="json")})

    def _preprocess_parsed_json(self, parsed: object) -> object:
        """Unwrap the shapes models use instead of a bare object."""

        if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
            return parsed[0]
        if isinstance(parsed, dict):
            for key in ("test_case", "test", "executable_test_case"):
                nested = parsed.get(key)
                if isinstance(nested, dict) and "steps" in nested:
                    return nested
        return parsed

    @staticmethod
    def _reconcile(
        case: ExecutableTestCase,
        item: TestStrategyItem,
        operations: list[OpenAPIOperation],
    ) -> ExecutableTestCase:
        """Align a validated case with the contract and the item it came from.

        Validation proves the case is well-formed; it does not prove the case is
        admissible. A step calling an endpoint the contract does not document would fail
        for a reason that says nothing about the service, so it cannot be executed.

        The three phases are not equivalent, though. In ``setup`` or ``steps`` such a step
        makes the test unrunnable and the case is rejected. In ``cleanup`` it does not: the
        test still exercises exactly what it was asked to, it merely fails to tidy up
        afterwards. Rejecting the case for that would discard a usable test over its
        epilogue -- and it is the common case, because a model that creates a resource
        reasonably tries to delete it even when the contract exposes no deletion.
        """

        documented = {(operation.method, _template(operation.path)) for operation in operations}

        def is_documented(step: TestStep) -> bool:
            return (step.request.method, _template(step.request.path)) in documented

        unknown = sorted(
            {
                f"{step.request.method} {step.request.path}"
                for step in (*case.setup, *case.steps)
                if not is_documented(step)
            }
        )
        if unknown:
            raise AgentResponseError(
                "test writer produced steps calling operations absent from the contract: "
                + ", ".join(unknown)
            )

        kept, dropped = _admissible_cleanup(case, is_documented)
        if dropped:
            _logger.warning(
                "Dropped %d cleanup step(s) from %s: %s",
                len(dropped),
                case.name,
                "; ".join(dropped),
            )
            case = case.model_copy(update={"cleanup": kept})

        missing = case.undefined_placeholders()
        if missing:
            raise AgentResponseError(
                "test writer referenced placeholders that no earlier step captures: "
                + ", ".join(missing)
            )

        body_problems = _body_problems(case, operations)
        if body_problems:
            raise AgentResponseError(
                "test writer produced request bodies that do not satisfy the contract: "
                + "; ".join(body_problems)
            )

        # Traceability is the pipeline's, not the model's, to assign.
        updates: dict[str, Any] = {}
        if case.requirement_id != item.requirement_id:
            updates["requirement_id"] = item.requirement_id
        if case.test_type != item.test_type:
            updates["test_type"] = item.test_type
        if updates:
            _logger.debug("Restoring strategy-item traceability on %s: %s", case.name, updates)
            case = case.model_copy(update=updates)
        return case


def _admissible_cleanup(
    case: ExecutableTestCase,
    is_documented: Callable[[TestStep], bool],
) -> tuple[list[TestStep], list[str]]:
    """Filter teardown down to the steps that can actually run.

    A cleanup step is dropped when it calls an endpoint the contract does not document,
    or when it interpolates a value no surviving earlier step captures -- which happens
    precisely because dropping one step can orphan the next. Walking the phase in order
    and tracking what is still available handles both in a single pass, and keeps the
    failure confined to teardown instead of costing the whole test.
    """

    available = {
        capture.name for step in (*case.setup, *case.steps) for capture in step.captures
    }
    kept: list[TestStep] = []
    dropped: list[str] = []
    for step in case.cleanup:
        target = f"{step.request.method} {step.request.path}"
        if not is_documented(step):
            dropped.append(f"{target} (not in the contract)")
            continue
        required = {
            name
            for name in _placeholder_names(step)
            if not _BUILTIN_PLACEHOLDER.match(name)
        }
        orphaned = sorted(required - available)
        if orphaned:
            dropped.append(f"{target} (needs {', '.join(orphaned)}, no longer captured)")
            continue
        kept.append(step)
        available.update(capture.name for capture in step.captures)
    return kept, dropped


def _placeholder_names(step: TestStep) -> set[str]:
    request = step.request
    fragments = [request.path, *request.query.values(), *request.headers.values()]
    if request.json_body is not None:
        fragments.append(json.dumps(request.json_body, ensure_ascii=False))
    return {name for fragment in fragments for name in _PLACEHOLDER.findall(fragment)}


_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_BUILTIN_PLACEHOLDER = re.compile(r"^(unique|long_\d+)$")
_PLACEHOLDER_ONLY = re.compile(r"^\s*\{\{\s*[A-Za-z_][A-Za-z0-9_]*\s*\}\}\s*$")
# Test types whose whole purpose is to send something the contract does not accept.
_MAY_VIOLATE_SCHEMA = {"negative", "edge_case"}


def _body_problems(case: ExecutableTestCase, operations: list[OpenAPIOperation]) -> list[str]:
    """Check each request body against the fields its operation documents.

    Only the steps that are meant to succeed are checked. A negative or edge-case test
    exists precisely to send a malformed or incomplete body, so enforcing the schema on
    it would reject the very tests the strategy asked for; those steps are exempt. Setup
    steps are always checked, because a setup call is meant to work whatever the test
    type is, and a setup that the service rejects makes the test meaningless.
    """

    documented = {
        (operation.method, _template(operation.path)): operation for operation in operations
    }
    problems: list[str] = []
    checked_phases: list[tuple[str, list[Any]]] = [("setup", case.setup)]
    if case.test_type not in _MAY_VIOLATE_SCHEMA:
        checked_phases.append(("step", case.steps))

    for phase, steps in checked_phases:
        for index, step in enumerate(steps, start=1):
            operation = documented.get((step.request.method, _template(step.request.path)))
            if operation is None:
                continue
            spec = body_field_spec(operation.request_body_schema)
            if spec is None:
                # No inline schema (absent, or an unresolvable $ref): nothing to check
                # against, so no claim is made about this body.
                continue
            problems.extend(
                f"{phase} {index} ({step.request.method} {step.request.path}): {problem}"
                for problem in body_problems(step.request.json_body, spec)
            )
    return problems


def _template(path: str) -> str:
    """Normalize a path so parameter names do not decide whether it is documented.

    ``/reports/{reportId}`` and ``/reports/{id}`` address the same operation; a
    generated test that spells the placeholder differently is still calling a
    documented endpoint.
    """

    result: list[str] = []
    depth = 0
    for character in path:
        if character == "{":
            depth += 1
            if depth == 1:
                result.append("{}")
            continue
        if character == "}":
            depth = max(0, depth - 1)
            continue
        if depth == 0:
            result.append(character)
    return "".join(result).rstrip("/") or "/"
