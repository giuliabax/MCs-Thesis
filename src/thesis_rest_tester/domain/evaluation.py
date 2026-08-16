"""What the Evaluator concludes from an execution, and what it hands back.

The Evaluator is the only stage that reads execution evidence and says what should happen
next, and it does so by returning these records and nothing else. It never calls the
planner or the generator: the Orchestrator reads the report and decides. Keeping the
decision here and the action there is what makes a feedback iteration testable on a
fixture report, with no model and no containers.

A diagnosis answers one question -- *whose* mistake was this? -- and the answer decides
which stage runs again:

``generation``
    The plan was sound and the written test was not: it skipped the login the operation
    requires, called a documented operation the strategy never named, or asserted that a
    collection was populated without creating anything. Regenerating the test is enough.
``planning``
    The test faithfully executed a strategy item that could not have worked: a
    requirement mapped to an operation that cannot serve it, or expected status codes the
    contract contradicts. Rewriting the test would reproduce the same failure, so the
    project must be replanned first.
``environment``
    Neither. Something the system under test depends on is absent here -- a mail server,
    object storage, a cloud database. No amount of replanning or regeneration fixes it,
    so it is excluded from the feedback loop and reported separately.
``sut_defect``
    The test is right and the service is wrong. This is the finding the whole pipeline
    exists to produce, and it must never be fed back as something to repair.
``unknown``
    Unmatched by any rule. Reported as its own category rather than folded into the
    nearest bucket, because a silently misfiled diagnosis would send a healthy project
    round the loop again for nothing.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from thesis_rest_tester.domain.models import DomainModel, MetricSnapshot

# Ordered from "we can fix this" to "we should not try to".
DiagnosisCause = Literal["generation", "planning", "environment", "sut_defect", "unknown"]

# Causes the feedback loop can act on. The other three are reported and left alone.
ACTIONABLE_CAUSES: frozenset[str] = frozenset({"generation", "planning"})


class TestDiagnosis(DomainModel):
    """Why one test failed, and what would have to change for it to pass."""

    test_name: str
    requirement_id: str | None = None
    cause: DiagnosisCause
    # The rule that fired, named so a reader can audit the classification rather than
    # having to trust it.
    rule: str
    # The recorded facts the rule matched on: status codes, the endpoint called, the
    # server's own message. Quoted rather than summarised, so the evidence survives.
    evidence: list[str] = Field(default_factory=list)
    suggestion: str | None = None


class ProjectEvaluation(DomainModel):
    """One project's metrics and diagnoses, plus what the Orchestrator should re-run."""

    project_name: str
    metrics: MetricSnapshot
    diagnoses: list[TestDiagnosis] = Field(default_factory=list)
    # Requirement ids whose strategy items must be planned again before any test for them
    # can succeed.
    replan_requirements: list[str] = Field(default_factory=list)
    # Strategy item ids whose tests should simply be written again.
    regenerate_items: list[str] = Field(default_factory=list)
    # Set when the project produced no usable evidence at all -- it never started, or its
    # suite could not be collected -- so the loop can tell "nothing to learn from" apart
    # from "learned that everything failed".
    inconclusive_reason: str | None = None

    @property
    def cause_counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for diagnosis in self.diagnoses:
            tally[diagnosis.cause] = tally.get(diagnosis.cause, 0) + 1
        return tally

    @property
    def is_actionable(self) -> bool:
        """Whether another iteration could plausibly improve this project."""

        return bool(self.replan_requirements or self.regenerate_items)


class EvaluationReport(DomainModel):
    """Run-level roll-up, keyed by project like ExecutionReport."""

    run_id: str
    iteration: int = Field(ge=1)
    projects: dict[str, ProjectEvaluation] = Field(default_factory=dict)

    @property
    def actionable_projects(self) -> list[str]:
        return [name for name, project in self.projects.items() if project.is_actionable]

    def improved_over(self, previous: EvaluationReport | None) -> bool:
        """Whether any project's pass rate rose since the previous iteration.

        The loop's stopping condition. A project that appears for the first time counts
        as improvement only if it actually passed something, so a newly runnable project
        stuck at zero does not keep the loop going on its own.
        """

        if previous is None:
            return True
        for name, project in self.projects.items():
            current = project.metrics.pass_rate
            if current is None:
                continue
            earlier = previous.projects.get(name)
            baseline = earlier.metrics.pass_rate if earlier is not None else None
            if baseline is None:
                if current > 0.0:
                    return True
                continue
            if current > baseline:
                return True
        return False
