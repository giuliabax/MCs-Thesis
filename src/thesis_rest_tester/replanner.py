"""Re-plan the test strategy of a completed run without re-deciding its coverage.

The number of tests a project gets is capped by ``budget.max_tests_per_iteration``, and
that cap is applied by the strategy planner. Raising it therefore requires planning again
-- but not analysing the requirements again, and above all not matching them against the
API again.

That distinction is what this module exists for. The matcher runs before the planner and
does not read the budget, so its coverage decisions are unaffected by the cap. Re-running
a whole planning run to change the cap would produce fresh, stochastically different
coverage and invalidate the measurement the run was selected on. Re-planning reuses the
coverage verbatim and changes only what the cap actually governs.

The result is written to a new directory rather than in place: the source run is a
documented artifact, and a derived run keeps the provenance visible, as
``scripts/consolidate_runs.py`` does for its own output.
"""

from __future__ import annotations

import json
import logging
import math
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from thesis_rest_tester.agents import TestStrategyPlannerAgent
from thesis_rest_tester.artifacts.writer import ArtifactWriter
from thesis_rest_tester.config import AppConfig, BudgetConfig, load_config
from thesis_rest_tester.domain.coverage import ProjectRequirementCoverage
from thesis_rest_tester.domain.models import OpenAPIOperation, TestStrategyItem, WorkflowPlan
from thesis_rest_tester.domain.schemas import APIAnalysis, RequirementsAnalysis
from thesis_rest_tester.llm import LMStudioLLMClient
from thesis_rest_tester.llm.base import LLMClient
from thesis_rest_tester.orchestrator import Orchestrator

_logger = logging.getLogger(__name__)

# Copied verbatim from the source run: these are the coverage decisions the new plan must
# not disturb, plus the inputs needed to rebuild a workflow plan around them.
_CARRIED_ARTIFACTS = (
    "openapi_operations.json",
    "api_analysis.json",
    "api_analysis.raw.txt",
    "requirement_coverage.json",
    "requirement_coverage.raw.txt",
)


@dataclass(frozen=True, slots=True)
class ReplannedProject:
    project_name: str
    items_before: int
    items_after: int
    requirements_covered: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ReplanResult:
    source_run: Path
    output_run: Path
    projects: list[ReplannedProject]

    @property
    def total_before(self) -> int:
        return sum(project.items_before for project in self.projects)

    @property
    def total_after(self) -> int:
        return sum(project.items_after for project in self.projects)


