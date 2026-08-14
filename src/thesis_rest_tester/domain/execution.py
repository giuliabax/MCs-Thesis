"""Persisted records of what happened when a generated suite was executed.

These are the artifacts the metric collectors in ``evaluation.metrics`` consume.
``MetricInputs.execution_records`` is typed only as ``list[dict[str, Any]]`` and cannot
say what it expects, so the contract is stated here instead: it is the flattened list of
``CaseExecutionRecord.model_dump(mode="json")`` across every project of a run, each
carrying its own ``project_name``.

Two record levels exist because they answer different questions. A case record says
whether a *test* passed, which yields pass rate and execution success rate. An exchange
record says which *endpoint* was called and what it answered, which is the only thing
that can yield operation coverage, status-code coverage and server-error counts --
properties of the HTTP traffic that no test verdict preserves.

The classes are deliberately not named ``Test...``: pytest collects ``Test``-prefixed
classes, and importing one into a test module raises ``PytestCollectionWarning``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from thesis_rest_tester.domain.models import DomainModel

CaseOutcome = Literal["passed", "failed", "error", "skipped", "not_run"]
FailurePhase = Literal["setup", "step", "cleanup", "collection", "unknown"]
ProjectOutcome = Literal[
    "completed",
    "not_run",
    "startup_failed",
    "image_unavailable",
    "collection_error",
    "runner_error",
    "empty_suite",
    "suite_timeout",
    "interrupted",
]
Provenance = Literal["original", "env_supplied", "compose_extended", "adapted"]


class CaseExecutionRecord(DomainModel):
    """The outcome of one generated test."""

    project_name: str
    test_name: str
    requirement_id: str | None = None
    test_type: str | None = None
    outcome: CaseOutcome
    # Which phase of the test failed. Recoverable from the assertion message because the
    # renderer prefixes setup assertions with a fixed marker, and worth recovering: a
    # broken precondition and a broken behaviour under test are different findings.
    failure_phase: FailurePhase = "unknown"
    duration_seconds: float | None = Field(default=None, ge=0.0)
    message: str | None = None


class HttpExchangeRecord(DomainModel):
    """One HTTP request a test made, and what came back."""

    project_name: str
    test_name: str | None = None
    method: str
    # Path relative to the SUT base URL, so exchanges join onto OpenAPI operations.
    path: str
    status_code: int | None = None
    duration_seconds: float | None = Field(default=None, ge=0.0)
    error: str | None = None


class PhaseTiming(DomainModel):
    """How long one stage of a project's execution took, and whether it succeeded."""

    name: Literal["build", "up", "ready", "suite", "down"]
    started_at: datetime | None = None
    duration_seconds: float | None = Field(default=None, ge=0.0)
    ok: bool = True
    detail: str | None = None


class ProjectExecutionRecord(DomainModel):
    """Everything observed for one project, including why it did not run."""

    project_name: str
    outcome: ProjectOutcome
    provenance: Provenance = "original"
    provenance_notes: str | None = None
    blocker: str | None = None
    reason: str | None = None
    base_url: str | None = None
    # False when every probed documented operation answered 404, which almost always
    # means the base URL is missing the application's path prefix rather than that the
    # service is broken.
    base_url_plausible: bool | None = None
    compose_files: list[str] = Field(default_factory=list)
    phases: list[PhaseTiming] = Field(default_factory=list)
    pytest_exit_code: int | None = None
    cases: list[CaseExecutionRecord] = Field(default_factory=list)
    exchanges_recorded: int = Field(default=0, ge=0)

    @property
    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for case in self.cases:
            tally[case.outcome] = tally.get(case.outcome, 0) + 1
        return tally


class ExecutionReport(DomainModel):
    """Run-level roll-up, keyed by project like CoverageEvaluationReport."""

    run_id: str
    manifest_path: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    projects: dict[str, ProjectExecutionRecord] = Field(default_factory=dict)

    @property
    def execution_records(self) -> list[dict[str, object]]:
        """The flattened case records, in the shape ``MetricInputs`` expects."""

        return [
            case.model_dump(mode="json")
            for project in self.projects.values()
            for case in project.cases
        ]
