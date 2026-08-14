"""Turn a pytest run into case records.

Two things happen here, and both are pure functions over strings so they can be tested
without Docker, without a network, and without a language model.

First, pytest's exit code decides whether a JUnit report is even meaningful: a usage
error (4) produces no usable XML at all, and a collection error yields a ``<testcase>``
that corresponds to no generated test. Second, the XML says nothing about requirements or
test types, so each case is joined back onto ``generation_report.json``, which is what
restores traceability from an execution result to the requirement it came from.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from pathlib import Path

from thesis_rest_tester.domain.execution import (
    CaseExecutionRecord,
    FailurePhase,
    ProjectOutcome,
)

# Emitted by generation.renderer for setup-phase assertions; the only marker that
# distinguishes a broken precondition from a broken behaviour under test.
_SETUP_MARKER = "setup step failed:"

# pytest's documented exit codes. Anything else is treated as a runner error.
_EXIT_OUTCOMES: dict[int, ProjectOutcome] = {
    0: "completed",
    1: "completed",  # tests ran and some failed: a normal, informative result
    2: "interrupted",
    3: "runner_error",
    4: "collection_error",
    5: "empty_suite",
}
# Exit codes after which the XML, if present at all, cannot be trusted to describe tests.
_NO_USABLE_REPORT = {3, 4, 5}


@dataclass(frozen=True, slots=True)
class JUnitCase:
    name: str
    classname: str | None
    outcome: str
    duration_seconds: float | None
    message: str | None


def outcome_for_exit_code(exit_code: int) -> ProjectOutcome:
    return _EXIT_OUTCOMES.get(exit_code, "runner_error")


def report_is_usable(exit_code: int) -> bool:
    return exit_code not in _NO_USABLE_REPORT


def parse_junit(xml_text: str) -> list[JUnitCase]:
    """Read pytest's JUnit XML into flat cases.

    Tolerates both layouts pytest has emitted over the years: ``<testsuites>`` wrapping
    ``<testsuite>``, and a bare ``<testsuite>`` root.
    """

    root = ElementTree.fromstring(xml_text)
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    cases: list[JUnitCase] = []
    for suite in suites:
        for element in suite.findall("testcase"):
            outcome, message = _outcome_of(element)
            cases.append(
                JUnitCase(
                    name=element.get("name", ""),
                    classname=element.get("classname") or None,
                    outcome=outcome,
                    duration_seconds=_float_or_none(element.get("time")),
                    message=message,
                )
            )
    return cases


def _outcome_of(element: ElementTree.Element) -> tuple[str, str | None]:
    for tag, outcome in (("failure", "failed"), ("error", "error"), ("skipped", "skipped")):
        found = element.find(tag)
        if found is not None:
            return outcome, (found.get("message") or found.text or "").strip() or None
    return "passed", None


def _float_or_none(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def classify_failure_phase(message: str | None, outcome: str) -> FailurePhase:
    if outcome in {"passed", "skipped"}:
        return "unknown"
    if message and _SETUP_MARKER in message:
        return "setup"
    if outcome == "error":
        # pytest reports collection and fixture problems as errors, not failures.
        return "collection"
    return "step"


def load_generated_cases(generation_report: Path) -> dict[str, dict[str, str | None]]:
    """Map generated test name -> its traceability, from the generation report."""

    if not generation_report.is_file():
        return {}
    payload = json.loads(generation_report.read_text(encoding="utf-8"))
    return {
        case["name"]: {
            "requirement_id": case.get("requirement_id"),
            "test_type": case.get("test_type"),
        }
        for case in payload.get("cases", [])
        if case.get("name")
    }


def join_cases(
    project_name: str,
    generated: dict[str, dict[str, str | None]],
    executed: list[JUnitCase],
) -> list[CaseExecutionRecord]:
    """Reconcile what was generated with what actually ran.

    The generation report is authoritative for *which* tests exist: a generated test with
    no ``<testcase>`` did not run and is recorded as ``not_run`` rather than quietly
    vanishing, which keeps the denominator of every later metric honest. A ``<testcase>``
    matching nothing generated is kept too -- that is what a collection error looks like,
    and dropping it would hide the only evidence of it.
    """

    by_name = {case.name: case for case in executed}
    records: list[CaseExecutionRecord] = []

    for name, traceability in generated.items():
        executed_case = by_name.pop(name, None)
        if executed_case is None:
            records.append(
                CaseExecutionRecord(
                    project_name=project_name,
                    test_name=name,
                    requirement_id=traceability.get("requirement_id"),
                    test_type=traceability.get("test_type"),
                    outcome="not_run",
                )
            )
            continue
        records.append(
            CaseExecutionRecord(
                project_name=project_name,
                test_name=name,
                requirement_id=traceability.get("requirement_id"),
                test_type=traceability.get("test_type"),
                outcome=executed_case.outcome,  # type: ignore[arg-type]
                failure_phase=classify_failure_phase(
                    executed_case.message, executed_case.outcome
                ),
                duration_seconds=executed_case.duration_seconds,
                message=executed_case.message,
            )
        )

    for leftover in by_name.values():
        records.append(
            CaseExecutionRecord(
                project_name=project_name,
                test_name=leftover.name,
                outcome=leftover.outcome,  # type: ignore[arg-type]
                failure_phase=classify_failure_phase(leftover.message, leftover.outcome),
                duration_seconds=leftover.duration_seconds,
                message=leftover.message,
            )
        )
    return records


def all_not_run(
    project_name: str,
    generated: dict[str, dict[str, str | None]],
    message: str | None = None,
) -> list[CaseExecutionRecord]:
    """Every generated test marked as never executed, for a project that could not run."""

    return [
        CaseExecutionRecord(
            project_name=project_name,
            test_name=name,
            requirement_id=traceability.get("requirement_id"),
            test_type=traceability.get("test_type"),
            outcome="not_run",
            message=message,
        )
        for name, traceability in generated.items()
    ]