class Replanner:
    def __init__(
        self,
        source_run: str | Path,
        config: AppConfig,
        *,
        llm_client: LLMClient | None = None,
        prompt_root: str | Path | None = None,
    ) -> None:
        self._source = Path(source_run)
        self._config = config
        self._injected_client = llm_client
        repository_root = Path(__file__).resolve().parents[2]
        self._prompt_root = (
            Path(prompt_root) if prompt_root is not None else repository_root / "prompts"
        )

    def run(
        self,
        *,
        only: list[str] | None = None,
        output_run: Path | None = None,
        requirements: dict[str, list[str]] | None = None,
        planning_notes: dict[str, str] | None = None,
    ) -> ReplanResult:
        """Plan the strategy again, for whole projects or for named requirements.

        ``requirements`` narrows the work per project: only the strategy items belonging
        to those requirement ids are planned again, and the rest of the project's strategy
        is kept as it was. This is what a feedback iteration needs -- replanning a whole
        project to repair two requirements would churn the items that were working, and
        any change in the totals could then be attributed to the churn as easily as to the
        feedback.

        ``output_run`` may be the source run itself, which is how the loop advances a run
        in place rather than accumulating a directory per iteration.
        """

        source_projects = self._source / "projects"
        if not source_projects.is_dir():
            raise FileNotFoundError(f"No projects directory inside {self._source}")

        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        destination = output_run or self._source.parent / f"{stamp}-replanned"
        writer = ArtifactWriter(destination)
        in_place = destination.resolve() == self._source.resolve()
        if not in_place:
            self._carry_run_artifacts(destination)

        requirements_analysis = RequirementsAnalysis.model_validate_json(
            (self._source / "requirements_analysis.json").read_text(encoding="utf-8")
        )
        client = self._injected_client or self._build_client()
        base_urls = {
            project.name: project.sut_base_url
            for project in self._config.inputs.configured_projects(self._config.project_name)
        }

        results: list[ReplannedProject] = []
        for project_dir in sorted(source_projects.iterdir()):
            if not project_dir.is_dir() or not (project_dir / "workflow_plan.json").is_file():
                continue
            if only and project_dir.name not in only:
                continue
            results.append(
                self._replan_project(
                    project_dir,
                    destination / "projects" / project_dir.name,
                    requirements_analysis,
                    client,
                    base_urls.get(project_dir.name),
                    destination.name,
                    only_requirements=(requirements or {}).get(project_dir.name),
                    correction=(planning_notes or {}).get(project_dir.name),
                    in_place=in_place,
                )
            )

        writer.write_json(
            "replan.json",
            {
                "source_run": str(self._source),
                "created_at": datetime.now(UTC).isoformat(),
                "max_tests_per_iteration": self._config.budget.max_tests_per_iteration,
                "batch_strategy_planner": self._config.llm.batch_strategy_planner,
                "note": (
                    "Requirement coverage is carried over unchanged from the source run; "
                    "only the test strategy was planned again."
                ),
                "projects": [
                    {
                        "project_name": item.project_name,
                        "items_before": item.items_before,
                        "items_after": item.items_after,
                        "requirements_covered": item.requirements_covered,
                        "error": item.error,
                    }
                    for item in results
                ],
            },
        )
        return ReplanResult(self._source, destination, results)

    def _replan_project(
        self,
        source_dir: Path,
        destination_dir: Path,
        requirements: RequirementsAnalysis,
        client: LLMClient,
        sut_base_url: str | None,
        run_id: str,
        *,
        only_requirements: list[str] | None = None,
        correction: str | None = None,
        in_place: bool = False,
    ) -> ReplannedProject:
        name = source_dir.name
        writer = ArtifactWriter(destination_dir)
        if not in_place:
            for artifact in _CARRIED_ARTIFACTS:
                source_file = source_dir / artifact
                if source_file.is_file():
                    shutil.copyfile(source_file, destination_dir / artifact)

        previous = json.loads((source_dir / "test_strategy.json").read_text(encoding="utf-8"))
        api = APIAnalysis.model_validate_json(
            (source_dir / "api_analysis.json").read_text(encoding="utf-8")
        )
        coverage = ProjectRequirementCoverage.model_validate_json(
            (source_dir / "requirement_coverage.json").read_text(encoding="utf-8")
        )
        operations = [
            OpenAPIOperation.model_validate(entry)
            for entry in json.loads(
                (source_dir / "openapi_operations.json").read_text(encoding="utf-8")
            )
        ]
        scoped_requirements, scoped_api, scoped_operations = Orchestrator._project_scope(
            requirements, api, operations, coverage
        )

        # A targeted replan narrows the planner to the requirements that failed, and keeps
        # every other strategy item exactly as it was. Replanning the whole project would
        # churn items that were working, and any change in the totals could then be read
        # as the churn rather than as the repair.
        kept: list[TestStrategyItem] = []
        budget = self._config.budget
        if only_requirements:
            wanted = set(only_requirements)
            eligible = len(scoped_requirements.requirements) or 1
            scoped_requirements = scoped_requirements.model_copy(
                update={
                    "requirements": [
                        requirement
                        for requirement in scoped_requirements.requirements
                        if requirement.id in wanted
                    ]
                }
            )
            # Scale the budget to the share of the project being replanned. The budget is
            # a whole-project figure, and handing all of it to a narrowed set asks for as
            # many tests for two requirements as the project was meant to have in total:
            # one observed replan of two requirements took a project from 12 strategy
            # items to 54. The same reasoning as the planner's own per-batch share.
            share = math.ceil(
                budget.max_tests_per_iteration * len(scoped_requirements.requirements) / eligible
            )
            budget = budget.model_copy(
                update={
                    "max_tests_per_iteration": max(
                        3, min(budget.max_tests_per_iteration, share)
                    )
                }
            )
            kept = [
                TestStrategyItem.model_validate(entry)
                for entry in previous
                if entry.get("requirement_id") not in wanted
            ]
            _logger.info(
                "%s: replanning %d requirement(s), keeping %d existing item(s)",
                name,
                len(scoped_requirements.requirements),
                len(kept),
            )

        items: list[TestStrategyItem] = []
        error: str | None = None
        if scoped_requirements.requirements and scoped_operations:
            agent = TestStrategyPlannerAgent(
                prompt_path=self._prompt_root / "planning/test_strategy_planner.md",
                llm_client=client,
                artifact_writer=writer,
                temperature=self._config.llm.temperature,
                max_tokens=self._config.llm.max_tokens_for("test_strategy_planner"),
                batch_by_requirement=self._config.llm.batch_strategy_planner,
                think="test_strategy_planner" in self._config.llm.reasoning_agents,
            )
            try:
                items, _ = agent.run(
                    scoped_requirements,
                    scoped_api,
                    scoped_operations,
                    budget,
                    coverage,
                    correction=correction,
                )
            except Exception as exc:  # noqa: BLE001 - one project must not fail the batch
                error = f"{type(exc).__name__}: {exc}"
                _logger.warning("%s could not be re-planned: %s", name, error)

        # The repaired items join the ones that were left alone. On a whole-project
        # replan `kept` is empty and this is simply the new strategy.
        items = [*kept, *items]

        writer.write_json(
            "test_strategy.json", [item.model_dump(mode="json") for item in items]
        )
        plan = WorkflowPlan(
            run_id=run_id,
            project_name=name,
            requirements_summary=scoped_requirements.model_dump(mode="json"),
            api_summary=api.model_dump(mode="json"),
            strategy_items=items,
            assumptions=requirements.assumptions,
            risks=[
                *api.risks,
                "Requirement coverage is carried over from the source run and was not "
                "re-decided; only the test strategy was planned again.",
            ],
            created_at=datetime.now(UTC),
            sut_base_url=sut_base_url,
            requirement_coverage=coverage.model_dump(mode="json"),
        )
        writer.write_json("workflow_plan.json", plan)

        _logger.info(
            "%s: %d -> %d strategy items", name, len(previous), len(items)
        )
        return ReplannedProject(
            project_name=name,
            items_before=len(previous),
            items_after=len(items),
            requirements_covered=len({item.requirement_id for item in items}),
            error=error,
        )

    def _carry_run_artifacts(self, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        for artifact in (
            "requirements_analysis.json",
            "requirements_analysis.raw.txt",
            "requirements_compact.txt",
            "config.resolved.yaml",
            "requirement_coverage_matrix.json",
            "requirement_coverage_matrix.csv",
            "coverage_evaluation.json",
            "coverage_evaluation.csv",
            "coverage_evaluation.md",
        ):
            source_file = self._source / artifact
            if source_file.is_file():
                shutil.copyfile(source_file, destination / artifact)

    def _build_client(self) -> LLMClient:
        override = self._config.llm.overrides.get("test_strategy_planner")
        model = (
            override.model
            if override is not None and override.reroutes
            else self._config.llm.model
        )
        return LMStudioLLMClient(
            model=model,
            base_url=self._config.llm.base_url,
            default_temperature=self._config.llm.temperature,
            default_max_tokens=self._config.llm.max_tokens_for("test_strategy_planner"),
            timeout=self._config.llm.timeout_seconds,
        )


def replan_run(
    source_run: str | Path,
    config_path: str | Path,
    *,
    projects: list[str] | None = None,
    max_tests: int | None = None,
    llm_client: LLMClient | None = None,
) -> ReplanResult:
    """Entry point used by the CLI."""

    config = load_config(config_path)
    if max_tests is not None:
        budget = BudgetConfig(
            max_iterations=config.budget.max_iterations,
            max_tests_per_iteration=max_tests,
            max_llm_calls=config.budget.max_llm_calls,
        )
        config = config.model_copy(update={"budget": budget})
    return Replanner(source_run, config, llm_client=llm_client).run(only=projects)
