"""Command-line entry point for workflow preparation."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from thesis_rest_tester.domain.models import MetricSnapshot
from thesis_rest_tester.evaluation.coverage import evaluate_requirement_coverage
from thesis_rest_tester.evaluation.evaluator import evaluate_run
from thesis_rest_tester.execution.executor import execute_suites
from thesis_rest_tester.feedback_loop import run_feedback_loop
from thesis_rest_tester.generation.generator import generate_suites
from thesis_rest_tester.logging_utils import configure_logging
from thesis_rest_tester.orchestrator import Orchestrator
from thesis_rest_tester.replanner import replan_run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Participium REST test workflow planner")
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan", help="Prepare a test-generation workflow plan")
    plan_parser.add_argument("--config", required=True, help="Path to a YAML configuration file")
    plan_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Use deterministic mock LLM responses; input documents are still required",
    )
    generate_parser = subparsers.add_parser(
        "generate",
        help="Generate an executable pytest suite per project from a completed run",
    )
    generate_parser.add_argument("--config", required=True, help="Path to a YAML configuration")
    generate_parser.add_argument(
        "--run-dir", required=True, help="Path to a completed planning run folder"
    )
    generate_parser.add_argument(
        "--project",
        action="append",
        default=None,
        help="Limit generation to this project; repeatable",
    )
    generate_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Use deterministic mock responses instead of calling the model",
    )
    replan_parser = subparsers.add_parser(
        "replan",
        help="Plan the test strategy again on a completed run, reusing its coverage",
    )
    replan_parser.add_argument("--config", required=True, help="Path to a YAML configuration")
    replan_parser.add_argument("--run-dir", required=True, help="The run to re-plan from")
    replan_parser.add_argument(
        "--max-tests",
        type=int,
        default=None,
        help="Override budget.max_tests_per_iteration for this re-planning",
    )
    replan_parser.add_argument(
        "--project", action="append", default=None, help="Limit to this project; repeatable"
    )
    execute_parser = subparsers.add_parser(
        "execute",
        help="Run each generated suite against its project, started in Docker",
    )
    execute_parser.add_argument("--run-dir", required=True, help="A run with generated suites")
    execute_parser.add_argument(
        "--config",
        default=None,
        help="Defaults to <run-dir>/config.resolved.yaml, the config that produced the suites",
    )
    execute_parser.add_argument(
        "--manifest", default=None, help="SUT manifest (default: data/sut_manifest.yaml)"
    )
    execute_parser.add_argument(
        "--project", action="append", default=None, help="Limit to this project; repeatable"
    )
    execute_parser.add_argument(
        "--skip-completed",
        action="store_true",
        help="Skip projects that already have an execution report",
    )
    execute_parser.add_argument(
        "--reset-state",
        action="store_true",
        help="Delete each project's declared reset_paths before starting it. Bind mounts "
        "survive `down -v`, so without this a second run starts from the first run's data",
    )
    execute_parser.add_argument(
        "--no-docker",
        action="store_true",
        help="Assume the service is already running; do not start or stop containers",
    )
    execute_parser.add_argument(
        "--attempt-unverified",
        action="store_true",
        help=(
            "Also try projects the manifest marks unverified but for which it already "
            "records a full recipe; this is how such a project earns promotion to runnable"
        ),
    )
    evaluate_run_parser = subparsers.add_parser(
        "evaluate",
        help="Score an executed run and classify each failure by cause",
    )
    evaluate_run_parser.add_argument(
        "--run-dir", required=True, help="Path to a run folder that has been executed"
    )
    evaluate_run_parser.add_argument(
        "--iteration",
        type=int,
        default=1,
        help="Which feedback iteration this execution belongs to (default: 1)",
    )
    evaluate_run_parser.add_argument(
        "--project", action="append", default=None, help="Limit to this project; repeatable"
    )
    loop_parser = subparsers.add_parser(
        "loop",
        help="Repair, re-execute and re-evaluate until the suites stop improving",
    )
    loop_parser.add_argument("--run-dir", required=True, help="An executed and evaluated run")
    loop_parser.add_argument("--config", required=True, help="Path to a YAML configuration")
    loop_parser.add_argument(
        "--max-iterations",
        type=int,
        default=3,
        help="Including the baseline already on disk (default: 3)",
    )
    loop_parser.add_argument(
        "--no-docker",
        action="store_true",
        help="Assume the services are already running; do not start or stop containers",
    )
    evaluate_parser = subparsers.add_parser(
        "evaluate-coverage",
        help="Compare inferred requirement coverage with a manual ground-truth file",
    )
    evaluate_parser.add_argument("--run-dir", required=True, help="Path to a completed run folder")
    evaluate_parser.add_argument(
        "--ground-truth",
        required=True,
        help="Path to a YAML file with implemented requirement IDs per project",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging()
    if args.command == "plan":
        result = Orchestrator(args.config, dry_run=args.dry_run).run()
        print(f"run_id: {result.run_id}")
        print(f"output_folder: {result.output_dir}")
        print(f"projects: {', '.join(result.workflow_plans)}")
        return 0
    if args.command == "generate":
        result = generate_suites(
            args.config, args.run_dir, projects=args.project, dry_run=args.dry_run
        )
        for suite in result.suites:
            print(
                f"{suite.project_name}: {len(suite.cases)}/{suite.requested} tests -> "
                f"{suite.suite_dir}"
            )
            for item, reason in suite.skipped:
                print(f"  skipped {item}: {reason}")
        print(
            f"total: {result.total_cases} generated, {result.total_skipped} skipped, "
            f"{len(result.suites)} project(s)"
        )
        return 0
    if args.command == "replan":
        result = replan_run(
            args.run_dir, args.config, projects=args.project, max_tests=args.max_tests
        )
        for project in result.projects:
            suffix = f"  ERROR {project.error}" if project.error else ""
            print(
                f"{project.project_name}: {project.items_before} -> {project.items_after} items "
                f"({project.requirements_covered} requirements){suffix}"
            )
        print(f"total: {result.total_before} -> {result.total_after} strategy items")
        print(f"output_folder: {result.output_run}")
        return 0
    if args.command == "execute":
        result = execute_suites(
            args.run_dir,
            config_path=args.config,
            manifest_path=args.manifest,
            projects=args.project,
            use_docker=not args.no_docker,
            skip_completed=args.skip_completed,
            reset_state=args.reset_state,
            attempt_unverified=args.attempt_unverified,
        )
        for name, project in result.report.projects.items():
            counts = project.counts
            print(
                f"{name}: {project.outcome} "
                f"(passed={counts.get('passed', 0)} failed={counts.get('failed', 0)} "
                f"error={counts.get('error', 0)} not_run={counts.get('not_run', 0)})"
                + (f" -- {project.reason}" if project.reason else "")
            )
        totals = result.totals
        print(f"total: {totals}")
        print(f"output_folder: {result.run_dir}")
        return 0
    if args.command == "evaluate":
        report = evaluate_run(args.run_dir, iteration=args.iteration, projects=args.project)
        for name, project in report.projects.items():
            metrics = project.metrics
            print(f"{name}: {_format_metrics(metrics)}")
            if project.inconclusive_reason:
                print(f"  inconclusive -- {project.inconclusive_reason}")
                continue
            for cause, count in sorted(project.cause_counts.items()):
                print(f"  {cause}: {count}")
            if project.replan_requirements:
                print(f"  replan: {', '.join(project.replan_requirements)}")
            if project.regenerate_items:
                print(f"  regenerate: {len(project.regenerate_items)} item(s)")
        actionable = report.actionable_projects
        print(f"\nactionable projects: {len(actionable)}")
        return 0
    if args.command == "loop":
        result = run_feedback_loop(
            args.run_dir,
            args.config,
            max_iterations=args.max_iterations,
            use_docker=not args.no_docker,
        )
        for outcome in result.iterations:
            rate = outcome.mean_pass_rate
            print(
                f"iteration {outcome.iteration}: "
                f"mean pass rate {'-' if rate is None else f'{rate:.3f}'}"
                + (f", replanned {len(outcome.replanned)}" if outcome.replanned else "")
                + (
                    f", regenerated {sum(outcome.regenerated.values())} test(s)"
                    if outcome.regenerated
                    else ""
                )
            )
        print(f"stopped: {result.stopped_because}")
        print(f"output_folder: {result.run_dir}")
        return 0
    if args.command == "evaluate-coverage":
        report = evaluate_requirement_coverage(args.run_dir, args.ground_truth)
        print(f"run_id: {report.run_id}")
        for project in report.projects.values():
            print(
                f"{project.project_name}: "
                f"precision={_format_metric(project.precision)} "
                f"recall={_format_metric(project.recall)} "
                f"f1={_format_metric(project.f1)} "
                f"tp={project.true_positives} fp={project.false_positives} "
                f"fn={project.false_negatives} tn={project.true_negatives}"
            )
        print(f"output_folder: {args.run_dir}")
        return 0
    raise RuntimeError(f"Unsupported command: {args.command}")


def _format_metric(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _format_metrics(metrics: MetricSnapshot) -> str:
    """One line per project, with an absent metric shown as absent rather than as zero."""

    def ratio(value: float | None) -> str:
        return "-" if value is None else f"{value:.2f}"

    return (
        f"pass={ratio(metrics.pass_rate)} "
        f"ops={ratio(metrics.operation_coverage)} "
        f"codes={ratio(metrics.status_code_coverage)} "
        f"5xx={metrics.server_errors_count or 0}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
