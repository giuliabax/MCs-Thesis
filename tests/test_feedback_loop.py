"""The loop's control flow, exercised without a model and without containers.

This is the property that made the Evaluator return a document and nothing else: because
its only output is data, a feedback iteration can be driven from a fixture report. The
alternative would be an hour of Docker time to test an `if`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from thesis_rest_tester.domain.evaluation import EvaluationReport, ProjectEvaluation
from thesis_rest_tester.domain.models import MetricSnapshot
from thesis_rest_tester.feedback_loop import FeedbackLoop


def _evaluation(iteration: int, projects: dict[str, dict]) -> EvaluationReport:
    return EvaluationReport(
        run_id="r",
        iteration=iteration,
        projects={
            name: ProjectEvaluation(
                project_name=name,
                metrics=MetricSnapshot(iteration=iteration, pass_rate=spec.get("pass_rate")),
                replan_requirements=spec.get("replan", []),
                regenerate_items=spec.get("regenerate", []),
            )
            for name, spec in projects.items()
        },
    )


@dataclass
class _Recorder:
    """Stands in for every stage the loop drives, and remembers what it was asked."""

    evaluations: list[EvaluationReport]
    executed: int = 0
    replanned: list[list[str]] = field(default_factory=list)
    regenerated: list[dict[str, list[str]]] = field(default_factory=list)
    feedback_calls: list[str] = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import thesis_rest_tester.feedback_loop as loop

        queue = list(self.evaluations)

        def fake_evaluate(run_dir, *, iteration=1, projects=None):
            return queue.pop(0)

        def fake_execute(run_dir, **kwargs):
            self.executed += 1
            return None

        class FakeGenerator:
            def __init__(self, run_dir, client, config, *, regenerate=None, **kwargs):
                self._regenerate = regenerate or {}

            def run(inner, *, only=None):  # noqa: N805 - mirrors the real signature
                self.regenerated.append(dict(inner._regenerate))
                return None

        class FakeReplanner:
            def __init__(self, run_dir, config, **kwargs):
                pass

            def run(inner, *, only=None, output_run=None, requirements=None, planning_notes=None):
                self.replanned.append(list(only or []))
                return None

        class FakeFeedback:
            def __init__(self, **kwargs):
                pass

            def run(inner, evaluation):
                self.feedback_calls.append(evaluation.project_name)
                raise RuntimeError("no model in this test")

        monkeypatch.setattr(loop, "evaluate_run", fake_evaluate)
        monkeypatch.setattr(loop, "execute_suites", fake_execute)
        monkeypatch.setattr(loop, "SuiteGenerator", FakeGenerator)
        monkeypatch.setattr(loop, "Replanner", FakeReplanner)
        monkeypatch.setattr(loop, "FeedbackManagerAgent", FakeFeedback)


def _loop(tmp_path: Path, config, max_iterations: int = 3) -> FeedbackLoop:
    return FeedbackLoop(tmp_path, config, llm_client=None, max_iterations=max_iterations)


@pytest.fixture()
def config():
    from thesis_rest_tester.config import load_config

    return load_config("configs/participium.example.yaml")


def test_a_planning_fault_reaches_the_planner_and_a_generation_fault_does_not(
    tmp_path, monkeypatch, config
) -> None:
    """The two are routed to different stages; sending a planning fault to the generator
    would faithfully reproduce the failure against an unrepaired instruction."""

    baseline = _evaluation(1, {"alpha": {"replan": ["PT01"], "pass_rate": 0.1}})
    recorder = _Recorder([_evaluation(2, {"alpha": {"pass_rate": 0.5}})])
    recorder.install(monkeypatch)

    _loop(tmp_path, config, max_iterations=2).run(baseline=baseline)

    assert recorder.replanned == [["alpha"]]
    assert recorder.regenerated == []


def test_only_the_named_items_are_sent_for_regeneration(tmp_path, monkeypatch, config) -> None:
    items = ["PT02|POST /reports|happy_path"]
    baseline = _evaluation(1, {"alpha": {"regenerate": items, "pass_rate": 0.1}})
    recorder = _Recorder([_evaluation(2, {"alpha": {"pass_rate": 0.5}})])
    recorder.install(monkeypatch)

    _loop(tmp_path, config, max_iterations=2).run(baseline=baseline)

    assert recorder.regenerated == [{"alpha": items}]
    assert recorder.replanned == []


def test_execution_and_evaluation_run_once_per_iteration_over_everything(
    tmp_path, monkeypatch, config
) -> None:
    """Repair every project, then execute every project, then evaluate every project. A
    per-project loop would measure each project at a different point in the iteration."""

    baseline = _evaluation(
        1,
        {
            "alpha": {"regenerate": ["a|GET /a|happy_path"], "pass_rate": 0.1},
            "beta": {"replan": ["PT02"], "pass_rate": 0.1},
        },
    )
    recorder = _Recorder(
        [_evaluation(2, {"alpha": {"pass_rate": 0.5}, "beta": {"pass_rate": 0.5}})]
    )
    recorder.install(monkeypatch)

    _loop(tmp_path, config, max_iterations=2).run(baseline=baseline)

    assert recorder.executed == 1
    assert recorder.regenerated == [{"alpha": ["a|GET /a|happy_path"]}]
    assert recorder.replanned == [["beta"]]


def test_the_loop_stops_when_an_iteration_improves_nothing(tmp_path, monkeypatch, config) -> None:
    baseline = _evaluation(1, {"alpha": {"regenerate": ["a|GET /a|happy_path"], "pass_rate": 0.4}})
    recorder = _Recorder(
        [
            _evaluation(2, {"alpha": {"regenerate": ["a|GET /a|happy_path"], "pass_rate": 0.4}}),
            _evaluation(3, {"alpha": {"pass_rate": 0.9}}),
        ]
    )
    recorder.install(monkeypatch)

    result = _loop(tmp_path, config, max_iterations=3).run(baseline=baseline)

    assert recorder.executed == 1
    assert result.stopped_because == "no project improved"


def test_a_run_with_nothing_repairable_does_not_execute_at_all(
    tmp_path, monkeypatch, config
) -> None:
    """Every remaining failure is environmental, a service defect, or a contract mismatch.
    Another iteration would cost an hour of containers to change nothing."""

    baseline = _evaluation(1, {"alpha": {"pass_rate": 0.4}})
    recorder = _Recorder([])
    recorder.install(monkeypatch)

    result = _loop(tmp_path, config).run(baseline=baseline)

    assert recorder.executed == 0
    assert result.stopped_because == "no project had a repairable failure left"


def test_feedback_that_cannot_be_written_costs_its_notes_and_not_the_iteration(
    tmp_path, monkeypatch, config
) -> None:
    """The repair still runs, just without a note -- the same as the first attempt rather
    than worse than it."""

    baseline = _evaluation(1, {"alpha": {"regenerate": ["a|GET /a|happy_path"], "pass_rate": 0.1}})
    recorder = _Recorder([_evaluation(2, {"alpha": {"pass_rate": 0.5}})])
    recorder.install(monkeypatch)

    _loop(tmp_path, config, max_iterations=2).run(baseline=baseline)

    assert recorder.feedback_calls == ["alpha"]
    assert recorder.regenerated == [{"alpha": ["a|GET /a|happy_path"]}]


def test_the_baseline_is_read_and_never_recomputed(tmp_path, monkeypatch, config) -> None:
    """After one pass the artifacts describe the repaired suites, so re-evaluating them
    as iteration 1 would overwrite the record of what the pipeline achieved without
    feedback -- the one thing every later iteration is compared against."""

    baseline = _evaluation(1, {"alpha": {"pass_rate": 0.4}})
    (tmp_path / "evaluation_report.iteration1.json").write_text(
        baseline.model_dump_json(), encoding="utf-8"
    )
    recorder = _Recorder([])
    recorder.install(monkeypatch)

    result = _loop(tmp_path, config).run()

    # The queue of fake evaluations is empty: reaching evaluate_run would raise.
    assert result.iterations[0].evaluation is not None
    assert result.iterations[0].evaluation.projects["alpha"].metrics.pass_rate == 0.4
