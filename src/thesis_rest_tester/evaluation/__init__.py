"""Metric models and future evaluation functions."""

from thesis_rest_tester.evaluation.coverage import (
    CoverageEvaluationReport,
    ProjectCoverageEvaluation,
    RequirementCoverageEvaluationRow,
    evaluate_requirement_coverage,
)
from thesis_rest_tester.evaluation.metrics import (
    MetricInputs,
    aggregate_token_usage,
    calculate_execution_success_rate,
    calculate_operation_coverage,
    calculate_pass_rate,
    calculate_status_code_coverage,
    count_seeded_bugs_detected,
    count_server_errors,
    estimate_cost,
    evaluate_metrics,
    ingest_coverage_report,
    measure_execution_time,
)

__all__ = [
    "CoverageEvaluationReport",
    "MetricInputs",
    "ProjectCoverageEvaluation",
    "RequirementCoverageEvaluationRow",
    "aggregate_token_usage",
    "calculate_execution_success_rate",
    "calculate_operation_coverage",
    "calculate_pass_rate",
    "calculate_status_code_coverage",
    "count_seeded_bugs_detected",
    "count_server_errors",
    "estimate_cost",
    "evaluate_metrics",
    "evaluate_requirement_coverage",
    "ingest_coverage_report",
    "measure_execution_time",
]
