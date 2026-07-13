"""Command-line entry point for workflow preparation."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from thesis_rest_tester.evaluation.coverage import evaluate_requirement_coverage
from thesis_rest_tester.logging_utils import configure_logging
from thesis_rest_tester.orchestrator import Orchestrator


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


if __name__ == "__main__":
    raise SystemExit(main())
