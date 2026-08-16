"""The feedback loop: repair every project, execute every project, evaluate every project.

The Evaluator decides what is wrong and the Orchestrator decides what to do about it, and
this module is where that second decision lives. It calls no model directly except through
the agents, and it holds no opinion about causes: everything it acts on comes from an
``EvaluationReport``.

The phases run in blocks rather than per project, which is a deliberate shape. Repairing
project 1, executing it, evaluating it, then moving to project 2 would interleave model
calls with container start-ups for hours, and -- more importantly -- would measure each
project at a different point in the loop. Repairing all, then executing all, then
evaluating all keeps every project on the same iteration, which is what makes an aggregate
across iterations mean anything.

Within the repair block the order per project is fixed: plan first, then generate. A
planning fault makes the instruction wrong, so regenerating against the old instruction
would faithfully reproduce the failure; the strategy has to be repaired before anything is
written from it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from thesis_rest_tester.agents.feedback_manager import FeedbackManagerAgent, FeedbackNotes
from thesis_rest_tester.artifacts.writer import ArtifactWriter
from thesis_rest_tester.config import AppConfig, load_config
from thesis_rest_tester.domain.evaluation import EvaluationReport, ProjectEvaluation
from thesis_rest_tester.evaluation.evaluator import evaluate_run
from thesis_rest_tester.execution.executor import execute_suites
from thesis_rest_tester.generation.generator import SuiteGenerator
from thesis_rest_tester.llm import LMStudioLLMClient
from thesis_rest_tester.llm.base import LLMClient
from thesis_rest_tester.llm.usage import UsageRecorder
from thesis_rest_tester.replanner import Replanner

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IterationOutcome:
    """What one pass around the loop changed and what it achieved."""

    iteration: int
    replanned: list[str] = field(default_factory=list)
    regenerated: dict[str, int] = field(default_factory=dict)
    evaluation: EvaluationReport | None = None
    stopped_because: str | None = None

    @property
    def mean_pass_rate(self) -> float | None:
        if self.evaluation is None:
            return None
        rates = [
            project.metrics.pass_rate
            for project in self.evaluation.projects.values()
            if project.metrics.pass_rate is not None
        ]
        return sum(rates) / len(rates) if rates else None


@dataclass(frozen=True, slots=True)
class LoopResult:
    run_dir: Path
    iterations: list[IterationOutcome]

    @property
    def stopped_because(self) -> str:
        return self.iterations[-1].stopped_because or "reached the iteration limit"


class FeedbackLoop:
    """Drive repair, execution and evaluation until the suite stops improving."""

    def __init__(
        self,
        run_dir: str | Path,
        config: AppConfig,
        llm_client: LLMClient,
        *,
        prompt_root: str | Path | None = None,
        max_iterations: int = 3,
        use_docker: bool = True,
        reset_state: bool = True,
        attempt_unverified: bool = True,
    ) -> None:
        self._run_dir = Path(run_dir)
        self._config = config
        self._llm_client = llm_client
        repository_root = Path(__file__).resolve().parents[2]
        self._prompt_root = (
            Path(prompt_root) if prompt_root is not None else repository_root / "prompts"
        )
        self._max_iterations = max_iterations
        self._use_docker = use_docker
        self._reset_state = reset_state
        self._attempt_unverified = attempt_unverified

    def run(self, *, baseline: EvaluationReport | None = None) -> LoopResult:
        """Iterate from an existing evaluation until nothing improves.

        ``baseline`` is the evaluation of the run as it already stands -- normally the
        first execution, which is the non-iterative configuration every later iteration is
        compared against. It is read from disk when not supplied.
        """

        previous = baseline or self._baseline()
        outcomes: list[IterationOutcome] = [IterationOutcome(iteration=1, evaluation=previous)]

        for iteration in range(2, self._max_iterations + 1):
            actionable = previous.actionable_projects
            if not actionable:
                outcomes.append(
                    IterationOutcome(
                        iteration=iteration,
                        stopped_because="no project had a repairable failure left",
                    )
                )
                break

            _logger.info(
                "Iteration %d: repairing %d project(s)", iteration, len(actionable)
            )
            replanned, regenerated = self._repair(previous, actionable, iteration)

            _logger.info("Iteration %d: executing every project", iteration)
            execute_suites(
                self._run_dir,
                config_path=None,
                projects=None,
                use_docker=self._use_docker,
                reset_state=self._reset_state,
                attempt_unverified=self._attempt_unverified,
            )

            current = evaluate_run(self._run_dir, iteration=iteration)
            improved = current.improved_over(previous)
            outcomes.append(
                IterationOutcome(
                    iteration=iteration,
                    replanned=replanned,
                    regenerated=regenerated,
                    evaluation=current,
                    stopped_because=None if improved else "no project improved",
                )
            )
            previous = current
            if not improved:
                _logger.info("Iteration %d improved nothing; stopping", iteration)
                break

        self._write_summary(outcomes)
        return LoopResult(self._run_dir, outcomes)

    def _baseline(self) -> EvaluationReport:
        """The first iteration's evaluation, read rather than recomputed when it exists.

        This is the non-iterative baseline the whole comparison rests on, and recomputing
        it would destroy it. After one pass of the loop the artifacts on disk describe the
        *repaired* suites, so evaluating them again and writing the result as iteration 1
        would overwrite the record of what the pipeline achieved without feedback -- and
        would do so silently, leaving a run whose iterations all look alike.
        """

        existing = self._run_dir / "evaluation_report.iteration1.json"
        if existing.is_file():
            _logger.info("Using the recorded iteration-1 evaluation as the baseline")
            return EvaluationReport.model_validate_json(existing.read_text(encoding="utf-8"))
        return evaluate_run(self._run_dir, iteration=1)

    # --- the repair block --------------------------------------------------------------

    def _repair(
        self,
        evaluation: EvaluationReport,
        actionable: list[str],
        iteration: int,
    ) -> tuple[list[str], dict[str, int]]:
        notes_by_project: dict[str, FeedbackNotes] = {}
        for name in actionable:
            notes = self._write_feedback(evaluation.projects[name], iteration)
            if notes is not None:
                notes_by_project[name] = notes

        # Plan first. A planning fault makes the instruction wrong, and a test regenerated
        # against an unrepaired instruction reproduces the failure faithfully.
        replan_projects = [
            name for name in actionable if evaluation.projects[name].replan_requirements
        ]
        if replan_projects:
            self._replan(replan_projects, evaluation, notes_by_project)

        regenerate = {
            name: evaluation.projects[name].regenerate_items
            for name in actionable
            if evaluation.projects[name].regenerate_items
        }
        corrections = {
            name: {entry.item: entry.note for entry in notes.generation_notes}
            for name, notes in notes_by_project.items()
        }
        if regenerate:
            SuiteGenerator(
                self._run_dir,
                self._llm_client,
                self._config,
                prompt_root=self._prompt_root,
                regenerate=regenerate,
                corrections=corrections,
            ).run(only=list(regenerate))

        return replan_projects, {name: len(items) for name, items in regenerate.items()}

    def _write_feedback(
        self, evaluation: ProjectEvaluation, iteration: int
    ) -> FeedbackNotes | None:
        """Ask the model what to say about this project's repairable failures.

        A failure here costs this project its corrections, not the iteration: the repair
        still runs, just without a note, which is the same as the first attempt rather
        than worse than it.
        """

        agent = FeedbackManagerAgent(
            llm_client=self._llm_client,
            prompt_path=self._prompt_root / "evaluation/feedback_manager.md",
            artifact_writer=ArtifactWriter(
                self._run_dir / "projects" / evaluation.project_name / "feedback"
            ),
            temperature=self._config.llm.temperature,
            max_tokens=self._config.llm.max_tokens_for("feedback_manager"),
            think="feedback_manager" in self._config.llm.reasoning_agents,
        )
        try:
            notes, _ = agent.run(evaluation)
        except Exception as exc:  # noqa: BLE001 - one project must not stop the loop
            _logger.warning(
                "No feedback written for %s at iteration %d: %s",
                evaluation.project_name,
                iteration,
                exc,
            )
            return None
        return notes

    def _replan(
        self,
        projects: list[str],
        evaluation: EvaluationReport,
        notes: dict[str, FeedbackNotes],
    ) -> None:
        Replanner(
            self._run_dir,
            self._config,
            llm_client=self._llm_client,
            prompt_root=self._prompt_root,
        ).run(
            only=projects,
            output_run=self._run_dir,
            requirements={
                name: evaluation.projects[name].replan_requirements for name in projects
            },
            planning_notes={
                name: note.planning_note for name, note in notes.items() if note.planning_note
            },
        )

    # --- reporting ---------------------------------------------------------------------

    def _write_summary(self, outcomes: list[IterationOutcome]) -> None:
        writer = ArtifactWriter(self._run_dir)
        writer.write_json(
            "feedback_loop.json",
            {
                "iterations": [
                    {
                        "iteration": outcome.iteration,
                        "replanned": outcome.replanned,
                        "regenerated": outcome.regenerated,
                        "mean_pass_rate": outcome.mean_pass_rate,
                        "stopped_because": outcome.stopped_because,
                    }
                    for outcome in outcomes
                ],
                "stopped_because": outcomes[-1].stopped_because
                or "reached the iteration limit",
            },
        )


def run_feedback_loop(
    run_dir: str | Path,
    config_path: str | Path,
    *,
    max_iterations: int = 3,
    use_docker: bool = True,
    llm_client: LLMClient | None = None,
) -> LoopResult:
    """Entry point used by the CLI."""

    config = load_config(config_path)
    client = llm_client or LMStudioLLMClient(
        model=config.llm.model,
        base_url=config.llm.base_url,
        default_temperature=config.llm.temperature,
        default_max_tokens=config.llm.max_tokens,
        timeout=config.llm.timeout_seconds,
    )
    recorder = UsageRecorder(client)
    loop = FeedbackLoop(
        run_dir,
        config,
        recorder,
        max_iterations=max_iterations,
        use_docker=use_docker,
    )
    result = loop.run()
    ArtifactWriter(Path(run_dir)).write_json("usage.feedback_loop.json", recorder.report())
    return result
